"""Operation-log sink isolation helper for tests (Round 12 §34.3b).

Tests that spawn ``scripts/qd.py`` subprocesses (or call
``quantdesk.operation_log`` writers with their default sink paths) must call
``isolate_operation_log_dir()`` once at module import time so the real
``results/operation_log/operation_timeline.jsonl`` and
``latest_operation_status.json`` are never appended to from tests.

``quantdesk.operation_log`` resolves ``QUANTDESK_OPERATION_LOG_DIR`` lazily
(only when the default sink paths are used), so the redirect applies to every
qd.py subprocess that inherits this process environment while explicit
per-test log paths keep their exact destination.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_OPERATION_LOG_DIR: Path | None = None


def isolate_operation_log_dir() -> Path:
    """Idempotently redirect the operation-log sink to a fresh temp dir."""
    global _OPERATION_LOG_DIR
    if _OPERATION_LOG_DIR is None:
        _OPERATION_LOG_DIR = Path(
            tempfile.mkdtemp(prefix="qd_test_operation_log_")
        ).resolve()
        os.environ["QUANTDESK_OPERATION_LOG_DIR"] = str(_OPERATION_LOG_DIR)
    return _OPERATION_LOG_DIR


def operation_log_timeline_path() -> Path:
    """Path of the isolated timeline sink (created on demand by the writer)."""
    return isolate_operation_log_dir() / "operation_timeline.jsonl"
