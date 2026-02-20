"""
_launcher_entry.py — PyInstaller entry point for ZeroToken.exe

This is NOT the same as ZeroToken.pyw.  It is a frozen-aware wrapper that:
  1. Locates a real Python interpreter (venv → system PATH fallback)
  2. Launches ai_build.py gui as a subprocess under that interpreter
  3. Opens the Tkinter launcher GUI so the user can see logs and stop the server

The frozen exe is placed in the project root alongside ai_build.py.
sys.executable in a frozen build points to the .exe itself — we must find
a real python.exe separately.
"""

import os
import sys
import subprocess
import threading
import webbrowser
import queue
import time
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import scrolledtext, font as tkfont

# ── Locate the project root (where ai_build.py lives) ─────────────────────────
# When frozen: sys.executable = ZeroToken.exe in the project root.
# When unfrozen: __file__ = _launcher_entry.py in the project root.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

SCRIPT = os.path.join(PROJECT_ROOT, "ai_build.py")

# ── Find a real Python interpreter ────────────────────────────────────────────
def _find_python() -> str:
    """
    Return path to a usable python.exe.
    Search order:
      1. .venv/Scripts/python.exe next to the exe (standard venv layout)
      2. venv/Scripts/python.exe  (alternate name)
      3. python on system PATH
    """
    candidates = [
        os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, "venv",  "Scripts", "python.exe"),
        os.path.join(PROJECT_ROOT, ".venv", "bin", "python"),   # Linux/Mac
        os.path.join(PROJECT_ROOT, "venv",  "bin", "python"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Fall back to system Python
    return "python"

PYTHON = _find_python()

# ── UI colours ────────────────────────────────────────────────────────────────
BG      = "#0d0d0f"
SURFACE = "#131316"
BORDER  = "#252530"
ACCENT  = "#5b7fff"
GREEN   = "#22c55e"
RED     = "#f87171"
YELLOW  = "#facc15"
TEXT    = "#e8e8f0"
TEXT2   = "#9090a8"
TEXT3   = "#55556a"
APP_URL = "http://127.0.0.1:5000"

MONO_FAMILIES = ("JetBrains Mono", "Cascadia Code", "Consolas", "Courier New")

def _mono(size=10, bold=False):
    for f in MONO_FAMILIES:
        try:
            return tkfont.Font(family=f, size=size, weight="bold" if bold else "normal")
        except Exception:
            pass
    return tkfont.Font(size=size, weight="bold" if bold else "normal")


class ZeroTokenApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ZeroToken")
        self.geometry("720x480")
        self.minsize(560, 360)
        self.configure(bg=BG)
        self._proc   = None
        self._log_q  = queue.Queue()
        self._ready  = False
        self._stopping = False

        try:
            # Use the ico if it exists next to the exe
            ico = os.path.join(PROJECT_ROOT, "ZeroToken.ico")
            if os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._start_server)
        self.after(200, self._poll_log)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header bar
        hdr = tk.Frame(self, bg=SURFACE, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="ZeroToken", bg=SURFACE, fg=TEXT,
                 font=_mono(14, bold=True)).pack(side="left", padx=16, pady=10)
        self._status_lbl = tk.Label(hdr, text="● Starting…", bg=SURFACE, fg=YELLOW,
                                     font=_mono(10))
        self._status_lbl.pack(side="left", padx=8)

        # Button row
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=12, pady=6)
        self._open_btn = tk.Button(btn_row, text="Open in browser",
                                    command=self._open_browser,
                                    bg=ACCENT, fg="white", relief="flat",
                                    font=_mono(10, bold=True),
                                    padx=14, pady=6, cursor="hand2",
                                    state="disabled")
        self._open_btn.pack(side="left", padx=(0, 8))
        self._stop_btn = tk.Button(btn_row, text="Stop server",
                                    command=self._stop_server,
                                    bg=SURFACE, fg=TEXT2, relief="flat",
                                    font=_mono(10), padx=14, pady=6,
                                    cursor="hand2")
        self._stop_btn.pack(side="left")

        # Log area
        log_frame = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._log = scrolledtext.ScrolledText(
            log_frame, bg=SURFACE, fg=TEXT2, insertbackground=TEXT,
            font=_mono(9), relief="flat", wrap="word",
            state="disabled", padx=10, pady=8)
        self._log.pack(fill="both", expand=True)
        self._log.tag_config("err",  foreground=RED)
        self._log.tag_config("ok",   foreground=GREEN)
        self._log.tag_config("info", foreground=TEXT2)

    def _append(self, msg: str, tag="info"):
        self._log.configure(state="normal")
        self._log.insert("end", msg + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    # ── Server lifecycle ──────────────────────────────────────────────────────
    def _start_server(self):
        if not os.path.isfile(SCRIPT):
            self._append(f"ERROR: ai_build.py not found at:\n  {SCRIPT}", "err")
            self._status_lbl.config(text="● Error", fg=RED)
            return
        if not os.path.isfile(PYTHON) and PYTHON != "python":
            self._append(f"ERROR: Python not found at:\n  {PYTHON}", "err")
            self._status_lbl.config(text="● Error", fg=RED)
            return

        self._append(f"Python  : {PYTHON}", "info")
        self._append(f"Script  : {SCRIPT}", "info")
        self._append(f"URL     : {APP_URL}\n", "info")

        def _run():
            try:
                self._proc = subprocess.Popen(
                    [PYTHON, SCRIPT, "gui"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=PROJECT_ROOT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                for line in self._proc.stdout:
                    self._log_q.put(("info", line.rstrip()))
            except Exception as exc:
                self._log_q.put(("err", f"Failed to start server: {exc}"))

        threading.Thread(target=_run, daemon=True).start()
        threading.Thread(target=self._wait_for_ready, daemon=True).start()

    def _wait_for_ready(self):
        for _ in range(40):  # 20 s timeout
            time.sleep(0.5)
            try:
                urllib.request.urlopen(APP_URL, timeout=1)
                self._log_q.put(("ok", f"✓ Server ready at {APP_URL}"))
                self._log_q.put(("__ready__", ""))
                return
            except Exception:
                pass
        self._log_q.put(("err", "Server did not respond within 20 s. Check the log above."))

    def _poll_log(self):
        while not self._log_q.empty():
            tag, msg = self._log_q.get_nowait()
            if tag == "__ready__":
                self._on_ready()
            else:
                self._append(msg, tag)
        self.after(150, self._poll_log)

    def _on_ready(self):
        self._ready = True
        self._status_lbl.config(text="● Running", fg=GREEN)
        self._open_btn.config(state="normal")
        webbrowser.open(APP_URL)

    def _open_browser(self):
        webbrowser.open(APP_URL)

    def _stop_server(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._append("Server stopped.", "err")
        self._status_lbl.config(text="● Stopped", fg=RED)
        self._open_btn.config(state="disabled")

    def _on_close(self):
        self._stop_server()
        self.destroy()


if __name__ == "__main__":
    app = ZeroTokenApp()
    app.mainloop()
