"""Regression evidence for maturity, fee attribution, paired prices and daily risk."""
from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk import morning_brief, weekly_review
from quantdesk.paper.trade_costs import TradeCostConfig
from quantdesk.research import exit_rule_replay as replay


def days(count: int = 22, start: date = date(2026, 9, 1)) -> tuple[str, ...]:
    found = []
    while len(found) < count:
        if start.weekday() < 5:
            found.append(start.isoformat())
        start += timedelta(days=1)
    return tuple(found)


def bar(day: str, close: float = 10.0, *, opening: float | None = None,
        volume: float | None = 1000.0, pre_close: float | None = None) -> replay.Bar:
    opening = close if opening is None else opening
    return replay.Bar(day, opening, close, None, max(opening, close), min(opening, close), volume,
                      pre_close=pre_close)


def series(count: int = 22) -> list[replay.Bar]:
    return [bar(day, 10 + number * .03, pre_close=10 + (number - 1) * .03) for number, day in enumerate(days(count))]


def row(ident: str, ret: float, *, open_position: bool = False, bench: float | None = 0.0,
        entry: str = "2026-09-01", exit_day: str = "2026-09-03") -> dict:
    return {"trade_id": ident, "symbol": "600001", "entry_date": entry,
            "exit_date": "" if open_position else exit_day, "observation_date": exit_day,
            "entry_price": 10.0, "exit_price": 10.0, "buy_fee": 5.0, "sell_fee": 0.0 if open_position else 5.0,
            "still_open": open_position, "eligible_metrics": True, "pnl": ret * 10,
            "gross_return_pct": ret + 1.0, "fee_drag_pct": 1.0, "return_pct": ret,
            "cost": 10.0, "holding_days": 5, "benchmark_pct": bench, "out_of_sample": False}


def seed(root: Path, *, count: int = 22) -> list[replay.Bar]:
    quotes = series(count)
    (root / "state").mkdir(parents=True)
    (root / "state/auto_paper_portfolio.json").write_text(json.dumps({
        "positions": [{"symbol": "600001", "name": "样本", "entry_date": quotes[0].day,
                       "execution_price": quotes[0].close, "entry_price": 10.05}], "closed_positions": []}), encoding="utf-8")
    (root / "data/daily").mkdir(parents=True)
    (root / "data/benchmark").mkdir()
    (root / "data/calendar").mkdir()
    header = "date,symbol,open,high,low,close,volume,pre_close,adjust\n"
    body = "".join(f"{b.day},600001,{b.open},{b.high},{b.low},{b.close},{b.volume},{b.pre_close},raw\n" for b in quotes)
    (root / "data/daily/600001.csv").write_text(header + body, encoding="utf-8")
    (root / "data/benchmark/hs300.csv").write_text("date,open,high,low,close\n" + "".join(
        f"{b.day},{100 + n * .09},{100 + n * .1},{100 + n * .09},{100 + n * .1}\n" for n, b in enumerate(quotes)), encoding="utf-8")
    (root / "data/calendar/a_share_trading_calendar.csv").write_text(
        "date,is_open\n" + "".join(f"{day},true\n" for day in days(count + 10)), encoding="utf-8")
    return quotes


def test_unmatured_winner_does_not_raise_matured_average() -> None:
    summary = replay.summarize([row("old", -2.0), row("young", 40.0, open_position=True)])
    assert summary["trades"] == 1 and summary["still_open"] == 1
    assert summary["avg_return_pct"] == -2.0
    assert summary["avg_gross_return_pct"] - summary["avg_fee_drag_pct"] == summary["avg_return_pct"]
    assert replay.summarize([row("young", 40.0, open_position=True)])["trades"] == 0


def test_missing_benchmark_uses_paired_excess_not_difference_of_unequal_means() -> None:
    summary = replay.summarize([row("paired", 2.0, bench=1.0), row("unpaired", -90.0, bench=None)])
    assert summary["benchmark_pairs"] == 1
    assert summary["avg_net_excess_pct"] == 1.0
    assert summary["avg_gross_excess_pct"] == 2.0


def test_fee_loaded_legacy_entry_is_not_used_as_raw_fill() -> None:
    ledger = {"positions": [{"symbol": "600001", "entry_date": "2026-09-01", "entry_price": 10.05}]}
    trades = replay.trades_from_ledger(ledger)
    assert len(trades) == 1 and trades[0].entry_price is None
    result = replay.evaluate(trades, lambda _: series(), series(), last_day=days()[-1])
    assert result["unusable"][0]["reason"] == "recorded_execution_price_missing"
    assert result["variants"]["hold5"].rows == []


def test_open_valuation_does_not_charge_an_unexecuted_sale() -> None:
    bars = series(3)
    trade = replay.Trade("600001", "样本", bars[0].day, 10.0, recorded_open=True)
    outcome = replay.evaluate([trade], lambda _: bars, bars, last_day=bars[-1].day)["variants"]["hold5"].rows[0]
    assert outcome["still_open"] and outcome["exit_date"] == ""
    assert outcome["sell_fee"] == 0.0 and outcome["cost"] == outcome["buy_fee"]
    assert outcome["estimated_liquidation_sell_fee"] > 0
    assert outcome["gross_return_pct"] - outcome["fee_drag_pct"] == pytest.approx(outcome["return_pct"], abs=1e-6)
    assert TradeCostConfig().transfer_fee_rate == 0.0  # the real ledger default is unchanged


def test_pairing_excludes_an_unmatched_winner_and_keeps_repeated_symbol_entries() -> None:
    left = [row("first", 1.0), row("second", -1.0), row("left_only", 80.0)]
    right = [row("first", 0.0), row("second", -2.0)]
    paired = replay.paired_comparison(left, right)
    assert paired["pairs"] == 2 and paired["left_only"] == 1
    assert paired["net_delta_pct"] == -1.0 and paired["different_exit_dates"] == 0


def test_same_exit_price_comparison_separates_a_full_policy_change() -> None:
    bars = series(15)
    trade = replay.Trade("600001", "样本", bars[0].day, 10.0, recorded_open=True)
    result = replay.evaluate([trade], lambda _: bars, bars, last_day=bars[-1].day, sessions=days(15))
    close = result["variants"]["hold10_signal_close"].rows
    price = result["variants"]["hold10_next_open_same_exit"].rows
    policy = result["variants"]["hold10_next_open"].rows
    assert replay.paired_comparison(close, price)["different_exit_dates"] == 0
    assert replay.paired_comparison(close, policy)["different_exit_dates"] == 1
    assert price[0]["holding_days"] == 9 and policy[0]["holding_days"] == 10


def test_next_open_benchmark_uses_the_next_index_open() -> None:
    stock = series(15)
    index = [bar(b.day, 100 + n, opening=99 + n) for n, b in enumerate(stock)]
    trade = replay.Trade("600001", "样本", stock[0].day, 10.0, recorded_open=True)
    result = replay.evaluate([trade], lambda _: stock, index, last_day=stock[-1].day, sessions=days(15))
    actual = result["variants"]["hold10_next_open"].rows[0]
    assert actual["benchmark_pct"] == pytest.approx((111 / 100 - 1) * 100)
    assert actual["signal_benchmark_pct"] == actual["benchmark_pct"]
    # Changing the next index open affects only exposure benchmark, not the signal benchmark.
    changed = [replace(b, open=110.0) if n == 1 else b for n, b in enumerate(index)]
    revised = replay.evaluate([trade], lambda _: stock, changed, last_day=stock[-1].day, sessions=days(15))["variants"]["hold10_next_open"].rows[0]
    assert revised["benchmark_pct"] == pytest.approx((111 / 110 - 1) * 100)
    assert revised["signal_benchmark_pct"] == actual["signal_benchmark_pct"]


def test_missing_market_session_is_not_counted_as_a_longer_hold() -> None:
    full = series(9)
    observed = [b for n, b in enumerate(full) if n != 2]
    trade = replay.Trade("600001", "样本", full[0].day, 10.0, recorded_open=True)
    result = replay.evaluate([trade], lambda _: observed, full, last_day=full[-1].day, sessions=days(9))
    outcome = result["variants"]["hold5"].rows[0]
    assert outcome["exit_date"] == full[5].day and outcome["holding_days"] == 5
    assert outcome["missing_sessions"] == [full[2].day] and not outcome["eligible_metrics"]
    assert replay.summarize([outcome])["trades"] == 0


def test_missing_next_session_does_not_buy_a_later_available_open() -> None:
    full = series(6)
    trade = replay.Trade("600001", "样本", full[0].day, 10.0, session_days=days(6))
    assert replay.next_open_entry(trade, full[:1] + full[2:]) is None


@pytest.mark.parametrize("field,value,reason", [("volume", 0.0, "no_trading_volume"),
    ("volume", None, "volume_evidence_missing"), ("open", None, "ohlc_evidence_missing")])
def test_unverifiable_next_open_does_not_fill(field: str, value: object, reason: str) -> None:
    bars = series(4)
    bars[1] = replace(bars[1], **{field: value})
    trade = replay.Trade("600001", "样本", bars[0].day, 10.0)
    result = replay.evaluate([trade], lambda _: bars, bars, last_day=bars[-1].day)
    assert result["not_fillable"][0]["reason"] == reason
    assert result["variants"]["hold10_next_open"].rows == []


def test_limit_down_exit_waits_for_a_sellable_close() -> None:
    dates = days(3)
    bars = [bar(dates[0], 10, pre_close=10), bar(dates[1], 9, pre_close=10), bar(dates[2], 9.3, pre_close=9)]
    trade = replay.Trade("600001", "样本", dates[0], 10, session_days=dates)
    outcome = replay.exit_stop_take(-8, None)(trade, bars)
    assert outcome.exit_date == dates[2] and outcome.exit_price == 9.3
    assert outcome.blocked_exit_days == [{"date": dates[1], "reason": "limit_down"}]


def test_signal_close_limit_up_exclusion_is_reported_instead_of_hidden() -> None:
    dates = days(3)
    bars = [bar(dates[0], 11, pre_close=10), bar(dates[1], 10.5, pre_close=11), bar(dates[2], 10.6, pre_close=10.5)]
    trade = replay.Trade("600001", "样本", dates[0], 10.8, recorded_open=True)
    result = replay.evaluate([trade], lambda _: bars, bars, last_day=dates[-1], sessions=dates)
    assert result["close_not_fillable"][0]["reason"] == "limit_up"
    assert result["variants"]["hold10_signal_close"].rows == []


def test_unknown_previous_close_is_not_reconstructed_from_a_gap() -> None:
    dates = days(4)
    bars = [bar(dates[0], 10), bar(dates[2], 10), bar(dates[3], 10)]
    trade = replay.Trade("600001", "样本", dates[0], 10, session_days=dates)
    assert replay._fill_reason(trade, bars[1], bars, "buy", 10) == "limit_unverifiable"


@pytest.mark.parametrize("adjust", ["", "qfq", "raw,qfq"])
def test_price_basis_is_checked_before_date_cleanup(tmp_path: Path, adjust: str) -> None:
    path = tmp_path / "600001.csv"
    rows = "date,close,adjust\ninvalid,10," + adjust.split(",")[0] + "\n"
    if "," in adjust:
        rows += "2027-01-01,11,qfq\n"
    path.write_text(rows, encoding="utf-8")
    with pytest.raises(replay.EvidenceError) as error:
        replay.load_bars(path)
    assert error.value.reason in {"unknown_price_basis", "consumer_price_basis_not_allowed", "mixed_price_basis"}


def test_unknown_basis_in_a_future_row_still_blocks_the_original_frame(tmp_path: Path) -> None:
    path = tmp_path / "600001.csv"
    path.write_text("date,close,adjust\n2026-09-01,10,raw\n2027-01-01,11,\n", encoding="utf-8")
    with pytest.raises(replay.EvidenceError, match="unknown_price_basis"):
        replay.load_bars(path)


def test_duplicate_dates_are_not_silently_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "600001.csv"
    path.write_text("date,close,adjust\n2026-09-01,10,raw\n2026-09-01,11,raw\n", encoding="utf-8")
    with pytest.raises(replay.EvidenceError, match="duplicate_daily_date"):
        replay.load_bars(path)


def test_daily_drawdown_sees_a_float_loss_that_recovers_before_exit() -> None:
    dates = days(3)
    trade_row = row("r", 0, entry=dates[0], exit_day=dates[2])
    bars = [bar(dates[0], 10), bar(dates[1], 9), bar(dates[2], 10)]
    result = replay.daily_portfolio([trade_row], {"600001": bars}, dates, dates[-1], capital=10000)
    assert result["status"] == "observed_daily_equity"
    assert result["max_drawdown_pct"] == pytest.approx(1.05)
    assert result["curve"][1]["equity"] == 9895
    assert result["curve"][-1]["equity"] == 9990
    assert replay.summarize([trade_row])["max_drawdown_pct"] is None


def test_daily_missing_mark_remains_null_and_suppresses_drawdown() -> None:
    dates = days(3)
    result = replay.daily_portfolio([row("r", 0, entry=dates[0], exit_day=dates[2])],
                                    {"600001": [bar(dates[0]), bar(dates[2])]}, dates, dates[-1])
    assert result["status"] == "missing_daily_marks" and result["max_drawdown_pct"] is None
    assert result["curve"][1]["equity"] is None


def test_a_negative_cash_pool_is_not_promoted_to_valid_portfolio_metrics() -> None:
    dates = days(3)
    result = replay.daily_portfolio([row("r", 0, entry=dates[0], exit_day=dates[2])],
                                    {"600001": [bar(d) for d in dates]}, dates, dates[-1], capital=500)
    assert result["status"] == "insufficient_capital" and result["max_drawdown_pct"] is None


def test_a_late_same_day_sale_cannot_finance_an_earlier_buy() -> None:
    dates = days(3)
    first = row("first", 0, entry=dates[0], exit_day=dates[1])
    second = row("second", 0, entry=dates[1], exit_day=dates[2])
    result = replay.daily_portfolio([first, second], {"600001": [bar(d) for d in dates]}, dates, dates[-1], capital=1500)
    assert all(day["cash"] > 0 for day in result["curve"])
    assert result["minimum_cash"] == -510
    assert result["status"] == "insufficient_capital" and result["max_drawdown_pct"] is None


def test_untrusted_symbol_blocks_the_complete_requested_report(tmp_path: Path) -> None:
    quotes = seed(tmp_path)
    path = tmp_path / "data/daily/600001.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",raw\n", ",qfq\n"), encoding="utf-8")
    text, result = replay.build_report(tmp_path, quotes[-1].day)
    assert result["status"] == "blocked" and result["summaries"] == {} and result["rows"] == {}
    assert "停止计算" in text and "avg_return_pct" not in text


def test_changed_inputs_cannot_leave_mixed_version_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quotes = seed(tmp_path)
    def changed(self: object) -> None:
        raise replay.EvidenceError("input_changed_during_read")
    monkeypatch.setattr(replay.InputAudit, "verify", changed)
    _, result = replay.build_report(tmp_path, quotes[-1].day)
    assert result["status"] == "blocked" and result["input_verification"] == "not_verified"
    assert result["rows"] == {} and result["summaries"] == {}
    assert "daily_portfolios" not in result and "comparisons" not in result


def test_report_has_mature_common_cohort_and_a_distinct_out_of_sample_count(tmp_path: Path) -> None:
    quotes = seed(tmp_path)
    text, data = replay.build_report(tmp_path, quotes[-1].day)
    assert data["status"] == "research_partial" and data["input_verification"] == "stable_sha256"
    assert data["comparisons"]["fixed_common"]["count"] == 1
    assert data["summaries"]["hold20"]["out_of_sample"]["trades"] == 0
    assert data["protocol_sha256"] == replay.PROTOCOL_SHA256
    assert data["corporate_actions_status"] == "not_fully_verified" and not data["formal_comparison_ready"]
    assert "选股本身跑输大盘" not in text and "股价表现、费用" in text


def test_cli_updates_a_failed_run_with_explicit_blocked_evidence(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "inputs"
    quotes = seed(root)
    spec = importlib.util.spec_from_file_location("exit_cli_contract", Path(__file__).resolve().parents[1] / "scripts/run_exit_rule_replay.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    output = tmp_path / "output"
    assert cli.main(["--root", str(root), "--as-of", quotes[-1].day, "--output-dir", str(output)]) == 0
    path = root / "data/daily/600001.csv"
    path.write_text("date,close\n2026-09-01,10\n", encoding="utf-8")
    assert cli.main(["--root", str(root), "--as-of", quotes[-1].day, "--output-dir", str(output)]) == 2
    latest = json.loads((output / f"exit_rule_replay_{quotes[-1].day}.json").read_text(encoding="utf-8"))
    assert latest["status"] == "blocked" and latest["summaries"] == {}


def test_entry_feature_does_not_look_at_later_prices() -> None:
    bars = [bar(d, 10 + n * .01) for n, d in enumerate(days(25, date(2026, 8, 3)))]
    trade = replay.Trade("600001", "样本", bars[20].day, bars[20].close, session_days=tuple(b.day for b in bars))
    before = replay.entry_features(trade, bars)
    changed = [replace(b, close=100) if n > 20 else b for n, b in enumerate(bars)]
    assert replay.entry_features(trade, changed) == before


def test_weekly_entry_note_refuses_unknown_stock_price_basis(tmp_path: Path) -> None:
    path = tmp_path / "data/daily/600001.csv"
    path.parent.mkdir(parents=True)
    path.write_text("date,close\n2026-09-01,10\n", encoding="utf-8")
    assert "价格口径未核实" in weekly_review.entry_note(tmp_path, "600001", "2026-09-01")


@pytest.mark.parametrize("date_status,status,reason", [("stale", "warning", "brief_data_not_current"),
                                                       ("current", "blocked", "brief_data_not_current")])
def test_weekly_replay_does_not_run_on_unverified_brief_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          date_status: str, status: str, reason: str) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("Replay was called for unverified data")
    monkeypatch.setattr(replay, "write_report", forbidden)
    result = morning_brief._weekly_exit_replay(tmp_path, {"as_of": "2026-11-06", "date_status": date_status, "status": status},
                                              datetime(2026, 11, 9, 8, 35, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["reason"] == reason


def test_weekly_replay_uses_existing_first_session_and_correct_business_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def written(root: Path, as_of: str, output: Path) -> dict:
        calls.append((root, as_of, output))
        return {"status": "research_partial", "as_of": as_of}
    monkeypatch.setattr(replay, "write_report", written)
    result = morning_brief._weekly_exit_replay(tmp_path, {"as_of": "2026-11-06", "date_status": "current", "status": "ok"},
                                              datetime(2026, 11, 9, 8, 35, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["status"] == "research_partial"
    assert calls == [(tmp_path, "2026-11-06", tmp_path / "reports/research")]
    result = morning_brief._weekly_exit_replay(tmp_path, {"as_of": "2026-11-02", "date_status": "current", "status": "ok"},
                                              datetime(2026, 11, 3, 8, 35, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["status"] == "skipped" and len(calls) == 1


def test_failed_weekly_research_retains_generated_owner_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "write_report", lambda *args: (_ for _ in ()).throw(replay.EvidenceError("test_missing")))
    now = datetime(2026, 11, 9, 8, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = {"as_of": "2026-11-06", "date_status": "current", "status": "ok"}
    payload["weekly_exit_replay"] = morning_brief._weekly_exit_replay(tmp_path, payload, now)
    monkeypatch.setattr(morning_brief.owner_view, "build_owner_digest", lambda *args, **kwargs: {})
    monkeypatch.setattr(morning_brief.owner_view, "render_owner_brief", lambda *args, **kwargs: ("早报", "正文已经生成"))
    monkeypatch.setattr(weekly_review, "build_weekly_review", lambda *args: {})
    monkeypatch.setattr(weekly_review, "render_weekly_review", lambda *args: "上周回顾")
    owner = morning_brief._owner_brief(tmp_path, payload, now)
    assert "正文已经生成" in owner["body"] and "本次未能更新" in owner["body"]
    assert payload["weekly_exit_replay"]["status"] == "warning"
