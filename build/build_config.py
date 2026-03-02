"""
build_config.py — Build parameters for Job Tracker.

Values are loaded from build/build_settings.json when it exists, so the
Build Dashboard (start_builder.py) can update them without touching this file.
If build_settings.json is absent the hardcoded defaults below are used.

build.py accesses all settings as module-level constants (C.APP_NAME, etc.)
— that interface is unchanged regardless of where the values come from.
"""

from __future__ import annotations

import json
from pathlib import Path

# ── Locate settings file ──────────────────────────────────────────────────────

BUILD_DIR     = Path(__file__).parent.resolve()
_SETTINGS_FILE = BUILD_DIR / "build_settings.json"

# ── Defaults (edit here if you are NOT using the Build Dashboard) ─────────────

_DEFAULTS: dict = {
    # Application identity
    "APP_NAME":        "Job Tracker",
    "APP_VERSION":     "1.0.0",
    "APP_DESCRIPTION": "Personal job search tracker",
    "APP_PUBLISHER":   "Your Name or Company",
    "APP_URL":         "https://github.com/yourname/job_tracker",
    "APP_SUPPORT_URL": "https://github.com/yourname/job_tracker/issues",

    # Compiled exe name (no extension)
    "APP_EXE_NAME":    "JobTracker",

    # Where the finished installer .exe is written (relative to project root)
    "OUTPUT_DIR":      "dist",

    # Windows installer behaviour
    "DEFAULT_INSTALL_DIR": r"{autopf}\JobTracker",
    "REQUIRE_ADMIN":   True,
    "DESKTOP_ICON":    True,
    "START_MENU_ICON": True,
    "ADD_TO_STARTUP":  False,

    # Optional asset paths (relative to project root; "" = skip)
    "ICON_FILE":    "",
    "LICENSE_FILE": "",

    # Code protection — mutually exclusive
    # False / False  → plain PyInstaller (default)
    # True  / False  → PyInstaller + PyArmor (AES-256 encrypted bytecode)
    # False / True   → Nuitka (Python → C → native machine code)
    "USE_PYARMOR": False,
    "USE_NUITKA":  False,
}

# ── Load settings ─────────────────────────────────────────────────────────────

def _load() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            overrides = json.loads(_SETTINGS_FILE.read_text("utf-8"))
            return {**_DEFAULTS, **overrides}
        except Exception:
            pass
    return dict(_DEFAULTS)

_cfg = _load()

# ── Public constants (same names as before — build.py uses these) ─────────────

APP_NAME            = _cfg["APP_NAME"]
APP_VERSION         = _cfg["APP_VERSION"]
APP_DESCRIPTION     = _cfg["APP_DESCRIPTION"]
APP_PUBLISHER       = _cfg["APP_PUBLISHER"]
APP_URL             = _cfg["APP_URL"]
APP_SUPPORT_URL     = _cfg["APP_SUPPORT_URL"]

APP_EXE_NAME        = _cfg["APP_EXE_NAME"]
OUTPUT_DIR          = _cfg["OUTPUT_DIR"]
DEFAULT_INSTALL_DIR = _cfg["DEFAULT_INSTALL_DIR"]

REQUIRE_ADMIN       = bool(_cfg["REQUIRE_ADMIN"])
DESKTOP_ICON        = bool(_cfg["DESKTOP_ICON"])
START_MENU_ICON     = bool(_cfg["START_MENU_ICON"])
ADD_TO_STARTUP      = bool(_cfg["ADD_TO_STARTUP"])

ICON_FILE           = _cfg["ICON_FILE"]    or None
LICENSE_FILE        = _cfg["LICENSE_FILE"] or None

USE_PYARMOR         = bool(_cfg["USE_PYARMOR"])
USE_NUITKA          = bool(_cfg["USE_NUITKA"])

# ── Hidden imports (not editable from the dashboard — too technical) ──────────
# Modules that PyInstaller / Nuitka may fail to detect automatically because
# they are loaded dynamically at runtime.

HIDDEN_IMPORTS: list[str] = _cfg.get("HIDDEN_IMPORTS", [
    "email.mime.text",
    "email.mime.multipart",
    "email.mime.base",
    "email.mime.application",
    "email.encoders",
    "pkg_resources.py2_warn",
    "sqlalchemy.dialects.sqlite",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.executors.pool",
    "jinja2.ext",
    "feedparser",
    "bs4",
    "icalendar",
    "pypdf",
    "docx",
    "pytz",
    "PIL._tkinter_finder",
    "ctypes.wintypes",
    "winreg",
    "google.auth.transport.requests",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "msal",
])

# Additional (source_path, dest_in_bundle) data file pairs beyond
# templates/ and static/ which are included automatically.
EXTRA_DATA: list[tuple[str, str]] = _cfg.get("EXTRA_DATA", [])
