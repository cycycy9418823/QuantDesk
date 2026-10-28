"""BSE gaps are local evidence failures with bounded routing, not account gates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from quantdesk.intraday_refresh import collector as c
from quantdesk.intraday.intraday_provider_capability import minute_exchange_capability

NOW = datetime(2026, 10, 19, 10, 20, tzinfo=c.SHANGHAI)
BSE = ["920014", "920022", "920023", "920066", "920768", "920821", "430047", "830799"]


def bar(symbol: str = "920066") -> dict:
    return {"symbol": symbol, "datetime": "2026-10-19 10:19:00", "open": 10, "high": 10,
        "low": 10, "close": 10, "volume": 100, "amount": 1000, "price_basis": "raw", "source": "offline_fixture"}


@pytest.mark.parametrize("symbol", BSE)
def test_bse_probe_single_attempt_and_no_guessed_sina(monkeypatch, symbol: str) -> None:
    calls = []
    def native(code, **kwargs):
        calls.append((code, kwargs))
        return [], "provider_failed:mootdx:ProtocolError", {"error_stage": "request"}
    monkeypatch.setattr(c, "_mootdx_minute_rows", native)
    monkeypatch.setattr(c, "_sina_minute_rows", lambda *_a, **_k: pytest.fail("unsupported Sina route"))
    rows, error, proof = c._live_minute_rows(symbol, offset=240, now=NOW, timeout_seconds=8)
    assert not rows and error and len(calls) == 1
    assert calls[0][1]["max_attempts"] == 1 and 0 < calls[0][1]["timeout_seconds"] <= 8
    assert proof["exchange_capability"]["market_id"] == 2
    assert proof["global_paper_account_block"] is False
    assert minute_exchange_capability("sina_intraday", symbol)["route_supported"] is False


def test_explicit_injected_bse_fallback_preserves_budget(monkeypatch) -> None:
    monkeypatch.setattr(c, "_mootdx_minute_rows", lambda *_a, **_k: ([], "provider_no_rows:mootdx", {}))
    seen = []
    def fallback(symbol, **kwargs):
        seen.append(kwargs["timeout_seconds"])
        return [bar(symbol)], "", {"provider": "offline_bse", "minute_label_convention": "minute_start"}
    rows, error, proof = c._live_minute_rows("920066", offset=240, now=NOW, timeout_seconds=3,
        fallback_fetchers=[{"provider": "offline_bse", "exchanges": ["BJ"], "fetcher": fallback}])
    assert not error and rows == [bar()] and 0 < seen[0] <= 3
    assert proof["live_availability"] == "mock_or_injected_only"


@pytest.mark.parametrize("damage", ["none", "bad_ohlc", "stale", "missing_basis", "cross_symbol"])
def test_actual_round_rejects_invalid_single_symbol_without_global_account_gate(tmp_path: Path, monkeypatch, damage: str) -> None:
    monkeypatch.setattr(c, "refresh_full_market_snapshot", lambda *_a, **_k: {"movers": [], "reason_codes": []})
    row = bar()
    if damage == "bad_ohlc": row["high"] = 9
    elif damage == "stale": row["datetime"] = "2026-10-16 10:19:00"
    elif damage == "missing_basis": row.pop("price_basis")
    elif damage == "cross_symbol": row["symbol"] = "600000"
    rows = [] if damage == "none" else [row]
    result = c.collect_intraday_refresh(tmp_path, network=True, now=NOW, trading_day=True,
        fetcher=lambda *_a: (rows, ""), scope_builder=lambda *_a, **_k: {
            "symbols": [{"symbol": "920066", "priority": "normal"}], "warnings": []})
    assert result["requested_count"] == 1 and result["appended_rows"] == 0
    assert result["missing_count"] == 1 and not result.get("paper_account_execution_blocked", False)
    assert not (tmp_path / "data/intraday/1m/920066.csv").exists()


def test_session_completeness_requires_declared_convention_and_cutoff() -> None:
    rows = [dict(bar("600000"), datetime=f"2026-10-19 09:{i:02d}:00") for i in range(30, 36)]
    current = NOW.replace(hour=9, minute=35)
    unknown = c._response_minute_window(rows, as_of=current)
    assert unknown["expected_session_minutes"] is None
    known = c._response_minute_window(rows, as_of=current, convention="minute_start")
    assert known["expected_session_minutes"] == 5 and known["missing_session_minutes"] == 0
    assert known["full_day_verified"] is False
    gap = c._response_minute_window(rows[:-2], as_of=current, convention="minute_start")
    assert gap["missing_session_minutes"] == 1


def test_fallback_after_deadline_never_returns_bars(monkeypatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(c, "_mootdx_minute_rows", lambda *_a, **_k: ([], "provider_no_rows:mootdx", {}))
    def late(symbol, **kwargs):
        clock["now"] = 10
        return [bar(symbol)], "", {}
    rows, error, proof = c._live_minute_rows("920066", offset=240, now=NOW, timeout_seconds=3,
        monotonic=lambda: clock["now"], fallback_fetchers=[{"exchanges": ["BJ"], "fetcher": late}])
    assert rows == [] and error == "provider_failed:deadline_exhausted" and proof["error_stage"] == "timeout"
