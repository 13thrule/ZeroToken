"""
storage.py - Save/load plan.json and step state. Scan the repo file tree.
            Also saves generated prompts to .ai-build/prompts/ for reference.
"""

import os
import json
import pathlib

AI_BUILD_DIR = ".ai-build"
PLAN_FILE = os.path.join(AI_BUILD_DIR, "plan.json")
PATCHES_DIR = os.path.join(AI_BUILD_DIR, "patches")
PROMPTS_DIR = os.path.join(AI_BUILD_DIR, "prompts")

# Directories and file patterns to ignore when scanning the repo
IGNORE_DIRS = {
    ".git", ".ai-build", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".eggs", ".claude",
}

# Directory name prefixes to ignore
IGNORE_DIR_PREFIXES = ("tmpclaude",)

IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".bin",
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov", ".zip", ".tar", ".gz",
    ".lock", ".log",
}

# Exact filenames to ignore (no extension matching)
IGNORE_FILENAMES = {
    ".env", ".env.example", ".env.local", ".env.production",
    ".DS_Store", "Thumbs.db",
}

# Filename prefixes to ignore
IGNORE_FILENAME_PREFIXES = ("tmpclaude",)


def ensure_dirs():
    os.makedirs(AI_BUILD_DIR, exist_ok=True)
    os.makedirs(PATCHES_DIR, exist_ok=True)
    os.makedirs(PROMPTS_DIR, exist_ok=True)


def save_plan(plan: dict, quiet: bool = False):
    ensure_dirs()
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    if not quiet:
        print(f"Plan saved to {PLAN_FILE}")


def load_plan() -> dict | None:
    if not os.path.exists(PLAN_FILE):
        return None
    with open(PLAN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def update_step_status(step_id: int, status: str):
    plan = load_plan()
    if not plan:
        return
    for step in plan["steps"]:
        if step["id"] == step_id:
            step["status"] = status
            break
    save_plan(plan, quiet=True)


def patch_path(step_id: int) -> str:
    ensure_dirs()
    return os.path.join(PATCHES_DIR, f"step-{step_id}.diff")


def save_patch(step_id: int, patch_content: str) -> str:
    ensure_dirs()
    path = patch_path(step_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(patch_content)
    return path


def load_patch(step_id: int) -> str | None:
    path = patch_path(step_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_prompt(filename: str, content: str) -> str:
    """
    Save a generated prompt to .ai-build/prompts/<filename> so the user
    can always find the full prompt text even after closing the terminal.
    Returns the saved file path.
    """
    ensure_dirs()
    path = os.path.join(PROMPTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_ollama_prompt(step_id: int) -> str | None:
    """Load the Ollama prompt that was used to generate the patch for *step_id*."""
    path = os.path.join(PROMPTS_DIR, f"ollama_patch_step_{step_id}.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Refined patches  (.ai-build/patches/step-X-refined.diff)
# ---------------------------------------------------------------------------

def save_refined_patch(step_id: int, content: str) -> str:
    """Save the Ollama-Refiner output for *step_id*."""
    ensure_dirs()
    path = os.path.join(PATCHES_DIR, f"step-{step_id}-refined.diff")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_refined_patch(step_id: int) -> str | None:
    """Load the refined diff for *step_id*, or None if not yet refined."""
    path = os.path.join(PATCHES_DIR, f"step-{step_id}-refined.diff")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Final agent prompt  (.ai-build/prompts/final_prompt.txt)
# ---------------------------------------------------------------------------

def save_final_prompt(content: str) -> str:
    """Save the assembled final agent prompt (the one you paste into Claude)."""
    ensure_dirs()
    path = os.path.join(PROMPTS_DIR, "final_prompt.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_final_prompt() -> str | None:
    """Load the assembled final agent prompt from disk, or None if not yet built."""
    path = os.path.join(PROMPTS_DIR, "final_prompt.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_repo_file_tree(root: str = ".") -> str:
    """Return a text representation of the repository file tree."""
    lines = []
    root_path = pathlib.Path(root).resolve()

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune ignored directories in-place so os.walk doesn't descend into them
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in IGNORE_DIRS
            and not d.endswith(".egg-info")
            and not d.startswith(IGNORE_DIR_PREFIXES)
        ]

        rel_dir = pathlib.Path(dirpath).relative_to(root_path)
        depth = len(rel_dir.parts)
        indent = "  " * depth
        folder_name = rel_dir.name if rel_dir.name else "."
        lines.append(f"{indent}{folder_name}/")

        sub_indent = "  " * (depth + 1)
        for filename in sorted(filenames):
            if filename in IGNORE_FILENAMES:
                continue
            if filename.startswith(IGNORE_FILENAME_PREFIXES):
                continue
            ext = pathlib.Path(filename).suffix.lower()
            if ext not in IGNORE_EXTENSIONS:
                lines.append(f"{sub_indent}{filename}")

    return "\n".join(lines)


def read_files(file_paths: list[str]) -> dict[str, str]:
    """Read the contents of the given files. Returns {path: content}."""
    contents = {}
    for path in file_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    contents[path] = f.read()
            except Exception as e:
                contents[path] = f"(could not read file: {e})"
        else:
            contents[path] = "(file does not exist yet - create it)"
    return contents


# Key files whose contents are included verbatim (truncated) in the manifest
_MANIFEST_KEY_FILES = [
    "README.md", "README.rst", "README.txt",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
]
_MANIFEST_FILE_MAX_CHARS = 2000


def build_project_manifest(root: str = ".") -> str:
    """
    Build a compact project context string for the patch reviewer.
    Includes: project name/root, file tree, key file contents, plan summary.
    """
    root_path = pathlib.Path(root).resolve()
    project_name = root_path.name
    sections: list[str] = []

    # Header
    sections.append(f"PROJECT: {project_name}")
    sections.append(f"ROOT:    {root_path}")

    # File tree
    sections.append("\nFILE TREE:\n" + get_repo_file_tree(root))

    # Key file contents (capped)
    key_blocks: list[str] = []
    for fname in _MANIFEST_KEY_FILES:
        fpath = root_path / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").rstrip()
                if len(content) > _MANIFEST_FILE_MAX_CHARS:
                    content = content[:_MANIFEST_FILE_MAX_CHARS] + "\n...(truncated)"
                key_blocks.append(f"--- {fname} ---\n{content}")
            except Exception:
                pass
    if key_blocks:
        sections.append("\nKEY FILES:\n" + "\n\n".join(key_blocks))

    # Plan summary
    plan = load_plan()
    if plan:
        goal = plan.get("goal", "(no goal set)")
        steps = plan.get("steps", [])
        STATUS_ICON = {"applied": "✓", "skipped": "~", "pending": "·", "in_progress": "►"}
        step_lines = [
            f"  {STATUS_ICON.get(s.get('status', 'pending'), '·')} "
            f"Step {s['id']}: {s['title']} [{s.get('status', 'pending')}]"
            for s in steps
        ]
        sections.append(f"\nPROJECT GOAL: {goal}")
        sections.append("IMPLEMENTATION PLAN:\n" + "\n".join(step_lines))

    return "\n".join(sections)
