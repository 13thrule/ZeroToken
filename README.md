# 🪙 ZeroToken

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Ollama required](https://img.shields.io/badge/Ollama-required-orange)](https://ollama.com)
[![No API keys](https://img.shields.io/badge/API%20keys-none%20required-brightgreen)](#)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Mac%20%7C%20Linux-lightgrey)](#)

> **The local-first agent builder that plans for free and executes for pennies.**
>
> ZeroToken is a zero-fee agent orchestration system powered by [Ollama](https://ollama.com). It shifts the expensive "thinking" phase of AI development — planning, patching, reviewing — onto your own hardware, then generates a single high-context execution prompt you paste into whichever cloud LLM you already use. No subscriptions. No middleman. No data leaving your machine until you decide.

---

![ZeroToken infographic](screenshots/infographic.png)

---

![ZeroToken start screen](screenshots/start%20screen.png)

---

## 🚀 The ZeroToken Philosophy

Traditional AI agents are expensive because they **think in the cloud**. Every mistake, file-search, and retry burns your API credits.

ZeroToken flips the script — all the thinking is free:

| Phase | Runs on | Cost |
|---|---|---|
| **Planner** — reads your codebase, writes a numbered plan | Your machine (Ollama) | **$0** |
| **Patcher** — writes a precise unified diff per step | Your machine (Ollama) | **$0** |
| **Reviewer** — checks for syntax errors and logic bugs | Your machine (Ollama) | **$0** |
| **Refiner** — rewrites a rejected diff using review feedback | Your machine (Ollama) | **$0** |
| **Execution** — you paste one assembled prompt into your LLM | Cloud (your choice) | ~$0.01–$0.05 |

You review and approve each diff before anything leaves your machine. Nothing is applied to your files automatically.

> **Honest note:** output quality depends heavily on your local model. `gemma3:4b` is fast but makes mistakes on complex diffs. `gemma3:12b` or `qwen2.5-coder:7b` are significantly better. A GPU with 12 GB+ VRAM is recommended for the 12B models.

---

## 🛠️ Key Features

- **Zero service fees** — no subscriptions, no pro tiers, no middleman markup
- **Context tax killer** — stop sending your entire codebase to the cloud; ZeroToken generates compact unified diffs
- **Privacy-first** — your project structure and drafts never leave your machine until you choose to send them
- **Bring your own LLM** — works with Claude, Gemini, or any model you already have access to (free tiers included)
- **Human in the loop** — you review and approve every diff; nothing is written to your files automatically
- **Per-agent model control** — assign a different Ollama model to each agent from the sidebar, no restart needed

---

## How it works

```
PLAN    Ollama reads your file tree and writes a numbered step plan

PATCH   For each step, Ollama writes a unified diff (or you paste one from Claude)

REVIEW  Ollama checks the diff: line numbers correct? scope right? logic sound?
        Optionally: Ollama Refiner rewrites bad diffs automatically

DELIVER ZeroToken assembles all approved diffs into one Final Prompt
        Paste it into your LLM of choice — it applies every change in one shot
```

**Step view — review, approve, or refine each patch before it goes anywhere:**

![Approve or refine prompt](screenshots/approve%20or%20refine%20prompt.png)

**Final prompt — one assembled block ready to paste into your LLM:**

![Final prompt output](screenshots/final%20prompt%20output.png)

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Earlier versions untested |
| [Ollama](https://ollama.com) | Running locally on port 11434 |
| A pulled Ollama model | `gemma3:4b` (fast), `gemma3:12b` (recommended), `qwen2.5-coder:7b` |
| Claude / Gemini (optional) | Free browser tier works — no API key needed |

---

## Quick start

```bash
# 1. Clone and enter the project
git clone https://github.com/13thrule/ZeroToken.git
cd ZeroToken

# 2. Create a virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Mac / Linux
pip install -r requirements.txt

# 3. Pull an Ollama model (in a separate terminal)
ollama pull gemma3:12b          # recommended
# ollama pull gemma3:4b         # faster, lower quality
```

**Windows  double-click launcher (no terminal needed)**

Run `build_exe.bat` once to produce `ZeroToken.exe`, then double-click it.
A launcher window starts the server, streams the log, and opens the browser automatically.

**All platforms  command line**

```bash
python ai_build.py gui
# Open http://127.0.0.1:5000
```

---

## Changing models

Use the **Models** panel in the sidebar to set a different Ollama model for each agent (Planner, Patcher, Reviewer, Refiner) independently, and adjust the context window (`num_ctx`) on the fly — no restart needed.

Or set defaults in `.env`:

```env
OLLAMA_MODEL=gemma3:12b
OLLAMA_NUM_CTX=32768
```

Copy `.env.example` to `.env` to get started.

---

## 📊 Cost Comparison (2026)

| Phase | Cloud agents (Devin / Replit) | ZeroToken |
|---|---|---|
| Project planning | ~$0.20 | **$0.00** |
| Diff generation | ~$0.50 | **$0.00** |
| Code review | ~$0.30 | **$0.00** |
| Refining / patching | ~$0.50 | **$0.00** |
| **Total tool fee** | **~$1.50+** | **$0.00** |
| Final execution pass | included in above | ~$0.01–$0.05 (your LLM) |

*ZeroToken cost figures are accurate — Ollama runs locally and charges nothing. Final execution cost depends on which cloud model you use and how large your assembled prompt is. Free-tier Claude and Gemini work fine for most tasks.*

---

## Pipeline diagram

```
Your goal (text)
    │
    ▼
┌──────────┐
│ Planner  │  Reads your file tree → writes a numbered JSON plan
└──────────┘
    │ plan.json
    ▼
┌──────────┐
│ Patcher  │  Per step: reads relevant files → writes a unified diff
└──────────┘
    │ step-N.diff
    ▼
┌──────────┐
│ Reviewer │  Checks the diff: line numbers correct? scope respected?
└──────────┘
    │ verdict: approve / concerns / reject
    ▼
┌──────────┐
│ Refiner  │  (optional) Re-reads file + feedback → improved diff
└──────────┘
    │ step-N-refined.diff
    ▼
┌──────────┐
│ Assembler│  Combines all approved diffs → one Final Prompt
└──────────┘
    │ final_prompt.txt
    ▼
  Claude / Gemini / GPT  →  applies every change to your codebase
```

---

## File structure

```
ZeroToken/
+-- ai_build.py            Main entrypoint (CLI + GUI launcher)
+-- _launcher_entry.py     PyInstaller entry point (source for ZeroToken.exe)
+-- build_exe.bat          One-click exe builder (Windows)
+-- test_gui.py            Integration test suite (15 tests)
+-- requirements.txt       Python dependencies (Flask only)
+-- .env.example           Environment variable template
+-- ai_build/
    +-- server.py          Flask web UI -- all routes and HTML
    +-- planner.py         Claude planning prompt builder
    +-- local_planner.py   Ollama automatic planner
    +-- executor.py        Claude patch prompt builder
    +-- local_patcher.py   Ollama automatic patcher
    +-- reviewer.py        Ollama diff reviewer
    +-- refiner.py         Ollama diff refiner
    +-- assembler.py       Final Prompt assembler
    +-- storage.py         plan.json / patch / prompt I/O
    +-- context.py         File tree and stack detection
    +-- context_engine.py  Rich project context for the Reviewer
    +-- git_ops.py         Git detection and status checks
    +-- shutdown.py        Graceful server shutdown
    +-- ui.py              Terminal display helpers (CLI mode)
```

At runtime ZeroToken creates `.ai-build/` inside whichever project you point it at:

```
.ai-build/
+-- plan.json
+-- patches/
|   +-- step-1.diff
|   +-- step-1-refined.diff
+-- prompts/
|   +-- patch_prompt_step_1.txt
+-- final_prompt.txt
```

Add `.ai-build/` to your project's `.gitignore`.

---

## Known limitations

- **Model quality matters a lot.** `gemma3:4b` frequently produces diffs with wrong line numbers. Use `gemma3:12b` or larger for anything non-trivial.
- **Diffs sometimes need manual fixes.** The Reviewer and Refiner catch most issues but are not perfect — especially on large multi-file changes.
- **No direct file writing.** ZeroToken never touches your source files. That happens in the final cloud step — intentionally.
- **Single-user, local only.** The Flask server is not designed for multi-user or internet-facing deployment.
- **Large files get excerpted.** The Patcher sends only the most relevant section of large files to Ollama. The `@@` line numbers are calculated against the full file, but this can occasionally be off.

---

## Running the tests

```bash
# Server must be running first
python ai_build.py gui

# In another terminal
python -m pytest test_gui.py -v
# Expected: 15 passed
```

---

## CLI reference

```bash
python ai_build.py gui                # launch the web GUI (default)
python ai_build.py plan "your goal"   # generate a plan and save it
python ai_build.py show-plan          # print plan with statuses
python ai_build.py run                # run all pending steps in the terminal
python ai_build.py resume             # skip done steps and continue
python ai_build.py reset [step_id]    # reset one step (or all) to pending
```

---

## Troubleshooting

**Ollama shows "offline" in the topbar**
Run `ollama serve` in a terminal.

**"The pasted text doesn't look like a unified diff"**
Copy the LLM's full reply — a valid diff starts with `---`/`+++` and contains `@@` markers.

**"The pasted text is not valid JSON"**
Copy the full JSON reply (starts with `{`, ends with `}`).

**"No approved steps to assemble"**
Approve at least one step before clicking Assemble.

**Blank page or 500 error**
Check Flask is installed: `python -m flask --version`

---

## Licence

[MIT](LICENSE) © 2026 13thrule

---

Built with ❤️ for the local LLM community.
