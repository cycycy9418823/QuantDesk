"""Round 13 收尾：账本 `applied_execution_ids` 的集合语义契约。

问题背景（磁盘实测，2026-08-06）：`state/auto_paper_portfolio.json` 里
`plan_20260804_for_20260805:2026-08-05` 出现两次 —— 27 条记录只对应 26 个
真实执行。成因是 `apply_conditional_execution_to_state` 在 `allow_same_day_rerun=True`
时绕过去重闸门后，仍无条件 `applied.append(execution_id)`。

这不是美观问题：该列表既是"这天是否已放过"的判据，也是漏放扫描
（[[test_round13_unreplayed_plan_scan]]）的分母。重复会让"发生过多少次执行"
虚高，而这正是判断策略是否真在动作的关键计数。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

from typing import Any

from quantdesk.paper.position_management_engine import apply_conditional_execution_to_state


def _state() -> dict[str, Any]:
    return {
        "initial_cash": 100000,
        "cash": 100000,
        "positions": [],
        "closed_positions": [],
        "actions": [],
        "applied_execution_ids": [],
    }


def _execution(plan_id: str = "plan_20260804_for_20260805", trade_date: str = "2026-08-05") -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "trade_date": trade_date,
        "events": [],
        "no_fill": [],
        "blocked": [],
    }


def test_first_application_records_the_execution_id() -> None:
    result = apply_conditional_execution_to_state(_state(), _execution())

    assert result["state"]["applied_execution_ids"] == [
        "plan_20260804_for_20260805:2026-08-05"
    ]


def test_same_day_rerun_does_not_duplicate_the_ledger_key() -> None:
    """同日重跑允许，但账本键是集合 —— 不得出现第二条同值记录。"""

    first = apply_conditional_execution_to_state(_state(), _execution())
    second = apply_conditional_execution_to_state(
        first["state"], _execution(), allow_same_day_rerun=True
    )

    ids = second["state"]["applied_execution_ids"]
    assert ids == ["plan_20260804_for_20260805:2026-08-05"]
    assert len(ids) == len(set(ids)), "账本不得含重复执行键"


def test_distinct_executions_are_all_recorded_in_order() -> None:
    state = _state()
    for plan_id, trade_date in (
        ("plan_a", "2026-08-03"),
        ("plan_b", "2026-08-04"),
        ("plan_c", "2026-08-05"),
    ):
        state = apply_conditional_execution_to_state(
            state, _execution(plan_id, trade_date), allow_same_day_rerun=True
        )["state"]

    assert state["applied_execution_ids"] == [
        "plan_a:2026-08-03",
        "plan_b:2026-08-04",
        "plan_c:2026-08-05",
    ]


def test_dedup_does_not_drop_a_pre_existing_ledger_history() -> None:
    """去重只作用于本次追加，历史条目一个都不能少。"""

    state = _state()
    history = [f"plan_{i}:2026-07-{i:02d}" for i in range(10, 20)]
    state["applied_execution_ids"] = list(history)

    result = apply_conditional_execution_to_state(state, _execution())

    assert result["state"]["applied_execution_ids"][: len(history)] == history
    assert result["state"]["applied_execution_ids"][-1] == (
        "plan_20260804_for_20260805:2026-08-05"
    )
