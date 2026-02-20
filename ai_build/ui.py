"""
ui.py - Terminal UI helpers: colour output, prompt display, paste input,
        and optional browser-based prompt viewer.
No API keys required.
"""

import os
import sys
import webbrowser

from ai_build.shutdown import ManagedTempFile


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _color(text: str, code: str) -> str:
    """Wrap text in ANSI colour codes when the terminal supports them."""
    if sys.stdout.isatty() or os.getenv("FORCE_COLOR"):
        return f"\033[{code}m{text}\033[0m"
    return text


def red(t: str) -> str:    return _color(t, "31")
def green(t: str) -> str:  return _color(t, "32")
def yellow(t: str) -> str: return _color(t, "33")
def cyan(t: str) -> str:   return _color(t, "36")
def bold(t: str) -> str:   return _color(t, "1")


# ---------------------------------------------------------------------------
# Section header
# ---------------------------------------------------------------------------

def print_section(title: str):
    """Print a visible section header."""
    bar = "=" * 60
    print(f"\n{bold(bar)}")
    print(bold(f"  {title}"))
    print(bold(bar))
    print()


# ---------------------------------------------------------------------------
# Prompt display
# ---------------------------------------------------------------------------

def print_prompt_block(prompt: str, label: str = "PROMPT"):
    """
    Print the prompt text inside a clearly marked border so the user
    knows exactly what to copy.
    """
    border = "▓" * 60
    print()
    print(bold(border))
    print(bold(f"  >>>  {label}  <<<"))
    print(bold(border))
    print()
    print(prompt)
    print()
    print(bold(border))
    print(bold(f"  END OF PROMPT — copy everything between the ▓ borders above"))
    print(bold(border))
    print()


def open_prompt_in_browser(prompt: str, title: str = "ai-build Prompt", enabled: bool = True):
    """
    Write the prompt to a temporary HTML file and open it in the default
    browser, so it is easy to copy even for long prompts.
    Falls back silently if the browser cannot be opened.
    Set enabled=False or export NO_BROWSER=1 to skip.
    """
    if not enabled or os.getenv("NO_BROWSER"):
        return
    try:
        html = _build_html(title, prompt)
        # ManagedTempFile registers the path with ShutdownManager so it is
        # always deleted on exit, even if the browser never loads it.
        with ManagedTempFile(mode="w", suffix=".html", encoding="utf-8") as f:
            f.write(html)
            temp_path = f.name

        opened = webbrowser.open(f"file://{temp_path}")
        if opened:
            print(f"Prompt also opened in your browser for easy copying.")
        else:
            print(f"(Could not open browser automatically. The prompt is printed above.)")
    except Exception:
        # Never crash just because the browser helper failed
        pass


def _build_html(title: str, prompt: str) -> str:
    """Wrap the prompt in a minimal HTML page with a Copy button."""
    # Escape HTML special characters
    escaped = (
        prompt
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <style>
    body {{
      font-family: monospace;
      background: #1e1e1e;
      color: #d4d4d4;
      margin: 0;
      padding: 20px;
    }}
    h1 {{
      font-size: 1rem;
      color: #9cdcfe;
      margin-bottom: 10px;
    }}
    #copy-btn {{
      display: inline-block;
      margin-bottom: 16px;
      padding: 8px 20px;
      background: #0e639c;
      color: #fff;
      border: none;
      border-radius: 4px;
      font-size: 0.95rem;
      cursor: pointer;
    }}
    #copy-btn:active {{ background: #1177bb; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #252526;
      padding: 16px;
      border-radius: 6px;
      border: 1px solid #333;
      font-size: 0.85rem;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <button id="copy-btn" onclick="copyPrompt()">Copy Entire Prompt</button>
  <pre id="prompt-text">{escaped}</pre>
  <script>
    function copyPrompt() {{
      var text = document.getElementById('prompt-text').innerText;
      navigator.clipboard.writeText(text).then(function() {{
        document.getElementById('copy-btn').textContent = 'Copied!';
        setTimeout(function() {{
          document.getElementById('copy-btn').textContent = 'Copy Entire Prompt';
        }}, 2000);
      }});
    }}
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Paste input
# ---------------------------------------------------------------------------

def paste_multiline(prompt: str = "Paste here, then press Enter twice when done:") -> str:
    """
    Collect a multi-line paste from the user.
    The user signals they are done by pressing Enter twice on a blank line.
    Returns the pasted text, or an empty string if nothing was entered.
    """
    print(yellow(prompt))
    print(cyan("(Press Enter twice on a blank line — two blank lines in a row — when done.)"))
    print()

    lines = []
    try:
        while True:
            line = input()
            # Two consecutive empty lines = done
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print("\nInput interrupted.")
        return ""

    # Remove the trailing blank line that triggered the end
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step review UI (diff + Ollama review + approve/skip/retry)
# ---------------------------------------------------------------------------

def _print_diff(patch: str):
    """Print a unified diff with syntax highlighting."""
    print(bold("\n--- DIFF ---"))
    lines = patch.splitlines()
    truncated = len(lines) > 200
    display_lines = lines[:200] if truncated else lines

    for line in display_lines:
        if line.startswith("+++") or line.startswith("---"):
            print(bold(line))
        elif line.startswith("+"):
            print(green(line))
        elif line.startswith("-"):
            print(red(line))
        elif line.startswith("@@"):
            print(cyan(line))
        else:
            print(line)

    if truncated:
        print(yellow(f"\n... (diff truncated — {len(lines) - 200} more lines not shown) ..."))
    print()


def _print_review(review: str):
    """Print the Ollama review with simple formatting."""
    print(bold("--- OLLAMA REVIEW ---"))
    for line in review.splitlines():
        if line.startswith("VERDICT:"):
            if "APPROVE" in line:
                print(green(line))
            elif "REJECT" in line:
                print(red(line))
            else:
                print(yellow(line))
        elif line.startswith("SUMMARY:"):
            print(bold(line))
        elif line.startswith("-"):
            print(f"  {line}")
        else:
            print(line)
    print()


def show_step_and_ask(step: dict, patch: str, review: str) -> str:
    """
    Display the step info, diff, and Ollama review, then ask what to do.
    Returns one of: "apply", "skip", "retry"
    """
    print()
    print(bold("=" * 60))
    print(bold(f"  STEP {step['id']}: {step['title']}"))
    print(bold("=" * 60))
    print(f"Description: {step['description']}")

    files = step.get("suggested_files", [])
    if files:
        print(f"Files:       {', '.join(files)}")

    _print_diff(patch)
    _print_review(review)

    print(bold("What would you like to do?"))
    print(f"  {green('[A]')} Apply this patch")
    print(f"  {yellow('[S]')} Skip this step")
    print(f"  {cyan('[R]')} Retry — go back to Claude with extra instructions")
    print()

    while True:
        try:
            choice = input("Your choice (A/S/R): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print("\nInterrupted. Exiting.")
            sys.exit(0)

        if choice in ("A", "APPLY"):
            return "apply"
        elif choice in ("S", "SKIP"):
            return "skip"
        elif choice in ("R", "RETRY"):
            return "retry"
        else:
            print("Please enter A, S, or R.")
