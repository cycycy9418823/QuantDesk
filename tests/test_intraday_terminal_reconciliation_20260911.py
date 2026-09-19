"""2026-09-11 runtime regressions: minute-provider cause + stuck-round terminal.

Evidence: the 15:00 round recorded 89/89 ``provider_failed:mootdx:
TdxFunctionCallError`` with only "calling function error" (the wrapped cause was
discarded), and the 15:05 round wrote a ``running`` marker then disappeared
(task exit 0x41306).  Being the last invocation of the day, ``current`` kept
showing a dead PID as running.  These tests pin the cause-surfacing and the
stale-round reconciliation.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quantdesk.intraday_refresh import collector
from quantdesk.intraday_refresh.collector import (
    collect_intraday_refresh,
    reconcile_abandoned_round,
    _mootdx_minute_rows,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT_REL = Path("results/intraday/intraday_refresh_current.json")
LATEST_REL = Path("results/intraday/intraday_refresh_latest.json")


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 11, hour, minute, tzinfo=SHANGHAI)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "state" / "ai_trader").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results" / "intraday").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {"600000": {"qty": 100}}}), encoding="utf-8"
    )
    (tmp_path / "state" / "ai_trader" / "plan_latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "ai_trader" / "watchlist.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "universe" / "a_share_symbols.csv").write_text(
        "symbol\n600000\n", encoding="utf-8"
    )
    return tmp_path


def _seed_running(root: Path, *, runtime_pid: int | None, run_id: str = "intraday-killed") -> None:
    marker = {
        "run_id": run_id,
        "run_state": "running",
        "status": "ready",
        "as_of": "15:05:02",
        "finished_at": "",
        "reason_codes": [],
    }
    if runtime_pid is not None:
        marker["runtime_pid"] = runtime_pid
    (root / CURRENT_REL.parent).mkdir(parents=True, exist_ok=True)
    (root / CURRENT_REL).write_text(json.dumps(marker), encoding="utf-8")


class _Wrapped(Exception):
    def __init__(self, message: str, original_exception: BaseException | None = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class _Client:
    def settimeout(self, value: float) -> None:  # noqa: D401 - recorded but unused
        self.timeout = value

    def bars(self, **_kwargs):
        raise _Wrapped("calling function error", ConnectionError("socket reset by peer"))

    def close(self) -> None:
        return None


def test_mootdx_wrapped_cause_is_surfaced() -> None:
    rows, error, diagnostics = _mootdx_minute_rows(
        "600000",
        offset=0,
        now=_now(15, 0),
        client_factory=_Client,
    )
    assert rows == []
    assert error == "provider_failed:mootdx:_Wrapped"
    assert diagnostics["error_stage"] == "request"
    # The generic "calling function error" alone is not enough.
    assert "socket reset by peer" in diagnostics["message"]
    assert diagnostics["original_exception"]
    assert "socket reset by peer" in diagnostics["original_exception"]


def test_reconcile_dead_pid_publishes_abandoned(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _seed_running(root, runtime_pid=999999)
    (root / LATEST_REL).write_text(
        json.dumps({"run_id": "older", "run_state": "completed", "status": "blocked"}),
        encoding="utf-8",
    )

    result = reconcile_abandoned_round(
        root, now=_now(15, 10), process_alive=lambda pid: False
    )

    assert result["reconciled"] is True
    marker = json.loads((root / CURRENT_REL).read_text(encoding="utf-8"))
    assert marker["run_state"] == "abandoned"
    assert marker["status"] == "aborted"
    assert marker["finished_at"]
    assert "previous_run_process_gone" in marker["reason_codes"]
    # latest is "most recent attempt": the dead round is now explicit, not running.
    latest = json.loads((root / LATEST_REL).read_text(encoding="utf-8"))
    assert latest["run_state"] == "abandoned"


def test_reconcile_live_pid_is_a_noop(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _seed_running(root, runtime_pid=1234)

    result = reconcile_abandoned_round(
        root, now=_now(15, 10), process_alive=lambda pid: True
    )

    assert result["reconciled"] is False
    assert result["reason"] == "process_alive"
    marker = json.loads((root / CURRENT_REL).read_text(encoding="utf-8"))
    assert marker["run_state"] == "running"


def test_reconcile_without_pid_is_a_noop(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _seed_running(root, runtime_pid=None)
    result = reconcile_abandoned_round(root, now=_now(15, 10), process_alive=lambda pid: False)
    assert result["reconciled"] is False
    assert result["reason"] == "runtime_pid_unknown"
    marker = json.loads((root / CURRENT_REL).read_text(encoding="utf-8"))
    assert marker["run_state"] == "running"


def test_collect_reconciles_before_the_round(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        collector,
        "reconcile_abandoned_round",
        lambda *a, **k: seen.append(k) or {"reconciled": False},
    )
    collect_intraday_refresh(
        root,
        network=False,
        now=_now(15, 10),
        trading_day=True,
        snapshot_fetcher=lambda: ([], "fixture.snapshot"),
    )
    assert seen, "a writing round must reconcile a previous dead round first"


def test_collect_read_only_round_does_not_reconcile(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        collector,
        "reconcile_abandoned_round",
        lambda *a, **k: seen.append(k) or {"reconciled": False},
    )
    collect_intraday_refresh(
        root,
        network=False,
        now=_now(15, 10),
        trading_day=True,
        snapshot_fetcher=lambda: ([], "fixture.snapshot"),
        write_manifest=False,
    )
    assert not seen


def test_outside_window_round_publishes_a_terminal_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=_now(15, 10),
        trading_day=True,
        snapshot_fetcher=lambda: ([], "fixture.snapshot"),
    )
    assert manifest["status"] == "closed"
    assert manifest["run_state"] == "skipped"
    assert manifest["reason_codes"] == ["outside_collection_window"]
    assert manifest["finished_at"]
    marker = json.loads((root / CURRENT_REL).read_text(encoding="utf-8"))
    assert marker["run_state"] == "skipped"
    assert marker["finished_at"]


# ---------------------------------------------------------------------------
# Review R1 (2026-09-11): MIN-01 stale-reconcile overwrite, MIN-02 Windows probe.
# ---------------------------------------------------------------------------


def test_reconcile_does_not_overwrite_a_round_that_finished_before_lock(
    tmp_path: Path, monkeypatch
) -> None:
    from contextlib import contextmanager

    root = _root(tmp_path)
    _seed_running(root, runtime_pid=999999, run_id="old")
    newer = {
        "run_id": "newer",
        "run_state": "completed",
        "status": "blocked",
        "runtime_pid": 999998,
        "finished_at": "2026-09-11T15:10:01",
    }
    (root / LATEST_REL).write_text(json.dumps(newer), encoding="utf-8")

    @contextmanager
    def another_round_finishes_before_lock(_root):
        # A newer round completes in the window between the pre-lock read and
        # acquiring the lock.
        (root / CURRENT_REL).write_text(json.dumps(newer), encoding="utf-8")
        (root / LATEST_REL).write_text(json.dumps(newer), encoding="utf-8")
        yield

    monkeypatch.setattr(collector, "_intraday_refresh_lock", another_round_finishes_before_lock)
    result = reconcile_abandoned_round(root, now=_now(15, 11), process_alive=lambda pid: False)

    assert result["reconciled"] is False
    assert json.loads((root / CURRENT_REL).read_text(encoding="utf-8"))["run_id"] == "newer"
    assert json.loads((root / LATEST_REL).read_text(encoding="utf-8"))["run_id"] == "newer"


def _windows_fake_kernel32(open_result: int, last_error: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        OpenProcess=lambda *_args: open_result,
        GetLastError=lambda: last_error,
        CloseHandle=lambda _handle: 1,
    )


def _monkeypatch_windows(monkeypatch, open_result: int, last_error: int) -> None:
    import ctypes
    from types import SimpleNamespace

    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=_windows_fake_kernel32(open_result, last_error)),
        raising=False,
    )
    monkeypatch.setattr(collector, "os", SimpleNamespace(name="nt"))


def test_windows_access_denied_is_not_process_death(monkeypatch) -> None:
    _monkeypatch_windows(monkeypatch, 0, 5)  # ERROR_ACCESS_DENIED -> undeterminable
    assert collector._process_alive(999999) is not False


def test_windows_invalid_parameter_is_process_death(monkeypatch) -> None:
    _monkeypatch_windows(monkeypatch, 0, 87)  # ERROR_INVALID_PARAMETER -> gone
    assert collector._process_alive(999999) is False


def test_windows_open_handle_means_alive(monkeypatch) -> None:
    _monkeypatch_windows(monkeypatch, 1234, 0)
    assert collector._process_alive(999999) is True
