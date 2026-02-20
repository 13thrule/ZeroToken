# ZeroToken

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Ollama required](https://img.shields.io/badge/Ollama-required-orange)](https://ollama.com)
[![No API keys](https://img.shields.io/badge/API%20keys-none%20required-brightgreen)](#)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Mac%20%7C%20Linux-lightgrey)](#)

> A local AI coding assistant that uses **Ollama** to plan and draft code changes, then hands off to **Claude** to apply them.
> No API keys required. No data leaves your machine automatically.

---

## What is ZeroToken?

ZeroToken is a human-in-the-loop coding tool that breaks large development goals into small, reviewable steps  and lets two AI systems each do what they are best at:

| | What it does |
|---|---|
| **Ollama** (local) | Reads your code, generates a plan, drafts unified diffs, reviews patches for errors, and refines them if needed  all running privately on your machine |
| **Claude** (claude.ai) | Applies finished diffs cleanly and reliably  you paste a prompt in, it applies everything in one shot |

You stay in control at every step. Nothing is applied automatically; you review and approve each patch before it becomes part of the final prompt.

---

![ZeroToken start screen](screenshots/start%20screen.png)

---

## Why does it exist?

Writing code with AI assistants works well for small self-contained tasks. It breaks down when the goal is large and multi-step  the AI loses context, makes conflicting edits, and you lose track of what changed.

ZeroToken solves this by:

1. Breaking the goal into a numbered plan
2. Generating one precise diff per step
3. Having a local Ollama reviewer check each diff before you approve it
4. Assembling all approved diffs into a single, complete prompt Claude can apply without losing context

---

## The three-phase workflow

```
SETUP
  Point ZeroToken at your project folder
  Describe what you want to build
  Generate a plan (Ollama auto-plans, or copy a prompt to Claude for higher quality)

STEPS
  For each step in the plan:
    [Ollama Patch]  let Ollama write the diff automatically, OR
    [Claude Prompt] copy a prompt into claude.ai, paste the diff back
    Ollama reviews the diff (checks line numbers, scope, logic)
    Optionally: Ollama Refiner cleans it up
    You Approve or Skip

DELIVER
  ZeroToken assembles all approved diffs into one Final Prompt
  Paste it into claude.ai -> Claude applies every change to your codebase
```

---

## Screenshots

**Step view  review, approve, or refine each patch before it goes anywhere:**

![Approve or refine prompt](screenshots/approve%20or%20refine%20prompt.png)

**Final prompt  one assembled block ready to paste into Claude:**

![Final prompt output](screenshots/final%20prompt%20output.png)

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | Earlier versions may work but are untested |
| [Ollama](https://ollama.com) | latest | Must be running locally on port 11434 |
| Ollama model | any | `gemma3:4b` (fast), `gemma3:12b` (recommended), `qwen2.5-coder:7b` |
| Claude account | free tier | [claude.ai](https://claude.ai)  no API key needed, just the browser UI |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/13thrule/ZeroToken.git
cd ZeroToken

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Mac / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the environment template (optional  only needed to override defaults)
copy .env.example .env        # Windows
cp .env.example .env          # Mac/Linux
# Edit .env and set OLLAMA_MODEL if you want a different default model
```

---

## Start Ollama

In a separate terminal, make sure Ollama is running and you have a model pulled:

```bash
ollama serve                  # start the Ollama server (if not already running)
ollama pull gemma3:4b         # fast, suitable for most tasks
ollama pull gemma3:12b        # better quality, recommended for complex codebases
```

---

## Launch ZeroToken

**Option A  Windows GUI launcher (recommended)**

Double-click `ZeroToken.bat` in the project folder.

This opens a dark-themed launcher window where you can start/stop the server, watch the live log, and open the web UI  all in one place.

**Option B  command line**

```bash
python ai_build.py gui
```

Then open **http://127.0.0.1:5000** in your browser.

---

## Changing the Ollama model

Set `OLLAMA_MODEL` in your `.env` file, or use the **Models** panel in the sidebar to pick a different model for each agent independently:

```env
OLLAMA_MODEL=gemma3:12b
```

The sidebar Models section dynamically lists every model you have pulled in Ollama, with a separate dropdown for Planner, Patcher, Reviewer, and Refiner  plus a **Context tokens** field to increase the Ollama context window (`num_ctx`) without restarting anything.

---

## How the pipeline works

```
Goal (text)
    |
    v
+----------+
| Planner  |  Reads your file tree -> writes a numbered JSON plan
+----------+
    |  plan.json
    v
+----------+
| Patcher  |  For each step: reads relevant files -> writes a unified diff
+----------+
    |  step-N.diff
    v
+----------+
| Reviewer |  Checks the diff: line numbers correct? scope respected? logic sound?
+----------+
    |  verdict: approve / concerns / reject
    v
+----------+
| Refiner  |  (optional) Re-reads the file + review feedback -> improved diff
+----------+
    |  step-N-refined.diff
    v
+----------+
| Assembler|  Combines all approved diffs into one structured Final Prompt
+----------+
    |  final_prompt.txt
    v
  claude.ai  ->  Claude applies every change to your codebase
```

---

## File structure

```
ZeroToken/
+-- ai_build.py            Main entrypoint (CLI + GUI launcher)
+-- ZeroToken.pyw          Windows GUI launcher (no console window)
+-- ZeroToken.bat          Double-click to open the launcher
+-- test_gui.py            Integration test suite (15 tests)
+-- requirements.txt       Python dependencies (only Flask)
+-- .env.example           Environment variable template -- safe to commit
+-- LICENSE                MIT licence
+-- ai_build/
    +-- server.py          Flask web UI -- all routes and HTML template
    +-- planner.py         Builds Claude planning prompts
    +-- local_planner.py   Ollama-powered automatic planner
    +-- executor.py        Builds Claude patch prompts
    +-- local_patcher.py   Ollama-powered automatic patcher
    +-- reviewer.py        Sends diffs to Ollama for structured review
    +-- refiner.py         Ollama Refiner agent -- improves rejected diffs
    +-- assembler.py       Combines approved diffs into Final Agent Prompt
    +-- storage.py         Reads/writes plan.json, patches, prompts
    +-- context.py         Tech stack detection and file tree builder
    +-- context_engine.py  Rich project context for the Reviewer agent
    +-- git_ops.py         Git repo detection, clean/dirty check, git init
    +-- shutdown.py        Graceful server shutdown manager
    +-- ui.py              Terminal display helpers (CLI mode)
```

At runtime, ZeroToken creates a `.ai-build/` folder inside the project you are working on:

```
.ai-build/
+-- plan.json              Step plan (editable)
+-- patches/
|   +-- step-1.diff
|   +-- step-1-refined.diff
+-- prompts/
|   +-- plan_prompt.txt
|   +-- patch_prompt_step_1.txt
+-- final_prompt.txt       The assembled prompt to paste into Claude
```

Add `.ai-build/` to your target project's `.gitignore` to keep it out of version control.

---

## Known limitations

- **Quality depends on model size.** `gemma3:4b` is fast but sometimes produces diffs with incorrect line numbers. `gemma3:12b` or `qwen2.5-coder:7b` give significantly better results.
- **Diffs sometimes need manual fixing.** The Reviewer and Refiner catch most issues, but on complex multi-file changes you may need to edit a diff by hand before approving.
- **Context window limits.** Very large files may be truncated before being sent to Ollama. Increase the context window in the sidebar Models panel or set `OLLAMA_NUM_CTX` in `.env`.
- **No direct file writing.** ZeroToken never writes to your source files. Claude does that in the final step. This is intentional  you stay in control.
- **Single-user, local-only.** The Flask server is not designed for multi-user or internet-facing deployment.
- **Best results with gemma3:12b or larger.** For anything more complex than small utility functions, use a 12B+ parameter model.

---

## Running the tests

```bash
# Make sure the server is running first (python ai_build.py gui)
python -m pytest test_gui.py -v
```

Expected output: `15 passed`.

---

## CLI reference

```bash
python ai_build.py gui                # launch the web GUI (default)
python ai_build.py plan "your goal"   # generate a plan and save it
python ai_build.py show-plan          # pretty-print plan with statuses
python ai_build.py run                # run all pending steps in the terminal
python ai_build.py resume             # skip already-done steps and continue
python ai_build.py reset [step_id]    # reset one step (or all) to pending
```

---

## Troubleshooting

**Ollama shows "offline" in the topbar**
Make sure Ollama is running: open a terminal and run `ollama serve`

**"The pasted text doesn't look like a unified diff"**
Make sure you copied Claude's entire reply. A valid diff starts with `---`/`+++` lines and contains `@@` markers.

**"The pasted text is not valid JSON"**
During planning, make sure you copied Claude's full JSON reply (starts with `{`, ends with `}`).

**"No approved steps to assemble"**
Approve at least one step before clicking Assemble Final Prompt.

**GUI shows a blank page or 500 error**
Check that Flask installed correctly: `python -m flask --version`

---

## Licence

[MIT](LICENSE)  2026 13thrule
