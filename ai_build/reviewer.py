"""
reviewer.py - Local-only code review agent using Ollama.

Calls Ollama with full project context and returns a structured review dict:
  {
    "verdict":  "approve" | "concerns" | "reject",
    "summary":  "one-sentence summary",
    "issues":   ["issue 1", "issue 2", ...],
    "notes":    "optional extra notes",
    "raw":      "(original model output — for debugging)",
  }
"""

import json
import os
import re
import subprocess
import urllib.request
import urllib.error

OLLAMA_HOST = "http://localhost:11434"

# ---------------------------------------------------------------------------
# Agent Prompt
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """\
You are a LOCAL-ONLY Code Review Agent acting as an ADVERSARIAL REVIEWER. \
Assume the patch has problems and hunt for them. You have no internet access. \
You do not call APIs. You do not run code. You only read and reason.

Your job is to catch errors BEFORE they reach Claude. Be strict.

══════════════════════════ REVIEW CHECKLIST ════════════════════════
1.  CORRECTNESS   — Does the patch ACTUALLY do what the step description says?
                    Read the description literally. Does the code match it exactly?
2.  IMPORTS       — Are all used modules imported? Any missing imports?
3.  FILE PATHS    — Do all referenced paths exist in the FILE TREE?
4.  NAMING        — Are names consistent with the project's conventions?
5.  ARCHITECTURE  — Does this fit the project's patterns and structure?
6.  DEPENDENCIES  — Does it invent libraries not in requirements?
7.  LOGIC ERRORS  — Any bugs, off-by-one errors, or incorrect logic?
8.  SECURITY      — Any obvious vulnerabilities (injection, hardcoded secrets)?
9.  SIDE EFFECTS  — Any unintended consequences outside the step's scope?
10. MINIMAL       — Does it change more than necessary? Any unrelated code touched?
11. DIFF MATH     — For EVERY @@ hunk, count the body lines yourself:
                    - Lines starting with ' ' or '-' = ORIG_COUNT
                    - Lines starting with ' ' or '+' = NEW_COUNT
                    Check that @@ -X,ORIG_COUNT +Y,NEW_COUNT @@ is correct.
                    Flag any mismatch — wrong counts cause "corrupt patch" errors.
══════════════════════════════════════════════════════════

══════════════════════════ PROJECT CONTEXT ══════════════════════════
{context}
══════════════════════════════════════════════════════════

STEP BEING IMPLEMENTED:
  {description}

PATCH TO REVIEW:
{patch}

INSTRUCTIONS:
- Work through EVERY point in the checklist above.
- Be precise and cite specific line numbers or identifiers where possible.
- Do NOT hallucinate features. If something looks correct, say so.
- Return ONLY valid JSON — no markdown, no prose outside the JSON.

OUTPUT FORMAT (return this JSON object, nothing else):
{{
  "verdict": "approve",
  "summary": "One sentence describing the overall quality.",
  "issues": [
    "Issue 1 — specific description",
    "Issue 2 — specific description"
  ],
  "notes": "Any additional observations (optional, can be empty string)."
}}

VERDICT values:
  "approve"  — patch is correct and safe to apply
  "concerns" — patch works but has minor issues worth noting
  "reject"   — patch has errors that will break the project

IMPORTANT: If the patch is correct and complete, issues should be an empty list [].
"""


# ---------------------------------------------------------------------------
# Ollama transport
# ---------------------------------------------------------------------------

def _call_ollama_api(model: str, prompt: str, force_json: bool = False) -> str:
    """Call Ollama via its REST API. Returns the response text.

    Args:
        force_json: if True, passes ``"format": "json"`` in the request body,
                    which instructs Ollama to constrain the model's output to
                    valid JSON (supported by Ollama >= 0.1.9).
    """
    url = f"{OLLAMA_HOST}/api/generate"
    # num_ctx tells Ollama how many tokens to keep in the KV cache / context
    # window. Default is often 2048 which silently truncates large prompts.
    # 32768 is well within gemma3:4b's capability and safe for local hardware.
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
    body: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
    if force_json:
        body["format"] = "json"
    payload = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("response", "").strip()
    except urllib.error.URLError as e:
        return f"(Ollama connection error: {e}. Is Ollama running? Run: ollama serve)"
    except json.JSONDecodeError:
        return "(Ollama returned invalid JSON.)"
    except Exception as e:
        return f"(Unexpected error calling Ollama: {e})"


def _call_ollama_cli(model: str, prompt: str) -> str:
    """Fallback: Call Ollama via subprocess CLI."""
    try:
        result = subprocess.run(
            ["ollama", "run", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"(Ollama CLI error: {result.stderr.strip()})"
    except FileNotFoundError:
        return "(Ollama not found. Install from https://ollama.com)"
    except subprocess.TimeoutExpired:
        return "(Ollama timed out after 120 seconds.)"
    except Exception as e:
        return f"(Unexpected error calling Ollama CLI: {e})"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_review(raw: str) -> dict:
    """
    Parse Ollama's response into a structured review dict.
    Tries JSON first; falls back to heuristic text parsing.
    """
    text = raw.strip()

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        end   = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text  = "\n".join(lines[1:end]).strip()

    # Try to extract a JSON object
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group())
            verdict = str(data.get("verdict", "concerns")).lower().strip()
            if verdict not in ("approve", "concerns", "reject"):
                verdict = "concerns"
            issues = data.get("issues", [])
            if isinstance(issues, str):
                issues = [issues]
            return {
                "verdict": verdict,
                "summary": str(data.get("summary", "")).strip(),
                "issues":  [str(i).strip() for i in issues if str(i).strip()],
                "notes":   str(data.get("notes", "")).strip(),
                "raw":     raw,
            }
        except (json.JSONDecodeError, TypeError):
            pass

    # ── Heuristic text fallback ─────────────────────────────────────────────────
    verdict = "concerns"
    if re.search(r"\bAPPROVE\b|\bapprove\b", raw, re.I):
        verdict = "approve"
    if re.search(r"\bREJECT\b|\breject\b", raw, re.I):
        verdict = "reject"

    summary_match = re.search(r"SUMMARY[:\s]+(.+)", raw, re.I)
    summary = summary_match.group(1).strip() if summary_match else raw.splitlines()[0][:120]

    issues = [
        line.lstrip("- \u2022*").strip()
        for line in raw.splitlines()
        if line.strip().startswith(("-", "\u2022", "*")) and len(line.strip()) > 3
    ]

    return {
        "verdict": verdict,
        "summary": summary,
        "issues":  issues,
        "notes":   "",
        "raw":     raw,
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def format_review_text(review: dict) -> str:
    """Render a review dict as human-readable terminal text."""
    lines: list[str] = []
    verdict = review.get("verdict", "concerns").upper()
    lines.append(f"VERDICT : {verdict}")
    lines.append(f"SUMMARY : {review.get('summary', '')}")
    issues = review.get("issues", [])
    if issues:
        lines.append("ISSUES  :")
        for issue in issues:
            lines.append(f"  - {issue}")
    notes = review.get("notes", "")
    if notes:
        lines.append(f"NOTES   : {notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def review_patch(patch: str, step_description: str, context: str = "", model: str | None = None) -> dict:
    """
    Send the patch to Ollama for structured review.

    Returns a review dict:
      {"verdict": "approve"|"concerns"|"reject", "summary": str,
       "issues": [...], "notes": str, "raw": str}

    Pass `context` (from context_engine.context_to_text()) for full project awareness.
    """
    model = model or os.getenv("OLLAMA_MODEL", "gemma3:4b")
    prompt = REVIEW_PROMPT.format(
        context     = context if context else "(no project context provided)",
        description = step_description,
        patch       = patch,
    )

    # Try API first, fall back to CLI
    raw = _call_ollama_api(model, prompt)
    if raw.startswith("(Ollama connection error"):
        raw = _call_ollama_cli(model, prompt)

    return _parse_review(raw)
