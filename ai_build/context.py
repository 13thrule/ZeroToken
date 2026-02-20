"""
context.py - Project context detection for richer Claude prompts.

Provides three functions used by planner.py and executor.py:
  detect_stack(root)     → "Python 3, Flask, SQLAlchemy"
  build_file_tree(root)  → clean directory listing string
  get_git_status(root)   → git status + diff --stat summary
"""

import os
import re
import json
import subprocess
import pathlib

from ai_build.storage import get_repo_file_tree, IGNORE_DIRS, IGNORE_EXTENSIONS


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------

def detect_stack(project_root: str = ".") -> str:
    """
    Scan the project and return a human-readable description of the tech stack.

    Checks: requirements.txt, pyproject.toml, setup.py, package.json,
            composer.json, Gemfile, *.csproj, and dominant file extensions.

    Examples:
        "Python 3, Flask, SQLAlchemy, pytest"
        "Node.js, React, TypeScript"
        "PHP, Laravel"
        "primarily .rs, .toml files"
    """
    root = pathlib.Path(project_root).resolve()
    tags: list[str] = []

    # ── Python ───────────────────────────────────────────────────────────────
    req_file  = root / "requirements.txt"
    pyproject = root / "pyproject.toml"
    setup_py  = root / "setup.py"
    has_python = (
        req_file.exists() or pyproject.exists() or setup_py.exists()
        or bool(list(root.glob("*.py")))
    )

    if has_python:
        tags.append("Python 3")
        deps: set[str] = set()

        if req_file.exists():
            for line in req_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                dep = re.split(r"[>=<!;\[\s]", line)[0].strip().lower()
                if dep:
                    deps.add(dep)

        if pyproject.exists():
            for m in re.finditer(
                r'"([a-zA-Z][a-zA-Z0-9_-]+)\s*[>=<!]',
                pyproject.read_text(errors="replace"),
            ):
                deps.add(m.group(1).lower())

        FRAMEWORK_MAP = {
            "django":        "Django",
            "flask":         "Flask",
            "fastapi":       "FastAPI",
            "tornado":       "Tornado",
            "starlette":     "Starlette",
            "aiohttp":       "aiohttp",
            "pygame":        "pygame",
            "pyside6":       "PySide6",
            "pyside2":       "PySide2",
            "pyqt6":         "PyQt6",
            "pyqt5":         "PyQt5",
            "kivy":          "Kivy",
            "sqlalchemy":    "SQLAlchemy",
            "peewee":        "Peewee",
            "celery":        "Celery",
            "dramatiq":      "Dramatiq",
            "numpy":         "numpy",
            "pandas":        "pandas",
            "torch":         "PyTorch",
            "tensorflow":    "TensorFlow",
            "scikit-learn":  "scikit-learn",
            "scipy":         "scipy",
            "pytest":        "pytest",
            "click":         "Click",
            "typer":         "Typer",
            "pydantic":      "Pydantic",
        }
        found_frameworks = [label for key, label in FRAMEWORK_MAP.items() if key in deps]
        tags.extend(found_frameworks)

        # If no recognised frameworks, list up to 6 deps
        noise = {"pip", "wheel", "setuptools", "pkg-resources"}
        other_deps = [d for d in sorted(deps) if d not in FRAMEWORK_MAP and d not in noise]
        if other_deps and not found_frameworks:
            tags.append(f"packages: {', '.join(other_deps[:6])}")

    # ── JavaScript / Node.js ─────────────────────────────────────────────────
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(errors="replace"))
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            tags.append("Node.js")
            JS_FW = {
                "react":      "React",
                "vue":        "Vue",
                "angular":    "Angular",
                "next":       "Next.js",
                "nuxt":       "Nuxt",
                "svelte":     "Svelte",
                "express":    "Express",
                "fastify":    "Fastify",
                "typescript": "TypeScript",
            }
            tags.extend(v for k, v in JS_FW.items() if k in all_deps)
        except Exception:
            tags.append("JavaScript/Node.js")

    # ── PHP ──────────────────────────────────────────────────────────────────
    composer = root / "composer.json"
    if composer.exists():
        tags.append("PHP")
        try:
            c = json.loads(composer.read_text(errors="replace"))
            all_php = {**c.get("require", {}), **c.get("require-dev", {})}
            if "laravel/framework" in all_php:
                tags.append("Laravel")
            elif any(k.startswith("symfony/") for k in all_php):
                tags.append("Symfony")
        except Exception:
            pass

    # ── Ruby ─────────────────────────────────────────────────────────────────
    gemfile = root / "Gemfile"
    if gemfile.exists():
        tags.append("Ruby")
        gf_text = gemfile.read_text(errors="replace").lower()
        if "rails" in gf_text:
            tags.append("Rails")
        elif "sinatra" in gf_text:
            tags.append("Sinatra")

    # ── C# / .NET ────────────────────────────────────────────────────────────
    if list(root.glob("*.csproj")) or list(root.glob("**/*.csproj")):
        tags.append("C#/.NET")

    # ── Fallback: dominant extensions ─────────────────────────────────────────
    if not tags:
        ext_count: dict[str, int] = {}
        try:
            for f in root.rglob("*"):
                if f.is_file() and not any(p in IGNORE_DIRS for p in f.parts):
                    ext = f.suffix.lower()
                    if ext and ext not in IGNORE_EXTENSIONS:
                        ext_count[ext] = ext_count.get(ext, 0) + 1
        except Exception:
            pass
        if ext_count:
            top = sorted(ext_count, key=lambda x: -ext_count[x])[:3]
            tags.append(f"primarily {', '.join(top)} files")

    return ", ".join(tags) if tags else "unknown stack"


# ---------------------------------------------------------------------------
# File tree
# ---------------------------------------------------------------------------

def build_file_tree(project_root: str = ".") -> str:
    """
    Return a clean directory listing string, excluding noise directories.
    Delegates to storage.get_repo_file_tree which already applies all filters.
    """
    return get_repo_file_tree(project_root)


# ---------------------------------------------------------------------------
# Git status
# ---------------------------------------------------------------------------

def get_git_status(project_root: str = ".") -> str:
    """
    Run `git status --short` and `git diff --stat`, return a concise summary
    string suitable for inclusion in a Claude prompt.

    Returns a safe fallback string if git is unavailable or not a repo.
    """
    try:
        status_proc = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=10, cwd=project_root,
        )
        diff_proc = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, timeout=10, cwd=project_root,
        )

        parts: list[str] = []

        if status_proc.returncode == 0:
            out = status_proc.stdout.strip()
            parts.append(f"Working tree:\n{out}" if out else "Working tree: clean (no uncommitted changes)")
        else:
            return "(not a git repository)"

        if diff_proc.returncode == 0 and diff_proc.stdout.strip():
            parts.append(f"Unstaged diff stats:\n{diff_proc.stdout.strip()}")

        return "\n".join(parts)

    except FileNotFoundError:
        return "(git not found in PATH — install git or ignore this section)"
    except Exception as exc:
        return f"(git status error: {exc})"
