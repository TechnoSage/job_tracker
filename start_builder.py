"""
start_builder.py — Entry point for the Build Dashboard (port 5001).

  • Hides the console window on Windows
  • Adds build/ to sys.path so builder_app can be imported
  • Creates the Flask app and starts it on http://127.0.0.1:5001
  • Auto-opens the browser after a short delay

Usage:
    python start_builder.py
"""
import os
import sys
import threading

# Add build/ directory to path so builder_app (and its deps) can be imported.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "build"))

# ── Suppress the console window on Windows ────────────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE = 0
    except Exception:
        pass

from builder_app import create_builder_app   # noqa: E402

app = create_builder_app()


def _open_browser() -> None:
    import time
    import webbrowser
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5001")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(
        host="127.0.0.1",
        port=5001,
        debug=False,
        use_reloader=False,
    )
