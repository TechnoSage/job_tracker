"""
start_builder.py — Entry point for the Build Dashboard (default port 5001).

  • Hides the console window on Windows
  • Adds build/ to sys.path so builder_app can be imported
  • Creates the Flask app and starts it on http://127.0.0.1:<port>
  • Auto-opens the browser after a short delay (suppressed in headless mode)

Environment variables (used by the test pipeline):
  BD_PORT     — override the listen port (default 5001)
  BD_HEADLESS — set to "1" to suppress the auto-open browser tab

Usage:
    python start_builder.py
"""
import os
import sys
import threading

# ── Port / headless overrides (used by the test pipeline) ─────────────────────
# The test pipeline sets BD_PORT to a free port so the compiled app can run
# alongside the dev server without a port conflict.  BD_HEADLESS=1 suppresses
# the auto-open browser tab so no extra tab appears during automated testing.
_BD_PORT     = int(os.environ.get("BD_PORT", "5001"))
_BD_HEADLESS = os.environ.get("BD_HEADLESS", "").strip() in ("1", "true", "yes")

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


def _ssl_context():
    """Return (cert, key) paths if mkcert certs exist, else None (plain HTTP)."""
    base = os.path.dirname(os.path.abspath(__file__))
    cert = os.path.join(base, "certs", "localhost.pem")
    key  = os.path.join(base, "certs", "localhost-key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    return None


def _open_browser() -> None:
    if _BD_HEADLESS:
        return  # suppress browser tab during automated test runs
    import time, webbrowser
    time.sleep(1.2)
    scheme = "https" if _ssl_context() else "http"
    webbrowser.open(f"{scheme}://127.0.0.1:{_BD_PORT}")


if __name__ == "__main__":
    ssl = _ssl_context()
    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(
        host="127.0.0.1",
        port=_BD_PORT,
        debug=False,
        use_reloader=False,
        ssl_context=ssl,
    )
