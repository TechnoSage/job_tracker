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
import re as _re
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

# Strip ANSI colour/cursor codes that werkzeug embeds in its log messages
# (e.g.  \x1b[31m  \x1b[36m  [0m) before they reach the log file.
_ANSI_LOG_RE = _re.compile(r'\x1b\[[0-9;]*[mGKHFJSTA-Za-z]|\x1b[()=>]|\r')

class _AnsiStripper(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _ANSI_LOG_RE.sub('', record.msg)
        if record.args:
            try:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                record.args = tuple(
                    _ANSI_LOG_RE.sub('', a) if isinstance(a, str) else a
                    for a in args
                )
            except Exception:
                pass
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
    ],
)
logging.root.addFilter(_AnsiStripper())

from app import create_app
from scheduler import init_scheduler

flask_app = create_app()
init_scheduler(flask_app)


def _ssl_context():
    """Return (cert, key) paths if mkcert certs exist, else None (plain HTTP)."""
    base = os.path.dirname(os.path.abspath(__file__))
    cert = os.path.join(base, "certs", "localhost.pem")
    key  = os.path.join(base, "certs", "localhost-key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    return None


def _open_browser():
    """Open the browser after a short delay to let the server start."""
    import time, webbrowser
    time.sleep(1.2)
    scheme = "https" if _ssl_context() else "http"
    webbrowser.open(f"{scheme}://127.0.0.1:5000")


if __name__ == "__main__":
    ssl = _ssl_context()
    threading.Thread(target=_open_browser, daemon=True).start()

    flask_app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,       # Keep False — APScheduler fires twice in debug/reload mode
        use_reloader=False,
        ssl_context=ssl,
    )
