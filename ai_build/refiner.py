"""
refiner.py — Ollama-Refiner agent.

Takes a generated diff + the Ollama reviewer's feedback and produces a
cleaner, minimal, correctly-formatted unified diff.

This is a dedicated Ollama instance (separate token window from the patcher
and reviewer) so it has full context budget to reason about both the
original diff and the feedback simultaneously.
"""

import os

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REFINE_PROMPT = """\
You are a unified diff refiner. You receive:
  1. A description of the step to implement
  2. The CURRENT contents of the target files (as they exist RIGHT NOW)
  3. A review of a generated diff (with issues found)
  4. The original diff

Your job: output a CORRECTED, MINIMAL unified diff that fixes all issues
found in the review.

RULES (follow exactly):
- Output ONLY the corrected unified diff — no markdown, no explanation
- Start immediately with --- a/path
- @@ line numbers MUST match the CURRENT FILE CONTENTS shown below
  — count lines in the file yourself to get the right numbers
- @@ counts must match the actual hunk body lines you output
  - ORIG_COUNT = context lines + removed lines
  - NEW_COUNT  = context lines + added lines
- Include exactly 3 context lines before and after each change
- Do NOT change files not mentioned in the step description
- If the original diff is already correct, output it unchanged

STEP DESCRIPTION:
{description}

CURRENT FILE CONTENTS (use these for correct line numbers):
{file_contents}

REVIEW FEEDBACK:
{review_text}

ORIGINAL DIFF TO IMPROVE:
{diff}
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refine_patch(diff: str, step: dict, review: dict | str, root: str = ".", model: str | None = None) -> tuple[str, str]:
    """
    Use the Ollama-Refiner to clean up a generated diff based on review feedback.

    Each call uses OLLAMA_REFINER_MODEL (falls back to OLLAMA_MODEL) with its
    own fresh 32 768-token context window — independent of the patcher and reviewer.

    Returns:
        (refined_diff, error_string)
        If refiner cannot improve the diff, original is returned unchanged.
    """
    from ai_build.reviewer import _call_ollama_api
    from ai_build.local_patcher import _extract_diff, _sanitize_diff
    import pathlib

    # Format review into review text
    if isinstance(review, dict):
        verdict = review.get("verdict", "concerns")
        summary = review.get("summary", "")
        issues  = review.get("issues", [])
        review_text = f"Verdict: {verdict.upper()}\n{summary}"
        if issues:
            review_text += "\nIssues found:\n" + "\n".join(f"  - {i}" for i in issues)
        notes = review.get("notes", "")
        if notes:
            review_text += f"\nNotes: {notes}"
    else:
        review_text = str(review)[:1_200]

    # Read current file contents so refiner can produce correct line numbers
    root_path = pathlib.Path(root).resolve()
    file_parts: list[str] = []
    for rel_path in step.get("suggested_files", []):
        fpath = root_path / rel_path
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            # Cap at 4000 chars per file to stay within budget
            if len(content) > 4000:
                content = content[:4000] + "\n...(truncated)"
            file_parts.append(f"{'═' * 50}\nFILE: {rel_path}\n{'═' * 50}\n{content}")
        except Exception:
            file_parts.append(f"FILE: {rel_path} — [new file, does not exist yet]")
    file_contents = "\n".join(file_parts) if file_parts else "(no files listed)"

    prompt = _REFINE_PROMPT.format(
        description=step.get("description", step.get("title", "(no description)")),
        file_contents=file_contents,
        review_text=review_text,
        diff=diff,
    )

    model = model or os.getenv("OLLAMA_REFINER_MODEL", os.getenv("OLLAMA_MODEL", "gemma3:4b"))
    raw = _call_ollama_api(model, prompt, force_json=False)

    if raw.startswith("(Ollama"):
        # Connection / model error — return original diff unchanged
        return diff, f"Refiner unavailable: {raw}"

    extracted = _extract_diff(raw.strip())
    if extracted is None:
        # Refiner returned prose or couldn't produce a valid diff — keep original
        return diff, ""

    return _sanitize_diff(extracted), ""
