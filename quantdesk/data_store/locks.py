from __future__ import annotations

import threading

_GLOBAL_SQLITE_WRITE_LOCK = threading.Lock()


def sqlite_write_lock() -> threading.Lock:
    return _GLOBAL_SQLITE_WRITE_LOCK
