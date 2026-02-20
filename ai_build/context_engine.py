"""
context_engine.py - Build a rich project context dict for local Ollama inference.

This gives Ollama everything it needs to plan and generate patches:
  - project name + root
  - file tree
  - key file contents (README, requirements, pyproject, etc.)
  - source file contents (prioritised by size, capped)
  - architecture detection (languages, frameworks)
  - current plan status
"""

import os
import pathlib

from ai_build.storage import (
    get_repo_file_tree,
    load_plan,
    IGNORE_DIRS,
    IGNORE_DIR_PREFIXES,
    IGNORE_EXTENSIONS,
    IGNORE_FILENAMES,
    IGNORE_FILENAME_PREFIXES,
)

# Key documentation / config files — always included verbatim (up to cap)
_KEY_FILES = [
    "README.md", "README.rst", "README.txt",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Makefile", "Dockerfile",
]
_KEY_FILE_MAX_CHARS = 2_000

# Source extensions whose file contents are included (up to _MAX_SOURCE_FILES)
_SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".rs", ".rb", ".java", ".cs", ".cpp", ".c", ".h",
    ".sh", ".bash", ".yaml", ".yml", ".toml", ".html", ".css",
}
_SOURCE_FILE_MAX_CHARS = 3_000
_MAX_SOURCE_FILES = 25    # cap total number — prevents blowing up context

# Framework fingerprints (checked in requirements + file paths)
_FRAMEWORK_PATTERNS = {
    "flask":    ["flask"],
    "fastapi":  ["fastapi"],
    "django":   ["django"],
    "react":    ["react"],
    "vue":      ["vue"],
    "nextjs":   ["next", "nextjs"],
    "htmx":     ["htmx"],
    "pytest":   ["pytest"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_context(root: str = ".", priority_files: list[str] | None = None) -> dict:
    """
    Build the full project context dict.

    `priority_files` — relative paths (from root) that should be included in
    source_files even if they are large; they are prepended before the
    smallest-first selection so they are never evicted by the budget trimmer.

    Returns a JSON-serialisable dict with:
      project_name, root, file_tree, files, architecture, key_files,
      source_files, plan.
    """
    root_path = pathlib.Path(root).resolve()

    # ── 1. Walk the file list ───────────────────────────────────────────────
    files: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in IGNORE_DIRS
            and not d.endswith(".egg-info")
            and not d.startswith(IGNORE_DIR_PREFIXES)
        ]
        for fname in sorted(filenames):
            if fname in IGNORE_FILENAMES:
                continue
            if fname.startswith(IGNORE_FILENAME_PREFIXES):
                continue
            ext = pathlib.Path(fname).suffix.lower()
            if ext in IGNORE_EXTENSIONS:
                continue
            fpath = pathlib.Path(dirpath) / fname
            rel = str(fpath.relative_to(root_path)).replace("\\", "/")
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            files.append({"path": rel, "type": ext.lstrip(".") or "unknown", "size": size})

    # ── 2. Key file contents ────────────────────────────────────────────────
    key_file_contents: dict[str, str] = {}
    for fname in _KEY_FILES:
        fpath = root_path / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").rstrip()
                if len(content) > _KEY_FILE_MAX_CHARS:
                    content = content[:_KEY_FILE_MAX_CHARS] + "\n...(truncated)"
                key_file_contents[fname] = content
            except Exception:
                pass

    # ── 3. Source file contents (priority files first, then smallest-first) ──
    _priority = {p.replace("\\", "/") for p in (priority_files or [])}
    source_candidates = [
        f for f in files if f["type"] in {e.lstrip(".") for e in _SOURCE_EXTENSIONS}
    ]
    # Split into priority (preserve order given) and the rest (smallest first)
    priority_entries = [f for f in source_candidates if f["path"] in _priority]
    rest_entries     = sorted(
        [f for f in source_candidates if f["path"] not in _priority],
        key=lambda f: f["size"],
    )
    selected = (priority_entries + rest_entries)[:_MAX_SOURCE_FILES]
    source_contents: dict[str, str] = {}
    for f in selected:
        fpath = root_path / f["path"]
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace").rstrip()
            if len(content) > _SOURCE_FILE_MAX_CHARS:
                content = content[:_SOURCE_FILE_MAX_CHARS] + "\n...(truncated)"
            source_contents[f["path"]] = content
        except Exception:
            pass

    # ── 4. Architecture detection ───────────────────────────────────────────
    all_exts = {pathlib.Path(f["path"]).suffix.lower() for f in files}
    languages: list[str] = []
    if ".py" in all_exts:
        languages.append("python")
    if ".js" in all_exts or ".ts" in all_exts:
        languages.append("javascript/typescript")
    if ".go" in all_exts:
        languages.append("go")
    if ".rs" in all_exts:
        languages.append("rust")
    if ".rb" in all_exts:
        languages.append("ruby")
    if ".java" in all_exts:
        languages.append("java")

    haystack = (
        " ".join(key_file_contents.values()).lower()
        + " ".join(f["path"] for f in files).lower()
    )
    frameworks: list[str] = [
        name for name, patterns in _FRAMEWORK_PATTERNS.items()
        if any(p in haystack for p in patterns)
    ]

    # entrypoints — common names
    _EP_NAMES = {"main.py", "app.py", "server.py", "manage.py", "index.py",
                 "__main__.py", "cli.py", "run.py"}
    entrypoints = [f["path"] for f in files if pathlib.Path(f["path"]).name in _EP_NAMES]

    # ── 5. Architecture summary + conventions (auto-detected) ─────────────
    # Style conventions: infer from file tree + key file content
    naming_convention = "snake_case"  # Python default
    if "typescript" in " ".join(languages) or "react" in frameworks:
        naming_convention = "camelCase / PascalCase"
    elif "java" in languages or "cs" in languages:
        naming_convention = "PascalCase / camelCase"

    # Folder structure inference
    top_dirs = sorted({f["path"].split("/")[0] for f in files if "/" in f["path"]})
    folder_desc = ", ".join(top_dirs[:8]) if top_dirs else "flat"

    arch_parts: list[str] = []
    if languages:
        arch_parts.append(f"{', '.join(languages)} project")
    if frameworks:
        arch_parts.append(f"using {', '.join(frameworks)}")
    if entrypoints:
        arch_parts.append(f"entry via {', '.join(entrypoints)}")
    architecture_summary = "; ".join(arch_parts) if arch_parts else "unknown"

    conventions = {
        "naming":           naming_convention,
        "folder_structure": folder_desc,
        "style":            "pep8" if "python" in languages else "standard",
    }

    # ── 6. Plan summary ─────────────────────────────────────────────────────
    plan = load_plan()
    plan_summary: dict | None = None
    if plan:
        _ICON = {"applied": "✓", "skipped": "~", "pending": "·",
                 "in_progress": "►", "failed": "✗"}
        plan_summary = {
            "goal": plan.get("goal", ""),
            "steps": [
                {
                    "id": s["id"],
                    "title": s["title"],
                    "status": s.get("status", "pending"),
                    "icon": _ICON.get(s.get("status", "pending"), "·"),
                }
                for s in plan.get("steps", [])
            ],
        }

    return {
        "project_name":       root_path.name,
        "root":               str(root_path),
        "file_tree":          get_repo_file_tree(root),
        "files":              files,
        "architecture": {
            "languages":    languages,
            "frameworks":   frameworks,
            "entrypoints":  entrypoints,
        },
        "architecture_summary": architecture_summary,
        "conventions":     conventions,
        "key_files":       key_file_contents,
        "source_files":    source_contents,
        "plan":            plan_summary,
    }


def context_to_text(ctx: dict, max_chars: int = 16_000) -> str:
    """
    Render the context dict as a compact human-readable text block suitable
    for embedding in an Ollama prompt.

    `max_chars` caps the total output length; source file contents are trimmed
    first (smallest wins) to stay within budget.

    Default is 16 000 chars (~4 000 tokens) leaving ~4 000 tokens for the
    prompt template and model response in an 8 192-token context window.
    (Previously 28 000 — too tight for gemma3:4b; prompted truncation.)
    """
    parts: list[str] = []

    parts.append(f"PROJECT : {ctx['project_name']}")
    parts.append(f"ROOT    : {ctx['root']}")

    arch = ctx.get("architecture", {})
    if arch.get("languages"):
        parts.append(f"LANG    : {', '.join(arch['languages'])}")
    if arch.get("frameworks"):
        parts.append(f"STACK   : {', '.join(arch['frameworks'])}")
    if arch.get("entrypoints"):
        parts.append(f"ENTRY   : {', '.join(arch['entrypoints'])}")
    if ctx.get("architecture_summary"):
        parts.append(f"SUMMARY : {ctx['architecture_summary']}")

    conv = ctx.get("conventions", {})
    if conv:
        parts.append(
            f"CONVENTIONS: naming={conv.get('naming','?')}, "
            f"style={conv.get('style','?')}, "
            f"folders={conv.get('folder_structure','?')}"
        )

    parts.append(f"\nFILE TREE:\n{ctx['file_tree']}")

    for fname, content in ctx.get("key_files", {}).items():
        parts.append(f"\n{'─'*6} {fname} {'─'*6}\n{content}")

    if ctx.get("plan"):
        plan = ctx["plan"]
        parts.append(f"\nPROJECT GOAL: {plan['goal']}")
        step_lines = [
            f"  {s['icon']} Step {s['id']}: {s['title']} [{s['status']}]"
            for s in plan["steps"]
        ]
        parts.append("PLAN:\n" + "\n".join(step_lines))

    base_text = "\n".join(parts)

    # Budget remaining after the skeleton
    remaining = max_chars - len(base_text)

    source_parts: list[str] = []
    for fpath, content in ctx.get("source_files", {}).items():
        block = f"\n{'═'*6} {fpath} {'═'*6}\n{content}"
        if remaining - len(block) < 200:
            # Not enough room — skip remaining source files
            source_parts.append(f"\n[{len(ctx['source_files']) - len(source_parts)} more source files omitted to stay within context budget]")
            break
        source_parts.append(block)
        remaining -= len(block)

    return base_text + "".join(source_parts)
