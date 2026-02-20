"""
server.py - Flask web GUI for ZeroToken.
Run via:  python ai_build.py gui

Redesigned UI:
  Phase-based wizard layout (Setup → Steps → Deliver)
  Sidebar step navigator + focused main panel
  Auto-polling while Ollama runs (no manual refresh)
  Built-in contextual help / onboarding for every action
"""

import json
import os
import signal
import threading
import urllib.request
import urllib.error

from flask import Flask, request, render_template_string, redirect, url_for, jsonify

from ai_build.shutdown import get_shutdown_manager

from ai_build.storage import (
    load_plan, save_plan, save_prompt, save_patch, load_ollama_prompt,
    save_refined_patch, load_refined_patch, save_final_prompt, load_final_prompt,
    get_repo_file_tree, read_files, update_step_status,
)
from ai_build.planner import _build_planning_prompt
from ai_build.executor import _build_patch_prompt, _looks_like_diff, _strip_code_fences
from ai_build.reviewer import review_patch
from ai_build.git_ops import _is_git_repo, is_repo_clean
from ai_build.local_patcher import _sanitize_diff

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory session state (single-user local tool)
# ---------------------------------------------------------------------------
_state: dict = {
    "goal": "",
    "plan_prompt": "",
    "patch_prompts": {},    # step_id -> str  (Claude prompts)
    "ollama_prompts": {},   # step_id -> str  (Ollama prompts)
    "diffs": {},            # step_id -> str  (raw Ollama-Patcher output)
    "refined_diffs": {},    # step_id -> str  (Ollama-Refiner output)
    "reviews": {},          # step_id -> str | dict
    "final_prompt": "",     # assembled agent prompt (copy into Claude)
    "active_step_id": None,
    "error": "",
    "info": "",
    "project_root": "",  # empty = no folder chosen yet; set by user on first use
    "git_exists": False,
    "bg_running": False,
    "bg_label": "",
    "planner_model":  os.getenv("OLLAMA_MODEL", "gemma3:4b"),
    "patcher_model":  os.getenv("OLLAMA_MODEL", "gemma3:4b"),
    "reviewer_model": os.getenv("OLLAMA_MODEL", "gemma3:4b"),
    "refiner_model":  os.getenv("OLLAMA_MODEL", "gemma3:4b"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_status() -> dict:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            data = json.loads(r.read())
            models = [m["name"] for m in data.get("models", [])]
            ollama_ok = True
            model_name = _state.get("patcher_model", os.getenv("OLLAMA_MODEL", "gemma3:4b"))
            model_loaded = any(m.startswith(model_name.split(":")[0]) for m in models)
    except Exception:
        ollama_ok = False
        models = []
        model_loaded = False
        model_name = _state.get("patcher_model", os.getenv("OLLAMA_MODEL", "gemma3:4b"))

    in_git = _is_git_repo()
    if in_git:
        git_status = "clean" if is_repo_clean() else "dirty"
    else:
        git_status = "missing"

    plan = load_plan() if _state.get("project_root") else None
    if plan:
        steps = plan["steps"]
        total    = len(steps)
        approved = sum(1 for s in steps if s.get("status") == "approved")
        skipped  = sum(1 for s in steps if s.get("status") == "skipped")
        failed   = sum(1 for s in steps if s.get("status") == "failed")
        pending  = total - approved - skipped - failed
    else:
        total = approved = skipped = failed = pending = 0

    return {
        "ollama_ok":    ollama_ok,
        "model_name":   model_name,
        "model_loaded": model_loaded,
        "models":       models,
        "git_status":   git_status,
        "plan_loaded":  plan is not None,
        "goal":         plan["goal"] if plan else "",
        "total":        total,
        "approved":     approved,
        "skipped":      skipped,
        "failed":       failed,
        "pending":      pending,
        "active_step_id": _state["active_step_id"],
        "project_root": _state.get("project_root", ""),
        "git_exists":   _state.get("git_exists", False),
    }


def _active_step() -> dict | None:
    plan = load_plan() if _state.get("project_root") else None
    if not plan:
        return None
    sid = _state["active_step_id"]
    if sid is None:
        return None
    for s in plan["steps"]:
        if s["id"] == sid:
            return s
    return None


def _first_pending_step() -> dict | None:
    plan = load_plan() if _state.get("project_root") else None
    if not plan:
        return None
    for s in plan["steps"]:
        if s.get("status", "pending") == "pending":
            return s
    return None


def _flash(msg: str = "", error: str = ""):
    _state["info"]  = msg
    _state["error"] = error


# ---------------------------------------------------------------------------
# Project folder management
# ---------------------------------------------------------------------------

def _apply_project_root(path: str) -> tuple[bool, str]:
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return False, f"Folder does not exist: {path}"
    try:
        os.chdir(path)
    except PermissionError as e:
        return False, f"Cannot access folder: {e}"
    _state["project_root"] = path
    _state["git_exists"]   = os.path.isdir(os.path.join(path, ".git"))
    _state["goal"]           = ""
    _state["plan_prompt"]    = ""
    _state["patch_prompts"].clear()
    _state["ollama_prompts"].clear()
    _state["diffs"].clear()
    _state["refined_diffs"].clear()
    _state["reviews"].clear()
    _state["final_prompt"]   = ""
    _state["active_step_id"] = None
    return True, ""


# ---------------------------------------------------------------------------
# Template render
# ---------------------------------------------------------------------------

def _render(extra: dict | None = None):
    # Only read plan from disk once the user has chosen a project folder
    plan = load_plan() if _state.get("project_root") else None
    if plan:
        for step in plan.get("steps", []):
            sid = step["id"]
            if sid not in _state["ollama_prompts"]:
                saved = load_ollama_prompt(sid)
                if saved:
                    _state["ollama_prompts"][sid] = saved
            if sid not in _state["refined_diffs"]:
                saved = load_refined_patch(sid)
                if saved:
                    _state["refined_diffs"][sid] = saved
    if not _state["final_prompt"]:
        saved = load_final_prompt()
        if saved:
            _state["final_prompt"] = saved
    ctx = {
        "plan":        plan,
        "state":       _state,
        "status":      _get_status(),
        "active_step": _active_step(),
    }
    if extra:
        ctx.update(extra)
    return render_template_string(HTML_TEMPLATE, **ctx)


# ---------------------------------------------------------------------------
# Routes — folder picker
# ---------------------------------------------------------------------------

@app.route("/browse", methods=["GET"])
def browse_folder():
    result: dict = {"path": "", "error": ""}
    done = threading.Event()

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(
                title="Select your project folder",
                initialdir=_state.get("project_root", os.getcwd()),
                parent=root,
            )
            root.destroy()
            result["path"] = chosen or ""
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            done.set()

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    done.wait(timeout=60)
    if result["error"]:
        return jsonify({"path": "", "error": result["error"]}), 500
    return jsonify({"path": result["path"]})


@app.route("/set-folder", methods=["POST"])
def set_folder():
    path = request.form.get("project_root", "").strip()
    if not path:
        _flash(error="No folder path provided.")
        return redirect(url_for("index"))
    ok, err = _apply_project_root(path)
    if not ok:
        _flash(error=err)
        return redirect(url_for("index"))
    if _state["git_exists"]:
        _flash(f"Project folder set: {path}")
    else:
        _flash(f"Folder set: {path} — no .git found. Use 'Initialise Git' to set it up.")
    return redirect(url_for("index"))


@app.route("/git-init", methods=["POST"])
def git_init():
    import subprocess
    root = _state.get("project_root", os.getcwd())
    result = subprocess.run(["git", "init"], capture_output=True, text=True, cwd=root)
    if result.returncode == 0:
        _state["git_exists"] = True
        _flash(f"Git repo initialised in {root}")
    else:
        _flash(error=f"git init failed: {result.stderr.strip()}")
    return redirect(url_for("index"))


@app.route("/set-models", methods=["POST"])
def set_models():
    _default = os.getenv("OLLAMA_MODEL", "gemma3:4b")
    _state["planner_model"]  = request.form.get("planner_model",  _default).strip() or _default
    _state["patcher_model"]  = request.form.get("patcher_model",  _default).strip() or _default
    _state["reviewer_model"] = request.form.get("reviewer_model", _default).strip() or _default
    _state["refiner_model"]  = request.form.get("refiner_model",  _default).strip() or _default
    _flash(f"✓ Models updated — Planner:{_state['planner_model']}  Patcher:{_state['patcher_model']}  Reviewer:{_state['reviewer_model']}  Refiner:{_state['refiner_model']}")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — main
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if _state["active_step_id"] is None:
        step = _first_pending_step()
        if step:
            _state["active_step_id"] = step["id"]
    return _render()


@app.route("/status", methods=["GET"])
def poll_status():
    """AJAX polling endpoint — returns current bg state + flash messages."""
    _default = os.getenv("OLLAMA_MODEL", "gemma3:4b")
    return jsonify({
        "bg_running":     _state["bg_running"],
        "bg_label":       _state["bg_label"],
        "info":           _state["info"],
        "error":          _state["error"],
        "planner_model":  _state.get("planner_model",  _default),
        "patcher_model":  _state.get("patcher_model",  _default),
        "reviewer_model": _state.get("reviewer_model", _default),
        "refiner_model":  _state.get("refiner_model",  _default),
    })


@app.route("/goal", methods=["POST"])
def set_goal():
    _state["goal"] = request.form.get("goal", "").strip()
    _flash(f"Goal set: {_state['goal'][:60]}")
    return redirect(url_for("index"))


@app.route("/generate-plan", methods=["POST"])
def generate_plan():
    goal = request.form.get("goal", "").strip() or _state["goal"]
    if not goal:
        _flash(error="Please enter a goal first.")
        return redirect(url_for("index"))
    _state["goal"] = goal
    from ai_build.context import detect_stack
    file_tree = get_repo_file_tree()
    stack = detect_stack(_state.get("project_root", "."))
    prompt = _build_planning_prompt(goal, file_tree, stack)
    _state["plan_prompt"] = prompt
    save_prompt("plan_prompt.txt", prompt)
    _flash("Planning prompt generated — copy it into Claude, then paste the JSON response below.")
    return redirect(url_for("index"))


@app.route("/save-plan", methods=["POST"])
def save_plan_route():
    raw = request.form.get("plan_json", "").strip().lstrip("\ufeff")
    if not raw:
        _flash(error="Nothing pasted.")
        return redirect(url_for("index"))
    if raw.startswith("```"):
        lines = raw.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:end])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _flash(error=f"Invalid JSON: {e}")
        return redirect(url_for("index"))
    steps = data.get("steps")
    if not steps or not isinstance(steps, list):
        _flash(error="JSON has no 'steps' list.")
        return redirect(url_for("index"))
    for step in steps:
        step.setdefault("status", "pending")
    plan = {"goal": _state["goal"] or data.get("goal", ""), "steps": steps}
    save_plan(plan)
    _state["active_step_id"] = steps[0]["id"] if steps else None
    _flash(f"Plan saved — {len(steps)} steps loaded.")
    return redirect(url_for("index"))


@app.route("/select-step/<int:step_id>", methods=["POST"])
def select_step(step_id: int):
    _state["active_step_id"] = step_id
    return redirect(url_for("index"))


@app.route("/generate-patch", methods=["POST"])
def generate_patch():
    step = _active_step()
    if not step:
        _flash(error="No active step selected.")
        return redirect(url_for("index"))
    extra = request.form.get("extra_instructions", "").strip()
    prompt = _build_patch_prompt(step, extra_instructions=extra)
    _state["patch_prompts"][step["id"]] = prompt
    save_prompt(f"patch_prompt_step_{step['id']}.txt", prompt)
    _flash(f"Patch prompt for step {step['id']} generated — copy it into Claude, then paste the diff below.")
    return redirect(url_for("index"))


@app.route("/plan-local", methods=["POST"])
def plan_local():
    goal = request.form.get("goal", "").strip() or _state["goal"]
    if not goal:
        _flash(error="Please enter a goal first.")
        return redirect(url_for("index"))
    if _state["bg_running"]:
        _flash(error="Ollama is already running — please wait.")
        return redirect(url_for("index"))
    _state["goal"] = goal
    _state["bg_running"] = True
    _state["bg_label"] = "Ollama is generating a plan… (30–90 s)"
    def _run():
        try:
            from ai_build.local_planner import generate_plan_local
            plan_dict, err = generate_plan_local(goal, root=_state.get("project_root", "."), model=_state.get("planner_model"))
            if err:
                _flash(error=f"Ollama planning failed: {err}")
            else:
                save_plan(plan_dict)
                steps = plan_dict["steps"]
                _state["active_step_id"] = steps[0]["id"] if steps else None
                _state["plan_prompt"] = ""
                _flash(f"✓ Ollama generated a {len(steps)}-step plan.")
        except Exception as exc:
            _flash(error=f"Ollama planning error: {exc}")
        finally:
            _state["bg_running"] = False
            _state["bg_label"] = ""
    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/patch-local", methods=["POST"])
def patch_local():
    step = _active_step()
    if not step:
        _flash(error="No active step selected.")
        return redirect(url_for("index"))
    if _state["bg_running"]:
        _flash(error="Ollama is already running — please wait.")
        return redirect(url_for("index"))
    _state["bg_running"] = True
    _state["bg_label"] = f"Ollama generating patch for step {step['id']}… (30–90 s)"
    def _run():
        try:
            from ai_build.local_patcher import generate_patch_local
            from ai_build.storage import load_patch, load_refined_patch

            # Build prior_diffs: all steps approved before this one
            plan = load_plan()
            prior_diffs: dict = {}
            if plan:
                for s in plan["steps"]:
                    if s["id"] < step["id"] and s.get("status") in ("approved", "applied"):
                        d = _state["refined_diffs"].get(s["id"]) or _state["diffs"].get(s["id"])
                        if not d:
                            d = load_refined_patch(s["id"]) or load_patch(s["id"])
                        if d:
                            prior_diffs[s["id"]] = d

            diff, err, ollama_prompt = generate_patch_local(step, root=_state.get("project_root", "."), prior_diffs=prior_diffs, model=_state.get("patcher_model"))
            sid = step["id"]
            _state["ollama_prompts"][sid] = ollama_prompt
            save_prompt(f"ollama_patch_step_{sid}.txt", ollama_prompt)
            if err:
                _flash(error=f"Ollama patch generation failed: {err}")
                return
            _state["diffs"][sid] = diff
            save_patch(sid, diff)
            rev = review_patch(diff, step["description"], model=_state.get("reviewer_model"))
            _state["reviews"][sid] = rev
            _flash(f"✓ Patch generated and reviewed for step {sid}.")
        except Exception as exc:
            _flash(error=f"Ollama patch error: {exc}")
        finally:
            _state["bg_running"] = False
            _state["bg_label"] = ""
    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/review-patch", methods=["POST"])
def review_patch_route():
    step = _active_step()
    if not step:
        _flash(error="No active step selected.")
        return redirect(url_for("index"))
    raw = request.form.get("diff", "").strip()
    if not raw:
        _flash(error="No diff pasted.")
        return redirect(url_for("index"))
    patch = _strip_code_fences(raw)
    patch = _sanitize_diff(patch)
    if not _looks_like_diff(patch):
        _flash(error="Pasted text doesn't look like a unified diff (needs ---, +++, @@).")
        return redirect(url_for("index"))
    sid = step["id"]
    _state["diffs"][sid] = patch
    save_patch(sid, patch)
    _flash("Sending diff to Ollama for review…")
    review = review_patch(patch, step["description"], model=_state.get("reviewer_model"))
    _state["reviews"][sid] = review
    _flash("Review complete — choose Approve, Skip, or Retry.")
    return redirect(url_for("index"))


@app.route("/apply-patch", methods=["POST"])
def apply_patch_route():
    step = _active_step()
    if not step:
        _flash(error="No active step selected.")
        return redirect(url_for("index"))
    sid = step["id"]
    best_diff = _state["refined_diffs"].get(sid) or _state["diffs"].get(sid, "")
    if not best_diff:
        _flash(error="No diff found — generate a patch first.")
        return redirect(url_for("index"))
    update_step_status(sid, "approved")
    _state["final_prompt"] = ""
    plan = load_plan()
    if plan:
        next_step = next(
            (s for s in plan["steps"] if s.get("status", "pending") == "pending"), None
        )
        _state["active_step_id"] = next_step["id"] if next_step else None
    else:
        _state["active_step_id"] = None
    _state["diffs"].pop(sid, None)
    _state["reviews"].pop(sid, None)
    _flash(f"✓ Step {sid} approved.")
    return redirect(url_for("index"))


@app.route("/refine-patch", methods=["POST"])
def refine_patch_route():
    step = _active_step()
    if not step:
        _flash(error="No active step selected.")
        return redirect(url_for("index"))
    if _state["bg_running"]:
        _flash(error="Ollama is already running — please wait.")
        return redirect(url_for("index"))
    sid = step["id"]
    diff = _state["diffs"].get(sid, "")
    if not diff:
        _flash(error="No diff to refine — generate a patch first.")
        return redirect(url_for("index"))
    review = _state["reviews"].get(sid, "")
    _state["bg_running"] = True
    _state["bg_label"] = f"Ollama-Refiner cleaning up patch for step {sid}…"
    def _run():
        try:
            from ai_build.refiner import refine_patch
            refined, err = refine_patch(diff, step, review, root=_state.get("project_root", "."), model=_state.get("refiner_model"))
            _state["refined_diffs"][sid] = refined
            save_refined_patch(sid, refined)
            if err:
                _flash(f"✓ Step {sid} refined (note: {err[:120]})")
            else:
                _flash(f"✓ Step {sid} patch refined — review then Approve.")
        except Exception as exc:
            _flash(error=f"Refiner error: {exc}")
        finally:
            _state["bg_running"] = False
            _state["bg_label"] = ""
    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/assemble-prompt", methods=["POST"])
def assemble_prompt():
    plan = load_plan()
    if not plan:
        _flash(error="No plan loaded.")
        return redirect(url_for("index"))
    approved_diffs: dict[int, str] = {}
    for step in plan["steps"]:
        sid = step["id"]
        if step.get("status") == "approved":
            diff = _state["refined_diffs"].get(sid) or _state["diffs"].get(sid, "")
            if not diff:
                from ai_build.storage import load_refined_patch, load_patch
                diff = load_refined_patch(sid) or load_patch(sid) or ""
            if diff:
                approved_diffs[sid] = diff
    if not approved_diffs:
        _flash(error="No approved steps yet — approve at least one step first.")
        return redirect(url_for("index"))
    from ai_build.assembler import assemble_final_prompt
    goal = _state.get("goal", plan.get("goal", ""))
    root = _state.get("project_root", ".")
    prompt = assemble_final_prompt(goal, plan, approved_diffs, root=root)
    _state["final_prompt"] = prompt
    save_final_prompt(prompt)
    _flash(f"✓ Final prompt assembled from {len(approved_diffs)} approved step(s).")
    return redirect(url_for("index"))


@app.route("/skip-step", methods=["POST"])
def skip_step():
    step = _active_step()
    if not step:
        _flash(error="No active step.")
        return redirect(url_for("index"))
    sid = step["id"]
    update_step_status(sid, "skipped")
    plan = load_plan()
    if plan:
        next_step = next(
            (s for s in plan["steps"] if s.get("status", "pending") == "pending"), None
        )
        _state["active_step_id"] = next_step["id"] if next_step else None
    else:
        _state["active_step_id"] = None
    _state["diffs"].pop(sid, None)
    _state["reviews"].pop(sid, None)
    _flash(f"Step {sid} skipped.")
    return redirect(url_for("index"))


@app.route("/reset-step/<int:step_id>", methods=["POST"])
def reset_step(step_id: int):
    update_step_status(step_id, "pending")
    _state["active_step_id"] = step_id
    _state["diffs"].pop(step_id, None)
    _state["reviews"].pop(step_id, None)
    _flash(f"Step {step_id} reset to pending.")
    return redirect(url_for("index"))


@app.route("/clear-plan", methods=["POST"])
def clear_plan():
    import pathlib
    plan_file = pathlib.Path(".ai-build") / "plan.json"
    if plan_file.exists():
        plan_file.unlink()
    _state["plan_prompt"] = ""
    _state["patch_prompts"].clear()
    _state["ollama_prompts"].clear()
    _state["diffs"].clear()
    _state["refined_diffs"].clear()
    _state["reviews"].clear()
    _state["final_prompt"] = ""
    _state["active_step_id"] = None
    _state["goal"] = ""
    _flash("Plan cleared.")
    return redirect(url_for("index"))


@app.route("/run-all", methods=["POST"])
def run_all():
    plan = load_plan()
    if not plan:
        _flash(error="No plan loaded.")
        return redirect(url_for("index"))
    if _state["bg_running"]:
        _flash(error="Already running — please wait.")
        return redirect(url_for("index"))
    pending = [s for s in plan["steps"] if s.get("status", "pending") == "pending"]
    if not pending:
        _flash(error="No pending steps.")
        return redirect(url_for("index"))
    _state["bg_running"] = True
    _state["bg_label"] = f"Run All: starting {len(pending)} step(s)…"
    def _run():
        try:
            from ai_build.local_patcher import generate_patch_local
            root = _state.get("project_root", ".")
            n = len(pending)
            approved_count = 0
            for i, step in enumerate(pending, 1):
                sid = step["id"]
                _state["bg_label"] = f"Step {i}/{n} — Generating: {step['title'][:40]}…"

                # Build prior_diffs from in-memory state — more reliable than
                # re-reading plan.json mid-loop (avoids stale disk reads).
                prior_diffs: dict = {}
                for s in plan["steps"]:
                    if s["id"] < sid and s.get("status") in ("approved", "applied"):
                        d = _state["refined_diffs"].get(s["id"]) or _state["diffs"].get(s["id"])
                        if d:
                            prior_diffs[s["id"]] = d

                diff, err, ollama_prompt = generate_patch_local(step, root=root, prior_diffs=prior_diffs, model=_state.get("patcher_model"))
                _state["ollama_prompts"][sid] = ollama_prompt
                save_prompt(f"ollama_patch_step_{sid}.txt", ollama_prompt)
                if err:
                    _flash(error=f"Step {sid} patch failed: {err}")
                    continue
                _state["diffs"][sid] = diff
                save_patch(sid, diff)
                _state["bg_label"] = f"Step {i}/{n} — Reviewing: {step['title'][:40]}…"
                rev = review_patch(diff, step["description"], model=_state.get("reviewer_model"))
                _state["reviews"][sid] = rev
                update_step_status(sid, "approved")
                _state["active_step_id"] = sid
                approved_count += 1
            _state["bg_label"] = "Assembling Final Agent Prompt…"
            plan2 = load_plan()
            approved_diffs = {}
            for s in (plan2 or {}).get("steps", []):
                s2id = s["id"]
                if s.get("status") == "approved":
                    d = _state["refined_diffs"].get(s2id) or _state["diffs"].get(s2id, "")
                    if d:
                        approved_diffs[s2id] = d
            if approved_diffs:
                from ai_build.assembler import assemble_final_prompt
                goal = _state.get("goal", plan2.get("goal", ""))
                fp = assemble_final_prompt(goal, plan2, approved_diffs, root=root)
                _state["final_prompt"] = fp
                save_final_prompt(fp)
            _flash(f"✓ Run All complete — {approved_count}/{n} steps approved. Final prompt ready!")
        except Exception as exc:
            _flash(error=f"Run All error: {exc}")
        finally:
            _state["bg_running"] = False
            _state["bg_label"] = ""
    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/shutdown", methods=["POST"])
def shutdown_route():
    sm = get_shutdown_manager()
    threading.Thread(target=lambda: sm.shutdown(reason="GUI shutdown button"), daemon=True).start()
    return render_template_string("""
    <!DOCTYPE html><html><head>
    <meta charset="UTF-8">
    <style>body{background:#0f0f0f;color:#ccc;font-family:'JetBrains Mono',monospace;display:flex;
    align-items:center;justify-content:center;height:100vh;margin:0;}</style>
    </head><body>
    <div style="text-align:center">
      <div style="font-size:2rem;margin-bottom:12px;">✓</div>
      <div>ZeroToken is shutting down.</div>
      <div style="color:#555;margin-top:8px;font-size:12px;">You can close this tab.</div>
    </div></body></html>
    """)


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZeroToken</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Reset & tokens ─────────────────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:       #0d0d0f;
  --surface:  #131316;
  --surface2: #1a1a1f;
  --border:   #252530;
  --border2:  #2e2e3a;
  --accent:   #5b7fff;
  --accent-dim:#1e2a5e;
  --teal:     #2dd4bf;
  --teal-dim: #0d2b28;
  --green:    #22c55e;
  --green-dim:#0d2b1a;
  --yellow:   #facc15;
  --yellow-dim:#2a2200;
  --red:      #f87171;
  --red-dim:  #2b0d0d;
  --text:     #e8e8f0;
  --text2:    #9090a8;
  --text3:    #55556a;
  --mono:     'JetBrains Mono', 'Fira Code', monospace;
  --sans:     'Space Grotesk', system-ui, sans-serif;
  --r:        10px;
  --r-sm:     6px;
}

/* ── Layout skeleton ─────────────────────────────────────────── */
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;}
.shell{display:grid;grid-template-rows:48px 1fr;grid-template-columns:200px 1fr;height:100vh;}
.topbar{grid-column:1/-1;grid-row:1;display:flex;align-items:center;gap:12px;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--border);z-index:50;}
.sidebar{grid-row:2;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;}
.main{grid-row:2;overflow-y:auto;padding:20px;background:var(--bg);}

/* ── Topbar ──────────────────────────────────────────────────── */
.logo{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px;letter-spacing:-.3px;}
.logo svg{flex-shrink:0;}
.topbar-sep{width:1px;height:20px;background:var(--border);margin:0 4px;}
.project-name{font-size:11px;color:var(--accent);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;border:none;background:none;font-family:var(--mono);padding:2px 4px;border-radius:4px;}
.project-name:hover{background:var(--accent-dim);}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:8px;}
.pill{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:10px;font-weight:600;font-family:var(--sans);}
.pill-green{background:#0d2b1a;color:var(--green); border:1px solid #1a5c3a;}
.pill-red  {background:var(--red-dim);color:var(--red);border:1px solid #5c1a1a;}
.pill-yellow{background:var(--yellow-dim);color:var(--yellow);border:1px solid #5c4a00;}
.pill-grey {background:#1a1a22;color:var(--text3);border:1px solid var(--border);}
.pill-blue {background:var(--accent-dim);color:var(--accent);border:1px solid #2a3a8e;}
.dot{width:5px;height:5px;border-radius:50%;display:inline-block;}

/* ── Sidebar ─────────────────────────────────────────────────── */
.sidebar-header{padding:14px 14px 10px;font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--text3);}
.phase-block{padding:6px 10px;margin:0 6px;border-radius:var(--r-sm);}
.phase-label{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text3);padding:8px 10px 4px;margin-top:4px;}
.step-item{
  display:flex;align-items:center;gap:8px;
  padding:7px 10px;margin:1px 6px;border-radius:var(--r-sm);
  cursor:pointer;transition:background .12s;
  font-size:11px;color:var(--text2);text-decoration:none;border:none;background:none;width:calc(100% - 12px);text-align:left;
}
.step-item:hover{background:var(--surface2);}
.step-item.active{background:var(--accent-dim);color:var(--text);}
.step-dot{width:18px;height:18px;border-radius:50%;border:1.5px solid var(--border2);display:flex;align-items:center;justify-content:center;font-size:9px;flex-shrink:0;}
.sd-pending {border-color:var(--text3);color:var(--text3);}
.sd-approved{border-color:var(--green);background:var(--green-dim);color:var(--green);}
.sd-skipped {border-color:var(--yellow);background:var(--yellow-dim);color:var(--yellow);}
.sd-failed  {border-color:var(--red);background:var(--red-dim);color:var(--red);}
.step-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;}
.sidebar-footer{margin-top:auto;padding:8px;border-top:1px solid var(--border);}
.sidebar-stat{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);padding:3px 4px;}
.sidebar-stat span:last-child{color:var(--text2);}

/* ── Model-settings collapsible ─────────────────────────────── */
.model-details{border-top:1px solid var(--border);margin-top:4px;}
.model-details>summary{
  list-style:none;display:flex;align-items:center;justify-content:space-between;
  font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;
  color:var(--text3);padding:8px 10px 6px;cursor:pointer;
  font-family:var(--sans);
}
.model-details>summary::-webkit-details-marker{display:none;}
.model-details>summary:hover{color:var(--text2);}
.model-details[open]>summary{color:var(--accent);}
.model-details>.ms-body{padding:4px 10px 10px;}
.ms-row{margin-bottom:7px;}
.ms-label{display:block;font-size:9px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--text3);margin-bottom:3px;font-family:var(--sans);}
.ms-select{
  width:100%;background:var(--bg);border:1px solid var(--border2);
  border-radius:4px;color:var(--text);font-family:var(--mono);
  font-size:10px;padding:3px 5px;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 8 5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%239090a8'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:calc(100% - 6px) center;padding-right:18px;
}
.ms-select:focus{outline:none;border-color:var(--accent);}

/* ── Cards & sections ────────────────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);margin-bottom:16px;overflow:hidden;}
.card-title{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text3);padding:14px 16px 0;font-family:var(--sans);}
.card-body{padding:14px 16px 16px;}
.page-title{font-family:var(--sans);font-size:20px;font-weight:700;color:var(--text);margin-bottom:4px;}
.page-sub{font-size:12px;color:var(--text2);margin-bottom:20px;line-height:1.5;}

/* ── How-to boxes ────────────────────────────────────────────── */
.howto{
  background:var(--accent-dim);border:1px solid #2a3a8e;
  border-radius:var(--r);padding:14px 16px;margin-bottom:16px;
}
.howto-title{font-family:var(--sans);font-size:12px;font-weight:600;color:var(--accent);margin-bottom:8px;display:flex;align-items:center;gap:6px;}
.howto-body{font-size:11px;color:#8ba0e8;line-height:1.7;}
.howto-body b{color:var(--text);font-weight:600;}
.howto-steps{list-style:none;padding:0;margin:8px 0 0;}
.howto-steps li{display:flex;gap:10px;margin-bottom:6px;font-size:11px;color:#8ba0e8;line-height:1.5;}
.howto-steps li .num{
  width:18px;height:18px;border-radius:50%;background:var(--accent);color:#fff;
  font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;
}
.tip{background:var(--teal-dim);border:1px solid #1a4a44;border-radius:var(--r-sm);padding:10px 12px;font-size:11px;color:#7dddd4;margin-top:10px;line-height:1.6;}
.tip b{color:var(--teal);}

/* ── Alerts ──────────────────────────────────────────────────── */
.alert{padding:10px 14px;border-radius:var(--r-sm);margin-bottom:14px;font-size:12px;line-height:1.5;}
.alert-info {background:var(--accent-dim);border:1px solid #2a3a8e;color:#8ba0e8;}
.alert-ok   {background:var(--green-dim);border:1px solid #1a5c3a;color:#6dda8a;}
.alert-error{background:var(--red-dim);border:1px solid #5c1a1a;color:var(--red);}

/* ── Loading banner ──────────────────────────────────────────── */
.loading-bar{
  display:flex;align-items:center;gap:10px;
  background:var(--accent-dim);border:1px solid var(--accent);
  border-radius:var(--r-sm);padding:12px 14px;margin-bottom:14px;
}
.loading-bar .lb-text{flex:1;font-size:12px;color:var(--text);}
.loading-bar .lb-time{font-size:10px;color:var(--text2);font-family:var(--sans);}
@keyframes spin{to{transform:rotate(360deg);}}
.spinner{width:14px;height:14px;border:2px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0;}

/* ── Forms ───────────────────────────────────────────────────── */
textarea,input[type=text]{
  width:100%;background:var(--bg);border:1px solid var(--border2);
  border-radius:var(--r-sm);color:var(--text);
  font-family:var(--mono);font-size:12px;padding:10px 12px;
  resize:vertical;outline:none;transition:border-color .15s;
}
textarea:focus,input[type=text]:focus{border-color:var(--accent);}
label.field-label{display:block;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text3);margin-bottom:6px;font-family:var(--sans);}

/* ── Buttons ─────────────────────────────────────────────────── */
.btn{
  display:inline-flex;align-items:center;gap:6px;
  padding:9px 16px;border:none;border-radius:var(--r-sm);
  font-family:var(--mono);font-size:12px;font-weight:600;
  cursor:pointer;transition:opacity .12s,transform .08s;white-space:nowrap;
}
.btn:active{transform:scale(.97);}
.btn:hover{opacity:.85;}
.btn-sm{padding:6px 12px;font-size:11px;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-teal   {background:var(--teal-dim);color:var(--teal);border:1px solid var(--teal);}
.btn-green  {background:var(--green-dim);color:var(--green);border:1px solid var(--green);}
.btn-yellow {background:var(--yellow-dim);color:var(--yellow);border:1px solid var(--yellow);}
.btn-red    {background:var(--red-dim);color:var(--red);border:1px solid var(--red);}
.btn-ghost  {background:transparent;color:var(--text2);border:1px solid var(--border2);}
.btn-copy   {background:var(--accent-dim);color:var(--accent);border:1px solid #2a3a8e;}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center;}

/* ── Diff viewer ─────────────────────────────────────────────── */
.diff-view{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r-sm);
  padding:8px;font-size:11px;line-height:1.5;overflow:auto;max-height:240px;
}
.diff-view .add{background:#0d2b0d;color:#6abf69;display:block;white-space:pre;}
.diff-view .del{background:#2b0d0d;color:#f07070;display:block;white-space:pre;}
.diff-view .hdr{color:#64d2ff;display:block;white-space:pre;}
.diff-view .ctx{color:var(--text3);display:block;white-space:pre;}
.diff-view .fn {color:var(--yellow);font-weight:bold;display:block;white-space:pre;}

/* ── Review output ───────────────────────────────────────────── */
.verdict-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-family:var(--sans);margin-bottom:8px;}
.verdict-approve {background:var(--green-dim);color:var(--green);border:1px solid var(--green);}
.verdict-concerns{background:var(--yellow-dim);color:var(--yellow);border:1px solid var(--yellow);}
.verdict-reject  {background:var(--red-dim);color:var(--red);border:1px solid var(--red);}
.review-summary{font-size:12px;color:#8ba0e8;line-height:1.6;margin-bottom:8px;}
.review-issues{list-style:none;padding:0;margin:0 0 6px;}
.review-issues li{font-size:11px;color:var(--text2);line-height:1.8;padding-left:14px;position:relative;}
.review-issues li::before{content:"·";position:absolute;left:4px;color:var(--yellow);}
.review-plain{background:var(--bg);border:1px solid var(--border);border-radius:var(--r-sm);padding:10px;font-size:11px;line-height:1.8;color:var(--text2);}

/* ── Prompt box ──────────────────────────────────────────────── */
.prompt-box{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r-sm);
  padding:10px;font-size:10px;line-height:1.6;white-space:pre-wrap;
  word-break:break-word;max-height:200px;overflow-y:auto;color:var(--text);
}
.prompt-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}

/* ── Final prompt ────────────────────────────────────────────── */
.final-ta{
  width:100%;height:220px;background:#060610;
  border:1px solid var(--teal);border-radius:var(--r-sm);
  color:var(--text);font-family:var(--mono);font-size:11px;
  padding:12px;resize:vertical;line-height:1.5;
}
.final-card{background:linear-gradient(135deg,#08101e,var(--teal-dim));border:1px solid var(--teal);}

/* ── Step workflow stages ────────────────────────────────────── */
.stage{border-left:2px solid var(--border2);padding-left:14px;margin-bottom:18px;position:relative;}
.stage::before{
  content:attr(data-n);
  position:absolute;left:-10px;top:0;
  width:18px;height:18px;border-radius:50%;
  background:var(--surface2);border:1.5px solid var(--border2);
  font-size:9px;font-weight:700;color:var(--text3);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--sans);
}
.stage.done{border-color:var(--green);}
.stage.done::before{background:var(--green-dim);border-color:var(--green);color:var(--green);}
.stage.active-stage{border-color:var(--accent);}
.stage.active-stage::before{background:var(--accent-dim);border-color:var(--accent);color:var(--accent);}
.stage-title{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text3);margin-bottom:8px;font-family:var(--sans);}

/* ── Divider ─────────────────────────────────────────────────── */
.div{border:none;border-top:1px solid var(--border);margin:14px 0;}

/* ── Folder bar ──────────────────────────────────────────────── */
.folder-display{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r-sm);
  padding:6px 10px;font-size:11px;color:#64d2ff;flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}

/* ── Onboarding screen ───────────────────────────────────────── */
.onboard-wrap{
  max-width:560px;margin:40px auto 0;
  display:flex;flex-direction:column;gap:0;
}
.onboard-logo{
  display:flex;align-items:center;gap:10px;margin-bottom:6px;
}
.onboard-logo-text{
  font-size:22px;font-weight:700;letter-spacing:-.5px;color:var(--text);
}
.onboard-sub{
  font-size:13px;color:var(--text3);margin-bottom:22px;
}
.onboard-field-label{
  font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
  color:var(--text2);margin-bottom:6px;
}
.onboard-folder-row{
  display:flex;gap:8px;align-items:center;
}
.onboard-btn-row{
  display:flex;gap:10px;margin-top:14px;
}
.onboard-btn{
  flex:1;padding:11px 16px;font-size:13px;
}
.onboard-howto{
  border:none;border-top:1px solid var(--border);padding-top:14px;
}
.onboard-howto summary{
  font-size:12px;color:var(--text3);cursor:pointer;
  list-style:none;outline:none;user-select:none;
}
.onboard-howto summary::-webkit-details-marker{ display:none; }
.onboard-howto summary::before{ content:"▸ "; color:var(--accent); }
.onboard-howto[open] summary::before{ content:"▾ "; }

/* ── FAB ─────────────────────────────────────────────────────── */
.fab{
  position:fixed;bottom:24px;right:24px;z-index:200;
  background:var(--accent);color:#fff;border:none;border-radius:28px;
  padding:13px 22px;font-family:var(--mono);font-size:13px;font-weight:700;
  cursor:pointer;box-shadow:0 4px 24px rgba(91,127,255,.45);
  transition:transform .15s,box-shadow .15s;
}
.fab:hover{transform:translateY(-2px);box-shadow:0 6px 32px rgba(91,127,255,.6);}

/* ── Scrollbar ───────────────────────────────────────────────── */
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}

/* ── Help modal ──────────────────────────────────────────────── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .2s;}
.modal-overlay.open{opacity:1;pointer-events:all;}
.modal{background:var(--surface);border:1px solid var(--border2);border-radius:var(--r);padding:24px;max-width:560px;width:90%;max-height:80vh;overflow-y:auto;transform:translateY(8px);transition:transform .2s;}
.modal-overlay.open .modal{transform:none;}
.modal-title{font-family:var(--sans);font-size:17px;font-weight:700;margin-bottom:4px;}
.modal-sub{font-size:11px;color:var(--text2);margin-bottom:18px;}
.modal-section{margin-bottom:18px;}
.modal-section h3{font-family:var(--sans);font-size:12px;font-weight:700;color:var(--accent);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px;}
.modal-section p{font-size:12px;color:var(--text2);line-height:1.7;margin-bottom:8px;}
.modal-section ul{list-style:none;padding:0;}
.modal-section ul li{font-size:12px;color:var(--text2);line-height:1.8;padding-left:14px;position:relative;}
.modal-section ul li::before{content:"→";position:absolute;left:0;color:var(--accent);}
.modal-close{margin-top:16px;width:100%;}

/* ── Animations ──────────────────────────────────────────────── */
@keyframes fadeUp{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:none;}}
.main > *{animation:fadeUp .2s ease both;}
</style>
</head>
<body>
<div class="shell">

<!-- ══ TOPBAR ══════════════════════════════════════════════════ -->
<header class="topbar">
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 26 26" fill="none">
      <rect width="26" height="26" rx="6" fill="#1a1a2e"/>
      <rect x="4" y="4" width="18" height="4" rx="2" fill="#5b7fff"/>
      <rect x="4" y="11" width="12" height="4" rx="2" fill="#2dd4bf"/>
      <rect x="4" y="18" width="7" height="4" rx="2" fill="#5b7fff" opacity=".5"/>
      <path d="M17 11 L14 17 L16.5 17 L13.5 23 L20 15 L17.5 15 Z" fill="#facc15"/>
    </svg>
    ZeroToken
  </div>
  <div class="topbar-sep"></div>
  <button type="button" class="project-name" onclick="browseFolder()" title="Click to select project folder&#10;{{ status.project_root or 'No folder selected' }}">
    📁 {{ status.project_root.split('\\')[-1] or status.project_root.split('/')[-1] or status.project_root or 'Select project folder…' }}
  </button>
  <div class="topbar-right">
    {% if status.ollama_ok %}
    <span class="pill pill-green"><span class="dot" style="background:var(--green)"></span>Ollama</span>
    {% else %}
    <span class="pill pill-red"><span class="dot" style="background:var(--red)"></span>Offline</span>
    {% endif %}
    {% if status.git_status == 'clean' %}
    <span class="pill pill-green"><span class="dot" style="background:var(--green)"></span>Git clean</span>
    {% elif status.git_status == 'dirty' %}
    <span class="pill pill-yellow"><span class="dot" style="background:var(--yellow)"></span>Git dirty</span>
    {% else %}
    <span class="pill pill-grey">No git</span>
    {% endif %}
    {% if plan %}<span class="pill pill-blue">{{ status.approved }}/{{ status.total }} done</span>{% endif %}
    <button class="btn btn-ghost btn-sm" onclick="openHelp()">? Help</button>
    <form method="POST" action="/shutdown" style="margin:0;" onsubmit="return confirm('Shut down ZeroToken?')">
      <button type="submit" class="btn btn-ghost btn-sm" style="color:var(--red);border-color:var(--red-dim);">⏻ Off</button>
    </form>
  </div>
</header>

<!-- ══ SIDEBAR ═════════════════════════════════════════════════ -->
<aside class="sidebar">
  <div class="sidebar-header">Workflow</div>

  <!-- Phase 1 -->
  <div class="phase-label">① Setup</div>
  <form method="POST" action="/select-step/0" style="display:contents;">
    <button type="button"
      class="step-item {% if not plan %}active{% endif %}"
      onclick="window.location='/'">
      <span class="step-dot {% if plan %}sd-approved{% else %}sd-pending{% endif %}">
        {% if plan %}✓{% else %}·{% endif %}
      </span>
      <span class="step-title">Project &amp; Goal</span>
    </button>
  </form>

  {% if plan %}
  <!-- Phase 2 -->
  <div class="phase-label">② Steps</div>
  {% for step in plan.steps %}
  {% set st = step.get('status','pending') %}
  <form method="POST" action="/select-step/{{ step.id }}" style="display:contents;">
    <button type="submit"
      class="step-item {% if active_step and active_step.id == step.id %}active{% endif %}">
      <span class="step-dot sd-{{ st }}">
        {% if st == 'approved' %}✓{% elif st == 'skipped' %}–{% elif st == 'failed' %}✕{% else %}·{% endif %}
      </span>
      <span class="step-title">{{ loop.index }}. {{ step.title }}</span>
    </button>
  </form>
  {% endfor %}

  {% if status.pending == 0 and status.approved > 0 %}
  <!-- Phase 3 -->
  <div class="phase-label">③ Deliver</div>
  <button type="button"
    class="step-item {% if state.final_prompt and not active_step %}active{% endif %}"
    onclick="document.getElementById('output-anchor').scrollIntoView({behavior:'smooth'})">
    <span class="step-dot {% if state.final_prompt %}sd-approved{% else %}sd-pending{% endif %}">
      {% if state.final_prompt %}✓{% else %}·{% endif %}
    </span>
    <span class="step-title">Final Prompt</span>
  </button>
  {% endif %}
  {% endif %}

  <!-- Sidebar footer stats -->
  <div class="sidebar-footer">
    {% if plan %}
    <div class="sidebar-stat"><span>Approved</span><span style="color:var(--green)">{{ status.approved }}</span></div>
    <div class="sidebar-stat"><span>Pending</span><span>{{ status.pending }}</span></div>
    {% if status.skipped %}<div class="sidebar-stat"><span>Skipped</span><span style="color:var(--yellow)">{{ status.skipped }}</span></div>{% endif %}
    {% if status.failed %}<div class="sidebar-stat"><span>Failed</span><span style="color:var(--red)">{{ status.failed }}</span></div>{% endif %}
    {% else %}
    <div class="sidebar-stat"><span>No plan loaded</span></div>
    {% endif %}
    <div class="sidebar-stat" style="margin-top:6px;"><span style="color:var(--text3)">{{ status.model_name }}</span></div>

    <!-- ── Model settings ────────────────────────────── -->
    <details class="model-details">
      <summary>⚙ Models <span id="ms-arrow">▾</span></summary>
      <form method="POST" action="/set-models" class="ms-body">
        {% for key, label in [('planner_model','Planner'),('patcher_model','Patcher'),('reviewer_model','Reviewer'),('refiner_model','Refiner')] %}
        <div class="ms-row">
          <label class="ms-label">{{ label }}</label>
          <select name="{{ key }}" class="ms-select">
            {% set cur = state[key] %}
            {% if status.models %}
              {% for m in status.models %}
              <option value="{{ m }}" {% if m == cur %}selected{% endif %}>{{ m }}</option>
              {% endfor %}
              {% if cur not in status.models %}
              <option value="{{ cur }}" selected>{{ cur }}</option>
              {% endif %}
            {% else %}
            <option value="{{ cur }}" selected>{{ cur }}</option>
            {% endif %}
          </select>
        </div>
        {% endfor %}
        <button type="submit" class="btn btn-ghost btn-sm" style="width:100%;font-size:10px;margin-top:2px;">Apply</button>
      </form>
    </details>

    <hr style="border:none;border-top:1px solid var(--border);margin:8px 0;">
    <!-- Always-accessible folder picker -->
    <div style="padding:2px 4px;">
      <form method="POST" action="/set-folder" id="sidebar-folder-form" style="display:flex;gap:4px;margin-bottom:6px;">
        <input type="text" name="project_root" id="sidebar-folder-input"
          value="{{ status.project_root }}"
          style="font-size:10px;padding:4px 6px;flex:1;min-width:0;"
          placeholder="Project path…">
        <button type="submit" class="btn btn-ghost btn-sm" style="padding:4px 7px;font-size:10px;">Set</button>
      </form>
      <button type="button" class="btn btn-copy btn-sm" style="width:100%;font-size:10px;" onclick="browseFolder('sidebar')">📁 Browse folder…</button>
    </div>
  </div>
</aside>

<!-- ══ MAIN PANEL ══════════════════════════════════════════════ -->
<main class="main" id="main">

  <!-- Flash messages -->
  {% if state.error %}<div class="alert alert-error">⚠ {{ state.error }}</div>{% endif %}
  {% if state.info and not state.error %}<div class="alert alert-ok" id="flash-ok">✓ {{ state.info }}</div>{% endif %}

  <!-- Loading banner with auto-poll -->
  <div id="loading-bar" class="loading-bar" style="display:{% if state.bg_running %}flex{% else %}none{% endif %};">
    <span class="spinner"></span>
    <span class="lb-text" id="lb-text">{{ state.bg_label }}</span>
    <span class="lb-time" id="lb-time">0s</span>
  </div>

  <!-- ────────────────────────────────────────────────────────── -->
  <!-- PHASE 1: No plan — Setup screen                           -->
  <!-- ────────────────────────────────────────────────────────── -->
  {% if not plan %}

  <!-- ── ONBOARDING SCREEN ─────────────────────────────────── -->
  <div class="onboard-wrap">

    <div class="onboard-logo">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none"><rect x="3" y="3" width="7" height="7" rx="2" fill="#5b7fff"/><rect x="14" y="3" width="7" height="7" rx="2" fill="#5b7fff" opacity=".6"/><rect x="3" y="14" width="7" height="7" rx="2" fill="#5b7fff" opacity=".6"/><rect x="14" y="14" width="7" height="7" rx="2" fill="#5b7fff" opacity=".3"/><path d="M17 11 L14 17 L16.5 17 L13.5 23 L20 15 L17.5 15 Z" fill="#facc15"/></svg>
      <span class="onboard-logo-text">ZeroToken</span>
    </div>
    <div class="onboard-sub">Select your project, describe your goal, and let Ollama build a plan.</div>

    {% if not status.ollama_ok %}
    <div class="alert alert-error" style="margin-bottom:16px;">⚠ Ollama is offline — start it with <code>ollama serve</code> then refresh.</div>
    {% endif %}

    <!-- ── FOLDER ROW ── -->
    <div class="onboard-field-label">📁 Project folder</div>
    <div class="onboard-folder-row">
      <div class="folder-display" id="folder-display" title="{{ status.project_root }}">{{ status.project_root or 'No folder selected — click Browse' }}</div>
      <button type="button" class="btn btn-copy btn-sm" onclick="browseFolder()">Browse…</button>
    </div>
    <form method="POST" action="/set-folder" id="set-folder-form" style="display:flex;gap:6px;margin-top:6px;">
      <input type="text" name="project_root" id="folder-input"
        placeholder="Or paste a path and press Set"
        value="{{ status.project_root }}" style="font-size:12px;">
      <button type="submit" class="btn btn-ghost btn-sm">Set</button>
    </form>
    {% if not status.git_exists %}
    <div style="display:flex;align-items:center;gap:10px;margin-top:8px;">
      <span style="font-size:11px;color:var(--red);">⚠ No git repo found</span>
      <form method="POST" action="/git-init" style="margin:0;">
        <button type="submit" class="btn btn-yellow btn-sm">⚙ Init Git</button>
      </form>
    </div>
    {% else %}
    <div style="font-size:11px;color:var(--green);margin-top:6px;">✓ Git repo found</div>
    {% endif %}

    <!-- ── GOAL + ACTIONS ── -->
    <form method="POST" style="margin-top:20px;">
      <div class="onboard-field-label">✏ What do you want to build or change?</div>
      <textarea name="goal" rows="5"
        placeholder="e.g. Add a login system with username/password stored in SQLite. Include /login and /logout routes and protect the /dashboard route."
        style="font-size:13px;line-height:1.6;">{{ state.goal }}</textarea>

      <div class="onboard-btn-row">
        <button type="submit" formaction="/plan-local" class="btn btn-primary onboard-btn"
          {% if not status.ollama_ok %}disabled title="Ollama is offline"{% endif %}>
          🤖 Auto-Plan with Ollama
        </button>
        <button type="submit" formaction="/generate-plan" class="btn btn-ghost onboard-btn">
          ⚡ Generate Claude Prompt
        </button>
      </div>
      <div style="font-size:11px;color:var(--text3);margin-top:10px;text-align:center;">
        <b>Auto-Plan</b> — fully local, no copy-paste needed &nbsp;·&nbsp;
        <b>Claude Prompt</b> — higher quality, requires one copy-paste into claude.ai
      </div>
    </form>

    <!-- ── HOW IT WORKS (collapsed) ── -->
    <details class="onboard-howto" style="margin-top:24px;">
      <summary>How does ZeroToken work?</summary>
      <ol class="howto-steps" style="margin-top:10px;">
        <li><span class="num">1</span><span><b>Setup:</b> Point ZeroToken at your project and describe your goal. Ollama generates a step-by-step plan automatically.</span></li>
        <li><span class="num">2</span><span><b>Steps:</b> For each step, Ollama drafts a code patch, reviews it, and you approve or retry.</span></li>
        <li><span class="num">3</span><span><b>Deliver:</b> All approved patches are assembled into one prompt. Paste it into Claude — Claude applies everything to your repo.</span></li>
      </ol>
    </details>

  </div><!-- /onboard-wrap -->

  {% if state.plan_prompt %}
  <!-- Claude prompt appeared — show paste-back area -->
  <div class="card" style="margin-top:20px;">
    <div class="card-title">Planning prompt — copy into Claude</div>
    <div class="card-body">
      <div class="prompt-header">
        <span style="font-size:11px;color:var(--text3);">Copy this → paste into claude.ai → copy Claude's reply → paste below</span>
        <button class="btn btn-copy btn-sm" onclick="copyEl('plan-prompt-box')">⌘ Copy</button>
      </div>
      <div class="prompt-box" id="plan-prompt-box">{{ state.plan_prompt }}</div>
      <hr class="div">
      <label class="field-label">Paste Claude's JSON reply here</label>
      <form method="POST" action="/save-plan">
        <textarea name="plan_json" rows="8" placeholder="Paste the JSON plan from Claude here…"></textarea>
        <div class="btn-row">
          <button type="submit" class="btn btn-primary">💾 Save Plan</button>
        </div>
      </form>
    </div>
  </div>
  {% endif %}

  <!-- ────────────────────────────────────────────────────────── -->
  <!-- PHASE 2: Plan loaded — active step view                   -->
  <!-- ────────────────────────────────────────────────────────── -->
  {% elif active_step %}

  {% set step = active_step %}
  {% set sid  = step.id %}
  {% set st   = step.get('status','pending') %}
  {% set has_prompt        = sid in state.patch_prompts %}
  {% set has_ollama_prompt = sid in state.ollama_prompts %}
  {% set has_diff          = sid in state.diffs %}
  {% set has_review        = sid in state.reviews %}
  {% set has_refined       = sid in state.refined_diffs %}

  <div class="page-title">Step {{ sid }} — {{ step.title }}</div>
  <div class="page-sub">{{ step.description }}</div>

  {% if step.get('suggested_files') %}
  <div class="alert alert-info" style="margin-bottom:14px;">
    📄 <b>Files involved:</b> {{ step.suggested_files | join(', ') }}
  </div>
  {% endif %}

  {% if step.get('acceptance_criteria') %}
  <div class="card" style="margin-bottom:16px;">
    <div class="card-title">Acceptance Criteria</div>
    <div class="card-body">
      <ul style="list-style:none;padding:0;">
        {% for c in step.acceptance_criteria %}
        <li style="font-size:12px;color:var(--text2);line-height:1.8;padding-left:14px;position:relative;">
          <span style="position:absolute;left:0;color:var(--green);">✓</span>{{ c }}
        </li>
        {% endfor %}
      </ul>
    </div>
  </div>
  {% endif %}

  {% if st == 'approved' %}
  <div class="alert alert-ok">✓ This step is approved and included in the final prompt. Select another step or proceed to Deliver.</div>
  <div style="margin-top:10px;">
    <form method="POST" action="/reset-step/{{ sid }}" style="display:inline;">
      <button type="submit" class="btn btn-ghost btn-sm">↩ Reset to pending</button>
    </form>
  </div>
  {% elif st == 'skipped' %}
  <div class="alert alert-info">This step was skipped.</div>
  <form method="POST" action="/reset-step/{{ sid }}" style="margin-top:10px;">
    <button type="submit" class="btn btn-ghost btn-sm">↩ Reset to pending</button>
  </form>
  {% else %}

  <!-- STAGE 1 — Generate patch -->
  <div class="stage {% if has_diff or has_review %}done{% else %}active-stage{% endif %}" data-n="1">
    <div class="stage-title">Generate Patch</div>
    <div class="howto">
      <div class="howto-title">🔧 What happens here?</div>
      <div class="howto-body">
        ZeroToken generates a <b>code patch</b> (a diff) for this step. Choose how:<br><br>
        <b>🤖 Ollama Patch</b> — Ollama automatically reads the relevant files and writes the diff. Fast, fully local, works without internet. Quality varies.<br><br>
        <b>⚡ Claude Prompt</b> — Generates a prompt you copy into Claude. Claude writes a higher-quality, more precise diff. Best for complex or delicate changes.
      </div>
    </div>
    <form method="POST">
      <input type="text" name="extra_instructions"
        placeholder="Optional extra instructions (e.g. 'use async/await', 'add docstrings')…"
        style="margin-bottom:8px;">
      <div class="btn-row">
        <button type="submit" formaction="/patch-local" class="btn btn-primary" style="flex:1;" {% if not status.ollama_ok %}disabled{% endif %}>🤖 Ollama Patch</button>
        <button type="submit" formaction="/generate-patch" class="btn btn-ghost">⚡ Claude Prompt</button>
      </div>
    </form>
  </div>

  {% if has_ollama_prompt %}
  <!-- Ollama prompt preview (collapsible) -->
  <div class="stage done" data-n="✓">
    <div class="stage-title">Prompt sent to Ollama <span style="font-weight:400;color:var(--text3);text-transform:none;letter-spacing:0;">(for reference)</span></div>
    <div class="prompt-header">
      <span></span>
      <button class="btn btn-copy btn-sm" onclick="copyEl('op-{{ sid }}')">⌘ Copy</button>
    </div>
    <div class="prompt-box" id="op-{{ sid }}" style="max-height:120px;font-size:10px;">{{ state.ollama_prompts[sid] }}</div>
  </div>
  {% endif %}

  {% if has_prompt %}
  <!-- STAGE 2 — Claude patch prompt -->
  <div class="stage {% if has_diff %}done{% else %}active-stage{% endif %}" data-n="2">
    <div class="stage-title">Copy Prompt into Claude</div>
    <div class="howto">
      <div class="howto-title">📋 Instructions</div>
      <ol class="howto-steps">
        <li><span class="num">1</span><span>Click <b>⌘ Copy</b> to copy this patch prompt.</span></li>
        <li><span class="num">2</span><span>Paste it into a <b>new Claude conversation</b> at claude.ai.</span></li>
        <li><span class="num">3</span><span>Claude will reply with a unified diff. Copy the entire diff.</span></li>
        <li><span class="num">4</span><span>Come back here and paste into Stage 3 below.</span></li>
      </ol>
    </div>
    <div class="prompt-header">
      <span style="font-size:11px;color:var(--text2);">Patch prompt for step {{ sid }}</span>
      <button class="btn btn-copy btn-sm" onclick="copyEl('pp-{{ sid }}')">⌘ Copy</button>
    </div>
    <div class="prompt-box" id="pp-{{ sid }}">{{ state.patch_prompts[sid] }}</div>
  </div>

  <!-- STAGE 3 — Paste diff -->
  <div class="stage {% if has_diff %}done{% else %}active-stage{% endif %}" data-n="3">
    <div class="stage-title">Paste Claude's Diff &amp; Review</div>
    <div class="howto">
      <div class="howto-title">📥 What is a diff?</div>
      <div class="howto-body">A <b>unified diff</b> shows exactly what lines of code to add or remove. Lines starting with <span style="color:var(--green);">+</span> are additions, lines starting with <span style="color:var(--red);">–</span> are deletions. Just paste Claude's full reply — ZeroToken will extract the diff automatically.</div>
    </div>
    <form method="POST" action="/review-patch">
      <label class="field-label">Paste Claude's diff here</label>
      <textarea name="diff" rows="8"
        placeholder="Paste the unified diff from Claude here…">{% if has_diff %}{{ state.diffs[sid] }}{% endif %}</textarea>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary" style="flex:1;">🔍 Review with Ollama</button>
      </div>
    </form>
  </div>
  {% endif %}

  {% if has_diff and not has_review %}
  <!-- Diff preview before review -->
  <div class="stage active-stage" data-n="→">
    <div class="stage-title">Diff Preview</div>
    <div class="diff-view">
      {% for line in state.diffs[sid].splitlines() %}
        {% if line.startswith('+++') or line.startswith('---') %}<span class="fn">{{ line }}</span>
        {% elif line.startswith('+') %}<span class="add">{{ line }}</span>
        {% elif line.startswith('-') %}<span class="del">{{ line }}</span>
        {% elif line.startswith('@@') %}<span class="hdr">{{ line }}</span>
        {% else %}<span class="ctx">{{ line }}</span>
        {% endif %}
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {% if has_review %}
  <!-- STAGE 4 — Review result + diff -->
  <div class="stage done" data-n="4">
    <div class="stage-title">Ollama Review</div>
    <div class="howto" style="margin-bottom:10px;">
      <div class="howto-title">🔍 What does Ollama check?</div>
      <div class="howto-body">Ollama reviews the diff against the step description — checking for syntax errors, wrong line numbers, missing context lines, and whether the change actually matches the goal. <b>Approve</b> sends it to the final prompt. <b>Retry</b> if there are concerns.</div>
    </div>
    {% set r = state.reviews[sid] %}
    {% if r is mapping %}
      {% set verdict = r.get('verdict','concerns') %}
      <span class="verdict-badge verdict-{{ verdict }}">{{ verdict.upper() }}</span>
      <div class="review-summary">{{ r.get('summary','') }}</div>
      {% if r.get('issues') %}
      <ul class="review-issues">{% for issue in r.issues %}<li>{{ issue }}</li>{% endfor %}</ul>
      {% endif %}
      {% if r.get('notes') %}<div style="font-size:11px;color:var(--text3);margin-top:4px;">{{ r.notes }}</div>{% endif %}
    {% else %}
      <div class="review-plain">
        {% for line in r.splitlines() %}
          {% if 'APPROVE' in line %}<span style="color:var(--green);font-weight:700;">{{ line }}</span><br>
          {% elif 'REJECT' in line %}<span style="color:var(--red);font-weight:700;">{{ line }}</span><br>
          {% elif 'CONCERNS' in line %}<span style="color:var(--yellow);font-weight:700;">{{ line }}</span><br>
          {% else %}{{ line }}<br>
          {% endif %}
        {% endfor %}
      </div>
    {% endif %}

    <hr class="div">
    <div class="stage-title">Diff {% if has_refined %}<span style="color:var(--green);font-weight:400;text-transform:none;letter-spacing:0;">✓ Refined</span>{% endif %}</div>
    <div class="diff-view">
      {% set display_diff = state.refined_diffs.get(sid) or state.diffs.get(sid, '') %}
      {% for line in display_diff.splitlines() %}
        {% if line.startswith('+++') or line.startswith('---') %}<span class="fn">{{ line }}</span>
        {% elif line.startswith('+') %}<span class="add">{{ line }}</span>
        {% elif line.startswith('-') %}<span class="del">{{ line }}</span>
        {% elif line.startswith('@@') %}<span class="hdr">{{ line }}</span>
        {% else %}<span class="ctx">{{ line }}</span>
        {% endif %}
      {% endfor %}
    </div>
  </div>

  <!-- STAGE 5 — Refine (optional) -->
  <div class="stage" data-n="5">
    <div class="stage-title">Refine <span style="font-weight:400;color:var(--text3);text-transform:none;letter-spacing:0;">(optional)</span></div>
    <div class="howto">
      <div class="howto-title">🔧 When should I refine?</div>
      <div class="howto-body">If the review flagged issues with <b>@@ line numbers</b>, indentation, or minor syntax errors, the Refiner can often fix them automatically. Skip this if the review approved the diff cleanly.</div>
    </div>
    <form method="POST" action="/refine-patch">
      <button type="submit" class="btn btn-yellow" style="width:100%;" {% if not status.ollama_ok %}disabled{% endif %}>🔧 Refine with Ollama-Refiner</button>
    </form>
  </div>

  <!-- STAGE 6 — Decision -->
  <div class="stage active-stage" data-n="6">
    <div class="stage-title">Decision</div>
    <div class="howto">
      <div class="howto-title">✅ What do Approve / Skip / Retry mean?</div>
      <div class="howto-body">
        <b>Approve</b> — You're happy with this diff. It gets added to the final prompt that Claude will apply.<br><br>
        <b>Skip</b> — Skip this step entirely. It won't be applied. Use this if a step is already done or not needed.<br><br>
        <b>Retry</b> — Something's wrong. Generate a new patch, optionally with extra instructions.
      </div>
    </div>
    <div class="btn-row">
      <form method="POST" action="/apply-patch">
        <button type="submit" class="btn btn-green">✓ Approve → Final Prompt</button>
      </form>
      <form method="POST" action="/skip-step">
        <button type="submit" class="btn btn-yellow">→ Skip</button>
      </form>
    </div>
    <hr class="div">
    <div class="stage-title" style="margin-bottom:8px;">Retry</div>
    <form method="POST">
      <input type="text" name="extra_instructions"
        placeholder="Optional extra instructions for retry…"
        style="margin-bottom:8px;">
      <div class="btn-row">
        <button type="submit" formaction="/patch-local" class="btn btn-red" style="flex:1;" {% if not status.ollama_ok %}disabled{% endif %}>🤖 Ollama Retry</button>
        <button type="submit" formaction="/generate-patch" class="btn btn-ghost">⚡ Claude Retry</button>
      </div>
    </form>
  </div>

  {% endif %}{# has_review #}
  {% endif %}{# st == pending #}

  <!-- ────────────────────────────────────────────────────────── -->
  <!-- PHASE 2 overview: no active step (all done)              -->
  <!-- ────────────────────────────────────────────────────────── -->
  {% elif plan %}

  <div class="page-title">All steps complete</div>
  <div class="page-sub">Every step has been approved or skipped. You can now assemble the Final Agent Prompt and paste it into Claude.</div>
  <form method="POST" action="/assemble-prompt">
    <button type="submit" class="btn btn-teal" style="margin-bottom:14px;">⚡ Assemble Final Prompt</button>
  </form>
  <form method="POST" action="/clear-plan">
    <button type="submit" class="btn btn-ghost btn-sm">🗑 Start new plan</button>
  </form>

  {% endif %}

  <!-- ────────────────────────────────────────────────────────── -->
  <!-- PHASE 3: Final prompt (always shown when available)       -->
  <!-- ────────────────────────────────────────────────────────── -->
  {% if state.final_prompt %}
  <div id="output-anchor" style="height:1px;margin-top:8px;"></div>
  <div class="card final-card" style="margin-top:16px;">
    <div class="card-title" style="color:var(--teal);">⚡ Final Agent Prompt</div>
    <div class="card-body">

      <div class="howto" style="background:var(--teal-dim);border-color:#1a4a44;margin-bottom:14px;">
        <div class="howto-title" style="color:var(--teal);">🚀 You're done with ZeroToken — here's what to do next</div>
        <ol class="howto-steps">
          <li><span class="num" style="background:var(--teal);color:#000;">1</span><span>Click <b>⌘ Copy All</b> below to copy the entire prompt.</span></li>
          <li><span class="num" style="background:var(--teal);color:#000;">2</span><span>Open <b>claude.ai</b> in a new tab.</span></li>
          <li><span class="num" style="background:var(--teal);color:#000;">3</span><span>Paste the prompt into a <b>new conversation</b>.</span></li>
          <li><span class="num" style="background:var(--teal);color:#000;">4</span><span>Claude will read your code, apply all the approved diffs, and report what it did. You don't need to do anything else.</span></li>
        </ol>
        <div class="tip" style="background:rgba(0,0,0,.2);border-color:var(--teal-dim);">
          <b>Tip:</b> Make sure your repo is in a <b>clean git state</b> before pasting (commit or stash any uncommitted changes). That way you can easily <code>git diff</code> or <code>git revert</code> if anything looks wrong.
        </div>
      </div>

      <div class="prompt-header">
        <span style="font-size:11px;color:var(--text2);">{{ state.final_prompt | length }} characters</span>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-copy btn-sm" onclick="copyEl('final-prompt-ta')">⌘ Copy All</button>
          <form method="POST" action="/assemble-prompt" style="margin:0;">
            <button type="submit" class="btn btn-ghost btn-sm">↺ Rebuild</button>
          </form>
        </div>
      </div>
      <textarea id="final-prompt-ta" class="final-ta" readonly onclick="this.select()">{{ state.final_prompt }}</textarea>
    </div>
  </div>
  {% endif %}

  <div style="height:60px;"></div>
</main>

</div>{# /.shell #}

<!-- ── Run All FAB ──────────────────────────────────────────── -->
{% if plan and status.pending > 0 and not state.bg_running %}
<form method="POST" action="/run-all" style="margin:0;" onsubmit="return confirm('Run all pending steps automatically with Ollama and auto-approve? Best for simple changes.')">
  <button type="submit" class="fab">▶▶ Run All</button>
</form>
{% endif %}

<!-- ── Help Modal ───────────────────────────────────────────── -->
<div class="modal-overlay" id="help-modal" onclick="if(event.target===this)closeHelp()">
  <div class="modal">
    <div class="modal-title">ZeroToken — Help &amp; Reference</div>
    <div class="modal-sub">A local AI coding assistant that uses Ollama + Claude to plan, draft, and apply code changes.</div>

    <div class="modal-section">
      <h3>The Workflow</h3>
      <p>ZeroToken follows three phases. You work through them in order:</p>
      <ul>
        <li><b>Setup</b> — Choose your project folder, describe your goal, generate a plan.</li>
        <li><b>Steps</b> — For each step: generate a patch, review it, approve or retry.</li>
        <li><b>Deliver</b> — Assemble the final prompt, paste it into Claude, done.</li>
      </ul>
    </div>

    <div class="modal-section">
      <h3>Ollama vs Claude — when to use which</h3>
      <p><b>Ollama</b> runs entirely on your machine. It's free, offline, and fast but the quality depends on your local model. Use it for straightforward changes.</p>
      <p><b>Claude</b> (via claude.ai) produces higher-quality plans and patches. It requires a copy-paste step but is worth it for complex changes. You need a Claude account.</p>
    </div>

    <div class="modal-section">
      <h3>What is a diff / patch?</h3>
      <p>A unified diff is a compact way to represent code changes. Lines starting with <span style="color:#6abf69;">+</span> are added, lines starting with <span style="color:#f07070;">–</span> are removed. ZeroToken never applies diffs directly — Claude does that in the final step.</p>
    </div>

    <div class="modal-section">
      <h3>The Review step</h3>
      <p>After a patch is generated, Ollama reviews it to check for errors before you approve it. It checks line numbers, syntax, and whether the change matches the step goal. If it flags concerns, you can Refine or Retry.</p>
    </div>

    <div class="modal-section">
      <h3>Run All</h3>
      <p>The blue <b>▶▶ Run All</b> button (bottom right) runs every pending step automatically using Ollama — generating, reviewing, and auto-approving each patch. Best for simple changes where you trust Ollama's output.</p>
    </div>

    <div class="modal-section">
      <h3>Status indicators</h3>
      <ul>
        <li><b>Ollama green</b> — Ollama is running locally and reachable.</li>
        <li><b>Git clean</b> — No uncommitted changes in your repo. Recommended before applying.</li>
        <li><b>Git dirty</b> — Uncommitted changes exist. Not a blocker but commit first if possible.</li>
        <li><b>No git</b> — No git repo found. Use "Initialise Git" in Setup.</li>
      </ul>
    </div>

    <button class="btn btn-ghost modal-close" onclick="closeHelp()">Close</button>
  </div>
</div>

<script>
/* ── Auto-poll while Ollama runs ───────────────────────────── */
let pollTimer = null;
let pollStart = null;

function startPoll() {
  if (pollTimer) return;
  pollStart = Date.now();
  pollTimer = setInterval(doPoll, 2500);
}

function stopPoll() {
  clearInterval(pollTimer);
  pollTimer = null;
  pollStart = null;
}

async function doPoll() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    const bar = document.getElementById('loading-bar');
    const lbText = document.getElementById('lb-text');
    const lbTime = document.getElementById('lb-time');
    if (d.bg_running) {
      if (bar) { bar.style.display = 'flex'; }
      if (lbText) lbText.textContent = d.bg_label || 'Ollama is working…';
      if (lbTime && pollStart) {
        const secs = Math.floor((Date.now() - pollStart) / 1000);
        lbTime.textContent = secs + 's';
      }
    } else {
      stopPoll();
      // Reload to show new state
      window.location.reload();
    }
  } catch(e) { /* network error, keep polling */ }
}

// Start polling if already running on load
(function(){
  const bar = document.getElementById('loading-bar');
  if (bar && bar.style.display !== 'none') {
    startPoll();
  }
})();

/* ── Copy helper ────────────────────────────────────────────── */
function copyEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.tagName === 'TEXTAREA' ? el.value : el.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector(`[onclick="copyEl('${id}')"]`);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ Copied!';
      setTimeout(() => btn.textContent = orig, 2000);
    }
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
  });
}

/* ── Folder browse ──────────────────────────────────────────── */
function browseFolder(mode) {
  const allBtns = document.querySelectorAll('[onclick^="browseFolder"]');
  allBtns.forEach(b => { b.textContent = 'Opening…'; b.disabled = true; });

  fetch('/browse')
    .then(r => r.json())
    .then(d => {
      allBtns.forEach(b => { b.disabled = false; b.textContent = b.textContent.includes('sidebar') ? '📁 Browse folder…' : '📁 Browse'; });
      if (d.error) { alert('Browse error: ' + d.error); return; }
      if (!d.path) return;
      // Update topbar inputs if present
      const fi = document.getElementById('folder-input');
      const fd = document.getElementById('folder-display');
      const sf = document.getElementById('set-folder-form');
      if (fi) fi.value = d.path;
      if (fd) { fd.textContent = d.path; fd.title = d.path; }
      // Update sidebar inputs
      const sfi = document.getElementById('sidebar-folder-input');
      if (sfi) sfi.value = d.path;
      // Submit whichever form is available
      if (sf) { sf.submit(); }
      else {
        const sff = document.getElementById('sidebar-folder-form');
        if (sff) sff.submit();
      }
    })
    .catch(e => {
      allBtns.forEach(b => { b.disabled = false; });
      alert('Browse failed: ' + e);
    });
}

/* ── Help modal ─────────────────────────────────────────────── */
function openHelp()  { document.getElementById('help-modal').classList.add('open'); }
function closeHelp() { document.getElementById('help-modal').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeHelp(); });

/* ── Auto-dismiss flash after 6s ───────────────────────────── */
const flash = document.getElementById('flash-ok');
if (flash) setTimeout(() => { flash.style.opacity = '0'; flash.style.transition = 'opacity .5s'; }, 6000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_server(host: str = "127.0.0.1", port: int = 5000, open_browser: bool = True):
    from werkzeug.serving import make_server
    import webbrowser, time

    sm = get_shutdown_manager()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  lambda s, f: sm.shutdown(reason="SIGINT"))
        signal.signal(signal.SIGTERM, lambda s, f: sm.shutdown(reason="SIGTERM"))

    srv = make_server(host, port, app)
    srv.timeout = 1

    sm.register_callback(srv.shutdown, name="werkzeug-server")

    server_thread = threading.Thread(target=srv.serve_forever, daemon=True, name="flask-server")
    server_thread.start()

    print(f"\n  ZeroToken")
    print(f"  → http://{host}:{port}")
    print(f"  Ctrl-C or the Shutdown button to stop.\n")

    if open_browser and not os.getenv("NO_BROWSER"):
        def _open():
            time.sleep(0.8)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=_open, daemon=True).start()

    sm.wait()
    server_thread.join(timeout=5)
    print("[ZeroToken] Server stopped.")
