"""Cross-platform process lock for the market-data update task.

v2.6.7 stability patch.  Multiple ``update_market_data.py`` processes
running at the same time clobber each other when writing
``data/cache/update_status.json`` and
``data/cache/update_status_running.json``.  The UI progress polling
then sees interleaved counters and can mislead the user.

This module provides a small file-based advisory lock:

- ``acquire_update_lock(lock_path, command)`` writes a JSON file with
  the current ``pid``, ``started_at``, and ``command`` only when no
  live process already owns the lock.  Stale locks whose pid is no
  longer alive are cleared automatically.
- ``release_update_lock(lock_path)`` removes the lock file if it
  exists; safe to call multiple times.
- ``read_update_lock(lock_path)`` returns the parsed JSON payload, or
  ``None`` if the lock file is missing or unreadable.
- ``is_update_lock_held(lock_path)`` returns ``True`` only when the
  lock file exists *and* its pid is still running on this host.
- ``UpdateLockHeld`` is raised by ``acquire_update_lock`` when another
  live process owns the lock; the exception carries the existing lock
  payload so callers can produce a clear error message.

The lock is intentionally lightweight and does *not* try to be a
distributed lock.  It only protects the local single-machine case the
project actually runs in.  No new third-party dependency is added.
"""

from __future__ import annotations

import json
import os
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

from quantdesk.time_utils import now_text

DEFAULT_LOCK_FILENAME = "update_market_data.lock"


class UpdateLockHeld(RuntimeError):
    """Raised when another live process already owns the update lock."""

    def __init__(self, lock_info: dict[str, Any]) -> None:
        self.lock_info = dict(lock_info)
        pid = self.lock_info.get("pid")
        started_at = self.lock_info.get("started_at") or "unknown"
        super().__init__(
            f"another update_market_data task is running (pid={pid}, started_at={started_at})"
        )


_now_text = partial(now_text, sep=" ")


def is_pid_alive(pid: int | None) -> bool:
    """Return ``True`` if ``pid`` looks like a live process on this host.

    On POSIX this uses ``os.kill(pid, 0)``.  On Windows we use the
    ``OpenProcess`` Win32 API via ``ctypes`` and treat ``ERROR_INVALID_PARAMETER``
    (87) as "no such pid".  Falls back conservatively to ``False`` when the
    pid is missing or invalid so a stale lock is always safe to clear.
    """

    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        except Exception:
            return False
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            still_active = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not still_active:
                return False
            # 259 == STILL_ACTIVE.  Anything else means the process has exited
            # but the handle was still cached.
            return int(exit_code.value) == 259
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_update_lock(lock_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    path = Path(lock_path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_update_lock_held(lock_path: str | os.PathLike[str]) -> bool:
    info = read_update_lock(lock_path)
    if not info:
        return False
    pid = info.get("pid")
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_int = None
    return is_pid_alive(pid_int)


def clear_stale_lock(lock_path: str | os.PathLike[str]) -> bool:
    """Remove the lock file if it exists and its pid is no longer alive.

    Returns ``True`` when a stale lock was cleared, ``False`` otherwise.
    Never raises for missing files.
    """

    info = read_update_lock(lock_path)
    if info is None:
        return False
    pid = info.get("pid")
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_int = None
    if is_pid_alive(pid_int):
        return False
    release_update_lock(lock_path)
    return True


def acquire_update_lock(
    lock_path: str | os.PathLike[str],
    command: str = "",
    *,
    pid: int | None = None,
) -> dict[str, Any]:
    """Try to acquire the update lock.

    If a live process already owns the lock, raises ``UpdateLockHeld``
    with the existing payload attached.  Stale locks (whose pid is no
    longer alive) are cleared first.
    """

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_update_lock(path)
    if existing is not None:
        existing_pid_raw = existing.get("pid")
        try:
            existing_pid = int(existing_pid_raw) if existing_pid_raw is not None else None
        except (TypeError, ValueError):
            existing_pid = None
        if is_pid_alive(existing_pid):
            raise UpdateLockHeld(existing)
        # stale lock -> clear and continue
        try:
            path.unlink()
        except OSError:
            pass
    payload = {
        "pid": int(pid if pid is not None else os.getpid()),
        "started_at": _now_text(),
        "command": str(command or ""),
        "hostname": _safe_hostname(),
        "epoch": int(time.time()),
    }
    # Best-effort atomic write: write to .tmp then replace.
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    return payload


def release_update_lock(lock_path: str | os.PathLike[str]) -> bool:
    """Remove the lock file if it exists.  Idempotent."""

    path = Path(lock_path)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _safe_hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return ""


__all__ = [
    "DEFAULT_LOCK_FILENAME",
    "UpdateLockHeld",
    "acquire_update_lock",
    "clear_stale_lock",
    "is_pid_alive",
    "is_update_lock_held",
    "read_update_lock",
    "release_update_lock",
]
