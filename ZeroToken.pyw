"""
ZeroToken — Windows launcher for the ZeroToken local AI coding assistant.
Double-click ZeroToken.pyw to open the launcher (no console window).
"""

import subprocess
import sys
import os
import threading
import webbrowser
import tkinter as tk
from tkinter import scrolledtext, font as tkfont
import queue
import time
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
APP_URL   = "http://127.0.0.1:5000"
PYTHON    = sys.executable
SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_build.py")

# ── Colour palette (matches web UI) ───────────────────────────────────────────
BG        = "#0d0d0f"
SURFACE   = "#131316"
SURFACE2  = "#1a1a1f"
BORDER    = "#252530"
ACCENT    = "#5b7fff"
TEAL      = "#2dd4bf"
GREEN     = "#22c55e"
RED       = "#f87171"
YELLOW    = "#facc15"
TEXT      = "#e8e8f0"
TEXT2     = "#9090a8"
TEXT3     = "#55556a"
MONO      = ("JetBrains Mono", "Cascadia Code", "Consolas", "Courier New")


def _mono(size=11, bold=False):
    for f in MONO:
        try:
            return tkfont.Font(family=f, size=size, weight="bold" if bold else "normal")
        except Exception:
            continue
    return tkfont.Font(size=size, weight="bold" if bold else "normal")


class ZeroTokenLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ZeroToken")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(620, 480)

        # Try to set window icon (silently ignore if no icon file)
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

        self._proc: subprocess.Popen | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._server_ok = False
        self._poll_after_id = None
        self._reader_thread: threading.Thread | None = None

        self._build_ui()
        self._after_log()
        self._check_already_running()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar area ───────────────────────────────────────────────────
        header = tk.Frame(self, bg=SURFACE, pady=0)
        header.pack(fill="x", side="top")

        # Logo + name
        logo_frame = tk.Frame(header, bg=SURFACE)
        logo_frame.pack(side="left", padx=18, pady=14)

        logo_canvas = tk.Canvas(logo_frame, width=32, height=32, bg=SURFACE,
                                highlightthickness=0)
        logo_canvas.pack(side="left", padx=(0, 10))
        self._draw_logo(logo_canvas)

        name_frame = tk.Frame(logo_frame, bg=SURFACE)
        name_frame.pack(side="left")
        tk.Label(name_frame, text="ZeroToken", bg=SURFACE, fg=TEXT,
                 font=_mono(16, bold=True)).pack(anchor="w")
        tk.Label(name_frame, text="Local AI Coding Assistant", bg=SURFACE, fg=TEXT2,
                 font=_mono(9)).pack(anchor="w")

        # Status pill
        self._status_var = tk.StringVar(value="● Stopped")
        self._status_lbl = tk.Label(header, textvariable=self._status_var,
                                    bg=SURFACE, fg=RED, font=_mono(10, bold=True))
        self._status_lbl.pack(side="right", padx=18)

        # Separator
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ── Button bar ───────────────────────────────────────────────────────
        btn_bar = tk.Frame(self, bg=SURFACE2, pady=10)
        btn_bar.pack(fill="x")

        self._start_btn = self._btn(btn_bar, "▶  Launch Server", ACCENT, "#ffffff",
                                    self._start_server)
        self._start_btn.pack(side="left", padx=(14, 6))

        self._stop_btn = self._btn(btn_bar, "■  Stop", SURFACE2, RED,
                                   self._stop_server, border=RED)
        self._stop_btn.pack(side="left", padx=6)
        self._stop_btn.config(state="disabled")

        self._browser_btn = self._btn(btn_bar, "🌐  Open in Browser", SURFACE2, TEAL,
                                      self._open_browser, border=TEAL)
        self._browser_btn.pack(side="left", padx=6)
        self._browser_btn.config(state="disabled")

        self._clear_btn = self._btn(btn_bar, "🗑  Clear Log", SURFACE2, TEXT2,
                                    self._clear_log, border=BORDER)
        self._clear_btn.pack(side="right", padx=14)

        # ── Log area ─────────────────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        log_header = tk.Frame(self, bg=SURFACE, pady=5)
        log_header.pack(fill="x")
        tk.Label(log_header, text="SERVER LOG", bg=SURFACE, fg=TEXT3,
                 font=_mono(9, bold=True)).pack(side="left", padx=14)
        self._log_count_var = tk.StringVar(value="")
        tk.Label(log_header, textvariable=self._log_count_var, bg=SURFACE, fg=TEXT3,
                 font=_mono(9)).pack(side="right", padx=14)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        self._log = scrolledtext.ScrolledText(
            self, bg="#060608", fg=TEXT, insertbackground=ACCENT,
            font=_mono(10), relief="flat", padx=12, pady=10,
            selectbackground=ACCENT, selectforeground="#fff",
            wrap="word", state="disabled",
        )
        self._log.pack(fill="both", expand=True)

        # Configure tags for coloured log lines
        self._log.tag_config("info",    foreground=TEXT2)
        self._log.tag_config("ok",      foreground=GREEN)
        self._log.tag_config("warn",    foreground=YELLOW)
        self._log.tag_config("error",   foreground=RED)
        self._log.tag_config("accent",  foreground=ACCENT)
        self._log.tag_config("teal",    foreground=TEAL)
        self._log.tag_config("dim",     foreground=TEXT3)

        # ── Status bar ───────────────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        bar = tk.Frame(self, bg=SURFACE, pady=5)
        bar.pack(fill="x")
        self._url_var = tk.StringVar(value=APP_URL)
        tk.Label(bar, textvariable=self._url_var, bg=SURFACE, fg=TEXT3,
                 font=_mono(9), cursor="hand2").pack(side="left", padx=14)
        self._pid_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._pid_var, bg=SURFACE, fg=TEXT3,
                 font=_mono(9)).pack(side="right", padx=14)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log_line("ZeroToken launcher ready.", "accent")
        self._log_line(f"Script: {SCRIPT}", "dim")
        self._log_line(f"Python: {PYTHON}", "dim")
        self._log_line("Click  ▶ Launch Server  to start.\n", "info")

    @staticmethod
    def _btn(parent, text, bg, fg, cmd, border=None):
        kw = dict(text=text, bg=bg, fg=fg, activebackground=bg,
                  activeforeground=fg, font=_mono(10, bold=True),
                  relief="flat", padx=14, pady=7, cursor="hand2",
                  command=cmd, bd=0)
        if border:
            # Simulate border with a thin Frame wrapper
            wrap = tk.Frame(parent, bg=border, padx=1, pady=1)
            btn  = tk.Button(wrap, **kw)
            btn.pack()
            # Store pack handle on wrapper so caller can .pack() the wrapper
            wrap._btn = btn
            # Forward config/state to inner button
            def _config(**kwargs):
                btn.config(**kwargs)
                if "state" in kwargs and kwargs["state"] == "disabled":
                    wrap.config(bg=TEXT3)
                else:
                    wrap.config(bg=border)
            wrap.config_btn = _config
            wrap.config = lambda **kw2: (_config(**kw2) if any(k in kw2 for k in ("state",)) else tk.Frame.config(wrap, **kw2))
            return wrap
        return tk.Button(parent, **kw)

    @staticmethod
    def _draw_logo(canvas):
        """Draw the same hex-bolt logo as the web UI."""
        c = canvas
        # Dark background rect
        c.create_rectangle(0, 0, 32, 32, fill="#1a1a2e", outline="")
        # Blue bar
        c.create_rectangle(4, 4, 28, 9, fill=ACCENT, outline="")
        # Teal bar
        c.create_rectangle(4, 13, 22, 18, fill=TEAL, outline="")
        # Dim blue bar
        c.create_rectangle(4, 22, 14, 27, fill=ACCENT, outline="", stipple="gray50")
        # Yellow lightning bolt
        pts = [20, 13, 17, 20, 19, 20, 16, 29, 24, 19, 21, 19]
        c.create_polygon(pts, fill=YELLOW, outline="")

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_line(self, text: str, tag: str = "info"):
        self._log.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self._log.insert("end", f"[{ts}] {text}\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")
        lines = int(self._log.index("end-1c").split(".")[0])
        self._log_count_var.set(f"{lines} lines")

    def _after_log(self):
        """Drain the queue every 80ms from the main thread."""
        try:
            while True:
                text, tag = self._log_queue.get_nowait()
                self._log_line(text, tag)
        except queue.Empty:
            pass
        self.after(80, self._after_log)

    def _clear_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")
        self._log_count_var.set("0 lines")

    # ── Server control ────────────────────────────────────────────────────────

    def _start_server(self):
        if self._proc and self._proc.poll() is None:
            self._log_line("Server is already running.", "warn")
            return

        self._log_line("Starting ZeroToken server…", "accent")
        try:
            self._proc = subprocess.Popen(
                [PYTHON, SCRIPT, "gui"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=os.path.dirname(SCRIPT),
            )
        except Exception as exc:
            self._log_line(f"Failed to start: {exc}", "error")
            return

        self._pid_var.set(f"PID {self._proc.pid}")
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self._reader_thread.start()

        # Poll until server responds
        threading.Thread(target=self._wait_for_server, daemon=True).start()

    def _read_output(self):
        """Read subprocess stdout line-by-line and push to queue."""
        for line in self._proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tag = "info"
            lo = line.lower()
            if "error" in lo or "traceback" in lo or "exception" in lo:
                tag = "error"
            elif "warning" in lo or "warn" in lo:
                tag = "warn"
            elif "200" in line or "✓" in line or "ok" in line.lower():
                tag = "ok"
            elif line.startswith("  →") or "http" in lo:
                tag = "teal"
            elif line.startswith("  ") or line.startswith("[ai-build]"):
                tag = "accent"
            self._log_queue.put((line, tag))

        # Process ended
        rc = self._proc.wait()
        self._log_queue.put((f"Server exited (code {rc}).", "warn" if rc else "ok"))
        self.after(0, self._on_server_stopped)

    def _wait_for_server(self):
        """Poll HTTP until the server answers, then update UI."""
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(APP_URL, timeout=1)
                self._log_queue.put(("✓ Server is up — ready.", "ok"))
                self.after(0, self._on_server_up)
                return
            except Exception:
                time.sleep(0.5)
        self._log_queue.put(("Server did not respond within 30 s.", "warn"))

    def _stop_server(self):
        if self._proc and self._proc.poll() is None:
            self._log_line("Stopping server…", "warn")
            try:
                self._proc.terminate()
            except Exception:
                pass
        else:
            self._on_server_stopped()

    def _on_server_up(self):
        self._server_ok = True
        self._status_var.set("● Running")
        self._status_lbl.config(fg=GREEN)
        self._browser_btn.config(state="normal")
        # Auto-open browser on first successful start
        self._open_browser()

    def _on_server_stopped(self):
        self._server_ok = False
        self._proc = None
        self._status_var.set("● Stopped")
        self._status_lbl.config(fg=RED)
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._browser_btn.config(state="disabled")
        self._pid_var.set("")

    def _check_already_running(self):
        """If the server is already up when the launcher opens, reflect that."""
        def _check():
            try:
                urllib.request.urlopen(APP_URL, timeout=1)
                self._log_queue.put(("Server already running on " + APP_URL, "ok"))
                self.after(0, self._on_server_up)
            except Exception:
                pass
        threading.Thread(target=_check, daemon=True).start()

    # ── Browser ───────────────────────────────────────────────────────────────

    def _open_browser(self):
        webbrowser.open(APP_URL)
        self._log_line(f"Opened {APP_URL} in browser.", "teal")

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._proc and self._proc.poll() is None:
            import tkinter.messagebox as mb
            ans = mb.askyesnocancel(
                "ZeroToken",
                "Stop the ZeroToken server before closing?",
                icon="question",
            )
            if ans is None:        # Cancel
                return
            if ans:                # Yes — stop server
                self._stop_server()
                time.sleep(0.6)
        self.destroy()


if __name__ == "__main__":
    app = ZeroTokenLauncher()
    # Centre on screen
    app.update_idletasks()
    w, h = 780, 560
    sw = app.winfo_screenwidth()
    sh = app.winfo_screenheight()
    app.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    app.mainloop()
