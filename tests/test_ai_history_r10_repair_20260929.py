"""R10 三组反例的最小回归（T03 / T12 / T13）。

三件事各自钉一条最小反例：

1. 后来修正过的日线**不得**改写当晚已落盘的裁定、编号与身份（`HISTORY-REGATE`）；
2. 重建历史时**不得**丢掉当晚被拒的原提议（`REJECTION-MERGE`）；
3. 基准文件的日期**必须**是真日期，日历缺上一交易日时**不得**标绿（`BENCHMARK-DATE`）。

装置复用（不复制业务实现）：`tests/test_product_closeout_20260924_s2.py` 的真函数夹具
（当晚 gate→save_plan→日记，以及 ingest_state 重建）与 `tests/test_ops_write_health_line.py`
的日历/取数夹具。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import decision_log as dl
import test_ops_write_health_line as health
import test_product_closeout_20260924_s2 as s2

BENCH_DAY = "2026-09-24"
WRONG_DAY_NOTE = "无法判定"


def decisions(rows: list[dict]) -> list[tuple[str, str, bool, str]]:
    """一眼可比的裁定指纹：谁、什么结果、有没有通过、编号是多少。"""

    return sorted(
        (str(row.get("symbol") or ""), str(row.get("result") or ""),
         bool(row.get("committed")), str(row.get("decision_id") or ""))
        for row in rows
    )


def seed_benchmarks(root: Path, *, hs300_day: str) -> None:
    for name in health.load_mod().BENCHMARK_FILES:
        path = root / "data" / "benchmark" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        day = hs300_day if name == "hs300.csv" else BENCH_DAY
        path.write_text(f"date,close\n{day},100\n", encoding="utf-8")


def test_corrected_daily_bar_does_not_rewrite_the_accepted_night_decision(tmp_path) -> None:
    """当晚通过、当时对得上数据：事后把收盘价改成 10.2，原判与编号都不许变。"""

    daily, state, plan, gated = s2._night(tmp_path, reason=s2.GOOD_REASON)
    night = dl.records_from_plan(plan, daily_dir=daily, rejections=gated.rejections)

    s2._write_daily_bars(daily, close=10.2)
    _, rebuilt = s2._rebuild(tmp_path, daily, state)

    assert decisions(rebuilt) == decisions(night), "历史被后来的行情改判了"
    row = next(item for item in rebuilt if item.get("symbol") == s2.SYMBOL)
    assert row["committed"] is True
    assert row["judgment"] != dl.RESULT_MISMATCH
    # 新证据不能装作没看见：差异要另存为复核状态，而不是回写当晚裁定。
    recheck = row.get("post_night_recheck") or {}
    assert recheck.get("verdict") == dl.RESULT_MISMATCH, "事后不符必须留痕"
    assert recheck.get("hits"), "复核要带上具体哪句话对不上"
    assert recheck.get("as_of") == night[0]["decision_date"]


def test_rebuild_keeps_both_the_accepted_and_the_rejected_night_decisions(tmp_path, monkeypatch) -> None:
    """同一晚一通过一被拒：改掉通过那笔的价格后，两条历史都要留着。"""

    make_plan = s2._plan

    def two_intents(reason: str) -> dict:
        plan = make_plan(reason)
        plan["intents"].append({"symbol": "600001", "side": "buy", "ref_close": 10.0,
                                "reason": s2.BAD_REASON, "style": "波段", "planned_hold_days": 1})
        return plan

    monkeypatch.setattr(s2, "_plan", two_intents)
    s2._write_daily_bars(tmp_path / "daily", close=10.0)
    sample = tmp_path / "daily" / f"{s2.SYMBOL}.csv"
    (tmp_path / "daily" / "600001.csv").write_text(
        sample.read_text(encoding="utf-8").replace(s2.SYMBOL, "600001"), encoding="utf-8")

    daily, state, plan, gated = s2._night(tmp_path, reason=s2.GOOD_REASON)
    night = dl.records_from_plan(plan, daily_dir=daily, rejections=gated.rejections)

    s2._write_daily_bars(daily, close=10.2)
    _, rebuilt = s2._rebuild(tmp_path, daily, state)

    assert decisions(rebuilt) == decisions(night), "重建把当晚的原拒绝挤掉了"
    symbols = [row.get("symbol") for row in rebuilt]
    assert symbols.count(s2.SYMBOL) == 1, "同一提议不能既通过又被拒（重复）"
    assert symbols.count("600001") == 1, "原拒绝必须且只能出现一次"


def test_invalid_benchmark_date_is_never_green(tmp_path) -> None:
    """日期栏写不成真日期时，基准不能判绿，要点名坏日期。"""

    for invalid in ("not-a-date", "2026-99-99"):
        health.write_cal(tmp_path, [("2026-09-23", "true"), (BENCH_DAY, "true")])
        seed_benchmarks(tmp_path, hs300_day=invalid)

        cell, note = health.load_mod().benchmark_cell(tmp_path, BENCH_DAY)

        assert "✓" not in cell, (invalid, cell, note)
        assert "坏日期" in note, (invalid, cell, note)


def test_benchmark_without_a_previous_session_is_never_green(tmp_path) -> None:
    """日历里没有目标日之前的交易日时，没有可比的基准日，不能判绿。"""

    health.write_cal(tmp_path, [(BENCH_DAY, "true")])
    seed_benchmarks(tmp_path, hs300_day="2020-01-01")

    cell, note = health.load_mod().benchmark_cell(tmp_path, BENCH_DAY)

    assert "✓" not in cell, (cell, note)
    assert WRONG_DAY_NOTE in note, (cell, note)


def test_benchmark_that_reaches_the_previous_session_is_still_green(tmp_path) -> None:
    """对照：三份基准都到上一交易日（或更新）时仍是绿的，且显示最旧的那天。"""

    health.write_cal(tmp_path, [("2026-09-23", "true"), (BENCH_DAY, "true")])
    seed_benchmarks(tmp_path, hs300_day=BENCH_DAY)

    cell, note = health.load_mod().benchmark_cell(tmp_path, BENCH_DAY)

    assert cell == f"基准 ✓{BENCH_DAY}", (cell, note)
    assert note == "" or "降级" in note, (cell, note)


def test_records_from_plan_keeps_plan_rejections_even_when_caller_passes_its_own(tmp_path) -> None:
    """直接钉住 `records_from_plan`：调用方给了 rejections，也不能丢掉计划里的原拒绝。"""

    daily, _state, plan, gated = s2._night(tmp_path, reason=s2.BAD_REASON)
    assert plan.get("statement_rejections"), "夹具应产生一条当晚被拒的原提议"

    rows = dl.records_from_plan(plan, daily_dir=daily, rejections=gated.rejections)

    kept = [row for row in rows if row.get("symbol") == s2.SYMBOL and row.get("side") == "buy"]
    assert len(kept) == 1, json.dumps([row.get("decision_id") for row in rows], ensure_ascii=False)
    assert kept[0]["result"] == dl.RESULT_MISMATCH
