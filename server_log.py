"""
server_log.py — In-memory ring-buffer logging handler.

Attaches to the root logger so every Python log record emitted by any
module is captured and can be served to the browser without a separate
terminal window.

Capture modes
-------------
  "off"  : handler level is set to 9999 — no records pass through at all.
            The buffer is cleared immediately when switching to this mode.
            Zero memory is consumed by new log activity.
  "warn" : handler level = WARNING.  Only WARNING / ERROR / CRITICAL records
            are stored.  Minimal memory use.
  "all"  : handler level = DEBUG.  Every record is stored (up to maxlen).

Default is "off" — the user must explicitly enable capture on the Server
Log page.  This ensures the app does not waste memory on log records
until the user actually wants to inspect them.
"""
from __future__ import annotations

import collections
import logging
import threading

_lock    = threading.Lock()
_records: collections.deque = collections.deque(maxlen=500)  # hard cap

# Timestamp of the most-recently captured record (0 = nothing yet)
_latest_ts: float = 0.0

# Capture mode: "off" | "warn" | "all"
_capture_mode: str = "off"

# Handler level per mode — "off" uses a level above CRITICAL so Python's
# own logging framework filters records before they ever reach emit().
_LEVEL_MAP: dict[str, int] = {
    "off":  9999,
    "warn": logging.WARNING,
    "all":  logging.DEBUG,
}


class _RingHandler(logging.Handler):
    """Append formatted log records to the shared in-memory deque."""

    def emit(self, record: logging.LogRecord) -> None:
        global _latest_ts
        try:
            msg = self.format(record)
            with _lock:
                _records.append({
                    "ts":    record.created,       # float Unix timestamp
                    "level": record.levelname,     # DEBUG / INFO / WARNING / ERROR / CRITICAL
                    "name":  record.name,          # logger name (module)
                    "msg":   msg,                  # fully-formatted line
                })
                if record.created > _latest_ts:
                    _latest_ts = record.created
        except Exception:
            pass


_handler = _RingHandler()
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)
# Start in "off" mode — no records pass through until the user enables capture.
_handler.setLevel(_LEVEL_MAP["off"])


def init_server_log() -> None:
    """
    Attach the ring-buffer handler to the root logger.
    Safe to call multiple times (guard prevents duplicate handlers).
    Call once at app startup, after logging.basicConfig().
    """
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)


def set_capture_mode(mode: str) -> None:
    """
    Change the capture mode.  Valid values: "off", "warn", "all".
    Unknown values are silently coerced to "off".

    Side-effects
    ------------
    - "off"  : clears the buffer immediately (frees memory).
    - "warn" : sets handler level to WARNING; if transitioning from "all",
               trims INFO/DEBUG entries already in the buffer.
    - "all"  : sets handler level to DEBUG.
    """
    global _capture_mode, _latest_ts
    if mode not in _LEVEL_MAP:
        mode = "off"
    old_mode = _capture_mode
    _capture_mode = mode
    _handler.setLevel(_LEVEL_MAP[mode])

    if mode == "off":
        with _lock:
            _records.clear()
            _latest_ts = 0.0
    elif mode == "warn" and old_mode == "all":
        # Drop INFO/DEBUG entries that are no longer relevant
        with _lock:
            keep = [
                r for r in _records
                if r["level"] in ("WARNING", "ERROR", "CRITICAL")
            ]
            _records.clear()
            _records.extend(keep)


def get_capture_mode() -> str:
    """Return the current capture mode string."""
    return _capture_mode


def get_log_entries(since: float = 0.0) -> list[dict]:
    """
    Return all captured log records with ts > *since*.
    Pass the ts of the last record the client received to get only new ones.
    """
    with _lock:
        return [dict(r) for r in _records if r["ts"] > since]


def get_latest_ts() -> float:
    with _lock:
        return _latest_ts


def clear_log() -> None:
    """Discard all buffered log records."""
    global _latest_ts
    with _lock:
        _records.clear()
        _latest_ts = 0.0
