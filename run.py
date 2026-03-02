"""
run.py — Entry point for Job Tracker
  • Hides the console window on Windows (logs live in the browser instead)
  • Creates the Flask app
  • Initialises APScheduler (8 AM + 8 PM scans, follow-up reminders)
  • Auto-opens the browser, then starts the server

Usage:
    python run.py          (console hidden automatically on Windows)

For production:
    waitress-serve --host=127.0.0.1 --port=5000 run:flask_app
"""
import logging
import os
import sys
import threading


def _data_dir() -> str:
    """
    Return the writable data directory.
    When frozen (PyInstaller/Nuitka installer): directory of the .exe.
    During development: directory of this file.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# ── Suppress the console window on Windows ────────────────────────────────────
# Logs are captured in-memory and displayed in the browser via /server.
# The FileHandler below still writes to job_tracker.log for persistence.
if sys.platform == "win32":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE = 0
    except Exception:
        pass  # non-fatal; continue normally

# ── Logging: file only (no stdout — console is hidden) ────────────────────────
# Log file lives in the data directory (next to the .exe when installed).
_log_path = os.path.join(_data_dir(), "job_tracker.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
    ],
)

from app import create_app
from scheduler import init_scheduler

flask_app = create_app()
init_scheduler(flask_app)


def _open_browser():
    """Open the browser after a short delay to let the server start."""
    import time, webbrowser
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()

    flask_app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,       # Keep False — APScheduler fires twice in debug/reload mode
        use_reloader=False,
    )
