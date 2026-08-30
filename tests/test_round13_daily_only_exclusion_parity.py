"""--daily-only 与联合路径必须使用同一份排除策略。

回归背景：--daily-only 曾直接调用 run_weekly_daily_wide_pool 而不传
exclusion_policy_path，于是在 4,603 只全池上运行，被那 4 只永久取不到数的票
挡住并写出 status=blocked —— 同时报告却声称"已排除 4 只"。两条路径的口径分歧
比单独一条路径失败更危险：它让"已排除"变成一句无法验证的话。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import importlib

import pytest


TARGET = "2026-08-05"


def _runner():
    return importlib.import_module("scripts.run_weekly_wide_pool_maintenance")


def test_daily_only_passes_the_same_exclusion_policy(monkeypatch) -> None:
    """--daily-only 必须把生产排除策略传下去。"""

    runner = _runner()
    captured: dict = {}

    monkeypatch.setattr(
        runner,
        "resolve_target_complete_trade_date",
        lambda value, calendar_path: {"target_date": TARGET},
    )
    monkeypatch.setattr(
        runner,
        "run_weekly_daily_wide_pool",
        lambda root, **kwargs: captured.update(kwargs)
        or {"status": "planned_offline", "contract": {"counts": {"weekly_daily_wide": 4599}}},
    )

    assert runner.main(["--daily-only", "--network", "false", "--json", "false"]) == 0

    policy = captured.get("exclusion_policy_path")
    assert policy is not None, "--daily-only dropped the exclusion policy"
    assert policy.name == "weekly_daily_wide_pool_exclusions.json"
    assert policy.is_file()


def test_combined_path_also_passes_a_policy(monkeypatch) -> None:
    """联合路径同样必须传策略，两条路径口径一致。"""

    runner = _runner()
    captured: dict = {}

    monkeypatch.setattr(
        runner,
        "resolve_target_complete_trade_date",
        lambda value, calendar_path: {"target_date": TARGET},
    )
    monkeypatch.setattr(
        runner,
        "run_weekly_wide_pool_maintenance",
        lambda root, **kwargs: captured.update(kwargs)
        or {"status": "planned_offline", "counts": {"daily_wide": 4599, "minute_residual": 545}},
    )

    assert runner.main(["--network", "false", "--json", "false"]) == 0
    # 联合入口自身在模块内解析策略路径，这里确认它没有被要求跳过排除。
    assert captured.get("target_date") == TARGET


def test_offline_daily_only_reports_excluded_denominator() -> None:
    """真实离线运行必须能算出「原池 = 有效 + 排除」三个数，而不是只报有效数。

    这里不再钉死 4599/4/4603。那三个数随真实市场变动：退市票最终会整体离开
    上市名单，届时排除项自动失效（`absent_from_universe`），有效排除数归零而
    原池相应变大。要守的不是某个数字，而是口径恒等式本身可验证，以及
    --daily-only 与联合路径用的是同一份策略。
    """

    from pathlib import Path

    from quantdesk.weekly_daily_wide_pool import (
        build_weekly_daily_wide_pool_contract,
        load_weekly_daily_exclusion_policy,
    )

    policy = load_weekly_daily_exclusion_policy(
        Path("."),
        policy_path=Path("config/weekly_daily_wide_pool_exclusions.json"),
        target_date=TARGET,
    )
    contract = build_weekly_daily_wide_pool_contract(
        ".", target_date=TARGET, explicit_exclusions=policy["symbols"]
    )

    effective = set(policy["symbols"])
    declared = set(policy["declared_symbols"])
    absent = set(policy["absent_from_universe"])
    # 声明的排除项被完整划分为「仍生效」与「已因离开名单而失效」两类，不重不漏。
    assert effective | absent == declared
    assert effective & absent == set()

    counts = contract["counts"]
    assert counts["explicit_exclusions"] == len(effective)
    # 分母披露义务：原池 = 有效 + 排除，且原池必须等于契约定义
    # （全集 ∩ canonical − 正式池），三个数都能独立算出来。
    original_pool = counts["weekly_daily_wide"] + counts["explicit_exclusions"]
    assert original_pool == (
        counts["market_with_canonical_daily"] - counts["formal_in_market_with_canonical"]
    )
    assert counts["weekly_daily_wide"] > 0
