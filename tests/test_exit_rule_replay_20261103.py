"""Exit-rule replay (2026-11-03): rules decide on each close with no later rows."""

from __future__ import annotations

import json
from pathlib import Path

from quantdesk.research import exit_rule_replay as replay


def _bar(day: str, opening: float | None, close: float, delta: float | None = None) -> replay.Bar:
    opening = close if opening is None else opening
    return replay.Bar(day, opening, close, delta, max(opening, close), min(opening, close), 1000.0)


def _bars(closes: list[float], start_day: int = 1, change: bool = False) -> list[replay.Bar]:
    bars = []
    previous = None
    for offset, close in enumerate(closes):
        day = f"2026-09-{start_day + offset:02d}"
        delta = (close - previous) if (change and previous is not None) else None
        bars.append(_bar(day, close, close, delta))
        previous = close
    return bars


def _trade(entry_day: str = "2026-09-01", price: float = 10.0) -> replay.Trade:
    return replay.Trade("600001", "样本", entry_day, price)


def test_fixed_hold_exits_on_the_nth_close_after_entry() -> None:
    bars = _bars([10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
    outcome = replay.exit_fixed(5)(_trade(), bars)
    assert (outcome.exit_date, outcome.exit_price, outcome.holding_days, outcome.still_open) == ("2026-09-06", 10.5, 5, False)


def test_fixed_hold_without_enough_rows_stays_open_at_last_close() -> None:
    outcome = replay.exit_fixed(5)(_trade(), _bars([10, 10.1, 10.2]))
    assert (outcome.exit_date, outcome.still_open, outcome.holding_days) == ("2026-09-03", True, 2)


def test_stop_and_take_profit_use_only_the_close_of_each_day() -> None:
    stop = replay.exit_stop_take(-8.0, 15.0)(_trade(), _bars([10, 9.5, 9.19, 12]))
    assert (stop.exit_date, stop.exit_price) == ("2026-09-03", 9.19)
    take = replay.exit_stop_take(-8.0, 15.0)(_trade(), _bars([10, 11, 11.6, 8]))
    assert (take.exit_date, take.exit_price) == ("2026-09-03", 11.6)


def test_no_later_row_changes_an_earlier_exit() -> None:
    base = _bars([10, 9.0, 8.5, 9.1, 9.3])
    longer = base + _bars([20, 30], start_day=6)
    rule = replay.exit_stop_take(-8.0, None)
    # The first trigger is a limit-down close and cannot be sold there.
    assert rule(_trade(), base).exit_date == rule(_trade(), longer).exit_date == "2026-09-03"
    assert rule(_trade(), base).blocked_exit_days == [{"date": "2026-09-02", "reason": "limit_down"}]


def test_ma_exit_waits_for_a_full_window() -> None:
    closes = [10.0] * 20 + [10.5, 9.9]
    bars = [_bar(f"2026-08-{index + 1:02d}" if index < 31 else "", None, close, None)
            for index, close in enumerate(closes)]
    trade = replay.Trade("600001", "样本", bars[19].day, 10.0)
    outcome = replay.exit_stop_or_ma(-8.0, 20)(trade, bars)
    assert outcome.exit_date == bars[21].day


def test_ex_rights_jump_is_flagged_not_repaired() -> None:
    bars = _bars([10, 10.2, 7.0, 7.1], change=False)
    assert replay.ex_rights_days(bars, "600001", "2026-09-01", "2026-09-04") == ["2026-09-03"]
    gap = [replay.Bar("2026-09-01", 10, 10, None), replay.Bar("2026-09-02", 9.7, 9.8, 0.1)]
    assert replay.ex_rights_days(gap, "600001", "2026-09-01", "2026-09-02") == ["2026-09-02"]
    assert replay.ex_rights_days(_bars([10, 11.9, 13.9]), "300001", "2026-09-01", "2026-09-03") == []


def test_next_open_entry_uses_the_following_open_and_skips_limit_up() -> None:
    bars = [_bar("2026-09-01", 10.0, 10.0), _bar("2026-09-02", 10.3, 10.5)]
    shifted = replay.next_open_entry(_trade(), bars)
    assert (shifted.entry_date, shifted.entry_price) == ("2026-09-02", 10.3)
    sealed = [_bar("2026-09-01", 10.0, 10.0), _bar("2026-09-02", 11.0, 11.0)]
    assert replay.next_open_entry(_trade(), sealed) is None


def test_next_open_variant_is_reported_beside_same_day_close() -> None:
    series = {"600001": [_bar(f"2026-09-{day:02d}", 10 + day * 0.1, 10 + day * 0.1) for day in range(1, 16)]}
    index = [replay.Bar(f"2026-09-{day:02d}", 100, 100 + day, None) for day in range(1, 16)]
    result = replay.evaluate([_trade("2026-09-01", 10.1)], lambda symbol: series[symbol], index, last_day="2026-09-15")
    same_day = result["variants"]["hold10"].rows[0]
    next_open = result["variants"]["hold10_next_open"].rows[0]
    assert (next_open["entry_date"], next_open["signal_date"]) == ("2026-09-02", "2026-09-01")
    assert next_open["benchmark_pct"] == same_day["benchmark_pct"] or next_open["exit_date"] != same_day["exit_date"]
    assert result["not_fillable"] == []


def test_costs_include_the_five_yuan_minimum_both_ways() -> None:
    cost = replay.trade_cost(1220.0, 1250.0)
    assert round(cost, 4) == round(5.0 + 1220 * 0.00001 + 5.0 + 1250 * 0.0005 + 1250 * 0.00001, 4)


def test_evaluate_splits_out_of_sample_and_reports_unusable(tmp_path: Path) -> None:
    trades = [_trade("2026-09-01"), replay.Trade("600002", "缺数据", "2026-09-01", 10.0),
              replay.Trade("600001", "样本外", "2026-11-03", 10.0)]
    series = {"600001": _bars([10, 10.5, 11, 11.5, 12, 12.5]) + [_bar("2026-11-03", 10, 10),
                                                                _bar("2026-11-04", 10, 11)]}
    index = _bars([100, 101, 102, 103, 104, 105]) + [replay.Bar("2026-11-03", 100, 100, None),
                                                      replay.Bar("2026-11-04", 100, 101, None)]
    result = replay.evaluate(trades, lambda symbol: series.get(symbol, []), index, last_day="2026-11-04")
    assert [row["name"] for row in result["unusable"] if row["reason"] == "entry_daily_missing"] == ["缺数据"]
    hold5 = result["variants"]["hold5"].rows
    assert {row["out_of_sample"] for row in hold5} == {False, True}
    in_sample = replay.summarize([row for row in hold5 if not row["out_of_sample"]])
    assert in_sample["trades"] == 1 and in_sample["avg_benchmark_pct"] == 5.0


def test_cli_writes_report_without_touching_inputs(tmp_path: Path) -> None:
    import importlib.util
    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)
    ledger = {"closed_positions": [{"symbol": "600001", "name": "样本", "entry_date": "2026-09-01", "entry_price": 10.05,
                                    "execution_price": 10,
                                    "exit_date": "2026-09-03", "exit_price": 9.1}], "positions": []}
    (root / "state/auto_paper_portfolio.json").write_text(json.dumps(ledger), encoding="utf-8")
    daily = root / "data/daily"
    daily.mkdir(parents=True)
    rows = "".join(f"2026-09-{day:02d},600001,{c},{c},{c},{c},1,1,,,raw\n" for day, c in
                   [(1, 10), (2, 9.5), (3, 9.1), (4, 9.4), (5, 9.6), (8, 9.8)])
    (daily / "600001.csv").write_text("date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust\n" + rows,
                                      encoding="utf-8")
    bench = root / "data/benchmark"
    bench.mkdir(parents=True)
    (bench / "hs300.csv").write_text("date,open,high,low,close\n" + "".join(
        f"2026-09-{day:02d},1,1,1,{4000 + day}\n" for day in (1, 2, 3, 4, 5, 8)), encoding="utf-8")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    spec = importlib.util.spec_from_file_location("replay_cli", Path(__file__).resolve().parents[1] / "scripts/run_exit_rule_replay.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    out = tmp_path / "out"
    assert cli.main(["--root", str(root), "--output-dir", str(out)]) == 0
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    text = (out / "exit_rule_replay_2026-09-08.md").read_text(encoding="utf-8")
    assert "固定拿 5 个交易日" in text and "样本外" in text and "不改策略账" in text
