"""
scheduler_daemon.py — Background job-scan daemon.

Runs scheduled job scans independently of the Flask web application, so scans
keep happening even when the browser tab / web server is closed.

The daemon reads its schedule from the same SQLite database as the web app —
every change saved on the Settings page takes effect automatically on the
next sleep/wake cycle (no daemon restart needed).

Usage
-----
  python scheduler_daemon.py           # start daemon (tray icon if enabled in Settings)
  python scheduler_daemon.py --once    # run one scan immediately and exit
  python scheduler_daemon.py --no-tray # force daemon without tray icon

System tray
-----------
  When "Show tray icon" is enabled on the Settings page, a Job Tracker icon
  appears in the Windows taskbar notification area (bottom-right clock area).
  Right-clicking it shows:
    • Scan Now  — triggers an immediate scan
    • Open Job Tracker — opens the web UI in the default browser
    • Exit  — stops the daemon

Requires: pystray and Pillow  (auto-installed on first tray launch if missing)
          pip install pystray Pillow
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# sys.path fix for MS Store Python running as a copied .exe with pyvenv.cfg
#
# When launched as JobTrackerDaemon.exe (a copy of pythonw.exe with a custom
# name), the Windows Store app permissions no longer apply, so .pyd extension
# modules in the restricted WindowsApps\...\DLLs directory become inaccessible.
# Fix: insert a local DLLs/ directory (populated at daemon setup time) first.
#
# Additionally, pyvenv.cfg excludes user site-packages, so pystray/Pillow are
# not found.  We add the MS Store user site-packages path explicitly.
# ---------------------------------------------------------------------------
def _fix_sys_path() -> None:
    # 1. Local DLLs directory (overrides restricted WindowsApps DLLs)
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    _local_dlls = os.path.join(_exe_dir, "DLLs")
    if os.path.isdir(_local_dlls) and _local_dlls not in sys.path:
        sys.path.insert(0, _local_dlls)

    # 2. MS Store Python user site-packages (excluded by pyvenv.cfg venv mode)
    if sys.platform == "win32":
        _appdata = os.environ.get("LOCALAPPDATA", "")
        _pkgs = os.path.join(_appdata, "Packages")
        if os.path.isdir(_pkgs):
            _ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            for _d in os.listdir(_pkgs):
                if _d.startswith("PythonSoftwareFoundation.Python."):
                    _sp = os.path.join(
                        _pkgs, _d, "LocalCache", "local-packages", _ver, "site-packages"
                    )
                    if os.path.isdir(_sp) and _sp not in sys.path:
                        sys.path.insert(0, _sp)
                    break

_fix_sys_path()

# ---------------------------------------------------------------------------
# Project root on sys.path
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# ---------------------------------------------------------------------------
# Logging — persistent log file + stdout
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, "scheduler_daemon.log"), encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("daemon")

# ---------------------------------------------------------------------------
# PID file — written by the daemon so the web app can check if it is running
# ---------------------------------------------------------------------------
PID_FILE = os.path.join(PROJECT_DIR, "scheduler_daemon.pid")

_stop_event = threading.Event()   # set to request graceful shutdown


def _write_pid() -> None:
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Settings reader
# ---------------------------------------------------------------------------

def _read_settings() -> dict | None:
    """Return schedule + UI settings from the DB, or None on failure."""
    try:
        from app import create_app   # type: ignore
        flask_app = create_app()
        with flask_app.app_context():
            from models import Setting  # type: ignore
            from config import Config   # type: ignore
            cfg = Config()
            return {
                "enabled":         Setting.get("scan_auto_enabled", "true") == "true",
                "scan_times":      Setting.get("scan_times",        "08:00,20:00"),
                "frequency":       Setting.get("scan_frequency",    "daily"),
                "weekdays":        Setting.get("scan_weekdays",     "0,1,2,3,4"),
                "monthdays":       Setting.get("scan_monthdays",    "1"),
                "timezone":        Setting.get("timezone",          cfg.TIMEZONE),
                "show_tray_icon":  Setting.get("show_tray_icon",    "true") == "true",
                "app_port":        Setting.get("app_port",          "5000"),
            }
    except Exception as exc:
        logger.error("Failed to read settings: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Scan runner + timeout watchdog
# ---------------------------------------------------------------------------

def _run_scan() -> None:
    """Execute a full job scan inside a temporary Flask app context."""
    logger.info("Starting scan…")
    try:
        from app import create_app          # type: ignore
        from scheduler import run_job_scan  # type: ignore
        flask_app = create_app()
        run_job_scan(flask_app)
        logger.info("Scan completed.")
    except Exception as exc:
        logger.error("Scan error: %s", exc, exc_info=True)


def _read_timeout_minutes() -> float | None:
    """Return the scan_timeout_minutes setting from DB, or None if disabled/unset."""
    try:
        from app import create_app  # type: ignore
        flask_app = create_app()
        with flask_app.app_context():
            from models import Setting  # type: ignore
            raw = Setting.get("scan_timeout_minutes", "30").strip()
            if raw:
                val = float(raw)
                return val if val > 0 else None
    except Exception as exc:
        logger.warning("Could not read scan_timeout_minutes setting: %s", exc)
    return None


def _handle_scan_timeout(phase_info: dict, elapsed_secs: float) -> None:
    """Record timeout in DB (ScanLog + Notification + server log) then restart daemon."""
    phase          = phase_info.get("phase", "unknown")
    current_source = phase_info.get("current_source", "")
    done           = phase_info.get("done_sources", 0)
    total          = phase_info.get("total_sources", 0)
    elapsed_min    = elapsed_secs / 60

    detail = f"phase={phase!r}"
    if current_source:
        detail += f", scraping {current_source!r}"
    if total:
        detail += f", {done}/{total} sources completed"

    error_msg = (
        f"Scan timed out after {elapsed_min:.1f} min — {detail}. "
        "Daemon is restarting automatically."
    )
    logger.error("SCAN TIMEOUT: %s", error_msg)

    # Persist ScanLog + Notification to DB
    try:
        import datetime as _dt
        from app import create_app      # type: ignore
        flask_app = create_app()
        with flask_app.app_context():
            from extensions import db                   # type: ignore
            from models import ScanLog, Notification    # type: ignore

            db.session.add(ScanLog(
                scan_date=_dt.datetime.utcnow(),
                source="all",
                jobs_found=phase_info.get("total_fetched", 0),
                jobs_new=phase_info.get("total_new", 0),
                jobs_matched=phase_info.get("total_matched", 0),
                status="timeout",
                error_message=error_msg,
                duration_seconds=round(elapsed_secs, 1),
            ))
            db.session.add(Notification(
                type="scan_error",
                title="Scan Timeout",
                message=error_msg,
            ))
            db.session.commit()
        logger.info("Timeout ScanLog and Notification recorded.")
    except Exception as exc:
        logger.error("Could not record timeout in DB: %s", exc)

    # Launch a fresh daemon process before exiting
    logger.warning("Restarting daemon after timeout…")
    try:
        flags = 0
        if sys.platform == "win32":
            import subprocess as _sp
            flags = _sp.CREATE_NO_WINDOW | _sp.DETACHED_PROCESS
        import subprocess as _sp
        _sp.Popen(
            [sys.executable, os.path.abspath(__file__), "--no-tray"],
            creationflags=flags,
            close_fds=True,
        )
        logger.info("Replacement daemon process launched.")
    except Exception as exc:
        logger.error("Failed to launch replacement daemon: %s", exc)

    # Exit this (timed-out) process immediately
    os._exit(1)


def _run_scan_with_timeout(timeout_minutes: float | None = None) -> None:
    """Run _run_scan() in a worker thread, killing the daemon if it exceeds the timeout."""
    if timeout_minutes is None:
        timeout_minutes = _read_timeout_minutes()

    t = threading.Thread(target=_run_scan, name="scan-worker", daemon=True)
    t.start()

    if timeout_minutes and timeout_minutes > 0:
        timeout_secs = timeout_minutes * 60
        start = time.monotonic()
        t.join(timeout=timeout_secs)
        elapsed = time.monotonic() - start
        if t.is_alive():
            # Snapshot progress state before the process dies
            try:
                from scheduler import get_scan_progress  # type: ignore
                phase_info = get_scan_progress()
            except Exception:
                phase_info = {}
            _handle_scan_timeout(phase_info, elapsed)
            # _handle_scan_timeout calls os._exit() — execution stops here
    else:
        t.join()


# ---------------------------------------------------------------------------
# Response email checker
# ---------------------------------------------------------------------------

_last_email_check: datetime | None = None
_EMAIL_CHECK_INTERVAL_SECS = 3600  # default: every hour


def _run_email_check() -> None:
    """Check all configured response-email IMAP accounts for application replies."""
    global _last_email_check
    logger.info("Starting response-email check…")
    try:
        from app import create_app       # type: ignore
        from email_checker import check_all_accounts  # type: ignore
        flask_app = create_app()
        results = check_all_accounts(flask_app)
        total = sum(r.get("found", 0) for r in results)
        logger.info("Email check complete — %d response(s) processed across %d account(s).",
                    total, len(results))
    except Exception as exc:
        logger.error("Email check error: %s", exc, exc_info=True)
    _last_email_check = datetime.now()


def _email_check_due() -> bool:
    """Return True if enough time has passed since the last email check."""
    if _last_email_check is None:
        return True
    return (datetime.now() - _last_email_check).total_seconds() >= _EMAIL_CHECK_INTERVAL_SECS


# ---------------------------------------------------------------------------
# Schedule calculation
# ---------------------------------------------------------------------------

def _seconds_until_next(settings: dict) -> tuple[str, float]:
    """Return (time_label, seconds) for the soonest upcoming configured scan."""
    try:
        import pytz  # type: ignore
        tz = pytz.timezone(settings["timezone"])
    except Exception:
        import datetime as _dt
        tz = _dt.timezone.utc  # type: ignore

    now = datetime.now(tz)
    frequency = settings.get("frequency", "daily")
    raw_times = [t.strip() for t in settings["scan_times"].split(",") if t.strip()]
    if not raw_times:
        raw_times = ["08:00", "20:00"]

    best_label: str = raw_times[0]
    best_wait: float = float("inf")

    for time_str in raw_times:
        try:
            h, m = int(time_str.split(":")[0]), int(time_str.split(":")[1])
        except ValueError:
            continue

        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)

        for _ in range(62):
            if frequency == "weekly":
                day_set = {
                    int(d) for d in settings.get("weekdays", "0,1,2,3,4").split(",")
                    if d.strip().isdigit()
                }
                ok = candidate.weekday() in day_set
            elif frequency == "monthly":
                mday_set = {
                    int(d) for d in settings.get("monthdays", "1").split(",")
                    if d.strip().isdigit()
                }
                ok = candidate.day in mday_set
            else:
                ok = True

            if ok:
                wait = (candidate - now).total_seconds()
                if wait < best_wait:
                    best_wait = wait
                    best_label = time_str
                break
            candidate += timedelta(days=1)

    return best_label, best_wait if best_wait < float("inf") else 24 * 3600


# ---------------------------------------------------------------------------
# Core scan loop (runs in its own thread when tray is active)
# ---------------------------------------------------------------------------

def _scan_loop() -> None:
    """Perpetual scan loop — sleeps until the next scheduled time, then scans.
    Also triggers response-email checks every hour regardless of scan schedule."""
    # Run an initial email check shortly after startup (30 s delay)
    _stop_event.wait(30)
    if not _stop_event.is_set():
        _run_email_check()

    while not _stop_event.is_set():
        settings = _read_settings()

        if not settings:
            logger.warning("Settings unavailable — retrying in 60 s.")
            _stop_event.wait(60)
            continue

        if not settings["enabled"]:
            logger.info("Automated scans are disabled — rechecking in 5 min.")
            # Still run email check while scan is disabled
            if _email_check_due():
                _run_email_check()
            _stop_event.wait(300)
            continue

        next_label, wait_secs = _seconds_until_next(settings)
        logger.info("Next scan at %s — sleeping %.0f min.", next_label, wait_secs / 60)

        # Sleep in 30-second slices; wake up for email checks too
        slept = 0.0
        while slept < wait_secs and not _stop_event.is_set():
            chunk = min(30.0, wait_secs - slept)
            _stop_event.wait(chunk)
            slept += chunk
            # Mid-sleep email check
            if not _stop_event.is_set() and _email_check_due():
                _run_email_check()

        if _stop_event.is_set():
            break

        settings = _read_settings()
        if settings and settings["enabled"]:
            _run_scan_with_timeout()

        # Email check after each job scan too
        if _email_check_due():
            _run_email_check()


# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------

def _ensure_tray_deps() -> bool:
    """Auto-install pystray + Pillow if missing. Returns True on success."""
    try:
        import pystray   # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        pass
    logger.info("Installing pystray and Pillow for tray icon support…")
    try:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "pystray", "Pillow"],
            timeout=120,
        )
        return True
    except Exception as exc:
        logger.warning("Could not install tray dependencies: %s", exc)
        return False


def _make_icon_image():
    """Return a 64×64 RGBA PIL Image for the tray icon.

    Uses the custom icon configured on the Settings page, or falls back to
    the built-in blue circle with white 'JT' text.
    """
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    # Try to load the user-configured icon --------------------------------
    icon_path = ""
    icon_size = ""
    try:
        from app import create_app          # type: ignore
        flask_app = create_app()
        with flask_app.app_context():
            from models import Setting      # type: ignore
            icon_path = Setting.get("daemon_icon_path", "")
            icon_size = Setting.get("daemon_icon_size", "")
    except Exception:
        pass

    if icon_path and os.path.isfile(icon_path):
        try:
            src = Image.open(icon_path)
            if src.format == "ICO" and hasattr(src, "ico"):
                sizes = src.ico.sizes()
                target = (64, 64)
                if icon_size:
                    try:
                        w, h = map(int, icon_size.split("x"))
                        target = (w, h)
                    except Exception:
                        pass
                best = min(sizes, key=lambda s: abs(s[0] - target[0]) + abs(s[1] - target[1]))
                src.size = best
                src = src.copy()
            return src.convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            pass  # fall through to default

    # Built-in default: blue circle with 'JT' --------------------------------
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(0, 102, 204, 255))
    text = "JT"
    try:
        font = ImageFont.truetype("arialbd.ttf", 26)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 2), text, fill="white", font=font)
    return img


def run_with_tray() -> None:
    """Start the scan loop in a background thread and show a tray icon."""
    import pystray  # type: ignore

    # Start scan loop in daemon thread
    loop_thread = threading.Thread(target=_scan_loop, name="scan-loop", daemon=True)
    loop_thread.start()

    settings = _read_settings() or {}
    port = settings.get("app_port", "5000")

    def on_scan_now(icon, item):  # noqa: ANN001
        threading.Thread(target=_run_scan_with_timeout, daemon=True).start()

    def on_open(icon, item):  # noqa: ANN001
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")

    def on_exit(icon, item):  # noqa: ANN001
        logger.info("Tray icon exit — stopping daemon.")
        _stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Job Tracker Daemon", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Scan Now",         on_scan_now),
        pystray.MenuItem("Open Job Tracker", on_open),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit",             on_exit),
    )

    icon = pystray.Icon(
        "JobTrackerDaemon",
        _make_icon_image(),
        "Job Tracker — background scanner",
        menu,
    )

    logger.info("Tray icon started — right-click for options.")
    icon.run()          # blocks main thread; exits when on_exit() calls icon.stop()

    # After tray exits, wait for loop thread to finish
    _stop_event.set()
    loop_thread.join(timeout=5)
    _remove_pid()
    logger.info("Daemon exited.")


# ---------------------------------------------------------------------------
# Headless daemon (no tray)
# ---------------------------------------------------------------------------

def run_daemon() -> None:
    """Standard daemon loop on the main thread (no tray icon)."""
    logger.info("Job Tracker Scheduler Daemon starting (PID %d).", os.getpid())
    _write_pid()

    def _handle_stop(sig, frame):  # noqa: ANN001
        logger.info("Stop signal received — shutting down.")
        _stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_stop)

    try:
        _scan_loop()
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard.")
    finally:
        _remove_pid()
        logger.info("Daemon exited.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Job Tracker Scheduler Daemon")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scan immediately and exit.",
    )
    parser.add_argument(
        "--no-tray", action="store_true",
        help="Force headless mode even if 'Show tray icon' is enabled in Settings.",
    )
    args = parser.parse_args()

    if args.once:
        logger.info("Running single scan (--once mode)…")
        _run_scan()
        logger.info("Done.")
        return

    _write_pid()

    # Decide whether to show the tray icon
    use_tray = False
    if not args.no_tray:
        settings = _read_settings()
        use_tray = bool(settings and settings.get("show_tray_icon", True))

    if use_tray:
        if _ensure_tray_deps():
            run_with_tray()
            return
        else:
            logger.warning("Tray dependencies unavailable — falling back to headless mode.")

    run_daemon()


if __name__ == "__main__":
    main()
