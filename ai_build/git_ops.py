"""
git_ops.py - Apply patches safely using git or a pure-Python fallback.
"""

import subprocess
import os
import re


def _is_git_repo(root: str = ".") -> bool:
    """Check if *root* is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    return result.returncode == 0


def apply_patch(patch_file: str, root: str = ".") -> tuple[bool, str]:
    """
    Apply a unified diff patch.

    Tries `git apply` first (when inside a git repo).  Falls back to a
    pure-Python unified-diff applier so it also works on Windows without
    GNU patch installed.

    Returns:
        (success: bool, message: str)
    """
    if not os.path.exists(patch_file):
        return False, f"Patch file not found: {patch_file}"

    if _is_git_repo(root):
        # Dry-run first
        dry = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", patch_file],
            capture_output=True, text=True, cwd=root,
        )
        if dry.returncode != 0:
            # git apply is strict about context lines matching exactly.
            # Fall back to the Python patcher which is more tolerant —
            # it trusts @@ line numbers and ignores context-line mismatches.
            print("  git apply --check failed; trying Python patch fallback.")
            return _apply_patch_python(patch_file, root)

        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_file],
            capture_output=True, text=True, cwd=root,
        )
        if result.returncode == 0:
            return True, "Patch applied successfully."
        err = result.stderr.strip() or result.stdout.strip()
        # git apply passed the dry-run but failed for real — rare, but fall back.
        print(f"  git apply failed after passing check; trying Python fallback: {err}")
        return _apply_patch_python(patch_file, root)

    # ── Pure-Python fallback (works on Windows without GNU patch) ──────────
    print("  Warning: Not in a git repository. Using Python patch fallback.")
    return _apply_patch_python(patch_file, root)


def _apply_patch_python(patch_file: str, root: str = ".") -> tuple[bool, str]:
    """
    Pure-Python unified diff applier.  Handles standard `--- a/` / `+++ b/`
    prefixes and strips one path component (equivalent to `patch -p1`).
    """
    try:
        with open(patch_file, encoding="utf-8", errors="replace") as fh:
            patch_text = fh.read()
    except OSError as exc:
        return False, f"Cannot read patch file: {exc}"

    # Split into per-file blocks
    file_blocks = re.split(r"(?=^diff --git |\A(?=--- ))", patch_text, flags=re.M)

    applied = []
    errors = []

    for block in file_blocks:
        block = block.strip()
        if not block:
            continue

        # Locate +++ header to get the target file path
        m = re.search(r"^\+\+\+ (?:b/)?(.+)$", block, re.M)
        if not m:
            continue
        rel_path = m.group(1).strip()
        if rel_path == "/dev/null":
            continue  # deletion — skip for now

        target = os.path.normpath(os.path.join(root, rel_path))

        # Read existing file (may not exist for new files)
        if os.path.exists(target):
            with open(target, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        else:
            lines = []

        # Parse and apply hunks
        hunks = list(re.finditer(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n(.*?)(?=^@@|\Z)",
            block, re.M | re.S,
        ))
        if not hunks:
            continue

        new_lines = []
        src_pos = 0  # 0-based index into `lines`

        try:
            for hunk in hunks:
                src_start = int(hunk.group(1)) - 1  # convert to 0-based
                hunk_lines = hunk.group(5).splitlines(keepends=True)

                # Copy unchanged lines before this hunk
                new_lines.extend(lines[src_pos:src_start])
                src_pos = src_start

                for hl in hunk_lines:
                    if hl.startswith("+"):
                        new_lines.append(hl[1:])
                    elif hl.startswith("-"):
                        src_pos += 1  # consume the original line
                    else:
                        # context line — copy from original
                        new_lines.append(lines[src_pos])
                        src_pos += 1

            # Copy any remaining lines after the last hunk
            new_lines.extend(lines[src_pos:])
        except IndexError as exc:
            errors.append(f"{rel_path}: hunk mismatch — {exc}")
            continue

        # Write result
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)
        applied.append(rel_path)

    if errors:
        return False, "Patch errors:\n" + "\n".join(errors)
    if not applied:
        return False, "No files were changed — patch may be empty or already applied."
    return True, f"Patch applied to: {', '.join(applied)}"


def is_repo_clean(root: str = ".") -> bool:
    """Return True if *root*'s working tree has no uncommitted changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def commit_step(step_id: int, title: str, suggested_files: list[str] | None = None, root: str = ".") -> tuple[bool, str]:
    """
    Stage the files modified by this step and commit them.

    Staging strategy (in order):
      1. `git add -u` — stages all tracked files that were modified (safe, no secrets).
      2. If `suggested_files` contains paths that are untracked (new files created
         by the patch), stage those specific paths only.
      Never does a blanket `git add .` to avoid accidentally committing secrets,
      build artefacts, or editor temp files.

    Returns (success, message).
    """
    # Stage tracked modified files
    stage = subprocess.run(
        ["git", "add", "-u"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if stage.returncode != 0:
        return False, stage.stderr.strip() or stage.stdout.strip()

    # Stage only the specific new files the patch was supposed to create
    if suggested_files:
        for fpath in suggested_files:
            full = os.path.join(root, fpath) if not os.path.isabs(fpath) else fpath
            if os.path.exists(full):
                subprocess.run(["git", "add", fpath], capture_output=True, text=True, cwd=root)

    commit_msg = f"ai-build step {step_id}: {title}"
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip() or result.stdout.strip()


def get_git_diff(files: list[str] | None = None, root: str = ".") -> str:
    """Return the current git diff for the given files, or all files."""
    cmd = ["git", "diff"]
    if files:
        cmd += ["--"] + files
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    return result.stdout
