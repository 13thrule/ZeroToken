"""
shutdown.py - Central shutdown coordinator for ai-build.

Responsibilities
----------------
* Register cleanup callbacks from any module (server, ui, future workers).
* Register temp files that must be deleted on exit.
* Register subprocesses that must be terminated on exit.
* Handle SIGINT / SIGTERM so Ctrl-C and kill -15 both trigger the same path.
* Register an atexit fallback so even a plain sys.exit() cleans up.
* Expose shutdown() as a plain callable — safe to call multiple times.
* Never touch Ollama, Claude, or any unrelated system process.

Usage
-----
    from ai_build.shutdown import get_shutdown_manager
    sm = get_shutdown_manager()

    sm.register_callback(my_cleanup_fn, name="my thing")
    sm.register_temp_file("/tmp/foo.html")
    sm.register_subprocess(proc)

    sm.wait()          # block main thread until shutdown is triggered
    sm.shutdown()      # trigger shutdown from anywhere (route, signal, etc.)
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import tempfile
import threading
from typing import Callable

log = logging.getLogger("ai_build.shutdown")


class ShutdownManager:
    """
    Singleton coordinator.  Obtain via get_shutdown_manager().
    """

    def __init__(self) -> None:
        self._event       = threading.Event()
        self._lock        = threading.Lock()
        self._called      = False

        self._callbacks:    list[tuple[str, Callable]]      = []
        self._temp_files:   list[str]                       = []
        self._subprocesses: list[subprocess.Popen]          = []

        # atexit fallback — fires even on plain sys.exit()
        atexit.register(self._atexit_handler)

        # Signal handlers — SIGINT (Ctrl-C) and SIGTERM (kill / systemd)
        try:
            signal.signal(signal.SIGINT,  self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except (OSError, ValueError):
            # Can't set signal handlers from non-main thread at import time;
            # the main thread in run_server() will re-register them.
            pass

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register_callback(self, fn: Callable, name: str = "") -> None:
        """
        Register a zero-argument callable to be called during shutdown.
        Callbacks are called in LIFO order (last registered, first called).
        """
        with self._lock:
            self._callbacks.append((name or fn.__name__, fn))

    def register_temp_file(self, path: str) -> None:
        """
        Register a file path to be deleted during shutdown.
        Silently ignores files that do not exist at cleanup time.
        """
        with self._lock:
            self._temp_files.append(path)

    def register_subprocess(self, proc: subprocess.Popen) -> None:
        """
        Register a Popen process to be terminated during shutdown.
        Only long-running processes need registration; short git calls do not.
        """
        with self._lock:
            self._subprocesses.append(proc)

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    def shutdown(self, reason: str = "requested") -> None:
        """
        Trigger a clean shutdown.  Safe to call from any thread; idempotent.
        """
        with self._lock:
            if self._called:
                return
            self._called = True

        log.info("Shutdown triggered: %s", reason)
        print(f"\n[ai-build] Shutting down ({reason})…", flush=True)

        # 1. Run registered callbacks in LIFO order
        with self._lock:
            callbacks = list(reversed(self._callbacks))
        for name, fn in callbacks:
            try:
                log.debug("  callback: %s", name)
                fn()
            except Exception as exc:
                log.warning("  callback %r raised: %s", name, exc)

        # 2. Terminate registered subprocesses (non-Ollama, non-Claude)
        with self._lock:
            procs = list(self._subprocesses)
        for proc in procs:
            _safe_terminate(proc)

        # 3. Delete temporary files
        with self._lock:
            files = list(self._temp_files)
        for path in files:
            _safe_delete(path)

        # 4. Signal waiting threads
        self._event.set()
        print("[ai-build] Shutdown complete.", flush=True)

    def wait(self) -> None:
        """Block the calling thread until shutdown() is triggered."""
        self._event.wait()

    @property
    def is_shutdown(self) -> bool:
        return self._event.is_set()

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _signal_handler(self, signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        self.shutdown(reason=f"signal {sig_name}")

    def _atexit_handler(self) -> None:
        # Only fires if shutdown() was never called (e.g. sys.exit() directly)
        if not self._called:
            self.shutdown(reason="atexit")


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_manager: ShutdownManager | None = None
_manager_lock = threading.Lock()


def get_shutdown_manager() -> ShutdownManager:
    """Return the process-wide ShutdownManager singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ShutdownManager()
    return _manager


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _safe_terminate(proc: subprocess.Popen) -> None:
    """Try SIGTERM then SIGKILL on a subprocess.  Never raises."""
    try:
        if proc.poll() is None:          # still running
            log.debug("  terminating pid %d", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception as exc:
        log.warning("  failed to terminate process: %s", exc)


def _safe_delete(path: str) -> None:
    """Delete a file if it exists.  Never raises."""
    try:
        if os.path.exists(path):
            os.unlink(path)
            log.debug("  deleted temp file: %s", path)
    except Exception as exc:
        log.warning("  failed to delete %r: %s", path, exc)


# ------------------------------------------------------------------
# Convenience: a context manager for temp files that auto-registers
# ------------------------------------------------------------------

class ManagedTempFile:
    """
    Create a NamedTemporaryFile and register it with ShutdownManager so it
    is always deleted on exit, even if the caller forgets.

    Usage:
        with ManagedTempFile(suffix=".html", mode="w") as f:
            f.write(content)
            path = f.name
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("delete", False)
        self._kwargs = kwargs
        self._file   = None

    def __enter__(self):
        self._file = tempfile.NamedTemporaryFile(**self._kwargs)
        get_shutdown_manager().register_temp_file(self._file.name)
        return self._file

    def __exit__(self, *_):
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
