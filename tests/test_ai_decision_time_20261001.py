"""D2/T14：决策时间三分（行情日期 / 本次生成时间 / 恢复的旧计划时间）。

全部为合成临时夹具：无网络、无模型调用、不写正式账本与正式 state。
判据来自 D2 单：① 陈旧行情生成新判断不得倒填；② 正常当日；③ 恢复旧计划原时间不改；
④ 重建历史没有当晚时刻就留空，不拿重建那一刻的时钟补造。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ai_trader import decision_log as dl
from ai_trader import overnight as evening

STALE_DAY = "2026-09-25"  # 行情最后一根 K 线（比真实今天旧）
PREV_DAY = "2026-09-24"
NEXT_TRADE_DAY = "2026-09-28"
SYMBOL = "600000"
HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _write_daily(root: Path) -> Path:
    daily = root / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    rows = [
        HEADER,
        f"{PREV_DAY},{SYMBOL},10,10.1,9.9,10,1000,10000,0,raw",
        f"{STALE_DAY},{SYMBOL},10,10.0,10.0,10,1000,10000,0,raw",
    ]
    (daily / f"{SYMBOL}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return daily


def _plan(decision_date: str = STALE_DAY) -> dict:
    return {
        "schema_version": evening.PLAN_SCHEMA,
        "decision_date": decision_date,
        "trade_date": NEXT_TRADE_DAY,
        "committed": True,
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": "观望"},
        "intents": [
            {"symbol": SYMBOL, "side": "buy", "ref_close": 10.0,
             "reason": "今天收盘价10元", "style": "波段", "planned_hold_days": 1}
        ],
    }


def _row(records: list[dict], symbol: str = SYMBOL) -> dict:
    return next(item for item in records if item.get("symbol") == symbol)


def test_stale_quote_run_stamps_the_real_generation_time(tmp_path: Path) -> None:
    """① 陈旧行情：行情日保持真实，本次生成时间必须是今天（不倒填）。"""

    daily = _write_daily(tmp_path)
    plan = _plan()  # decision_date = 陈旧行情日
    gated = dl.apply_gate_to_plan(plan, daily_dir=daily, name_to_symbol={})

    assert plan["decision_date"] == STALE_DAY, "行情日期不得被改写成今天"
    recorded = str(plan["recorded_at"])
    assert recorded.startswith(_today()), f"生成时间应为今天，实际 {recorded}"
    assert not recorded.startswith(STALE_DAY), "生成时间不得倒填成行情日"
    assert gated.accepted_intents[0]["recorded_at"] == recorded

    row = _row(dl.records_from_plan(plan, daily_dir=daily, rejections=gated.rejections))
    assert row["decision_date"] == STALE_DAY, "记录里的行情日保持陈旧那一天"
    assert str(row["recorded_at"]).startswith(_today()), "记录里的生成时间也必须是今天"
    assert not str(row["recorded_at"]).startswith(STALE_DAY)
    assert str(row["structured_at"]).startswith(_today())


def test_same_day_run_keeps_one_real_date(tmp_path: Path) -> None:
    """② 正常当日：行情日与生成时间同为今天。"""

    daily = _write_daily(tmp_path)
    today = _today()
    plan = _plan(decision_date=today)
    gated = dl.apply_gate_to_plan(plan, daily_dir=daily, name_to_symbol={})

    row = _row(dl.records_from_plan(plan, daily_dir=daily, rejections=gated.rejections))
    assert row["decision_date"] == today
    assert str(row["recorded_at"]).startswith(today)
    assert str(row["structured_at"]).startswith(today)


def test_recovery_keeps_the_original_plan_time(tmp_path: Path) -> None:
    """③ 恢复旧计划：原时间不动，重建时刻另存。"""

    daily = _write_daily(tmp_path)
    original = f"{STALE_DAY}T20:20:00+08:00"
    plan = _plan()
    plan["recorded_at"] = original
    plan["intents"][0]["recorded_at"] = original

    saved = tmp_path / "state" / "plan_latest.json"
    evening.save_plan(plan, saved)
    reloaded = evening.load_plan(saved)
    assert reloaded["recorded_at"] == original, "落盘/读回不得改原时间"

    gated = dl.apply_gate_to_plan(reloaded, daily_dir=daily, name_to_symbol={})
    assert reloaded["recorded_at"] == original, "恢复旧计划不得被今天的时间覆盖"
    assert gated.accepted_intents[0]["recorded_at"] == original

    row = _row(dl.records_from_plan(reloaded, daily_dir=daily, rejections=gated.rejections))
    assert row["recorded_at"] == original, "记录沿用当晚原时间"
    assert row["decision_date"] == STALE_DAY
    assert str(row["structured_at"]) != original, "重建时刻与原时间分开保存"


def test_rebuild_without_stored_time_leaves_it_empty(tmp_path: Path) -> None:
    """④ 历史重建：没有当晚时刻就留空，不拿重建的时钟补造。"""

    daily = _write_daily(tmp_path)
    plan = _plan()
    plan.pop("recorded_at", None)  # 旧计划本来就没存时刻

    row = _row(dl.records_from_plan(plan, daily_dir=daily, rejections=[], backfill=True))

    assert row["recorded_at"] == "", "没有当晚时刻必须留空"
    assert row["backfill"] is True and row["provenance"] == "historical_review"

def _journal_row(*, recorded_at: str | None, as_of: str = STALE_DAY) -> dict:
    return {
        "as_of": as_of,
        "date": as_of,
        "tomorrow_plan": {
            "trade_date": NEXT_TRADE_DAY,
            "intents": _plan()["intents"],
            "statement_rejections": [],
            "recorded_at": recorded_at,
        },
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": "观望"},
    }


def test_journal_rebuild_keeps_stored_generation_time_on_a_stale_day(tmp_path: Path) -> None:
    """⑤ 日记重建：行情日仍是旧 K 线那天，生成时间沿用当晚存下的真实时刻。"""

    daily = _write_daily(tmp_path)
    generated = f"{_today()}T20:20:00+08:00"

    plan = dl.plan_from_journal_row(_journal_row(recorded_at=generated))
    assert plan["decision_date"] == STALE_DAY
    assert plan["recorded_at"] == generated

    row = _row(dl.records_from_plan(plan, daily_dir=daily, rejections=[], backfill=True))
    assert row["decision_date"] == STALE_DAY, "行情日保持旧 K 线那天"
    assert row["recorded_at"] == generated, "生成时间沿用当晚存下的时刻"
    assert row["provenance"] == "historical_review"


def test_journal_rebuild_without_stored_time_does_not_invent_one(tmp_path: Path) -> None:
    """⑥ 日记没存时刻时留空：不得拿行情日或重建时钟冒充当晚时刻。"""

    daily = _write_daily(tmp_path)

    plan = dl.plan_from_journal_row(_journal_row(recorded_at=None))
    assert plan["recorded_at"] == ""

    row = _row(dl.records_from_plan(plan, daily_dir=daily, rejections=[], backfill=True))
    assert row["recorded_at"] == "", "不得拿行情日或重建时钟冒充当晚时刻"
    assert row["decision_date"] == STALE_DAY
