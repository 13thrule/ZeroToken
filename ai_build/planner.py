"""
planner.py - Generates a planning prompt for you to paste into Claude manually,
then waits for you to paste Claude's JSON response back into the terminal.
No API keys required.
"""

import json
from ai_build.storage import save_plan, get_repo_file_tree
from ai_build.context import detect_stack
from ai_build.ui import (
    print_prompt_block,
    open_prompt_in_browser,
    paste_multiline,
    print_section,
    bold,
    green,
    yellow,
)

# The instruction block that tells Claude exactly what format to return.
PLAN_INSTRUCTIONS = """You are a senior software engineer helping to plan code changes.
You will receive a goal, the project's tech stack, and a file tree of the project.
Your job is to break the goal into clear, ordered implementation steps.

Respond ONLY with valid JSON in this exact format (no explanation, no markdown fences):
{
  "steps": [
    {
      "id": 1,
      "title": "Short title of the step",
      "description": "Detailed description of what needs to be done in this step",
      "suggested_files": ["path/to/file1.py", "path/to/file2.py"],
      "acceptance_criteria": [
        "Specific, testable outcome 1",
        "Specific, testable outcome 2"
      ]
    }
  ]
}

Rules:
- Each step must be small and focused on exactly one change.
- suggested_files lists files to be modified OR new files to be created.
- acceptance_criteria lists 1-4 concrete, testable outcomes for the step.
- Steps must be in the correct order to avoid dependency problems.
- Do NOT include any text outside the JSON object.
- Do NOT wrap the JSON in markdown code fences."""


def _build_planning_prompt(goal: str, file_tree: str, stack: str = "") -> str:
    """Assemble the full prompt to give to Claude."""
    return f"""{PLAN_INSTRUCTIONS}

========================================
GOAL:
{goal}
========================================
TECH STACK: {stack}
========================================
PROJECT STRUCTURE:
{file_tree}
========================================

Generate the step-by-step plan now."""


def generate_plan(goal: str):
    print_section("PLANNING")
    print(f"Goal: {bold(goal)}\n")
    print("Scanning repository...")

    file_tree = get_repo_file_tree()
    stack = detect_stack(".")
    print(f"Detected stack: {yellow(stack)}")
    prompt = _build_planning_prompt(goal, file_tree, stack)

    # Save the prompt to a file so the user can always find it again
    from ai_build.storage import save_prompt
    prompt_file = save_prompt("plan_prompt.txt", prompt)

    print(f"\nPlanning prompt generated and saved to: {yellow(prompt_file)}")
    print()

    # Show the prompt and offer to open it in a browser
    open_prompt_in_browser(prompt, title="ai-build: Planning Prompt")
    print_prompt_block(prompt, label="COPY THIS ENTIRE PROMPT AND PASTE IT INTO CLAUDE")

    print(bold("─" * 60))
    print(bold("ACTION REQUIRED:"))
    print("  1. Copy the prompt above (or from the browser tab that opened).")
    print("  2. Go to https://claude.ai and start a new conversation.")
    print("  3. Paste the prompt and send it.")
    print("  4. Claude will reply with a JSON plan.")
    print("  5. Copy Claude's entire response.")
    print("  6. Come back here and paste it below.")
    print(bold("─" * 60))
    print()

    raw = paste_multiline(
        prompt="Paste Claude's JSON response here, then press Enter twice when done:"
    )

    if not raw:
        print("Nothing was pasted. Aborting.")
        return

    # Strip BOM and markdown code fences if present
    stripped = raw.strip().lstrip('\ufeff')
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        stripped = "\n".join(lines[1:end])

    try:
        plan_data = json.loads(stripped)
    except json.JSONDecodeError as e:
        print(f"\n{yellow('ERROR')}: The pasted text is not valid JSON.")
        print(f"Detail: {e}")
        print("\nWhat you pasted:")
        print(stripped[:500] + ("..." if len(stripped) > 500 else ""))
        print("\nTip: Make sure you copied Claude's entire response and nothing extra.")
        return

    steps = plan_data.get("steps")
    if not steps or not isinstance(steps, list):
        print(f"\n{yellow('ERROR')}: The JSON has no 'steps' list.")
        print("Make sure Claude returned the JSON in the correct format.")
        return

    for step in steps:
        step["status"] = "pending"

    plan = {
        "goal": goal,
        "steps": steps,
    }

    save_plan(plan)

    print(f"\n{green('Plan saved!')} {len(steps)} steps:\n")
    for step in steps:
        files = ", ".join(step.get("suggested_files", []))
        step_id = step["id"]
        print(f"  {bold(f'Step {step_id}')}:  {step['title']}")
        print(f"           Files: {files or '(none)'}")
        criteria = step.get("acceptance_criteria", [])
        if criteria:
            extra = f" (+{len(criteria)-1} more)" if len(criteria) > 1 else ""
            print(f"           Criteria: {criteria[0]}{extra}")
    print()
    print(f"Run {bold('python ai_build.py run')} to begin execution.")
