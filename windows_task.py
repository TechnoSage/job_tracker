"""
windows_task.py — Auto-start the scheduler daemon at Windows user login.

Uses the HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run registry key
(the same mechanism used by Discord, Slack, Spotify, etc.) so that no
administrator rights are ever required.

The previous schtasks-based approach required elevated privileges on most
Windows 10/11 configurations and produced "Access denied" errors.
"""

from __future__ import annotations

import os
import sys
import winreg

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
DAEMON_SCRIPT = os.path.join(PROJECT_DIR, "scheduler_daemon.py")

_RUN_KEY             = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DEFAULT_VALUE_NAME  = "JobTracker_SchedulerDaemon"


def _python_exe() -> str:
    """
    Prefer pythonw.exe (no console window) beside the running interpreter.
    Falls back to the regular python.exe if pythonw is not found.
    """
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw if os.path.isfile(pythonw) else sys.executable


def is_task_installed(value_name: str = _DEFAULT_VALUE_NAME) -> bool:
    """Return True if the auto-start registry entry exists."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, value_name)
            return True
    except OSError:
        return False


def install_task(
    launcher_exe: str | None = None,
    value_name: str = _DEFAULT_VALUE_NAME,
) -> tuple[bool, str]:
    """
    Write a Run registry entry so the daemon launches at user login.
    Pass launcher_exe to use a custom-named copy of python(w).exe so the
    process appears with that name in Task Manager.
    Pass value_name to control the label shown in Windows Startup apps.
    No administrator rights required.
    Returns (success, message).
    """
    python = launcher_exe or _python_exe()
    cmd = f'"{python}" "{DAEMON_SCRIPT}"'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY,
            access=winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, cmd)
        return True, "Auto-start entry added. The daemon will start automatically at your next Windows login."
    except Exception as exc:
        return False, str(exc)


def uninstall_task(value_name: str = _DEFAULT_VALUE_NAME) -> tuple[bool, str]:
    """Remove the auto-start registry entry. Returns (success, message)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY,
            access=winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, value_name)
        return True, "Auto-start entry removed."
    except FileNotFoundError:
        return True, "Auto-start entry was not installed."
    except Exception as exc:
        return False, str(exc)
