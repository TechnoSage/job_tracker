"""
build_dash_daemon.py — Background daemon for the Build Dashboard.

Runs as a standalone process launched by builder_app.py. Responsibilities:
  - Writes its PID to build_dash_daemon.pid so the Flask app can track it
  - Reads build_settings.json to get backup schedule, source dir, and dest dir
  - Runs robocopy backups on the configured schedule
  - Optionally shows a system tray icon (requires pystray + Pillow)
  - Can be compiled to Build_Dash.exe via PyInstaller for tray placement

Usage:
  python build_dash_daemon.py
  (or as exe: Build_Dash.exe)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
DAEMON_DIR    = Path(__file__).parent.resolve()
SETTINGS_FILE = DAEMON_DIR / "build_settings.json"
PID_FILE      = DAEMON_DIR / "build_dash_daemon.pid"

# ── PID management ─────────────────────────────────────────────────────────────

def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid() -> None:
    PID_FILE.unlink(missing_ok=True)


# ── Settings ───────────────────────────────────────────────────────────────────

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


# ── Backup ─────────────────────────────────────────────────────────────────────

def _run_backup() -> None:
    settings = _load_settings()
    src  = settings.get("GIT_REPO_DIR",  "").strip()
    dest = settings.get("BACKUP_DEST",   "").strip()
    if not src or not dest:
        return
    if not os.path.isdir(src):
        return
    try:
        os.makedirs(dest, exist_ok=True)
        subprocess.run(
            ["robocopy", src, dest, "/MIR", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception:
        pass


def _schedule_to_seconds(schedule: str) -> int | None:
    """Return interval in seconds for a schedule string, or None for manual."""
    mapping = {
        "5":  300,   "5_running":  300,
        "10": 600,   "10_running": 600,
        "30": 1800,  "30_running": 1800,
    }
    return mapping.get(schedule)


# ── Tray icon (optional) ───────────────────────────────────────────────────────

def _try_tray(on_quit: callable) -> bool:
    """Attempt to show a tray icon. Returns True if tray is running."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    settings  = _load_settings()
    icon_path = settings.get("DAEMON_ICON", "").strip()
    app_name  = (settings.get("DAEMON_NAME", "") or "Build_Dash").strip()

    if icon_path and os.path.isfile(icon_path):
        try:
            image = Image.open(icon_path).convert("RGBA")
        except Exception:
            image = _default_icon_image()
    else:
        image = _default_icon_image()

    def _quit_action(icon, item):
        icon.stop()
        on_quit()

    def _backup_now(icon, item):
        _run_backup()

    menu = pystray.Menu(
        pystray.MenuItem("Backup Now", _backup_now),
        pystray.MenuItem("Quit", _quit_action),
    )
    icon = pystray.Icon(app_name, image, app_name, menu)
    icon.run()
    return True


def _default_icon_image():
    """Generate a simple 64×64 placeholder icon."""
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (28, 31, 36, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, 56, 56], fill=(100, 180, 255, 255))
    return img


# ── Main loop ──────────────────────────────────────────────────────────────────

_running = True


def _stop() -> None:
    global _running
    _running = False
    _remove_pid()


def _main_loop() -> None:
    last_backup = 0.0

    while _running:
        settings = _load_settings()
        schedule = settings.get("BACKUP_SCHEDULE", "manual")
        interval = _schedule_to_seconds(schedule)

        if interval is not None:
            now = time.time()
            if now - last_backup >= interval:
                _run_backup()
                last_backup = now

        time.sleep(30)  # check every 30 s


def main() -> None:
    _write_pid()
    try:
        # Try tray — it blocks until quit. If pystray isn't installed, fall
        # back to a plain background loop that can be killed via the PID file.
        import threading
        loop_thread = threading.Thread(target=_main_loop, daemon=True)
        loop_thread.start()

        if not _try_tray(_stop):
            # No tray — just run the loop in foreground
            loop_thread.join()
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
