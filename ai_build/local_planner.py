"""
local_planner.py - Use Ollama to generate a build plan entirely locally.

Replaces the Claude → paste-JSON workflow with a single local call:
  plan_dict, err = generate_plan_local(goal, root=".")
"""

import json
import os
import re

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
# Planning only needs the file tree — NOT full file contents.
# Sending the full source context to a small model eats all available tokens
# and causes it to return {} because there's no room left for a response.

_PLAN_PROMPT = """\
You are a senior software engineer. Output ONLY a JSON object — no prose, no markdown.

GOAL: {goal}

PROJECT FILE TREE:
{file_tree}

REQUIRED OUTPUT — return this JSON object and nothing else:
{{
  "plan_name": "Short plan title",
  "steps": [
    {{
      "id": 1,
      "title": "Short title (max 8 words)",
      "description": "Exact description of what code to add/change/remove",
      "suggested_files": ["relative/path/to/file.py"],
      "acceptance_criteria": ["Observable outcome that proves this step works"],
      "status": "pending"
    }}
  ]
}}

RULES:
- 3 to 7 steps maximum
- Each step touches 1-3 files from the FILE TREE above (or names a new file)
- Dependency order: no step may depend on a later step
- Do NOT add tests, docs, or CI unless the goal explicitly requires them
- Each acceptance_criteria item MUST be SHORT and TESTABLE — something you
  can verify by reading the file. Write what will literally appear in the code.
  GOOD: "import logging appears at top of server.py"
  GOOD: "function save_user() writes a row to the database"
  GOOD: "route /api/items returns a JSON list"
  BAD:  "the code works"
  BAD:  "feature is added"
  BAD:  "it runs correctly"
"""

# Fallback: used when model still fails with tree context
_PLAN_PROMPT_BARE = """\
You are a senior software engineer. Output ONLY a JSON object — no prose, no markdown.

GOAL: {goal}

REQUIRED OUTPUT — return this JSON object and nothing else:
{{
  "plan_name": "Short plan title",
  "steps": [
    {{
      "id": 1,
      "title": "Short title (max 8 words)",
      "description": "Exact description of what code to add/change/remove",
      "suggested_files": ["path/to/file.py"],
      "acceptance_criteria": ["Observable outcome that proves this step works"],
      "status": "pending"
    }}
  ]
}}

RULES:
- 3 to 7 steps maximum
- Dependency order: no step may depend on a later step
- Each acceptance_criteria item MUST be SHORT and TESTABLE — something you
  can verify by reading the file. Write what will literally appear in the code.
  GOOD: "import logging appears at top of server.py"
  GOOD: "function save_user() writes a row to the database"
  BAD:  "the code works" / "feature is added" / "it runs correctly"
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_tree_only(root: str, max_lines: int = 120) -> str:
    """Return just the file tree from context (no file contents — keeps prompt small)."""
    from ai_build.storage import get_repo_file_tree
    tree_lines = get_repo_file_tree(root).splitlines()
    if len(tree_lines) > max_lines:
        tree_lines = tree_lines[:max_lines] + [f"… ({len(tree_lines) - max_lines} more lines)"]
    return "\n".join(tree_lines)


def _parse_plan(raw: str) -> dict | None:
    """
    Extract a valid plan dict from a raw model response.
    Returns the dict if it contains a non-empty 'steps' list, else None.
    """
    text = raw.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    # Find outermost JSON object
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    steps = data.get("steps")
    if not steps or not isinstance(steps, list) or len(steps) == 0:
        return None
    return data


def _finalise(data: dict, goal: str) -> dict:
    """Normalise steps and return the canonical plan dict."""
    for i, step in enumerate(data["steps"], 1):
        step.setdefault("id",                  i)
        step.setdefault("status",              "pending")
        step.setdefault("suggested_files",     [])
        step.setdefault("acceptance_criteria", [])
        step.setdefault("description",         step.get("title", ""))
    return {"goal": goal, "plan_name": data.get("plan_name", goal[:60]), "steps": data["steps"]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_plan_local(goal: str, root: str = ".", model: str | None = None) -> tuple[dict | None, str]:
    """
    Use Ollama to generate a step plan for `goal` in the project at `root`.

    Attempt 1: file tree + goal  (small context, leaves room for response).
    Attempt 2: goal only         (bare prompt, last resort).

    Returns:
        (plan_dict, "")            on success
        (None,      error_string)  on failure
    """
    from ai_build.reviewer import _call_ollama_api

    model = model or os.getenv("OLLAMA_MODEL", "gemma3:4b")

    # ── Attempt 1: file tree context ──────────────────────────────────────
    file_tree = _file_tree_only(root)
    prompt1   = _PLAN_PROMPT.format(goal=goal, file_tree=file_tree)
    raw1      = _call_ollama_api(model, prompt1, force_json=True)

    if not raw1.startswith("(Ollama"):
        data = _parse_plan(raw1)
        if data:
            return _finalise(data, goal), ""

    # ── Attempt 2: bare prompt (no context) ───────────────────────────────
    prompt2 = _PLAN_PROMPT_BARE.format(goal=goal)
    raw2    = _call_ollama_api(model, prompt2, force_json=True)

    if raw2.startswith("(Ollama"):
        return None, raw2

    data = _parse_plan(raw2)
    if data:
        return _finalise(data, goal), ""

    # Both attempts failed
    last_raw = raw2 if raw2.strip() not in ("{}", "") else raw1
    return None, (
        f"Ollama did not return a valid plan after 2 attempts.\n\n"
        f"Last raw response:\n{last_raw[:800]}"
    )
