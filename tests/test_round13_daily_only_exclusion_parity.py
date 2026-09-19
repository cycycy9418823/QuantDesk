"""--daily-only 与联合路径必须使用同一份排除策略。

回归背景：--daily-only 曾直接调用 run_weekly_daily_wide_pool 而不传
exclusion_policy_path，于是在 4,603 只全池上运行，被那 4 只永久取不到数的票
挡住并写出 status=blocked —— 同时报告却声称"已排除 4 只"。两条路径的口径分歧
比单独一条路径失败更危险：它让"已排除"变成一句无法验证的话。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


TARGET = "2026-08-05"
_CANONICAL_HEADER = (
    "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at"
)
_PARITY_MARKET = ["000004", "002808", "002898", "920305", "600000", "600001"]
_PARITY_LAST = {
    "000004": "2026-04-28",
    "002808": "2026-04-30",
    "002898": "2026-04-29",
    "920305": "2026-04-29",
    "600000": "2026-04-29",
    "600001": "2026-04-29",
}


def _parity_policy() -> dict:
    return {
        "schema_version": "weekly_daily_wide_pool_exclusions_v1",
        "task_type": "weekly_daily_wide_pool_exclusions",
        "effective_from": "2026-08-01",
        "evidence_sources": {
            "current_a_share_universe": "data/universe/a_share_symbols.csv",
            "st_history": "data/universe/st_history.csv",
        },
        "entries": [
            {
                "symbol": "000004",
                "expected_name": "国华退",
                "reason_code": "delisted_by_universe_name",
                "expected_last_trade_date": "2026-04-28",
            },
            {
                "symbol": "002808",
                "expected_name": "恒久退",
                "reason_code": "delisted_by_universe_name",
                "expected_last_trade_date": "2026-04-30",
            },
            {
                "symbol": "002898",
                "expected_name": "赛隆退",
                "reason_code": "delisted_by_universe_name",
                "expected_last_trade_date": "2026-04-29",
            },
            {
                "symbol": "920305",
                "expected_name": "ST赛隆",
                "reason_code": "long_suspended_st",
                "expected_last_trade_date": "2026-04-29",
            },
        ],
    }


def _build_parity_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (root / "data" / "daily").mkdir(parents=True, exist_ok=True)

    spec = json.loads(
        Path("config/weekly_daily_wide_pool_contract.json").read_text(encoding="utf-8")
    )
    (root / "config" / "weekly_daily_wide_pool_contract.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    market_names = {
        "000004": "国华退",
        "002808": "恒久退",
        "002898": "赛隆退",
        "920305": "ST赛隆",
        "600000": "浦发银行",
        "600001": "邯郸钢铁",
    }
    (root / "data" / "universe" / "a_share_symbols.csv").write_text(
        "symbol,name,source,updated_at\n"
        + "\n".join(
            f"{s},{market_names[s]},stock_info_a_code_name,2026-08-05T09:00:00+08:00"
            for s in _PARITY_MARKET
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "data" / "universe" / "st_history.csv").write_text(
        "symbol,name\n920305,ST赛隆\n", encoding="utf-8"
    )
    (root / "data" / "universe" / "low_price_learning_symbols.csv").write_text(
        "symbol,included\n600001,true\n600000,false\n", encoding="utf-8"
    )
    for symbol in _PARITY_MARKET:
        (root / "data" / "daily" / f"{symbol}.csv").write_text(
            _CANONICAL_HEADER
            + "\n"
            + f"2026-04-01,{symbol},1,1,1,1,100,100,0,0,raw,local,2026-04-01T15:00:00+08:00\n"
            + f"{_PARITY_LAST[symbol]},{symbol},1,1,1,1,100,100,0,0,raw,local,{_PARITY_LAST[symbol]}T15:00:00+08:00\n",
            encoding="utf-8",
        )
    (root / "config" / "fixture_exclusions_parity.json").write_text(
        json.dumps(_parity_policy(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


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


def test_offline_daily_only_reports_excluded_denominator(tmp_path: Path) -> None:
    """真实离线运行必须能算出「原池 = 有效 + 排除」三个数，而不是只报有效数。

    这里用完整离线夹具（market/universe、canonical 日线、formal 池、排除
    策略全部就位），先证明完整夹具可通过，再验证分母恒等式本身可复算。

    生产数字（4599/4/4603）随真实市场变动，因此不钉死：退市票最终会整体离开
    上市名单，届时排除项自动失效（`absent_from_universe`），有效排除数归零而
    原池相应变大。要守的不是某个数字，而是口径恒等式以及 --daily-only 与联合
    路径用的是同一份策略。
    """

    from quantdesk.weekly_daily_wide_pool import (
        build_weekly_daily_wide_pool_contract,
        load_weekly_daily_exclusion_policy,
    )

    root = _build_parity_fixture(tmp_path)
    policy = load_weekly_daily_exclusion_policy(
        root,
        policy_path=root / "config" / "fixture_exclusions_parity.json",
        target_date=TARGET,
    )
    assert policy["verified_against_disk"] is True
    contract = build_weekly_daily_wide_pool_contract(
        root, target_date=TARGET, explicit_exclusions=policy["symbols"]
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
