"""Offline boundaries for the Muse data-only trial and its durable slots."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts import run_muse_intraday_trial as trial


def config_at(tmp_path: Path) -> tuple[dict, Path, str]:
    root = tmp_path / "project"
    calendar = root / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open\n2026-10-10,false\n2026-10-12,true\n2026-10-13,true\n2026-10-14,true\n", encoding="utf-8")
    source = root / "source.py"
    source.write_text("# deterministic source fixture\n", encoding="utf-8")
    config = {"schema": trial.SCHEMA, "trial_id": "test", "root": str(root), "data_only": True,
              "network_authorized": True, "trading_dates": ["2026-10-12", "2026-10-13", "2026-10-14"],
              "snapshot_budget_seconds": 150, "minute_budget_seconds": 240, "seed_as_of": "2026-10-09",
              "minute_seed": [{"symbol": "600000", "priority": "critical", "reasons": ["ai_position"]}],
              "source_sha256": {"source.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return trial.load_config(path, sha), path, sha


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=trial.SHANGHAI)


def test_config_detects_changed_code_before_network(tmp_path):
    _, path, sha = config_at(tmp_path)
    (tmp_path / "project/source.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source_hash_mismatch"):
        trial.load_config(path, sha)


@pytest.mark.parametrize("change,reason", [
    ({"root": "E:/codex/quant_new"}, "root_not_isolated"),
    ({"trading_dates": ["2026-10-10"]}, "not_verified_open"),
    ({"snapshot_budget_seconds": 151}, "invalid_budget"),
    ({"network_authorized": False}, "network_not_authorized"),
    ({"enabled_kinds": ["snapshot", "news"]}, "invalid_enabled_kinds"),
])
def test_config_rejects_scope_expansion(tmp_path, change, reason):
    _, path, _ = config_at(tmp_path)
    raw = json.loads(path.read_text())
    raw.update(change)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=reason):
        trial.load_config(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize("kind,slot,now", [
    ("snapshot", "2026-10-10T1000", "2026-10-10T10:00:01"),
    ("snapshot", "2026-10-12T1000", "2026-10-12T10:04:01"),
    ("snapshot", "2026-10-12T1200", "2026-10-12T12:00:01"),
    ("minute", "2026-10-12T1000", "2026-10-12T10:00:01"),
    ("minute", "2026-10-12T0935", "2026-10-12T10:00:35"),
    ("snapshot", "2026-10-15T1000", "2026-10-15T10:00:01"),
])
def test_nonmarket_and_missed_slots_do_not_request_or_write(tmp_path, kind, slot, now):
    config, _, _ = config_at(tmp_path)
    def forbidden(*args):
        pytest.fail("closed or missed slot contacted a provider")
    result = trial.run_once(config, kind, slot, now=stamp(now), collector=forbidden)
    assert result["status"] == "skipped" and result["network_attempted"] is False
    assert not (tmp_path / "scheduler").exists()


def test_same_slot_attempts_once_and_preserves_failure(tmp_path):
    config, _, _ = config_at(tmp_path)
    calls = []
    def fail(*args):
        calls.append(1)
        raise TimeoutError("provider timed out")
    args = (config, "snapshot", "2026-10-12T1000")
    first = trial.run_once(*args, now=stamp("2026-10-12T10:00:01"), collector=fail)
    original = trial.receipt_path(*args).read_bytes()
    second = trial.run_once(*args, now=stamp("2026-10-12T10:00:02"), collector=fail)
    assert first["status"] == "failed" and first["collection_succeeded"] is False
    assert second["status"] == "already_attempted" and calls == [1]
    assert trial.receipt_path(*args).read_bytes() == original


def test_no_rows_cannot_be_called_success(tmp_path):
    config, _, _ = config_at(tmp_path)
    config["enabled_kinds"] = ["snapshot"]
    blocked = trial.run_once(config, "minute", "2026-10-12T1000", now=stamp("2026-10-12T10:00:35"),
                             collector=lambda *args: pytest.fail("disabled minute collector contacted provider"))
    assert blocked["reason"] == "collector_not_enabled" and blocked["network_attempted"] is False
    config["enabled_kinds"] = ["snapshot", "minute"]
    result = trial.run_once(config, "minute", "2026-10-12T1000", now=stamp("2026-10-12T10:00:35"),
                            collector=lambda *args: {"status": "ready", "appended_rows": 0, "fresh_count": 0})
    assert result["collection_succeeded"] is False


def test_hq_batches_keep_identity_and_date_validation(monkeypatch):
    from types import SimpleNamespace
    from quantdesk import data_providers, snapshot_history
    calls = []
    default_batch = snapshot_history.SINA_HQ_BATCH_SIZE
    def fetch(keys, **kwargs):
        calls.append(list(keys))
        rows = [{"symbol": key, "date": "2026-10-09", "time": "15:00:00", "price": 10,
                 "prev_close": 9.9} for key in keys]
        rows.extend([{"symbol": "sh600999", "date": "2026-10-09", "time": "15:00:00"},
                     {"symbol": keys[0], "date": "", "time": "15:00:00"}])
        return SimpleNamespace(rows=rows, provider_errors=[], warnings=[])
    monkeypatch.setattr(data_providers, "fetch_spot_sina_batch", fetch)
    symbols = [f"{i:06d}" for i in range(1, 502)]
    rows, source = trial.fetch_trial_snapshot(symbols, 90)
    assert [len(c) for c in calls] == [500, 1]
    assert len(rows) == 501 and {r["symbol"] for r in rows} == set(symbols)
    assert all(r["date"] == "2026-10-09" for r in rows)
    assert source == "sina.hq_batch" and rows.warnings
    assert snapshot_history.SINA_HQ_BATCH_SIZE == default_batch


def test_seed_and_cloud_movers_are_capped_and_labelled(tmp_path):
    config, _, _ = config_at(tmp_path)
    movers = [{"symbol": f"{i:06d}", "reason": "snapshot_mover"} for i in range(120)]
    scope = trial.scope_from_seed(config, movers)
    assert len(scope["symbols"]) == 100
    assert scope["symbols"][0]["symbol"] == "600000"
    assert len({r["symbol"] for r in scope["symbols"]}) == 100
    assert "ai_intraday_selection_not_enabled" in scope["warnings"]


def test_real_minute_collector_isolated_and_never_calls_ai_or_second_spot(tmp_path, monkeypatch):
    config, _, _ = config_at(tmp_path)
    from quantdesk.intraday_refresh import collector, snapshot
    def no_spot(**kwargs):
        pytest.fail("minute worker issued an extra full-market request")
    monkeypatch.setattr(snapshot, "_default_network_fetch", no_spot)
    monkeypatch.setattr(collector, "_live_minute_rows", lambda *args, **kwargs: ([{
        "symbol": "600000", "datetime": "2026-10-12 10:00:00", "open": 10.0, "high": 10.1,
        "low": 9.9, "close": 10.0, "volume": 100, "amount": 1000,
        "source": "synthetic_test", "price_basis": "raw"}], "", {}))
    now = stamp("2026-10-12T10:00:35")
    result = trial.run_once(config, "minute", "2026-10-12T1000", now=now)
    assert result["payload"]["appended_rows"] == 1
    assert result["payload"]["trial_scope"]["symbols"][0]["symbol"] == "600000"
    root = Path(config["root"])
    files = list((root / "data/intraday/1m").glob("*.csv"))
    assert len(files) == 1 and "2026-10-12" in files[0].read_text(encoding="utf-8-sig")
    assert not (root / "state").exists()
    assert not (root / "data/daily").exists()


def test_reconstruction_marks_dead_worker_without_fabricating_end_time(tmp_path, monkeypatch):
    config, _, _ = config_at(tmp_path)
    path = trial.receipt_path(config, "snapshot", "2026-10-12T1000")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "running", "pid": 123, "process_identity": "old-boot:12",
                                "started_at": "2026-10-12T10:00:01+08:00", "finished_at": ""}))
    monkeypatch.setattr(trial, "process_identity", lambda pid: "new-boot:12")
    assert trial.reconcile_interrupted(config) == 1
    result = json.loads(path.read_text())
    assert result["status"] == "interrupted" and result["finished_at"] == ""
    assert result["started_at"] == "2026-10-12T10:00:01+08:00"
    assert result["collection_succeeded"] is False
    assert trial.reconcile_interrupted(config) == 0


def test_wakeup_does_not_launch_second_supervisor_or_run_after_expiry(tmp_path, monkeypatch):
    config, path, _ = config_at(tmp_path)
    def no_start(*args, **kwargs):
        pytest.fail("unexpected process launch")
    monkeypatch.setattr(trial.subprocess, "Popen", no_start)
    monkeypatch.setattr(trial, "local_now", lambda: stamp("2026-10-12T10:00:00"))
    monkeypatch.setattr(trial, "supervisor_status", lambda c: {"alive": True, "pid": 456})
    assert trial.wake(config, path)["status"] == "already_running"
    monkeypatch.setattr(trial, "local_now", lambda: stamp("2026-10-14T15:12:00"))
    assert trial.wake(config, path)["status"] == "expired"
