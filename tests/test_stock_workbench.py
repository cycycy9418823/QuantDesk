from __future__ import annotations

from datetime import date, datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.quote_evidence import inspect_quote
from quantdesk.stock_workbench import build_stock_reference


def quote(**changes):
    return {"symbol": "600000", "name": "fixture", "price": 10.6, "available": True,
            "quote_time": "2026-10-23 10:00:00", "updated_at": "2026-10-23T10:00:02+08:00",
            "source": "fixture.provider", "price_basis": "raw", **changes}


def check(row, *, now="2026-10-23T10:00:10+08:00", session="morning_session", expected=date(2026, 10, 22)):
    return inspect_quote(row, symbol="600000", now=datetime.fromisoformat(now),
                         session=session, expected_trade_date=expected)


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf"), True, "数据缺失"])
def test_invalid_price_is_never_a_usable_quote(price):
    result = check(quote(price=price))
    assert not result["price_valid"]
    assert not result["usable_latest_quote"]
    assert "quote_price_missing_or_invalid" in result["reason_codes"]


def test_clock_without_date_does_not_become_today():
    result = check(quote(quote_time="15:00:02"),
                   now="2026-10-24T03:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 23))
    # A clock is never promoted to the viewer's date: the display date comes
    # from the dated collection record (2026-10-23), not from now().
    assert result["quote_timestamp_inferred"] is True
    assert result["quote_timestamp"].startswith("2026-10-23")
    assert not result["quote_timestamp"].startswith("2026-10-24")
    assert not result["usable_latest_quote"]
    assert not result["comparable_to_raw_daily"]
    # 15:00 against a 10:00 collection record is not a plausible pair.
    assert result["status"] == "invalid_time"
    assert "quote_clock_after_collection" in result["reason_codes"]


def test_clock_without_date_and_without_collection_stays_unverified():
    result = check({"symbol": "600000", "price": 10.6, "available": True,
                    "quote_time": "15:00:02", "source": "fixture.provider"},
                   now="2026-10-23T16:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 23))
    assert result["quote_timestamp"] == ""
    assert result["quote_timestamp_inferred"] is False
    assert result["status"] == "unverified_time"
    assert not result["usable_latest_quote"]


def test_sina_style_clock_is_dated_by_our_own_collection_record():
    # Real shape of the Sina spot snapshot: clock only, collection written a
    # moment before the provider stamped the quote.
    result = check(quote(quote_time="15:05:09", updated_at="2026-10-27T15:05:02+08:00",
                         quote_timestamp="", price_basis=""),
                   now="2026-10-27T20:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 27))
    assert result["quote_timestamp_inferred"] is True
    assert result["quote_timestamp"] == "2026-10-27T15:05:09+08:00"
    assert result["status"] == "session_snapshot"
    assert result["usable_latest_quote"]
    assert "quote_date_inferred_from_collection" in result["reason_codes"]
    # The price basis is still unknown, so comparability is not claimed.
    assert not result["comparable_to_raw_daily"]


def test_closing_snapshot_window_covers_a_late_closing_print():
    late = check(quote(quote_time="15:09:30", updated_at="2026-10-27T15:09:00+08:00", quote_timestamp=""),
                 now="2026-10-27T20:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 27))
    assert late["usable_latest_quote"]
    too_late = check(quote(quote_time="15:30:00", updated_at="2026-10-27T15:29:00+08:00", quote_timestamp=""),
                     now="2026-10-27T20:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 27))
    assert not too_late["usable_latest_quote"]


def test_explicit_quote_date_and_clock_are_preserved():
    result = check(quote(quote_date="2026-10-23", quote_time="10:00:00"))
    assert result["quote_timestamp"] == "2026-10-23T10:00:00+08:00"
    assert result["status"] == "live"


def test_utc_quote_is_normalized_to_china_clock():
    result = check(quote(quote_time="2026-10-23T02:00:00Z"))
    assert result["quote_timestamp"] == "2026-10-23T10:00:00+08:00"
    assert result["usable_latest_quote"]


@pytest.mark.parametrize("changes,reason", [
    ({"symbol": "600001"}, "quote_symbol_mismatch"),
    ({"source": ""}, "quote_source_missing"),
    ({"quote_time": "2026-10-24 15:00:02"}, "quote_timestamp_in_future"),
    # A quote clock well ahead of our own collection record is not plausible;
    # the opposite direction is normal write-out latency and stays valid.
    ({"updated_at": "2026-10-23 09:40:00"}, "quote_clock_after_collection"),
    ({"updated_at": "2026-10-23 10:05:00"}, "quote_collection_in_future"),
    ({"quote_date": "2026-10-22"}, "quote_date_timestamp_conflict"),
])
def test_invalid_quote_evidence_never_triggers_reference_comparison(changes, reason):
    result = check(quote(**changes))
    assert reason in result["reason_codes"]
    assert not result["comparable_to_raw_daily"]


def test_old_quote_is_stale_even_if_recollected_now():
    # Collection later than the quote itself is normal write-out latency, so
    # the quote keeps its own older date and stays stale rather than becoming
    # "collected today".
    result = check(quote(quote_time="2026-10-22 15:00:02", updated_at="2026-10-23 10:00:00"))
    assert result["status"] == "stale"
    assert result["quote_timestamp"].startswith("2026-10-22")
    assert not result["usable_latest_quote"]


def test_friday_close_remains_a_weekend_snapshot_not_live():
    result = check(quote(quote_time="2026-10-23 15:00:02", updated_at="2026-10-23 15:05:02"),
                   now="2026-10-24T10:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 23))
    assert result["status"] == "session_snapshot"
    assert result["usable_latest_quote"]
    assert not result["market_active"]


def test_unknown_price_basis_does_not_become_raw_from_provider_name():
    result = check(quote(price_basis="", source="eastmoney.single_quote"))
    assert result["price_basis"] == "mixed_or_unknown"
    assert not result["comparable_to_raw_daily"]


def frame():
    dates = pd.bdate_range(end="2026-10-23", periods=90)
    return pd.DataFrame({"date": dates, "open": 10.0, "high": 10.2, "low": 9.8,
                         "close": 10.0, "volume": 1000, "adjust": "raw"})


def existing_scenario(data):
    return {"available": True, "price_band": {"latest": 10.0},
            "support_resistance": {"low_60d": 8.0, "high_60d": 10.5},
            "scenario_projection": {
                "five_day_range": {"low": 9.0, "high": 11.0}, "bias": "fixture bias",
                "thesis": "fixture thesis", "structure_note": "fixture structure",
                "scenarios": [{"tone": "strong", "trigger": "收盘和量能确认"},
                              {"tone": "risk", "trigger": "跌破时核对"}]}}


def reference(original=None, *, row=None, evidence=None, now="2026-10-23T10:00:10+08:00", calendar=None, builder=existing_scenario, complete=date(2026, 10, 23)):
    return build_stock_reference(frame() if original is None else original, symbol="600000",
                                 now=datetime.fromisoformat(now), complete_trade_date=complete,
                                 calendar=calendar if calendar is not None else {"is_open_by_date": {"2026-10-26": True}},
                                 research_builder=builder, quote=row or quote(),
                                 quote_evidence=evidence if evidence is not None else check(row or quote()))


def test_reference_uses_existing_anchors_without_orders_or_pnl():
    result = reference()
    assert result["available"]
    assert [row["price"] for row in result["levels"]] == [10.5, 9.0, 11.0]
    assert result["valid_until"] == "2026-10-26T15:00:00+08:00"
    assert not result["actionable_order"]
    assert result["label"] == "纸面 · 未验证"
    assert "收盘确认" in result["current_assessment"]
    assert "pnl" not in result


@pytest.mark.parametrize("changes", ["missing", "mixed", "unknown", "qfq", "hfq"])
def test_original_price_basis_is_checked_before_any_filtering(changes):
    original = frame()
    original.loc[0, "date"] = "2099-01-01"
    if changes == "missing":
        original = original.drop(columns=["adjust"])
    elif changes == "mixed":
        original.loc[0, "adjust"] = "qfq"
    elif changes == "unknown":
        original.loc[0, "adjust"] = "mystery"
    else:
        original["adjust"] = changes
    builder = Mock()
    result = reference(original, builder=builder)
    assert not result["available"]
    assert result["status"] == "blocked"
    builder.assert_not_called()


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_bad_daily_price_blocks_only_reference_not_quote(price):
    original = frame()
    original.loc[0, "close"] = price
    result = reference(original)
    assert result["reason_codes"] == ["daily_price_invalid"]
    assert not result["available"]


def test_future_daily_bar_is_not_used_to_form_current_levels():
    original = pd.concat([frame(), frame().iloc[-1:].assign(date=pd.Timestamp("2026-10-26"), close=11, high=12)])
    captured = []
    def builder(data):
        captured.append(data["date"].max())
        return existing_scenario(data)
    result = reference(original, builder=builder)
    assert captured == [pd.Timestamp("2026-10-23")]
    assert "incomplete_or_future_daily_rows_excluded" in result["reason_codes"]
    assert "1条未完成或未来日期日线" in result["warnings"][0]


def test_conflicting_same_day_prices_are_not_silently_deduplicated():
    original = pd.concat([frame(), frame().iloc[-1:].assign(close=10.1)])
    assert "conflicting_daily_duplicates" in reference(original)["reason_codes"]


def test_missing_calendar_does_not_invent_validity_date():
    result = reference(calendar={})
    assert result["available"]
    assert result["valid_until"] == ""
    assert not result["reference_current"]
    assert "next_trade_date_unverified" in result["reason_codes"]


def test_expired_daily_reference_cannot_claim_current_signal():
    result = reference(now="2026-10-26T15:01:00+08:00")
    assert not result["reference_current"]
    assert "daily_reference_expired" in result["reason_codes"]


def test_unverifiable_quote_falls_back_to_a_labelled_daily_close_comparison():
    # The quote itself cannot be verified, but the completed raw daily close is
    # an honest reference: the result must say so instead of hiding the levels.
    result = reference(evidence=check(quote(quote_time="15:00:02")))
    assert result["available"]
    assert result["assessment_basis"] == "daily_close"
    assert "日线收盘口径" in result["current_assessment"]
    assert not result["actionable_order"]


@pytest.mark.parametrize("price,expected", [(10.0, "观望"), (10.6, "收盘确认"), (8.9, "退出观察线"), (11.2, "止盈观察位")])
def test_current_comparison_is_conditional_not_an_order(price, expected):
    result = reference(row=quote(price=price))
    assert expected in result["current_assessment"]
    assert not result["actionable_order"]


def test_weekend_is_not_labelled_an_intraday_signal():
    row = quote(quote_time="2026-10-23 15:00:00", updated_at="2026-10-23 15:01:00")
    evidence = check(row, now="2026-10-24T10:00:00+08:00", session="non_trading_day", expected=date(2026, 10, 23))
    result = reference(row=row, evidence=evidence, now="2026-10-24T10:00:00+08:00")
    assert "休市" in result["current_assessment"]
    assert "盘中信号" in result["current_assessment"]
    assert result["assessment_basis"] == "daily_close"
    assert not result["actionable_order"]


def test_actual_existing_research_builder_remains_unchanged():
    from scripts.quant_api_server import build_research_reference
    original = frame()
    original["close"] = [10 + index * 0.01 + (index % 7) * 0.025 for index in range(90)]
    original["open"] = original["close"] - 0.03
    original["high"] = original["close"] + 0.08
    original["low"] = original["close"] - 0.09
    before = build_research_reference(original)
    result = reference(original, builder=build_research_reference)
    assert result["available"]
    assert result["levels"][0]["price"] == round(min(before["support_resistance"]["high_60d"], before["scenario_projection"]["five_day_range"]["high"]), 2)
    assert before == build_research_reference(original)


def test_overlapping_entry_and_profit_levels_do_not_invent_a_target():
    def builder(data):
        scenario = existing_scenario(data)
        scenario["support_resistance"]["high_60d"] = 12.0
        return scenario
    result = reference(builder=builder, evidence=check(quote(quote_time="15:00:02")))
    assert result["available"]
    assert not result["entry_setup_available"]
    assert result["levels"][2]["price"] is None
    assert "没有可单列" in result["levels"][2]["condition"]
    assert any("先观望" in warning for warning in result["warnings"])
