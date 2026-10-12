"""1C-B: data/daily has one writer; the spot amount fill checks the quote's own time.

Before: update_daily_prices / write_daily accepted any caller (UI button,
bootstrap, CLI fallback); only scripts/run_daily_data_refresh.py checked the
Pipeline lease. The spot-cache amount fill checked the date only, so an
intraday round's cumulative amount (or a fabricated "15:00:01") could become
the day's amount. These tests fail on the old code.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quantdesk import daily_price_providers, daily_price_update_engine as engine
from quantdesk import pipeline_process_guard as guard
from scripts import run_data_close_pipeline as pipeline

# Pre-fix code has no such class; fall back so its tests fail on behaviour.
Refused = getattr(guard, "CanonicalDailyWriteRefused", PermissionError)
TARGET = "2026-10-09"
HEADER = "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at\n"


def _bar(day: str, close: float = 10.0) -> dict:
    return {"date": day, "symbol": "600000", "open": close, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": 1000.0, "amount": 10000.0, "change": 0.0, "pct_chg": 0.0,
            "adjust": "raw", "source": "fixture", "updated_at": f"{day}T15:30:00+08:00"}


@pytest.fixture()
def canonical(tmp_path, monkeypatch):
    """Make tmp_path the project root for the guard, the engine and the providers."""

    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    (daily / "600000.csv").write_text(
        HEADER + "2026-10-08,600000,10.0,10.1,9.9,10.0,1000.0,10000.0,0.0,0.0,raw,fixture,t\n", encoding="utf-8")
    # raising=False keeps this fixture usable against the pre-fix code, so the
    # tests below fail there on behaviour rather than on a missing name.
    monkeypatch.setattr(guard, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(engine, "DAILY_DIR", daily)
    monkeypatch.setattr(daily_price_providers, "DAILY_DIR", daily)
    reset = getattr(guard, "unregister_daily_writer", lambda *a: None)
    reset()
    yield tmp_path
    reset()


def _lease(root, monkeypatch, *, scope="research_chain", run_id="fix1c"):
    owner, lock = pipeline.acquire_pipeline_lock(root, run_id=run_id)
    for key, value in pipeline._grant_market_writer(root, run_id=run_id, target=TARGET, owner=owner, scope=scope).items():
        monkeypatch.setenv(key, value)
    return owner, lock, pipeline.MarketWriterLease(root, run_id=run_id, target=TARGET, scope=scope)


def test_write_daily_without_lease_is_refused_and_file_unchanged(canonical):
    path = canonical / "data/daily/600000.csv"
    before = path.read_bytes()
    with pytest.raises(Refused, match="lease_required"):
        daily_price_providers.write_daily("600000", pd.DataFrame([_bar("2026-10-08"), _bar(TARGET)]))
    assert path.read_bytes() == before


def test_writes_outside_the_canonical_tree_are_unaffected(canonical, tmp_path_factory):
    other = tmp_path_factory.mktemp("staging")
    daily_price_providers.write_daily("600000", pd.DataFrame([_bar(TARGET)]), daily_dir=other)
    assert (other / "600000.csv").is_file()


def test_update_daily_prices_without_lease_is_blocked_before_any_provider(canonical, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "fetch_daily", lambda provider, symbol, *a, **k: calls.append(symbol) or (pd.DataFrame(), []))
    before = (canonical / "data/daily/600000.csv").read_bytes()
    payload = engine.update_daily_prices(
        symbols="600000", target_date=TARGET, network=True, providers=["tencent_daily"],
        workers=1, write_result=False, only_low_price=False,
    )
    assert calls == []
    assert payload["status"] == "blocked"
    assert "canonical_daily_writer_lease_required" in payload["warnings"]
    assert (canonical / "data/daily/600000.csv").read_bytes() == before


def test_pipeline_lease_still_writes_and_is_gone_after_exit(canonical, monkeypatch):
    fresh = pd.DataFrame([_bar("2026-10-08"), _bar(TARGET, 10.5)])
    calls = []
    monkeypatch.setattr(engine, "fetch_daily", lambda provider, symbol, *a, **k: calls.append(symbol) or (fresh.copy(), []))
    owner, lock, lease = _lease(canonical, monkeypatch)
    try:
        with lease:
            proof = guard.daily_writer_proof()
            assert proof and proof["scope"] == "research_chain" and proof["token"]
            payload = engine.update_daily_prices(
                symbols="600000", target_date=TARGET, network=True, providers=["tencent_daily"],
                workers=1, write_result=False, only_low_price=False,
            )
            guard.verify_daily_writer_proof(proof)
            with pytest.raises(Refused, match="capability_invalid"):
                guard.verify_daily_writer_proof({**proof, "token": "forged"})
        assert guard.daily_writer_proof() is None
        with pytest.raises(Refused):
            daily_price_providers.write_daily("600000", fresh)
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="fix1c")
    assert calls == ["600000"]
    assert payload["updated_symbol_list"] == ["600000"]
    saved = pd.read_csv(canonical / "data/daily/600000.csv", dtype={"symbol": str})
    assert saved["date"].tolist() == ["2026-10-08", TARGET]
    with pytest.raises(Refused):
        guard.verify_daily_writer_proof(proof)


def test_spawned_worker_must_adopt_a_live_proof(canonical, monkeypatch):
    owner, lock, lease = _lease(canonical, monkeypatch)
    try:
        with lease:
            proof = guard.daily_writer_proof()
            guard.unregister_daily_writer()  # what a fresh worker process sees
            with pytest.raises(Refused):
                daily_price_providers.write_daily("600000", pd.DataFrame([_bar(TARGET)]))
            with guard.adopt_daily_writer_proof(proof):
                daily_price_providers.write_daily("600000", pd.DataFrame([_bar("2026-10-08"), _bar(TARGET)]))
            with guard.adopt_daily_writer_proof(None):
                with pytest.raises(Refused):
                    daily_price_providers.write_daily("600000", pd.DataFrame([_bar(TARGET)]))
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="fix1c")


def test_audit_only_stage_lease_cannot_write_daily(canonical, monkeypatch):
    owner, lock, lease = _lease(canonical, monkeypatch, scope="reconcile")
    try:
        with lease:
            with pytest.raises(Refused, match="scope_not_allowed:reconcile"):
                daily_price_providers.write_daily("600000", pd.DataFrame([_bar(TARGET)]))
    finally:
        pipeline.release_pipeline_lock(owner, lock, run_id="fix1c")


# --- spot-cache amount fill -------------------------------------------------

DAILY_ROW = ("date,symbol,open,high,low,close,volume,amount,source,updated_at,adjust\n"
             "2026-06-15,000802,4.31,4.63,4.31,4.42,418214,,tencent_daily,test,raw\n")


def _fill(tmp_path, monkeypatch, cache_text):
    daily = tmp_path / "data" / "daily"
    cache = tmp_path / "data" / "cache"
    daily.mkdir(parents=True)
    cache.mkdir(parents=True)
    (daily / "000802.csv").write_text(DAILY_ROW, encoding="utf-8")
    (cache / "spot_snapshot_latest.csv").write_text(cache_text, encoding="utf-8")
    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(engine, "DAILY_DIR", daily)
    result = engine._backfill_target_amount_from_spot_snapshot(["000802"], "2026-06-15")
    return result, pd.read_csv(daily / "000802.csv")


def test_intraday_round_amount_is_not_the_days_amount(tmp_path, monkeypatch):
    result, saved = _fill(tmp_path, monkeypatch,
                          "symbol,name,price,volume,amount,quote_time,source,fetched_at\n"
                          "000802,x,4.42,30000000,120000000,2026-06-15 14:30:00,sina.spot_direct,2026-06-15T14:30:40+08:00\n")
    assert result["updated"] == 0 and result["rejected_rows"] == {"quote_before_close": 1}
    assert pd.isna(saved.iloc[-1]["amount"])


def test_quote_from_another_session_is_not_borrowed(tmp_path, monkeypatch):
    result, saved = _fill(tmp_path, monkeypatch,
                          "symbol,name,price,volume,amount,quote_time,source,fetched_at\n"
                          "000802,x,4.10,30000000,120000000,2026-06-15 15:00:01,sina.spot_direct,2026-06-15T15:05:00+08:00\n")
    assert result["updated"] == 0 and result["price_mismatch_count"] == 1
    assert pd.isna(saved.iloc[-1]["amount"])


def test_honest_time_only_quote_is_dated_by_its_fetch(tmp_path, monkeypatch):
    result, saved = _fill(tmp_path, monkeypatch,
                          "symbol,name,price,volume,amount,quote_time,quote_date,source,fetched_at\n"
                          "000802,x,4.42,41821352,186791640,15:00:03,,sina.spot_direct,2026-06-15T15:05:10+08:00\n")
    assert result["updated"] == 1
    assert float(saved.iloc[-1]["amount"]) == 186791640


@pytest.mark.parametrize(("cache_text", "reason"), [
    ("symbol,name,price,volume,amount,source\n000802,x,4.42,41821352,186791640,sina\n", None),
    # The pre-fix writer's shape: a full "15:00:01" stamp that was the round's
    # own clock, and no fetch record (the real 10/09 cache).
    ("symbol,name,price,volume,amount,quote_time,quote_date,source\n"
     "000802,x,4.42,41821352,186791640,2026-06-15 15:00:01,2026-06-15,sina.spot_direct\n", "fetched_at_missing"),
    ("symbol,name,price,volume,amount,quote_time,source,fetched_at\n"
     "000802,x,4.42,41821352,186791640,15:00:03,sina,2026-06-15T14:55:00+08:00\n", "quote_after_fetch"),
    ("symbol,name,price,volume,amount,quote_time,source,fetched_at\n"
     "000802,x,4.42,41821352,186791640,15:00:03,sina,2026-06-16T09:31:00+08:00\n", "quote_date_not_target"),
])
def test_unprovable_quotes_fill_nothing(tmp_path, monkeypatch, cache_text, reason):
    result, saved = _fill(tmp_path, monkeypatch, cache_text)
    assert result["updated"] == 0
    if reason is None:
        assert result["status"] == "cache_no_quote_time"
    else:
        assert result["rejected_rows"] == {reason: 1}
    assert pd.isna(saved.iloc[-1]["amount"])
