"""
local_patcher.py - Use Ollama to generate a unified diff patch for a step locally.

Replaces the Claude → paste-diff workflow with a single local call:
  diff_text, err = generate_patch_local(step, root=".")
"""

import os
import pathlib
import re

# ---------------------------------------------------------------------------
# Patcher-specific context builder
# ---------------------------------------------------------------------------

# Maximum total chars for project context in the patch prompt.
# Budget math for gemma3:4b (8 192-token window, ~4 chars/token):
#   12 000 chars ≈ 3 000 tokens for context
#   ~1 500 chars ≈   375 tokens for prompt template + step description
#   Remaining  ≈ 4 817 tokens (~19 000 chars) for the diff response — plenty.
_PATCHER_CONTEXT_BUDGET = 12_000

def _patcher_context(step: dict, root: str, prior_diffs: dict | None = None) -> str:
    """
    Build a compact context string optimised for the patch generator.

    Key differences from the generic context_to_text:
    - Reads suggested_files (the files to be patched) at FULL length,
      so the model sees correct line numbers and can write valid @@ headers.
    - Does NOT include unrelated source files — they waste tokens and
      distract the model.
    - Includes the lightweight file tree for project orientation.
    - Includes prior approved diffs so each step knows exactly what
      previous steps already changed (prevents conflicting patches).
    """
    from ai_build.storage import get_repo_file_tree

    root_path = pathlib.Path(root).resolve()
    parts: list[str] = []
    remaining = _PATCHER_CONTEXT_BUDGET

    # 1. File tree (lightweight — gives the model project orientation)
    tree = get_repo_file_tree(root)
    tree_block = f"FILE TREE:\n{tree}"
    parts.append(tree_block)
    remaining -= len(tree_block)

    # 2. Prior approved diffs — BEFORE file contents so Ollama sees them first
    #    and understands what already exists before reading the files.
    if prior_diffs:
        prior_block_lines = ["\n" + "═" * 60]
        prior_block_lines.append("⚠ CRITICAL — THESE CHANGES ARE ALREADY IN THE CODEBASE ⚠")
        prior_block_lines.append("The files below already contain these changes. Do NOT recreate them.")
        prior_block_lines.append("Your diff must ADD TO or MODIFY what already exists — not replace it.")
        prior_block_lines.append("If a file was created in a prior step, it already exists. Do not use --- /dev/null for it.")
        prior_block_lines.append("═" * 60)
        for step_id, diff_text in sorted(prior_diffs.items()):
            diff_preview = diff_text[:800]
            if len(diff_text) > 800:
                diff_preview += "\n...(diff truncated)"
            prior_block_lines.append(f"\n--- Step {step_id} diff ---\n{diff_preview}")
        prior_block = "\n".join(prior_block_lines)
        if remaining > len(prior_block):
            parts.append(prior_block)
            remaining -= len(prior_block)

    # 3. Read each suggested file, extracting only the relevant section for
    #    large files so Ollama can write accurate @@ line numbers.
    for rel_path in step.get("suggested_files", []):
        if remaining < 500:
            parts.append("[context budget exhausted — remaining suggested files omitted]")
            break
        fpath = root_path / rel_path

        if not fpath.exists():
            block = f"\n{'═' * 60}\nFILE: {rel_path}\n{'═' * 60}\n[NEW FILE — does not exist yet. Use --- /dev/null in diff header.]"
            parts.append(block)
            remaining -= len(block)
            continue

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            parts.append(f"\n[Could not read {rel_path}]")
            continue

        total_lines = len(content.splitlines())

        if total_lines <= 60:
            # Small file — send whole thing
            display = content
            location_note = f"(full file — {total_lines} lines)"
        else:
            # Large file — extract the section most relevant to this step
            section, start_line = _extract_relevant_section(
                content,
                step.get("description", "") + " " + step.get("title", "")
            )
            display = section
            section_lines = len(section.splitlines())
            end_line = start_line + section_lines - 1
            location_note = (
                f"(EXCERPT lines {start_line}–{end_line} of {total_lines} total — "
                f"write @@ headers relative to the FULL file line numbers)"
            )

        file_budget = min(remaining - 300, 8_000)
        if len(display) > file_budget:
            display = display[:file_budget] + "\n...(truncated)"

        block = (
            f"\n{'═' * 60}\n"
            f"FILE: {rel_path} {location_note}\n"
            f"{'═' * 60}\n"
            f"{display}"
        )
        parts.append(block)
        remaining -= len(block)

    return "\n".join(parts)


def _extract_relevant_section(content: str, description: str, context_lines: int = 25) -> tuple[str, int]:
    """
    Find the most relevant section of a file for a given step description.

    Returns (section_text, start_line) where start_line is the 1-based line
    number where the section begins in the original file, so Ollama can write
    correct @@ headers.

    Strategy:
    1. Score each line by how many words from the description appear near it
    2. Find the highest-scoring line
    3. Return that line plus context_lines above and below
    4. If no good match, return the end of the file (where new code goes)
    """
    lines = content.splitlines()
    total = len(lines)

    if total <= context_lines * 2:
        # File is small enough to send whole
        return content, 1

    # Extract keywords from description (ignore common words)
    stopwords = {'a','an','the','and','or','in','on','to','of','for','with',
                 'is','it','its','be','by','from','that','this','as','at','if'}
    words = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b', description.lower()))
    keywords = words - stopwords

    if not keywords:
        # No useful keywords — return end of file (where new code usually goes)
        start = max(0, total - context_lines * 2)
        section = '\n'.join(lines[start:])
        return section, start + 1

    # Score each line by keyword proximity with decay
    scores = [0] * total
    for i, line in enumerate(lines):
        line_lower = line.lower()
        for kw in keywords:
            if kw in line_lower:
                for offset in range(-5, 6):
                    idx = i + offset
                    if 0 <= idx < total:
                        scores[idx] += max(0, 5 - abs(offset))

    best = scores.index(max(scores))
    start = max(0, best - context_lines)
    end = min(total, best + context_lines)
    section = '\n'.join(lines[start:end])
    return section, start + 1


# ---------------------------------------------------------------------------
# Prompt
# NOTE: force_json is intentionally NOT used here — the model must emit a
#       raw unified diff, not JSON.  Passing format=json corrupts @@-headers.
# ---------------------------------------------------------------------------

_PATCH_PROMPT = """\
You are an expert software engineer. Output ONLY a standard unified diff patch \
— no markdown, no explanation, no JSON wrappers.

The patch must be directly applicable with: git apply

STEP TO IMPLEMENT:
  Title       : {title}
  Description : {description}
  Target files: {suggested_files}

UNIFIED DIFF FORMAT RULES (follow exactly):
  --- a/path/to/file
  +++ b/path/to/file
  @@ -ORIG_START,ORIG_COUNT +NEW_START,NEW_COUNT @@
   context line (space prefix)
  +added line (plus prefix)
  -removed line (minus prefix)

- ORIG_COUNT = number of lines taken from the original file in this hunk
  (context lines + removed lines)
- NEW_COUNT  = number of lines in the new file for this hunk
  (context lines + added lines)
- Include exactly 3 context lines before and after each change
- For a brand-new file: --- /dev/null   and   @@ -0,0 +1,N @@
- Do NOT change any file not listed in "Target files"
- Output starts with ---  (no preamble, no code fences)

SCOPE CONSTRAINT (critical):
- Make the SMALLEST change that satisfies the step description
- Touch ONLY the Target files listed above — no other files
- Do NOT restructure, reformat, or reorder unrelated code
- Do NOT add features not mentioned in the description
- Do NOT rename variables or functions that already exist and work

FILE EXISTENCE RULES (critical):
- If a file path appears in PRIOR STEPS above → it already exists → use --- a/path not --- /dev/null
- If a file is shown as NEW FILE in PROJECT CONTEXT above → use --- /dev/null
- NEVER use @@ -0,0 for a file that already has content
- Your context lines (space-prefixed) MUST match lines that literally exist in the file right now
- If the file context shows "EXCERPT lines X–Y of Z total", the @@ start
  numbers must use the FULL file line numbers shown (X–Y), not relative numbers

SELF-CHECK — before outputting, verify every hunk:
1. Count the hunk body lines yourself:
   - Lines starting with ' ' (space) or '-' → ORIG_COUNT
   - Lines starting with ' ' (space) or '+' → NEW_COUNT
2. Confirm @@ -X,ORIG_COUNT +Y,NEW_COUNT @@ matches those counts exactly
3. Confirm the 3 context lines before the change exist verbatim in the file
4. Confirm @@ line numbers match the actual line positions in the file shown above
If any check fails, fix the hunk before outputting.

PROJECT CONTEXT:
{context}
"""


# ---------------------------------------------------------------------------
# Diff sanitizer
# ---------------------------------------------------------------------------

def _extract_diff(text: str) -> str | None:
    """
    Extract the real unified diff block from potentially noisy model output.

    Looks for a ``--- a/`` / ``+++ b/`` (or ``/dev/null``) header pair and
    returns everything from that line onward.

    Also repairs the common gemma3 hallucination where the model emits
    ``- a/file`` (one dash) or ````` ``- a/file`` (fence+dash) instead of
    ``--- a/file``.  Both are normalised to the correct three-dash form
    before extraction.
    """
    # ── Step 1: normalise the ``` - a/ → --- a/ hallucination ────────────
    fixed_lines: list[str] = []
    raw_lines = text.splitlines(keepends=True)
    for i, line in enumerate(raw_lines):
        stripped = line.rstrip("\n\r")
        # Pattern: line starts with optional backtick-fence noise then "- a/"
        # e.g. "``` - a/foo.py" or "- a/foo.py"  (model forgot the two extra dashes)
        if re.match(r"^(?:```+\s*)?- a/", stripped):
            # Only repair when the very next line looks like +++ b/
            next_stripped = raw_lines[i + 1].rstrip("\n\r") if i + 1 < len(raw_lines) else ""
            if re.match(r"^\+\+\+ (?:b/|/dev/null)", next_stripped):
                repaired = re.sub(r"^(?:```+\s*)?-\s+", "--- ", stripped)
                line = repaired + "\n"
        fixed_lines.append(line)
    text = "".join(fixed_lines)

    # ── Step 2: strip outer markdown code fences if present ──────────────
    if text.lstrip().startswith("```"):
        inner_lines = text.splitlines()
        # Drop opening fence line
        start = next((j for j, l in enumerate(inner_lines) if l.strip().startswith("```")), 0)
        # Drop closing fence line (last ``` line)
        end = len(inner_lines)
        for j in range(len(inner_lines) - 1, start, -1):
            if inner_lines[j].strip() == "```":
                end = j
                break
        text = "\n".join(inner_lines[start + 1:end])

    # ── Step 3: find the first valid --- / +++ header pair ───────────────
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if not (stripped.startswith("--- a/") or stripped.startswith("--- /dev/null") or
                (stripped.startswith("--- ") and "/" in stripped)):
            continue
        if i + 1 >= len(lines):
            continue
        next_line = lines[i + 1].rstrip("\n\r")
        if not (next_line.startswith("+++ b/") or next_line.startswith("+++ /dev/null") or
                (next_line.startswith("+++ ") and "/" in next_line)):
            continue
        return "".join(lines[i:])

    return None


def _sanitize_diff(text: str) -> str:
    """
    Fix the most common problems in Ollama-generated unified diffs:

    1. Normalise CRLF → LF (Windows line endings corrupt git apply).
    2. Recalculate every @@ -X,Y +X,Z @@ header so the line counts Y and Z
       match the actual hunk body that follows.  Ollama frequently gets these
       counts wrong, which causes "corrupt patch" errors.
    3. Ensure the patch ends with a newline.
    """
    # 1. Normalise line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Pass through file headers and non-hunk lines unchanged
        if not line.startswith("@@"):
            out.append(line)
            i += 1
            continue

        # Collect hunk body (lines that follow until the next @@ or file header)
        hunk_body: list[str] = []
        j = i + 1
        while j < len(lines) and not lines[j].startswith("@@") \
                               and not lines[j].startswith("--- ") \
                               and not lines[j].startswith("diff "):
            hunk_body.append(lines[j])
            j += 1

        # Recalculate counts from actual body
        orig_count = sum(1 for l in hunk_body if l.startswith(" ") or l.startswith("-"))
        new_count  = sum(1 for l in hunk_body if l.startswith(" ") or l.startswith("+"))

        # Extract the start positions from the existing @@ line
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
        if m:
            orig_start = m.group(1)
            new_start  = m.group(2)
            tail       = m.group(3)   # optional function-name hint
            line = f"@@ -{orig_start},{orig_count} +{new_start},{new_count} @@{tail}"

        out.append(line)
        out.extend(hunk_body)
        i = j

    result = "\n".join(out)
    if not result.endswith("\n"):
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_patch_local(step: dict, root: str = ".", prior_diffs: dict | None = None, model: str | None = None) -> tuple[str | None, str, str]:
    """
    Use Ollama to generate a unified diff implementing `step` in the project at `root`.

    Args:
        step:       The plan step dict
        root:       Project root path
        prior_diffs: dict of {step_id: diff_text} for all steps approved before
                    this one. Used to give Ollama context on what already changed.

    Returns:
        (diff_text,  "",            prompt_used)  on success
        (None,       error_string,  prompt_used)  on failure
    """
    from ai_build.reviewer import _call_ollama_api

    context   = _patcher_context(step, root, prior_diffs=prior_diffs)
    suggested = ", ".join(step.get("suggested_files", [])) or "(unspecified)"
    prompt = _PATCH_PROMPT.format(
        context         = context,
        title           = step.get("title", ""),
        description     = step.get("description", ""),
        suggested_files = suggested,
    )

    model = model or os.getenv("OLLAMA_MODEL", "gemma3:4b")
    # NOTE: force_json=False — the model must return a raw diff, not JSON.
    raw = _call_ollama_api(model, prompt, force_json=False)

    if raw.startswith("(Ollama"):
        return None, raw, prompt

    text = raw.strip()

    # Strip markdown fences the model may have added despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        end   = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text  = "\n".join(lines[1:end]).strip()

    # If the model wrapped the diff in JSON (e.g. {"patch": "..."}), unwrap it
    if text.startswith("{"):
        try:
            import json as _json
            data = _json.loads(text)
            candidate = data.get("patch", "")
            if candidate and "\\n" in candidate and "\n" not in candidate:
                candidate = candidate.replace("\\n", "\n")
            if candidate:
                text = candidate
        except Exception:
            pass

    # Extract only the real diff block — ignores markers inside string literals
    extracted = _extract_diff(text)
    if extracted is None:
        return None, (
            "Ollama did not return a valid unified diff "
            "(no proper --- a/ ... +++ b/ header found).\n\n"
            f"Raw response:\n{raw[:800]}"
        ), prompt

    # Sanitize: fix CRLF, recalculate @@ header counts, ensure trailing newline
    clean = _sanitize_diff(extracted)
    return clean, "", prompt
