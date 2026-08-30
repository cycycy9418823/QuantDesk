"""审计探针 (audit probes) —— 用可执行断言锁定全项目关键不变量。

方法论 (superpowers 风格)：不相信任何"读代码得出的结论"。每条关键不变量都写成
一个会真实运行的断言：
  * 绿 = 该不变量当前成立，该维度在这一点上是健康的；
  * 红 = 存在真实问题（而不是臆造的行号/代码）。

覆盖四个域：
  1. 复权/价格一致性 (P0-3)
  2. 技术指标 / 统计方法正确性 (P2 / P3)
  3. look-ahead bias / T+1 / 执行价时序 (P1)
  4. 研究边界：不下单 / 不接券商 / 不联网 / 免责声明 (P6)

所有探针都是纯读 + 内存计算，绝不写盘、绝不联网、绝不碰 data/results/reports。
统计/回测入口一律传 write=False。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 域 1：复权 / 价格一致性 (P0-3)
# ---------------------------------------------------------------------------
from quantdesk.data_truth.adjustment_factor_audit import audit_adjustment_factors
from quantdesk.data_truth.price_adjustment_consistency import (
    cross_validate_adjustment_factors,
)


def _write_adj_csv(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_probe_adjustment_audit_actually_counts_duplicate_keys(tmp_path: Path) -> None:
    """不变量：审计声称检查重复 (symbol, trade_date) 键，就必须真的数出来。

    这正是本轮修复的 P0-3。重复键 = 复权口径歧义。若探针红，说明审计又在谎报。
    """
    path = tmp_path / "adjustment_factors.csv"
    _write_adj_csv(
        path,
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n"
        "2026-01-01,000001,1.0,unit,test\n",  # 完全重复的键
    )
    result = audit_adjustment_factors(path, {"000001"}, {"000001"}, {"000001"})
    assert result["exists"] is True
    assert result["duplicates"] == 1, "重复键必须被真实计数，不能谎报 None/0"
    assert any("duplicate" in str(item).lower() for item in result["issues"])


def test_probe_adjustment_audit_clean_file_has_zero_dup_and_jump(tmp_path: Path) -> None:
    """不变量：干净文件不得误报重复键或跳变。"""
    path = tmp_path / "adjustment_factors.csv"
    _write_adj_csv(
        path,
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n"
        "2026-01-02,000001,1.1,unit,test\n",
    )
    result = audit_adjustment_factors(path, {"000001"}, {"000001"}, {"000001"})
    assert result["duplicates"] == 0
    assert result["abnormal_factor_jump_count"] == 0


def test_probe_adjustment_audit_detects_abnormal_factor_jump(tmp_path: Path) -> None:
    """不变量：同一 symbol 相邻交易日 adj_factor 跳变 >3x 必须被标为异常。"""
    path = tmp_path / "adjustment_factors.csv"
    _write_adj_csv(
        path,
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n"
        "2026-01-02,000001,5.0,unit,test\n",  # 5x 跳变，越过 3x 阈值
    )
    result = audit_adjustment_factors(path, {"000001"}, {"000001"}, {"000001"})
    assert result["abnormal_factor_jump_count"] >= 1
    assert any("jump" in str(item).lower() for item in result["issues"])


def test_probe_adjustment_audit_reads_utf8_sig_bom_symbols(tmp_path: Path) -> None:
    """不变量：复权文件用 utf-8-sig 读，BOM 不得污染首列列名。"""
    path = tmp_path / "adjustment_factors.csv"
    path.write_text(
        "﻿trade_date,symbol,adj_factor,source,provider\n"
        "20260102,1,1.0,tushare,proxy\n"
        "20260103,600000,1.1,tushare,proxy\n",
        encoding="utf-8",
    )
    result = audit_adjustment_factors(path, {"000001", "600000"}, {"600000"}, {"000001"})
    assert result["exists"] is True
    assert result["symbol_count"] == 2  # BOM 未污染，symbol 列被正确解析
    assert result["read_mode"] == "chunked_utf8_sig"


def test_probe_cross_validate_matches_pilot_when_factors_agree(tmp_path: Path) -> None:
    """不变量：隐含复权因子与既有因子一致时，交叉验证应判为 pilot 已验证。"""
    existing = tmp_path / "adjustment_factors.csv"
    existing.write_text(
        "﻿trade_date,symbol,adj_factor,source,provider\n"
        "20260102,1,1.5,tushare,proxy\n",
        encoding="utf-8",
    )
    implied = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "20260102",
                "raw_close": 10.0,
                "qfq_close": 15.0,
                "implied_adj_factor": 1.5,
                "usable_as_formal_adj_factor": False,
            }
        ]
    )
    merged, summary = cross_validate_adjustment_factors(implied, existing)
    assert len(merged) == 1
    assert summary["conclusion"] == "existing_adjustment_factors_validated_for_pilot"
    # 隐含因子明确标注不可作正式复权因子，防止代理被误当真值入库。
    assert summary["pilot_only_not_full_market"] is True


# ---------------------------------------------------------------------------
# 域 2：技术指标 / 统计方法 (P2 / P3)
# ---------------------------------------------------------------------------
from quantdesk import technical_indicators
from quantdesk.research import backtest_statistical_validation as stats


def test_probe_indicators_do_not_backfill_warmup_window() -> None:
    """不变量：滚动指标 warmup 期必须是 NaN，不得回填（前视/编造）。"""
    close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0], dtype=float)
    ma = technical_indicators.calculate_ma(close, [5])["ma5"]
    ret = technical_indicators.calculate_returns(close, [1])["return_1d"]
    assert ma.iloc[:4].isna().all()
    assert ma.iloc[4] == 12.0
    assert np.isnan(ret.iloc[0])


def test_probe_rsi_first_value_lands_at_period_not_earlier() -> None:
    """不变量：RSI 首个有效值落在 index=period，前 period 个为 NaN。

    这是本轮 P2 修复：旧代码把首日未知涨跌当成 0，导致 warmup 提前一天且首值偏低。
    若探针红，说明 RSI 又在用编造的 0 提前产出。
    """
    period = 6
    close = pd.Series([float(10 + i) for i in range(period + 4)], dtype=float)
    rsi = technical_indicators.calculate_rsi(close, [period])[f"rsi{period}"]
    assert rsi.iloc[:period].isna().all(), "warmup 期必须 NaN，不得提前产出"
    assert not math.isnan(rsi.iloc[period])
    assert rsi.iloc[period] == 100.0  # 全程上涨 → 满值


def test_probe_indicators_return_all_nan_when_insufficient_data() -> None:
    """不变量：数据不足时指标返回全 NaN，不得静默补值。"""
    close = pd.Series([10.0, 10.5, 11.0], dtype=float)
    frame = pd.DataFrame(
        {"high": [10.2, 10.7, 11.2], "low": [9.8, 10.1, 10.8], "close": close}
    )
    assert technical_indicators.calculate_ma(close, [5])["ma5"].isna().all()
    assert technical_indicators.calculate_macd(close)["dif"].isna().all()
    assert technical_indicators.calculate_kdj(frame)["k"].isna().all()
    assert technical_indicators.calculate_rsi(close, [6])["rsi6"].isna().all()
    assert technical_indicators.calculate_volatility(close, period=20).isna().all()


def test_probe_permutation_test_takes_returns_and_is_dimensionally_sound() -> None:
    """不变量：置换检验对 mean(returns) vs mean(shuffled) 同量纲比较。

    审计曾臆造"传入累计 annual_return 导致 p≈0 虚假显著"。此探针证伪：当 actual_metric
    与基线口径一致（都用样本复利收益）时，无真实信号的对称收益 p 值应偏大（不显著）。
    """
    returns = [0.02] * 12
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    compounded -= 1.0
    result = stats.permutation_test_signals(
        returns, iterations=100, seed=42, actual_metric=compounded
    )
    assert abs(result["random_mean"] - compounded) < 1e-6
    assert abs(result["actual_metric"] - compounded) < 1e-6
    # 恒定收益无"信号顺序"信息 → 置换后基线≈实际 → p 值不应接近 0
    assert result["p_value"] > 0.5


def test_probe_permutation_test_is_reproducible_under_seed() -> None:
    """不变量：同 seed 的置换检验结果必须可复现。"""
    returns = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.04, -0.01, 0.02, 0.01]
    left = stats.permutation_test_signals(returns, iterations=50, seed=42)
    right = stats.permutation_test_signals(returns, iterations=50, seed=42)
    assert left["p_value"] == right["p_value"]
    assert left["random_p95"] == right["random_p95"]


def test_probe_stat_validation_reports_insufficient_sample_not_fake_pass() -> None:
    """不变量：样本不足时如实报 warning + insufficient_sample，不得伪装通过。"""
    result = stats.run_backtest_statistical_validation(
        {"returns": [0.01, -0.01]}, iterations=20, seed=42, write=False
    )
    assert result["status"] == "warning"
    assert "insufficient_sample" in result["warnings"]


def test_probe_stat_validation_does_not_mutate_input_payload() -> None:
    """不变量：统计验证不得就地改写调用方传入的回测结果。"""
    import copy

    payload = {"returns": [0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.04, -0.01]}
    before = copy.deepcopy(payload)
    stats.run_backtest_statistical_validation(payload, iterations=20, seed=42, write=False)
    assert payload == before


# ---------------------------------------------------------------------------
# 域 3：look-ahead bias / T+1 / 执行价时序 (P1)
# ---------------------------------------------------------------------------
from quantdesk.backtest import (
    simulate_single_asset_backtest,
    t_plus_one_block_reason,
)
from quantdesk.strategy_lookahead_guard import (
    build_strategy_timeline_context,
    detect_future_dates_in_inputs,
    detect_target_date_leakage,
    validate_strategy_no_lookahead,
)

from tests.test_core_logic import tradeable_backtest_history, zero_cost_config


ALWAYS_LONG_STRATEGY = {
    "strategy_id": "audit_probe_always_long",
    "name": "Audit Probe Always Long",
    "entry_conditions": [{"field": "close", "op": ">", "value": 0}],
    "exit_conditions": [],
    "risk_line_type": "manual",
}


def _english_tradeable_history(amount: float = 100_000_000.0) -> pd.DataFrame:
    return (
        tradeable_backtest_history(days=190)
        .rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "pct_chg",
                "换手率": "turnover",
            }
        )
        .assign(amount=amount, pct_chg=0.0)
    )


def test_probe_future_date_input_is_flagged() -> None:
    """不变量：晚于 target_date 的输入行必须被判为 look-ahead。"""
    rows = [{"date": "2026-05-19", "close": 10}, {"date": "2026-05-20", "close": 11}]
    issues = detect_future_dates_in_inputs(rows, "2026-05-19")
    assert any(item["type"] == "future_date_in_input" for item in issues)
    context = build_strategy_timeline_context(target_date="2026-05-19", inputs=rows)
    leakage = detect_target_date_leakage(context)
    assert any(item["type"] == "post_target_row" for item in leakage)


def test_probe_forward_plan_is_info_not_serious() -> None:
    """不变量：带 plan_id 的次日条件计划是排程（info），不得当成 look-ahead 失败。"""
    result = validate_strategy_no_lookahead(
        target_date="2026-07-01",
        executions={"plan_id": "plan_20260702_for_20260703", "trade_date": "2026-07-03"},
        strict=True,
    )
    types = {item["type"] for item in result["issues"]}
    assert "future_date_in_input" not in types
    assert result["status"] != "failed"


def test_probe_reliable_fill_without_intraday_fails_strict() -> None:
    """不变量：声称可靠成交却无分钟线，strict 下必须判 failed（不许假装成交）。"""
    result = validate_strategy_no_lookahead(
        target_date="2026-05-19",
        executions=[
            {"action": "buy", "actual_fill_model": "missing_intraday", "is_reliable": True}
        ],
        intraday_status={"status": "missing_intraday"},
    )
    issue_types = {item["type"] for item in result["issues"]}
    assert "reliable_execution_without_intraday" in issue_types
    assert result["status"] == "failed"


def test_probe_t_plus_one_block_reason_handles_unparseable_dates() -> None:
    """不变量：T+1 日期不可解析时保守阻断并记 warning，不得静默放行。"""
    reason, warning = t_plus_one_block_reason("not-a-date", pd.Timestamp("2026-01-02"))
    assert reason == "t_plus_one_date_unparseable"
    assert "t_plus_one_date_unparseable" in warning


def test_probe_next_open_limit_check_uses_open_gap_not_full_day_pct() -> None:
    """不变量：next_open 下涨停判定用开盘缺口，不得让当日收盘泄漏进开盘成交决策。

    构造：开盘只涨 1%（可成交），但全天收盘涨 10%。若引擎错误地用全天 pct_chg 判涨停，
    会错误阻断这笔本可成交的开盘买入。探针要求 blocked=False。
    """
    hist = _english_tradeable_history(100_000_000.0)
    config = zero_cost_config(
        initial_cash=100_000, max_position_pct=1.0, max_participation_rate=0.0
    )
    baseline = simulate_single_asset_backtest(
        hist, "000001", config, strategy_definition=ALWAYS_LONG_STRATEGY
    )
    baseline_buy = next(t for t in baseline.trades if t["side"] == "BUY")
    execution_date = baseline_buy["execution_date"]
    exec_mask = hist["date"].eq(execution_date)
    exec_idx = hist.index[exec_mask][0]
    prev_close = float(hist.loc[exec_idx - 1, "close"])

    changed = hist.copy()
    changed.loc[exec_mask, "open"] = prev_close * 1.01
    changed.loc[exec_mask, "high"] = prev_close * 1.12
    changed.loc[exec_mask, "low"] = prev_close * 1.00
    changed.loc[exec_mask, "close"] = prev_close * 1.10
    changed.loc[exec_mask, "pct_chg"] = 10.0

    result = simulate_single_asset_backtest(
        changed, "000001", config, strategy_definition=ALWAYS_LONG_STRATEGY
    )
    trades = [
        t
        for t in result.trades
        if t["side"] == "BUY" and t["execution_date"] == execution_date
    ]
    assert trades
    assert trades[0]["blocked"] is False


def test_probe_next_open_liquidity_cap_uses_previous_day_amount() -> None:
    """不变量：next_open 参与率上限用前一日成交额（开盘时当日全天额未知）。"""
    hist = _english_tradeable_history(100_000_000.0)
    config = zero_cost_config(
        initial_cash=100_000, max_position_pct=1.0, max_participation_rate=0.05
    )
    baseline = simulate_single_asset_backtest(
        hist,
        "000001",
        zero_cost_config(
            initial_cash=100_000, max_position_pct=1.0, max_participation_rate=0.0
        ),
        strategy_definition=ALWAYS_LONG_STRATEGY,
    )
    baseline_buy = next(t for t in baseline.trades if t["side"] == "BUY")
    execution_date = baseline_buy["execution_date"]
    exec_idx = hist.index[hist["date"].eq(execution_date)][0]

    changed = hist.copy()
    changed["amount"] = 100_000_000.0
    changed.loc[exec_idx - 1, "amount"] = 1_000_000.0  # 只有前一日成交额很小

    result = simulate_single_asset_backtest(
        changed, "000001", config, strategy_definition=ALWAYS_LONG_STRATEGY
    )
    buy = next(t for t in result.trades if t["side"] == "BUY" and not t["blocked"])
    assert buy["execution_date"] == execution_date
    # 上限 = 前一日额 1_000_000 * 0.05 = 50_000
    assert buy["gross_amount"] <= 50_000.0


# ---------------------------------------------------------------------------
# 域 4：研究边界 (P6) —— 不下单 / 不接券商 / 不联网 / 免责声明
# ---------------------------------------------------------------------------
from quantdesk.paper import paper_trading
from quantdesk.research.composite_candidate_scoring import (
    build_composite_candidate_scoring_report,
)
from quantdesk.research.daily_watchpool_step import build_daily_watchpool_step


def test_probe_composite_report_is_offline_and_research_only() -> None:
    """不变量：综合评分报告必须 network=False、research_only、免责声明含不联网/不下单。"""
    payload = build_composite_candidate_scoring_report({"candidates": []})
    assert payload["network"] is False
    assert payload["research_only"] is True
    assert payload["paper_only"] is True
    assert "不联网" in payload["disclaimer"]
    assert "不下单" in payload["disclaimer"]


def test_probe_watchpool_step_never_places_orders(tmp_path: Path) -> None:
    """不变量：观察池步骤 is_real_order=False、不接实盘，条目只做观察不成交。"""
    result = build_daily_watchpool_step([], watchlist_path=tmp_path / "wl.json", top=5)
    assert result["network"] is False
    assert result["research_only"] is True
    assert result["paper_only"] is True
    assert result["is_real_order"] is False
    assert "不接实盘" in result["disclaimer"]


def test_probe_paper_trading_enforces_t_plus_one(tmp_path: Path) -> None:
    """不变量：纸面卖出遵守 A 股 T+1，当日买入不得当日卖出。"""
    path = tmp_path / "paper.json"
    bought = paper_trading.paper_buy(
        "000001", "sample", 10, 100, path=path, metadata={"trade_date": "2026-06-18"}
    )
    assert bought["ok"] is True
    assert "不会真实下单" in bought["message"]

    same_day = paper_trading.paper_sell(
        "000001", 11, 100, path=path, metadata={"trade_date": "2026-06-18"}
    )
    assert same_day["ok"] is False
    assert "T+1" in same_day["message"]


def test_probe_paper_trading_blocks_limit_up_buy(tmp_path: Path) -> None:
    """不变量：涨停附近不得假设纸面买入成交。"""
    path = tmp_path / "paper.json"
    blocked = paper_trading.paper_buy(
        "000001", "sample", 10, 100, path=path, metadata={"pct_chg": 9.9}
    )
    assert blocked["ok"] is False


@pytest.mark.parametrize(
    "module",
    [
        paper_trading,
        __import__(
            "quantdesk.research.composite_candidate_scoring",
            fromlist=["x"],
        ),
        __import__("quantdesk.research.daily_watchpool_step", fromlist=["x"]),
    ],
)
def test_probe_core_modules_have_no_network_or_order_symbols(module) -> None:
    """不变量：核心研究/纸面模块源码里不得出现联网或真实下单符号。"""
    pattern = re.compile(r"requests|urlopen|\bbroker\b|place_order", re.IGNORECASE)
    src = Path(module.__file__).read_text(encoding="utf-8")
    assert not pattern.search(src), f"{module.__name__} 出现联网/下单符号"
