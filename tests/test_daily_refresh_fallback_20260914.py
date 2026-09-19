"""A list outage must not block dated OHLCV from the existing quote source."""

import csv
import io
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from scripts import run_daily_data_refresh as refresh


TARGET = "2026-09-14"


@pytest.fixture
def setup_refresh(tmp_path, monkeypatch):
    for name, relative in [("PROJECT_ROOT", "."), ("DAILY_DIR", "data/daily"),
                           ("META_DIR", "data/metadata"), ("VAL_DIR", "data/metadata/valuation_snapshots")]:
        monkeypatch.setattr(refresh, name, tmp_path / relative)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "000001"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [])
    monkeypatch.setattr(refresh.requests, "Session", lambda: SimpleNamespace(close=lambda: None))
    old_check = refresh._close_quote_reason
    monkeypatch.setattr(refresh, "_close_quote_reason", lambda item, quote, target: old_check(
        item, quote, target, observed_at=datetime(2026, 9, 14, 16, 0, tzinfo=refresh.SHANGHAI)))
    lease = SimpleNamespace(assert_active=lambda: None, run_id="fixture-recovery")
    return tmp_path, lease


def quote(**changes):
    return {"symbol": "600000", "name": "fixture", "open": 10.0, "prev_close": 9.9,
            "price": 10.1, "high": 10.2, "low": 9.8, "volume": 10000,
            "amount": 100500.0, "date": TARGET, "time": "15:00:00", **changes}


def test_empty_list_uses_dated_quotes_without_fabricating_valuation(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    calls = []
    def fetch(symbols):
        calls.append(symbols)
        return {"600000": quote()}
    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)
    old_valuation = refresh.VAL_DIR / "20260914.csv"
    old_valuation.parent.mkdir(parents=True)
    old_valuation.write_bytes(b"existing valuation\n")

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    assert result["ok"] is True and result["bars"]["appended"] == 1
    assert calls == [["600000", "000001"]]
    rows = list(csv.DictReader(io.StringIO((refresh.DAILY_DIR / "600000.csv").read_text())))
    assert len(rows) == 1 and rows[0]["date"] == TARGET
    assert rows[0]["close"] == "10.1" and rows[0]["adjust"] == "raw"
    assert rows[0]["source"] == "sina_dated_hq_close"
    assert old_valuation.read_bytes() == b"existing valuation\n"
    assert result["valuation_snapshot"] == ""
    assert "valuation_not_updated:list_unavailable" in result["warnings"]
    evidence = json.loads((refresh.META_DIR / "daily_refresh_review/20260914.json").read_text())
    assert evidence["missing_in_snapshot"] == ["000001"]
    assert not (refresh.DAILY_DIR / "000001.csv").exists()


@pytest.mark.parametrize("changes", [{"date": "2026-09-11"}, {"time": "14:59:00"},
                                      {"time": "17:00:00"}, {"price": 0}, {"volume": 0}])
def test_fallback_keeps_date_and_price_requirements(setup_refresh, monkeypatch, changes):
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {"600000": quote(**changes)})
    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)
    assert result["ok"] is False
    assert not (refresh.DAILY_DIR / "600000.csv").exists()
    assert result["bars"]["flagged_for_review"] == 1


def test_one_invalid_quote_does_not_block_other_stock(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {
        "600000": quote(date="2026-09-11"), "000001": quote(symbol="000001"),
    })
    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)
    assert result["ok"] is True
    assert result["bars"]["appended"] == 1
    assert result["bars"]["flagged_for_review"] == 1
    assert (refresh.DAILY_DIR / "000001.csv").is_file()
    assert not (refresh.DAILY_DIR / "600000.csv").exists()


def test_fallback_reentry_does_not_duplicate_daily_bar(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {"600000": quote()})
    refresh.family_bars_and_valuation(TARGET, True, lease=lease)
    path = refresh.DAILY_DIR / "600000.csv"
    before = path.read_bytes()
    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)
    assert result["bars"]["already_had_bar"] == 1
    assert path.read_bytes() == before


def test_normal_list_path_keeps_valuation_output(setup_refresh, monkeypatch):
    root, lease = setup_refresh
    item = {"code": "600000", "name": "fixture", "open": 10.0, "high": 10.2,
            "low": 9.8, "trade": 10.1, "volume": 10000, "amount": 100500.0, "per": 12.0}
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [item])
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {"600000": quote()})
    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)
    assert result["ok"] is True
    assert result["quote_source"] == "sina_list_and_dated_hq"
    rows = list(csv.DictReader(io.StringIO((refresh.VAL_DIR / "20260914.csv").read_text())))
    assert rows[0]["pe"] == "12.0"
