"""
executor.py - Generates patch-request prompts for you to paste into Claude manually,
then waits for you to paste the unified diff back. Sends the diff to Ollama for review.
No API keys required.
"""

from ai_build.storage import (
    load_plan,
    update_step_status,
    save_patch,
    save_prompt,
    read_files,
)
from ai_build.reviewer import review_patch
from ai_build.context import detect_stack, build_file_tree, get_git_status
from ai_build.ui import (
    show_step_and_ask,
    print_prompt_block,
    open_prompt_in_browser,
    paste_multiline,
    print_section,
    bold,
    green,
    yellow,
    red,
)
from ai_build.git_ops import apply_patch, commit_step, _is_git_repo

PATCH_INSTRUCTIONS = """You are a senior software engineer producing code changes.
You will be given a step description, the current state of the relevant files,
the project's tech stack, and any additional context below.
Produce a unified diff patch that fully implements the described change.

Before returning, verify against this checklist:
- Does the diff fully implement the step description?
- Are all acceptance criteria satisfied?
- Are all used modules properly imported?
- Is the code consistent with the project's existing style and conventions?
- Is it the minimum change that achieves the goal? No unrelated edits."""

RETRY_PATCH_INSTRUCTIONS = """You are a senior software engineer producing code changes.
A previous patch for this step was rejected — see the "PREVIOUS ATTEMPT" section below.
Produce a corrected unified diff that fully addresses every point in the feedback.

Before returning, verify against this checklist:
- Does every point in the feedback get addressed?
- Does the diff fully implement the step description?
- Are all acceptance criteria satisfied?
- Are all used modules properly imported?"""

# Appended to every Claude patch prompt to enforce clean diff output.
OUTPUT_FORMAT = """
════════════════════════════════════════════════
OUTPUT INSTRUCTIONS — READ CAREFULLY:
Return ONLY a valid unified diff. Nothing else.
✗  No explanation or prose before or after the diff
✗  No markdown code fences (no ```diff or ``` markers)
✗  No chr() encoding hacks or escaped characters
✓  The diff MUST start with the --- line
✓  Must be directly applyable with: git apply

For a new file that does not yet exist:
  --- /dev/null
  +++ b/path/to/new/file.py

For an existing file:
  --- a/path/to/existing/file.py
  +++ b/path/to/existing/file.py
════════════════════════════════════════════════"""


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _previous_steps_summary(plan: dict, current_step_id: int) -> str:
    """
    Return a brief summary of all steps completed before current_step_id,
    including the first 800 chars of each applied diff for added context.
    Only includes steps with status applied/approved/skipped.
    Returns an empty string if there are no prior completed steps.
    """
    from ai_build.storage import load_patch

    done_statuses = {"applied", "approved", "skipped"}
    prior = [
        s for s in plan.get("steps", [])
        if s["id"] < current_step_id and s.get("status") in done_statuses
    ]
    if not prior:
        return ""
    lines: list[str] = []
    for s in prior:
        status  = s.get("status", "pending")
        files   = ", ".join(s.get("suggested_files", [])) or "(unknown files)"
        lines.append(f"  Step {s['id']} [{status}]: {s['title']}")
        lines.append(f"    Files touched: {files}")
        if status != "skipped":
            try:
                diff_text = load_patch(s["id"])
                if diff_text:
                    snippet = diff_text.strip()[:800]
                    if len(diff_text.strip()) > 800:
                        snippet += "\n    ...(truncated)"
                    lines.append(f"    Change applied (first 800 chars):\n{snippet}")
            except Exception:
                pass  # non-fatal — storage may not have a patch yet
    return "\n".join(lines)



def _build_patch_prompt(
    step: dict,
    plan: dict | None = None,
    extra_instructions: str = "",
    previous_draft: str = "",
    previous_review: dict | None = None,
) -> str:
    """
    Assemble the complete prompt the user will paste into Claude.

    Incorporates:
    - Tech stack and project structure (context.py)
    - Git working-tree status (context.py)
    - Current file contents for every file in step["suggested_files"]
    - Previous steps summary (so Claude knows what already changed)
    - Acceptance criteria (so Claude knows exactly what success looks like)
    - Previous attempt diff + Ollama review (on retry)
    - Ollama confidence framing (flags when draft is poor quality)
    - Mandatory output format instructions
    """

    # ── Pick the right instruction block ─────────────────────────────────────
    is_retry = bool(extra_instructions or previous_draft)
    instructions = RETRY_PATCH_INSTRUCTIONS if is_retry else PATCH_INSTRUCTIONS

    # ── Stack + project structure ─────────────────────────────────────────────
    stack = detect_stack(".")
    file_tree = build_file_tree(".")

    # ── Git status ────────────────────────────────────────────────────────────
    git_status = get_git_status(".")

    # ── Current file contents ─────────────────────────────────────────────────
    suggested_files = step.get("suggested_files", [])
    files_block_parts: list[str] = []
    for path in suggested_files:
        sep = "─" * 40
        if not path:
            continue
        import os
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                files_block_parts.append(
                    f"{sep}\nFile: {path}\n{sep}\n{content}"
                )
            except Exception as exc:
                files_block_parts.append(
                    f"{sep}\nFile: {path}\n{sep}\n(could not read: {exc})"
                )
        else:
            files_block_parts.append(
                f"{sep}\nFile: {path}\n{sep}\n[new file — does not exist yet]"
            )
    files_block = "\n".join(files_block_parts) or "(no files listed)"

    # ── Previous steps summary ────────────────────────────────────────────────
    prev_summary_section = ""
    if plan and step["id"] > 1:
        summary = _previous_steps_summary(plan, step["id"])
        if summary:
            prev_summary_section = (
                f"\n════════════════════════════════════════\n"
                f"PREVIOUSLY COMPLETED STEPS\n"
                f"(these changes are already in the codebase):\n"
                f"{summary}\n"
            )

    # ── Acceptance criteria ───────────────────────────────────────────────────
    criteria = step.get("acceptance_criteria", [])
    criteria_section = ""
    if criteria:
        criteria_lines = "\n".join(f"  • {c}" for c in criteria)
        criteria_section = (
            f"\n════════════════════════════════════════\n"
            f"ACCEPTANCE CRITERIA (your diff MUST satisfy ALL of these):\n"
            f"{criteria_lines}\n"
        )

    # ── Ollama draft + confidence framing (items 6 & 7) ─────────────────────
    draft_section = ""
    if previous_draft:
        # Map Ollama verdict → confidence level
        verdict = (previous_review or {}).get("verdict", "concerns")
        confidence = {
            "approve":   "approved",
            "concerns":  "uncertain",
            "reject":    "flagged",
        }.get(verdict, "uncertain")

        review_summary = (previous_review or {}).get("summary", "")
        review_issues  = (previous_review or {}).get("issues", [])

        if confidence in ("flagged", "uncertain"):
            confidence_note = (
                f"⚠  Ollama's local reviewer flagged this draft "
                f"({verdict.upper()}: {review_summary})\n"
                f"   Treat it as a rough starting point ONLY — rewrite it properly.\n"
            )
            if review_issues:
                confidence_note += "   Issues found:\n"
                for issue in review_issues:
                    confidence_note += f"     - {issue}\n"
        else:
            confidence_note = (
                f"ℹ  Ollama's local reviewer: {verdict.upper()} — {review_summary}\n"
            )

        draft_section = (
            f"\n════════════════════════════════════════\n"
            f"OLLAMA DRAFT (rough starting point — improve and fix this):\n"
            f"{confidence_note}\n"
            f"{previous_draft}\n"
        )

    # ── Previous attempt context (item 9) ─────────────────────────────────────
    previous_attempt_section = ""
    if is_retry and previous_draft:
        reason_text = extra_instructions or "(no specific reason given)"
        previous_attempt_section = (
            f"\n════════════════════════════════════════\n"
            f"PREVIOUS ATTEMPT — DO NOT REPEAT THESE MISTAKES:\n"
            f"Reason for rejection / what to fix:\n  {reason_text}\n"
            f"\nThe rejected diff was:\n{previous_draft}\n"
        )
    elif extra_instructions and not previous_draft:
        # Retry instructions without a prior diff (user typed manual instructions)
        previous_attempt_section = (
            f"\n════════════════════════════════════════\n"
            f"EXTRA INSTRUCTIONS FROM PREVIOUS ATTEMPT:\n{extra_instructions}\n"
        )

    # ── Assemble the full prompt ──────────────────────────────────────────────
    return f"""{instructions}

════════════════════════════════════════
TECH STACK: {stack}
════════════════════════════════════════
PROJECT STRUCTURE:
{file_tree}
════════════════════════════════════════
GIT STATUS:
{git_status}
{prev_summary_section}
════════════════════════════════════════
STEP {step['id']}: {step['title']}
DESCRIPTION:
{step['description']}
{criteria_section}{draft_section}{previous_attempt_section}
════════════════════════════════════════
CURRENT FILE CONTENTS:
{files_block}
{OUTPUT_FORMAT}

Produce the unified diff patch now."""


def _collect_patch_from_user(
    step: dict,
    plan: dict | None = None,
    extra_instructions: str = "",
    previous_draft: str = "",
    previous_review: dict | None = None,
    open_browser: bool = True,
) -> str:
    """Generate prompt, show it, wait for the user to paste the diff back."""
    prompt = _build_patch_prompt(
        step,
        plan=plan,
        extra_instructions=extra_instructions,
        previous_draft=previous_draft,
        previous_review=previous_review,
    )

    step_id = step["id"]
    prompt_filename = f"patch_prompt_step_{step_id}.txt"
    prompt_file = save_prompt(prompt_filename, prompt)
    print(f"\nPatch prompt saved to: {yellow(prompt_file)}")

    open_prompt_in_browser(prompt, title=f"ai-build: Patch Prompt — Step {step_id}", enabled=open_browser)
    print_prompt_block(
        prompt,
        label=f"COPY THIS PROMPT FOR STEP {step_id} AND PASTE INTO CLAUDE",
    )

    print(bold("─" * 60))
    print(bold("ACTION REQUIRED:"))
    print("  1. Copy the prompt above (or from the browser tab).")
    print("  2. Paste it into Claude at https://claude.ai")
    print("  3. Claude will reply with a unified diff patch.")
    print("  4. Copy Claude's entire response.")
    print("  5. Paste it back here below.")
    print(bold("─" * 60))
    print()

    raw = paste_multiline(
        prompt="Paste Claude's diff response here, then press Enter twice when done:"
    )

    return raw.strip() if raw else ""


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if Claude wrapped the diff in them."""
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        return "\n".join(lines[1:end]).strip()
    return text


def _looks_like_diff(text: str) -> bool:
    """Return True if the text contains a proper unified diff header sequence."""
    # Require a genuine --- a/ ... +++ b/ (or /dev/null) header pair,
    # not just --- / +++ appearing inside string literals or prose.
    import re
    return bool(re.search(
        r'^--- (?:a/|/dev/null).+\n\+\+\+ (?:b/|/dev/null)',
        text, re.MULTILINE
    ))


def run_all_steps(resume: bool = False):
    plan = load_plan()
    if not plan:
        print("No plan found. Run: python ai_build.py plan \"your goal\"")
        return

    steps = plan["steps"]
    print_section("RUNNING STEPS")
    print(f"Goal: {bold(plan['goal'])}")
    print(f"Total steps: {len(steps)}\n")

    for step in steps:
        status = step.get("status", "pending")

        if resume and status == "applied":
            print(f"  Step {step['id']}: {step['title']} — already applied, skipping.")
            continue

        if status == "skipped":
            print(f"  Step {step['id']}: {step['title']} — was skipped, skipping again.")
            continue

        print(f"\n{bold('=' * 60)}")
        print(bold(f"  STEP {step['id']} of {len(steps)}: {step['title']}"))
        print(bold("=" * 60))
        print(f"Description: {step['description']}")
        files = step.get("suggested_files", [])
        if files:
            print(f"Files:       {', '.join(files)}")
        print()

        extra_instructions = ""
        attempt = 0
        current_patch: str = ""
        current_review: dict | None = None

        while True:
            # Get the diff from the user (via Claude).
            # On retry, pass the previous draft + review so Claude sees what failed.
            raw_patch = _collect_patch_from_user(
                step,
                plan=plan,
                extra_instructions=extra_instructions,
                previous_draft=current_patch if attempt > 0 else "",
                previous_review=current_review if attempt > 0 else None,
                open_browser=(attempt == 0),
            )
            attempt += 1

            if not raw_patch:
                print(yellow("Nothing was pasted. What would you like to do?"))
                try:
                    choice = input("  (S)kip this step  /  (T)ry again: ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    choice = "S"
                if choice in ("S", "SKIP"):
                    update_step_status(step["id"], "skipped")
                    print(f"Skipped step {step['id']}.")
                    break
                else:
                    continue

            # Strip code fences if present
            patch = _strip_code_fences(raw_patch)

            if not _looks_like_diff(patch):
                print(red("\nThe pasted text doesn't look like a unified diff."))
                print("A valid diff must contain lines starting with ---, +++, and @@.")
                print("Make sure you copied Claude's full response.")
                try:
                    choice = input("  (T)ry again  /  (S)kip this step: ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    choice = "S"
                if choice in ("S", "SKIP"):
                    update_step_status(step["id"], "skipped")
                    print(f"Skipped step {step['id']}.")
                    break
                else:
                    continue

            patch_file = save_patch(step["id"], patch)
            print(f"\n{green('Diff received.')} Saved to: {patch_file}")

            print("\nSending patch to Ollama for review...")
            review = review_patch(patch, step["description"])
            current_patch = patch
            current_review = review

            from ai_build.reviewer import format_review_text
            action = show_step_and_ask(step, patch, format_review_text(review))

            if action == "apply":
                success, msg = apply_patch(patch_file)
                if success:
                    print(f"\n{green('Patch applied successfully.')}")
                    update_step_status(step["id"], "applied")
                    if _is_git_repo():
                        ok, commit_msg = commit_step(step["id"], step["title"])
                        if ok:
                            print(green(f"Committed: {commit_msg.splitlines()[0]}"))
                        else:
                            print(yellow(f"Auto-commit skipped: {commit_msg}"))
                else:
                    print(f"\n{red('Failed to apply patch:')} {msg}")
                    print("You can retry or skip this step.")
                    try:
                        retry = input("Retry? (y/n): ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        retry = "n"
                    if retry == "y":
                        try:
                            extra_instructions = input(
                                "Extra instructions for the retry (or press Enter to skip): "
                            ).strip()
                        except (EOFError, KeyboardInterrupt):
                            extra_instructions = ""
                        continue
                    else:
                        update_step_status(step["id"], "failed")
                break

            elif action == "skip":
                print(f"\nSkipping step {step['id']}.")
                update_step_status(step["id"], "skipped")
                break

            elif action == "retry":
                extra_instructions = input(
                    "Enter instructions for the retry (what to fix or change): "
                ).strip()
                print()
                continue

    print("\n" + bold("=" * 60))
    print(bold("  RUN COMPLETE"))
    print(bold("=" * 60))
    print("\nFinal status:")
    plan = load_plan()
    for step in plan["steps"]:
        icon = {"applied": "✓", "skipped": "-", "failed": "✗", "pending": "?"}.get(
            step.get("status", "pending"), "?"
        )
        colour = green if icon == "✓" else (red if icon == "✗" else yellow)
        print(colour(f"  [{icon}] Step {step['id']}: {step['title']}"))
    print()
