#!/usr/bin/env python3
"""
ai-build: A human-in-the-loop local AI development tool.
No API keys required. You copy prompts to Claude manually and paste responses back.
Local Ollama models review and refine each patch; Claude applies the final result.

Primary interface — web GUI (recommended):
    python ai_build.py gui                       # open http://127.0.0.1:5000

CLI commands (advanced / scripting):
    python ai_build.py plan "your goal here"    # scan repo, generate plan
    python ai_build.py run                       # execute every pending step
    python ai_build.py resume                    # skip already-done steps and continue
    python ai_build.py show-plan                 # pretty-print plan with statuses
    python ai_build.py reset [step_id]           # reset one step (or all) to pending
    python ai_build.py -h / --help / help        # show this help
"""

import sys


def cmd_plan(goal: str):
    from ai_build.planner import generate_plan
    generate_plan(goal)


def cmd_run():
    from ai_build.executor import run_all_steps
    run_all_steps(resume=False)


def cmd_resume():
    from ai_build.executor import run_all_steps
    run_all_steps(resume=True)


def cmd_show_plan():
    from ai_build.storage import load_plan
    from ai_build.ui import bold, green, red, yellow, cyan
    plan = load_plan()
    if not plan:
        print("No plan found. Run: python ai_build.py plan \"your goal\"")
        return
    print(f"\nGoal: {bold(plan['goal'])}\n")
    print(f"{'#':<4} {'Title':<40} {'Status':<12} Files")
    print("-" * 80)
    ICONS = {"applied": "✓", "skipped": "-", "failed": "✗", "pending": "·"}
    for step in plan["steps"]:
        files = ", ".join(step.get("suggested_files", []))
        status = step.get("status", "pending")
        icon = ICONS.get(status, "?")
        status_str = f"[{icon}] {status}"
        if status == "applied":
            coloured = green(f"{status_str:<14}")
        elif status == "failed":
            coloured = red(f"{status_str:<14}")
        elif status == "skipped":
            coloured = yellow(f"{status_str:<14}")
        else:
            coloured = cyan(f"{status_str:<14}")
        print(f"{step['id']:<4} {step['title']:<40} {coloured} {files}")
    print()


def cmd_gui(host: str = "127.0.0.1", port: int = 5000):
    try:
        from ai_build.server import run_server
    except ImportError:
        print("Flask is required for the GUI. Install it with:")
        print("  pip install flask")
        import sys; sys.exit(1)
    run_server(host=host, port=port)


def cmd_reset(step_id_str: str | None):
    from ai_build.storage import load_plan, save_plan
    plan = load_plan()
    if not plan:
        print("No plan found.")
        return
    if step_id_str is None:
        # Reset all steps
        for step in plan["steps"]:
            step["status"] = "pending"
        save_plan(plan, quiet=True)
        print(f"All {len(plan['steps'])} steps reset to pending.")
    else:
        try:
            target = int(step_id_str)
        except ValueError:
            print(f"Invalid step id: {step_id_str!r}. Must be an integer.")
            return
        matched = False
        for step in plan["steps"]:
            if step["id"] == target:
                old = step.get("status", "pending")
                step["status"] = "pending"
                matched = True
                print(f"Step {target} reset from '{old}' to 'pending'.")
        if not matched:
            print(f"No step with id {target} found.")
            return
        save_plan(plan, quiet=True)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1].lower()

    if command in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)

    elif command == "plan":
        if len(sys.argv) < 3:
            print("Usage: python ai_build.py plan \"your goal here\"")
            sys.exit(1)
        goal = " ".join(sys.argv[2:])
        cmd_plan(goal)

    elif command == "run":
        cmd_run()

    elif command == "resume":
        cmd_resume()

    elif command == "show-plan":
        cmd_show_plan()

    elif command == "gui":
        host = "127.0.0.1"
        port = 5000
        for arg in sys.argv[2:]:
            if arg.startswith("--port="):
                port = int(arg.split("=", 1)[1])
            elif arg.startswith("--host="):
                host = arg.split("=", 1)[1]
        cmd_gui(host=host, port=port)

    elif command == "reset":
        step_id = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_reset(step_id)

    else:
        print(f"Unknown command: {command!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
