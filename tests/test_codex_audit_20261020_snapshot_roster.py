"""Real collector/reader paths keep archive, price, roster and code proof apart."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk import snapshot_history as history
from quantdesk.full_a_daily_coverage import prepare_roster_version, load_roster_evidence


def publish_fixture(root: Path, symbols: list[str], *, day: str, run: str,
                    pointer: str = "a_share_roster_latest.json") -> dict:
    prepared = prepare_roster_version([{"symbol": s} for s in symbols], source="offline_fixture",
        as_of=day, run_id=run, accepted_at=day + "T08:00:00+08:00")
    meta = prepared["manifest"]
    path = root / meta["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prepared["csv_bytes"])
    (root / "data/universe" / pointer).write_text(json.dumps(meta), encoding="utf-8")
    return meta


def test_new_roster_nine_symbols_flow_to_actual_collector(tmp_path: Path, monkeypatch) -> None:
    symbols = ["600000", *[str(920001+i) for i in range(9)]]
    meta = publish_fixture(tmp_path, symbols, day="2026-10-19", run="forward9")
    monkeypatch.setattr(history, "_read_trading_day", lambda *_a: (True, "fixture", []))
    now = datetime(2026, 10, 19, 10, 0, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    seen = []
    def fetch(symbols):
        seen.extend(symbols)
        return [{"symbol": s, "price": 10, "open": 10, "high": 10, "low": 10,
            "volume": 100, "amount": 1000, "price_basis": "raw", "quote_date": "2026-10-19",
            "quote_time": "10:00:01"} for s in symbols], "offline_fixture"
    result = history.collect_market_snapshot(tmp_path, network=True, symbols=None, now=now,
        clock=lambda: now, fetcher=fetch)
    assert seen == sorted(symbols) and result["written"]
    stored = json.loads((tmp_path / result["metadata_path"]).read_text(encoding="utf-8"))
    assert stored["producer_provenance"]["roster_evidence"]["sha256"] == meta["sha256"]
    assert len(stored["code_version"]["source_sha256"]) == 64
    day = history.snapshot_day_summary(tmp_path, "2026-10-19", as_of=now)
    assert day["archive_hash_verified_slot_count"] == 1
    assert day["full_ohlcv_valid_count"] == 10
    assert day["unknown_quote_gap_count"] == 0


def test_invalid_latest_retains_last_accepted_identity_and_age(tmp_path: Path) -> None:
    old = publish_fixture(tmp_path, ["600000"], day="2026-10-12", run="accepted",
        pointer="a_share_roster_last_accepted.json")
    (tmp_path / "data/universe/a_share_roster_latest.json").write_text('{"accepted": false}', encoding="utf-8")
    result = load_roster_evidence(tmp_path, target_date="2026-10-19")
    assert result["symbols"] == ["600000"] and result["age_days"] == 7
    assert result["sha256"] == old["sha256"] and result["accepted_at"] == old["accepted_at"]
    assert result["fallback"] and result["reason_codes"]


def test_price_only_and_full_ohlcv_and_missing_basis_are_separate() -> None:
    now = datetime(2026, 10, 19, 10, 0, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    base = {"price": 10, "quote_date": "2026-10-19", "quote_time": "10:00:01",
        "source": "fixture", "price_basis": "raw"}
    rows = [dict(base, symbol="600000"), dict(base, symbol="600001", open=10, high=10,
        low=10, volume=100, amount=1000), dict(base, symbol="600002", price_basis="")]
    result = history._quote_ohlcv_quality(rows, "2026-10-19", now)
    assert result["explicit_raw_price_valid_count"] == 2
    assert result["full_ohlcv_valid_count"] == 1
    assert result["unknown_gap_count"] == 2
    assert result["confirmed_nontrade_count"] == 0
    assert rows[-1]["price_basis"] == ""


def test_reconciliation_keeps_raw_trade_when_suspension_source_conflicts(tmp_path: Path) -> None:
    from quantdesk.full_a_daily_reconciliation import build_reconciliation_partition
    from quantdesk.suspension_aware_completeness import SuspensionEvidence
    publish_fixture(tmp_path, ["300773", "600000"], day="2026-10-19", run="conflict")
    path = tmp_path / "data/daily/300773.csv"
    path.parent.mkdir(parents=True)
    path.write_text("date,symbol,open,high,low,close,volume,amount,adjust,price_basis\n"
        "2026-10-19,300773,10,10,10,10,100,1000,raw,raw\n", encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    run = SuspensionEvidence(target_date="2026-10-19", verified_exclusions={"300773": {"tradestatus": "0"}},
        present=True, source="fixture_provider")
    result = build_reconciliation_partition(tmp_path, target_date="2026-10-19", run_evidence=run)
    assert result["raw_trading_overrides_stale_nontrade_symbols"] == ["300773"]
    assert result["gap_classification"]["300773"] == "raw_trading_provider_conflict"
    assert "300773" not in result["verified_excluded_symbols"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_future_only_daily_is_not_target_date_coverage(tmp_path: Path) -> None:
    from quantdesk.full_a_daily_coverage import build_full_a_daily_coverage
    meta = publish_fixture(tmp_path, ["600000"], day="2026-10-19", run="future")
    daily = tmp_path / "data/daily"
    daily.mkdir()
    (daily / "600000.csv").write_text("date,symbol,close\n2026-10-20,600000,10\n", encoding="utf-8")
    result = build_full_a_daily_coverage(target_date="2026-10-19", universe_path=tmp_path / meta["path"], daily_dir=daily)
    assert result["at_target_date_count"] == 0 and result["future_latest_count"] == 1


def test_historical_daily_chain_uses_target_roster_instead_of_wall_clock(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import _full_a_research_universe
    old = publish_fixture(tmp_path, ["600000"], day="2026-10-12", run="past",
        pointer="a_share_roster_last_accepted.json")
    publish_fixture(tmp_path, ["600000", "920009"], day="2026-10-20", run="future")
    result = _full_a_research_universe(tmp_path, target_date="2026-10-19")
    assert result["symbols"] == ["600000"]
    assert result["source_sha256"] == old["sha256"]
    assert result["roster_evidence"]["age_days"] == 7


def test_historical_snapshot_summary_never_uses_future_legacy_roster(tmp_path: Path) -> None:
    universe = tmp_path / "data/universe"
    universe.mkdir(parents=True)
    (universe / "a_share_symbols.csv").write_text("symbol,as_of\n600000,2026-10-20\n", encoding="utf-8")
    result = load_roster_evidence(tmp_path, target_date="2026-10-19")
    assert result["symbols"] == [] and result["identity_status"] == "future_roster"
    summary = history.snapshot_day_summary(tmp_path, "2026-10-19")
    assert summary["expected_symbol_count"] == 0
