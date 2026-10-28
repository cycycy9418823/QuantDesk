"""Business dates survive catch-up launches; all writes use detached fixtures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from ai_trader import cycle, market, overnight
from ai_trader.ledger import empty_ledger, save_ledger
from ai_trader.paths import CyclePaths, ledger_id_for_path
from quantdesk.daily_review_orchestrator import run_evening_review
from quantdesk.interprocess_lock import interprocess_file_lock
from scripts import run_daily_review_orchestrator as review_cli

TODAY = "2026-10-22"
YESTERDAY = "2026-10-21"


def _json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _chain(root: Path, day: str, run_id: str) -> tuple[Path, Path]:
    latest = root / "results/data_chain/daily_data_chain_latest.json"
    metadata = root / "state/daily_data_chain.lock.json"
    _json(latest, {"authoritative": True, "task_type": "daily_data_chain",
                   "target_date": day, "run_id": run_id, "status": "completed"})
    _json(metadata, {"run_id": run_id, "status": "released"})
    return latest, metadata


def _hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("prior_status", ["released", "abandoned", "running"])
def test_review_wait_releases_producer_lock_and_does_not_rewrite_old_metadata(
    tmp_path: Path, monkeypatch, prior_status: str,
) -> None:
    latest, metadata = _chain(tmp_path, YESTERDAY, "old-run")
    if prior_status != "released":
        _json(metadata, {"run_id": "old-run", "target_date": YESTERDAY,
                         "pid": 987654, "status": prior_status,
                         "recovery_required": True,
                         "abandoned_reason": "producer_pid_not_alive"})
    original = metadata.read_bytes()
    monkeypatch.setattr(review_cli, "is_pid_alive", lambda _pid: False)
    polls: list[str] = []

    def publish_after_wait(_seconds: float) -> None:
        assert metadata.read_bytes() == original
        with interprocess_file_lock(tmp_path / "state/daily_data_chain.lock", timeout_seconds=0):
            _chain(tmp_path, TODAY, "today-run")
        polls.append("published")

    import time
    monkeypatch.setattr(time, "sleep", publish_after_wait)
    with review_cli.locked_authoritative_chain_event(
        project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
        timeout_seconds=1, poll_seconds=0.001,
    ) as selected:
        assert selected["target_date"] == TODAY
        assert selected["run_id"] == "today-run"
        with pytest.raises(TimeoutError, match="interprocess_file_lock_timeout"):
            with interprocess_file_lock(tmp_path / "state/daily_data_chain.lock", timeout_seconds=0):
                pass
    assert polls == ["published"]


def test_default_review_defers_yesterday_without_creating_a_review_receipt(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    latest, metadata = _chain(tmp_path, YESTERDAY, "yesterday-run")
    before = {p: p.read_bytes() for p in (latest, metadata)}
    monkeypatch.setattr(review_cli, "resolve_target_complete_trade_date",
                        lambda **_kwargs: {"target_date": TODAY})
    calls: list[dict] = []
    monkeypatch.setattr(review_cli, "run_evening_review", lambda **kw: calls.append(kw))
    assert review_cli.main(["--project-root", str(tmp_path), "--chain-wait-seconds", "0",
                            "--json", "true"]) == 4
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "deferred"
    assert result["target_date"] == TODAY
    assert result["observed_target_date"] == YESTERDAY
    assert result["daily_executed"] is False
    assert calls == []
    assert not (tmp_path / "state/daily_review_runs").exists()
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert "daily_data_chain_wait_timeout" in output.err


@pytest.mark.parametrize("metadata,reason", [
    ({"status": "unrecognized", "run_id": "today"}, "daily_data_chain_lock_status_invalid"),
    ({"status": "abandoned"}, "daily_data_chain_abandoned_identity_invalid"),
    ({"status": "running", "pid": 123, "run_id": "today", "target_date": TODAY},
     "daily_data_chain_metadata_running_without_os_lock"),
    ({"status": "released", "run_id": "other"}, "daily_data_chain_terminal_manifest_mismatch"),
])
def test_review_unknown_or_inconsistent_lock_is_never_a_retryable_data_gap(
    tmp_path: Path, monkeypatch, metadata: dict, reason: str,
) -> None:
    latest, lock_metadata = _chain(tmp_path, TODAY, "today")
    _json(lock_metadata, metadata)
    monkeypatch.setattr(review_cli, "is_pid_alive", lambda _pid: True)
    with pytest.raises(RuntimeError, match=reason):
        with review_cli.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
            timeout_seconds=0,
        ):
            pytest.fail("must not consume inconsistent producer state")


def test_review_does_not_relabel_a_child_timeout_as_missing_data(tmp_path: Path) -> None:
    latest, _ = _chain(tmp_path, TODAY, "today")
    with pytest.raises(TimeoutError, match="child_timeout"):
        with review_cli.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
            timeout_seconds=0,
        ):
            raise TimeoutError("child_timeout")


def test_review_corrupt_metadata_is_visible_and_left_unchanged(tmp_path: Path) -> None:
    latest, metadata = _chain(tmp_path, TODAY, "today-run")
    metadata.write_bytes(b'{"status":')
    with pytest.raises(RuntimeError, match="daily_data_chain_lock_unreadable"):
        with review_cli.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
            timeout_seconds=0,
        ):
            pytest.fail("corrupt metadata is not ready")
    assert metadata.read_bytes() == b'{"status":'


def test_review_never_falls_forward_to_future_manifest(tmp_path: Path) -> None:
    latest, _ = _chain(tmp_path, "2026-10-23", "future-run")
    with pytest.raises(RuntimeError, match="daily_data_chain_target_date_in_future"):
        with review_cli.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
            timeout_seconds=0,
        ):
            pytest.fail("tomorrow is not today")


def test_abandoned_current_day_does_not_consume_an_old_success_for_same_date(tmp_path: Path) -> None:
    latest, metadata = _chain(tmp_path, TODAY, "earlier-success")
    _json(metadata, {"status": "abandoned", "run_id": "failed-retry", "target_date": TODAY})
    before = metadata.read_bytes()
    with pytest.raises(review_cli.DailyChainWaitTimeout, match="producer_abandoned"):
        with review_cli.locked_authoritative_chain_event(
            project_root=tmp_path, manifest_path=latest, expected_target_date=TODAY,
            timeout_seconds=0,
        ):
            pytest.fail("the terminal manifest belongs to a different producer event")
    assert metadata.read_bytes() == before


def test_explicit_historical_manifest_remains_an_explicit_review(
    tmp_path: Path, monkeypatch,
) -> None:
    latest, _ = _chain(tmp_path, YESTERDAY, "historical-run")
    observed: list[dict] = []

    def run_review(**kwargs):
        observed.append(kwargs)
        return {"state": "daily_completed", "event_key": "historical"}

    monkeypatch.setattr(review_cli, "run_evening_review", run_review)
    assert review_cli.main(["--project-root", str(tmp_path), "--chain-manifest", str(latest),
                            "--chain-wait-seconds", "0"]) == 0
    assert observed[0]["target_date"] == YESTERDAY


def test_review_api_checks_requested_date_before_any_runtime_write(tmp_path: Path) -> None:
    calls: list[str] = []
    with pytest.raises(ValueError, match="review_target_date_mismatch"):
        run_evening_review(project_root=tmp_path, target_date=YESTERDAY,
                           expected_target_date=TODAY, authoritative_chain_run_id="old",
                           trigger="scheduled_daily_review", daily_runner=lambda: calls.append("daily") or 0)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_review_after_wait_runs_daily_only_once_for_the_date(tmp_path: Path) -> None:
    calls: list[str] = []
    kwargs = dict(project_root=tmp_path, target_date=TODAY, expected_target_date=TODAY,
                  trigger="scheduled_daily_review", daily_runner=lambda: calls.append("daily") or 0)
    first = run_evening_review(authoritative_chain_run_id="first", **kwargs)
    second = run_evening_review(authoritative_chain_run_id="retry", **kwargs)
    assert first["daily_executed"] is True
    assert second["daily_executed"] is False
    assert calls == ["daily"]


@pytest.mark.parametrize("held_byte", [0, 1])
def test_daily_price_reader_checks_parent_and_orphan_writer_without_changing_lock(
    tmp_path: Path, held_byte: int,
) -> None:
    from scripts.run_data_close_pipeline import _OSLockHandle
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    lock = tmp_path / "data/.locks/data_close_pipeline.lock.json"
    lock.parent.mkdir()
    lock.write_bytes(b"00")
    handle = _OSLockHandle(lock, existing=True, offset=held_byte)
    handle.acquire()
    try:
        with pytest.raises(market.DailyPriceWriterBusy, match="daily_price_writer_active"):
            with market.locked_daily_price_reader(daily):
                pytest.fail("a live writer byte is still held")
    finally:
        handle.release()
    with market.locked_daily_price_reader(daily) as evidence:
        assert evidence["status"] == "locked"
    assert lock.read_bytes() == b"00"


def test_daily_price_reader_does_not_create_a_producer_lock_for_detached_data(tmp_path: Path) -> None:
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    with market.locked_daily_price_reader(daily) as evidence:
        assert evidence["status"] == "absent"
    assert not (tmp_path / "data/.locks").exists()


def _runtime(root: Path, *, profile: str = "") -> CyclePaths:
    state = root / "state" / ("ai_trader" + ("_" + profile if profile else ""))
    results = root / "results" / state.name
    values = {}
    for item in fields(CyclePaths):
        default = getattr(CyclePaths(), item.name)
        if item.name == "daily_dir":
            values[item.name] = root / "data/daily"
        elif item.name == "minute_dir":
            values[item.name] = root / "data/intraday/1m"
        else:
            parent = results if item.name in {"cycle_latest", "snapshot", "decision_page", "decision_page_md"} else state
            values[item.name] = parent / default.name
    return CyclePaths(**values)


def _bars(runtime: CyclePaths, *, include_today: bool, symbol: str = "600500") -> None:
    runtime.daily_dir.mkdir(parents=True, exist_ok=True)
    rows = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,fetched_at",
            f"{YESTERDAY},{symbol},10,10.2,9.8,10,100000,1000000,0,raw,deterministic_fixture,{YESTERDAY}T15:30:00+08:00"]
    if include_today:
        rows.append(f"{TODAY},{symbol},10.1,10.5,10,10.4,100000,1040000,4,raw,deterministic_fixture,{TODAY}T15:30:00+08:00")
    (runtime.daily_dir / (symbol + ".csv")).write_text("\n".join(rows) + "\n", encoding="utf-8")


def _seed(runtime: CyclePaths) -> None:
    ledger = empty_ledger(ledger_id=ledger_id_for_path(runtime.ledger))
    ledger["as_of"] = YESTERDAY
    save_ledger(ledger, runtime.ledger)
    _json(runtime.plan_latest, {
        "schema_version": "ai_trader_plan_v1", "plan_id": "original-yesterday-plan",
        "ledger_id": ledger["ledger_id"], "decision_date": YESTERDAY, "trade_date": TODAY,
        "recorded_at": YESTERDAY + "T20:20:00+08:00", "status": "planned", "committed": True,
        "intents": [{"symbol": "600500", "side": "buy", "quantity": 100,
                     "ref_close": 10.0, "style": "波段", "hold_days": 5, "reason": "fixture"}],
        "llm": {"used": False, "unavailable_reason": "original_model_unavailable"},
    })
    _json(runtime.journal_latest, {"as_of": YESTERDAY, "headline": "original judgment"})


def _offline(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_: {"as_of": TODAY})
    monkeypatch.setattr(overnight, "load_recommendations", lambda **_: {"symbols": [], "warnings": []})
    monkeypatch.setattr(overnight, "propose_picks", lambda *_args, **_kwargs: calls.append("model_stub") or None)
    return calls


@pytest.mark.parametrize("profile", ["", "glm"])
def test_current_evening_with_yesterday_bars_preserves_original_account_and_judgment(
    tmp_path: Path, monkeypatch, profile: str,
) -> None:
    runtime = _runtime(tmp_path, profile=profile)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    before = _hashes(tmp_path)
    result = overnight.run_evening(paths=runtime, network_news=False, require_current_data=True,
                                   target_date=TODAY, data_wait_seconds=0)
    assert result["ok"] is False
    assert result["status"] == "deferred"
    assert result["as_of"] == TODAY
    assert result["observed_as_of"] == YESTERDAY
    assert result["ledger"]["as_of"] == YESTERDAY
    assert result["actions"] == []
    assert calls == []
    assert _hashes(tmp_path) == before


def test_cycle_deferral_is_not_a_successful_scheduler_exit(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    with pytest.raises(overnight.DailySessionNotReady, match="ai_trader_daily_data_not_ready"):
        cycle.run_cycle(paths=runtime, network=False, target_date=TODAY,
                        require_current_data=True, data_wait_seconds=0)
    assert calls == []


def test_managed_daily_default_keeps_today_instead_of_rolling_back(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    monkeypatch.setattr(overnight, "DEFAULT_DAILY_DIR", runtime.daily_dir)
    result = overnight.run_evening(paths=runtime, network_news=False, data_wait_seconds=0)
    assert result["status"] == "deferred"
    assert result["as_of"] == TODAY
    assert calls == []


def test_managed_daily_cannot_opt_out_to_yesterdays_business_date(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(overnight, "DEFAULT_DAILY_DIR", runtime.daily_dir)
    with pytest.raises(ValueError, match="managed_daily_requires_current_session"):
        overnight.run_evening(paths=runtime, require_current_data=False, network_news=False)
    assert list(tmp_path.iterdir()) == []


def test_ai_wait_releases_writer_bytes_then_reads_new_daily_data(tmp_path: Path, monkeypatch) -> None:
    from scripts.run_data_close_pipeline import _OSLockHandle
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    lock = tmp_path / "data/.locks/data_close_pipeline.lock.json"
    lock.parent.mkdir()
    lock.write_bytes(b"00")
    producer = _OSLockHandle(lock, existing=True, offset=1)
    producer.acquire()
    polls: list[str] = []

    def release_and_publish(_seconds: float) -> None:
        assert calls == []
        assert json.loads(runtime.ledger.read_text(encoding="utf-8"))["as_of"] == YESTERDAY
        producer.release()
        replacement = _OSLockHandle(lock, existing=True, offset=0)
        replacement.acquire()
        try:
            _bars(runtime, include_today=True)
        finally:
            replacement.release()
        polls.append("arrived")

    import time
    monkeypatch.setattr(time, "sleep", release_and_publish)
    try:
        result = overnight.run_evening(paths=runtime, network_news=False, target_date=TODAY,
                                       data_wait_seconds=1, data_poll_seconds=0.001)
    finally:
        producer.release()
    assert result["ok"] is True
    assert result["as_of"] == TODAY
    assert polls == ["arrived"]
    assert lock.read_bytes() == b"00"


def test_ai_invalid_price_basis_remains_a_visible_error_without_financial_writes(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=True)
    _seed(runtime)
    calls = _offline(monkeypatch)
    path = runtime.daily_dir / "600500.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",raw,", ",unknown,"), encoding="utf-8")
    before = _hashes(tmp_path)
    with pytest.raises(RuntimeError, match="ai_trader_daily_price_policy_invalid"):
        overnight.run_evening(paths=runtime, network_news=False, target_date=TODAY, data_wait_seconds=0)
    assert _hashes(tmp_path) == before
    assert calls == []


def test_ai_future_only_data_is_never_a_current_session(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    path = runtime.daily_dir / "600500.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(YESTERDAY, "2026-10-23"), encoding="utf-8")
    before = _hashes(tmp_path)
    result = overnight.run_evening(paths=runtime, network_news=False, target_date=TODAY, data_wait_seconds=0)
    assert result["status"] == "deferred"
    assert result["as_of"] == TODAY
    assert result["observed_as_of"] is None
    assert _hashes(tmp_path) == before
    assert calls == []


def test_ai_target_date_stays_frozen_when_clock_date_advances_during_wait(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    _offline(monkeypatch)
    clock_calls: list[str] = []

    def calendar(_daily: Path) -> dict:
        clock_calls.append("calendar")
        return {"as_of": TODAY if len(clock_calls) == 1 else "2026-10-23"}

    import time
    monkeypatch.setattr(overnight, "latest_as_of", calendar)
    monkeypatch.setattr(time, "sleep", lambda _seconds: _bars(runtime, include_today=True))
    result = overnight.run_evening(paths=runtime, network_news=False, require_current_data=True,
                                   data_wait_seconds=1, data_poll_seconds=0.001)
    assert result["as_of"] == TODAY
    assert clock_calls == ["calendar"]


def test_data_arrival_settles_original_next_open_once_and_keeps_target_date(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=False)
    _seed(runtime)
    calls = _offline(monkeypatch)
    arrivals: list[str] = []

    def data_arrives(_seconds: float) -> None:
        assert json.loads(runtime.plan_latest.read_text(encoding="utf-8"))["decision_date"] == YESTERDAY
        assert calls == []
        _bars(runtime, include_today=True)
        arrivals.append(TODAY)

    import time
    monkeypatch.setattr(time, "sleep", data_arrives)
    first = overnight.run_evening(paths=runtime, network_news=False, require_current_data=True,
                                  target_date=TODAY, data_wait_seconds=1, data_poll_seconds=0.001)
    assert first["ok"] is True
    assert first["as_of"] == TODAY
    assert first["ledger"]["as_of"] == TODAY
    filled = [row for row in first["actions"] if row.get("status") == "filled"]
    assert len(filled) == 1
    assert filled[0]["symbol"] == "600500"
    assert filled[0]["price"] == pytest.approx(10.1)
    ledger_before_retry = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    second = overnight.run_evening(paths=runtime, network_news=False, require_current_data=True,
                                   target_date=TODAY, data_wait_seconds=0)
    ledger_after_retry = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    assert ledger_after_retry["cash"] == ledger_before_retry["cash"]
    assert ledger_after_retry["trades"] == ledger_before_retry["trades"]
    assert not [row for row in second["actions"] if row.get("status") == "filled"]
    assert arrivals == [TODAY]
    assert calls == ["model_stub"]


def test_one_missing_symbol_remains_visible_without_blocking_the_current_session(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    _bars(runtime, include_today=True)
    _bars(runtime, include_today=False, symbol="600501")
    _seed(runtime)
    calls = _offline(monkeypatch)
    monkeypatch.setattr(overnight, "focus_universe", lambda *_args, **_kwargs: ["600500", "600501"])
    result = overnight.run_evening(paths=runtime, network_news=False, require_current_data=True,
                                   target_date=TODAY, data_wait_seconds=0)
    assert result["ok"] is True
    assert result["daily_readiness"]["status"] == "partial"
    assert result["daily_readiness"]["missing_symbols"] == ["600501"]
    assert any("daily_session_missing:600501" in item for item in result["warnings"])
    assert calls == ["model_stub"]
