"""D2: the roster receipt must name WHY each source failed, and must attempt the
last fallback instead of starving it.

Evidence this repairs (real 2026-10-14 run,
``data/cache/a_share_universe_status.json``)::

    provider_errors:
      sina.spot_direct           RuntimeError:provider_deadline_exhausted
      baostock.query_stock_basic RuntimeError:provider_deadline_exhausted
      stock_info_a_code_name     total_budget_exhausted   (budget 0.0, never attempted)

Every timed-out source recorded the same generic string, so the receipt could not
say whether a source never connected or merely answered too slowly; and the third
source never got a window at all.

All fixtures are synthetic. No network, no real subprocess, no real data tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quantdesk.process_deadline import ProcessDeadlineResult
from scripts import update_a_share_universe as updater

SOURCE = "sina.spot_direct"
BAO = "baostock.query_stock_basic"
AK = "stock_info_a_code_name"


# ------------------------------------------------ failure class separation


@pytest.mark.parametrize(
    "error,expected",
    [
        ("RuntimeError:provider_deadline_exhausted", "time_budget_exhausted"),
        ("TimeoutError:roster_source_deadline_exhausted", "time_budget_exhausted"),
        ("ConnectionError:HTTPSConnectionPool: Max retries exceeded", "connect_or_http_failure"),
        ("requests.exceptions.ProxyError: tunnel connection failed", "connect_or_http_failure"),
        ("HTTPError:HTTP 503 Service Unavailable", "connect_or_http_failure"),
        ("ValueError:provider_empty_or_invalid_symbols", "empty_roster"),
        ("incomplete_roster:pagination_or_response_unfinished", "pagination_incomplete"),
        ("ValueError:provider_attribution_mismatch", "date_or_symbol_validation_failed"),
        ("ValueError:provider_invalid_rows", "date_or_symbol_validation_failed"),
        ("KeyError:'close'", "protocol_or_schema_change"),
        ("RuntimeError:something_nobody_expected", "unknown_failure"),
    ],
)
def test_failure_classes_are_separable(error: str, expected: str) -> None:
    attempt = {"status": "failed", "error": error}
    assert updater._classify_source_failure(attempt, error) == expected
    assert expected in updater.FAILURE_CLASSES


def test_not_attempted_is_its_own_class() -> None:
    attempt = {"status": "not_attempted", "error": "total_budget_exhausted"}
    assert updater._classify_source_failure(attempt, "") == "not_attempted_budget"


# ------------------------------------------- timeout keeps the child's error


class _FakeChild:
    def __init__(self, *, stdout_text: str, parent_timeout: bool) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self._stdout = stdout_text
        self._parent_timeout = parent_timeout
        self._calls = 0
        self.killed = False

    def communicate(self, payload: str | None = None, timeout: float | None = None):
        self._calls += 1
        if self._parent_timeout and self._calls == 1:
            raise subprocess.TimeoutExpired(cmd="fixture", timeout=timeout or 0.0)
        return self._stdout, "QD_ROSTER_PHASE " + json.dumps(
            {"phase": "provider_call", "at": 1.0, "pid": self.pid}
        ) + "\n"

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1  # Windows TerminateProcess


def _run_with_fake_child(monkeypatch, child: _FakeChild) -> dict:
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    clock = [0.0]
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    result = updater._run_roster_source_process(
        updater._universe_source_worker,
        [SOURCE],
        context={"deadline_monotonic": 1000.0},
        max_workers=1,
        timeout_seconds=10.0,
    )
    return result.results[0]


def test_timeout_preserves_the_child_reported_error(monkeypatch) -> None:
    """A real upstream error must not be replaced by the generic deadline text."""
    child = _FakeChild(
        stdout_text=json.dumps({
            "worker_error": "ConnectionError:fixture_dns_failure",
            "rows": [{"symbol": "600000", "source": SOURCE}, {"symbol": "000001", "source": SOURCE}],
        }),
        parent_timeout=True,
    )

    payload = _run_with_fake_child(monkeypatch, child)

    assert payload["worker_error"] == "ConnectionError:fixture_dns_failure"
    assert payload["child_error"] == "ConnectionError:fixture_dns_failure"
    assert payload["timeout_kind"] == "parent_communicate_timeout"
    assert payload["deadline_exhausted"] is True
    assert payload["stdout_recovered"] is True
    # A timed-out child is never a roster source, even if it printed rows.
    assert "rows" not in payload
    assert payload["partial_row_count"] == 2


def test_timeout_without_a_child_error_still_names_the_budget(monkeypatch) -> None:
    child = _FakeChild(stdout_text="", parent_timeout=True)

    payload = _run_with_fake_child(monkeypatch, child)

    assert payload["worker_error"] == "provider_deadline_exhausted"
    assert payload["timeout_kind"] == "parent_communicate_timeout"
    assert payload["stdout_recovered"] is False


def test_worker_watchdog_exit_is_named_separately(monkeypatch) -> None:
    child = _FakeChild(stdout_text="", parent_timeout=False)
    child.returncode = 124  # the worker's own watchdog

    payload = _run_with_fake_child(monkeypatch, child)

    assert payload["timeout_kind"] == "worker_watchdog_exit_124"
    assert payload["worker_error"] == "provider_deadline_exhausted"


# ------------------------------------------------- last source is not starved


def test_last_fallback_source_is_actually_attempted() -> None:
    """The 2026-10-14 shape: two sources burn their windows, the third must run."""
    clock = [0.0]
    calls: list[tuple[str, float]] = []

    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        provider = tasks[0]
        calls.append((provider, timeout_seconds))
        clock[0] += timeout_seconds  # every source consumes its whole window
        return ProcessDeadlineResult({}, {0: "fixture_timeout"}, [0], True, [])

    updater.fetch_universe_from_akshare(runner=run, clock=lambda: clock[0])

    assert [name for name, _ in calls] == [SOURCE, BAO, AK]
    assert calls[2][1] > 0.0, "the last fallback must not be starved"
    assert clock[0] <= 35.0


def test_reserved_slice_does_not_grow_the_total_budget() -> None:
    clock = [0.0]

    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        clock[0] += timeout_seconds
        return ProcessDeadlineResult({}, {0: "fixture_timeout"}, [0], True, [])

    updater.fetch_universe_from_akshare(runner=run, clock=lambda: clock[0], budget_seconds=35.0)

    assert clock[0] <= 35.0


def test_failed_attempts_carry_a_failure_class() -> None:
    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        return ProcessDeadlineResult({}, {0: "RuntimeError:provider_deadline_exhausted"}, [0], True, [])

    rows, _source, _errors = updater.fetch_universe_from_akshare(
        runner=run, clock=lambda: 0.0, providers=[SOURCE]
    )

    assert rows.attempts[0]["failure_class"] == "time_budget_exhausted"


# ------------------------------------------------------- receipt surface


def test_status_reports_one_failure_class_per_source(monkeypatch, tmp_path: Path) -> None:
    rows = updater.UniverseSourceRows()
    rows.attempts.extend([
        {"provider": SOURCE, "budget_seconds": 17.5, "status": "failed",
         "error": "RuntimeError:provider_deadline_exhausted", "failure_class": "time_budget_exhausted"},
        {"provider": BAO, "budget_seconds": 12.0, "status": "failed",
         "error": "RuntimeError:provider_deadline_exhausted", "failure_class": "time_budget_exhausted"},
        {"provider": AK, "budget_seconds": 4.5, "status": "failed",
         "error": "ConnectionError:fixture", "failure_class": "connect_or_http_failure"},
    ])
    monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(updater, "load_config", lambda *a: {})
    monkeypatch.setattr(updater, "fetch_universe_from_akshare",
                        lambda **kwargs: (rows, "", {"a": "b"}))

    args = updater.parse_args([
        "--dry-run", "--output", str(tmp_path / "roster.csv"),
        "--status-path", str(tmp_path / "status.json"),
    ])
    status = updater.run_update_universe(args)

    assert status["provider_error_classes"] == {
        SOURCE: "time_budget_exhausted",
        BAO: "time_budget_exhausted",
        AK: "connect_or_http_failure",
    }
    assert status["ok"] is False and status["full_roster_published"] is False
