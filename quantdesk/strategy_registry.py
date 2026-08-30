from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is optional at runtime.
    yaml = None


_REGISTRY_CACHE: dict[tuple[str, int, int, bool], tuple[dict[str, dict[str, Any]], str]] = {}


STRATEGY_DEMO_DISCLAIMER = "此输出为策略规则的虚拟演示，不构成任何投资建议，不预测价格走势。"


@dataclass
class StrategyDefinition:
    strategy_id: str
    name: str
    description: str
    entry_conditions: list[dict[str, Any]] = field(default_factory=list)
    exit_conditions: list[dict[str, Any]] = field(default_factory=list)
    required_indicators: list[str] = field(default_factory=list)
    risk_line_type: str = "ma20"
    score_weights: dict[str, float] = field(default_factory=dict)
    enabled: bool = True
    why: list[str] = field(default_factory=list)
    integration_type: str = "builtin"
    disclaimer: str = STRATEGY_DEMO_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.setdefault("disclaimer", STRATEGY_DEMO_DISCLAIMER)
        return data


def _builtin_strategies() -> dict[str, StrategyDefinition]:
    return {
        "ma_breakout": StrategyDefinition(
            strategy_id="ma_breakout",
            name="均线突破",
            description="当价格站上20日均线且成交活跃度放大时，记录虚拟建仓观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">", "target_field": "ma20", "explain": "收盘价高于20日均线"},
                {
                    "field": "activity_value",
                    "op": ">",
                    "target_field": "activity_avg_20",
                    "multiplier": 1.5,
                    "explain": "成交活跃度高于20日均值的1.5倍",
                },
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "explain": "收盘价跌破20日均线"},
                {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "20日均线跌破60日均线"},
            ],
            required_indicators=["close", "ma20", "ma60", "activity_value", "activity_avg_20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.32, "momentum": 0.18, "activity": 0.20, "position": 0.12, "risk": 0.18},
            why=[
                "20日均线常被用作中期趋势观察线。",
                "成交活跃度放大说明该观察点更值得复核。",
                "该规则可能遇到假突破，因此只能作为学习演示。",
            ],
        ),
        "ma_pullback": StrategyDefinition(
            strategy_id="ma_pullback",
            name="均线回踩观察",
            description="当价格仍位于20日均线附近且数据可信度较高时，记录回踩观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">=", "target_field": "ma20", "multiplier": 0.98, "explain": "价格没有明显跌破20日均线"},
                {"field": "close", "op": "<=", "target_field": "ma20", "multiplier": 1.05, "explain": "价格没有明显远离20日均线"},
                {"field": "activity_value", "op": ">", "target_field": "activity_avg_20", "multiplier": 0.8, "explain": "成交活跃度不低于20日均值的0.8倍"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "multiplier": 0.97, "explain": "价格明显跌破20日均线"},
                {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "20日均线跌破60日均线"},
            ],
            required_indicators=["close", "ma20", "activity_value", "activity_avg_20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.28, "momentum": 0.14, "activity": 0.18, "position": 0.20, "risk": 0.20},
            why=[
                "20日均线附近常被当作趋势回踩观察区。",
                "活跃度不能太弱，否则规则复核意义有限。",
                "该规则只用于学习策略回踩后的机械检查流程。",
            ],
        ),
        "volume_breakout": StrategyDefinition(
            strategy_id="volume_breakout",
            name="量价突破观察",
            description="当价格接近阶段高位且成交活跃度放大时，记录规则复核点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">=", "target_field": "high_20d", "explain": "价格接近20日高点"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.5, "explain": "成交活跃度高于20日均值的1.5倍"},
                {"field": "ma5", "op": ">=", "target_field": "ma10", "explain": "短期均线高于中短期均线"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "explain": "价格跌破20日均线"},
                {"field": "activity_ratio_20", "op": "<", "value": 0.8, "explain": "成交活跃度回落"},
                {"field": "close", "op": "<", "target_field": "previous_close", "explain": "价格转弱"},
            ],
            required_indicators=["close", "ma5", "ma10", "ma20", "high_20d", "activity_ratio_20"],
            risk_line_type="atr_trailing",
            score_weights={"trend": 0.25, "momentum": 0.20, "activity": 0.25, "position": 0.12, "risk": 0.18},
            why=[
                "阶段高位配合放量才值得继续观察。",
                "如果活跃度不能持续，规则复核价值会下降。",
                "该规则只做机械观察，不代表未来一定延续。",
            ],
        ),
        "macd_strength": StrategyDefinition(
            strategy_id="macd_strength",
            name="MACD转强观察",
            description="当MACD动量改善且成交活跃度不弱时，记录转强观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "macd_dif", "op": ">", "target_field": "macd_dea", "explain": "MACD快线高于慢线"},
                {"field": "macd_histogram", "op": ">", "value": 0, "explain": "MACD柱体为正"},
                {"field": "macd_histogram", "op": ">", "target_field": "previous_macd_histogram", "explain": "MACD柱体较前一日改善"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.0, "explain": "成交活跃度不低于20日均值"},
            ],
            exit_conditions=[
                {"field": "macd_dif", "op": "<", "target_field": "macd_dea", "explain": "MACD快线低于慢线"},
                {"field": "macd_histogram", "op": "<", "value": 0, "explain": "MACD柱体转负"},
            ],
            required_indicators=["macd_dif", "macd_dea", "macd_histogram", "previous_macd_histogram", "activity_ratio_20"],
            risk_line_type="atr_trailing",
            score_weights={"trend": 0.18, "momentum": 0.32, "activity": 0.18, "position": 0.12, "risk": 0.20},
            why=[
                "MACD用于观察趋势动量变化。",
                "柱体改善只说明动量变化值得复核，不代表价格方向结论。",
                "成交活跃度不足时，动量指标容易误导。",
            ],
        ),
        "rsi_recovery": StrategyDefinition(
            strategy_id="rsi_recovery",
            name="超跌修复观察",
            description="当RSI处于偏低区间且价格尝试修复时，记录修复观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "rsi14", "op": "<=", "value": 35, "explain": "RSI14处于偏低区间"},
                {"field": "close", "op": ">=", "target_field": "boll_lower", "explain": "价格没有明显跌破布林下轨"},
                {"field": "close", "op": ">", "target_field": "previous_close", "explain": "价格较前一日修复"},
            ],
            exit_conditions=[
                {"field": "rsi14", "op": ">=", "value": 70, "explain": "RSI14进入偏热区间"},
                {"field": "close", "op": "<", "target_field": "boll_lower", "explain": "价格跌破布林下轨"},
            ],
            required_indicators=["rsi14", "boll_lower", "previous_close"],
            risk_line_type="atr_trailing",
            score_weights={"trend": 0.12, "momentum": 0.26, "activity": 0.12, "position": 0.28, "risk": 0.22},
            why=[
                "RSI偏低只能说明短期状态值得观察，不代表修复一定发生。",
                "布林下轨用于辅助判断价格是否仍处于异常区域。",
                "该规则偏向学习修复过程，风险复核要更严格。",
            ],
        ),
        "trend_continuation": StrategyDefinition(
            strategy_id="trend_continuation",
            name="趋势延续观察",
            description="当价格位于中期趋势线上方且波动不极端时，记录延续观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">", "target_field": "ma20", "explain": "收盘价高于20日均线"},
                {"field": "ma20", "op": ">", "target_field": "ma60", "explain": "20日均线高于60日均线"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.0, "explain": "成交活跃度不低于20日均值"},
                {"field": "vol20", "op": "<", "value": 0.08, "explain": "20日波动率没有过高"},
            ],
            exit_conditions=[
                {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "20日均线跌破60日均线"},
                {"field": "vol20", "op": ">", "value": 0.08, "explain": "20日波动率升高"},
            ],
            required_indicators=["ma20", "ma60", "activity_ratio_20", "vol20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.35, "momentum": 0.18, "activity": 0.15, "position": 0.10, "risk": 0.22},
            why=[
                "均线排列用于观察中期趋势结构。",
                "波动过高时，即使趋势结构较好也需要谨慎复核。",
                "该规则只展示趋势延续的机械条件检查。",
            ],
        ),
        "new_high_volume": StrategyDefinition(
            strategy_id="new_high_volume",
            name="放量新高观察",
            description="当价格接近60日高位且活跃度放大时，记录新高观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">=", "target_field": "high_60d", "multiplier": 0.99, "explain": "价格接近60日高点"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.5, "explain": "成交活跃度高于20日均值的1.5倍"},
                {"field": "close", "op": ">", "target_field": "ma20", "explain": "收盘价高于20日均线"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "high_20d", "multiplier": 0.95, "explain": "价格明显回落到20日高点下方"},
                {"field": "activity_ratio_20", "op": "<", "value": 1.0, "explain": "成交活跃度低于20日均值"},
            ],
            required_indicators=["high_60d", "high_20d", "ma20", "activity_ratio_20"],
            risk_line_type="atr_trailing",
            score_weights={"trend": 0.28, "momentum": 0.18, "activity": 0.26, "position": 0.10, "risk": 0.18},
            why=[
                "阶段新高通常需要结合活跃度一起复核。",
                "新高区域容易出现波动放大，因此风险线更重要。",
                "该规则只用于学习规则检查流程。",
            ],
        ),
        "shrink_pullback": StrategyDefinition(
            strategy_id="shrink_pullback",
            name="缩量回踩观察",
            description="当价格在20日均线附近整理且活跃度收敛时，记录缩量回踩观察点。",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "数据可信度足够高"},
                {"field": "close", "op": ">=", "target_field": "ma20", "multiplier": 0.98, "explain": "价格仍在20日均线附近"},
                {"field": "ma20", "op": ">", "target_field": "ma60", "explain": "20日均线高于60日均线"},
                {"field": "activity_ratio_20", "op": "<=", "value": 1.1, "explain": "成交活跃度没有明显放大"},
                {"field": "activity_ratio_20", "op": ">=", "value": 0.5, "explain": "成交活跃度没有过度萎缩"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "multiplier": 0.97, "explain": "价格明显跌破20日均线"},
                {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "20日均线跌破60日均线"},
            ],
            required_indicators=["ma20", "ma60", "activity_ratio_20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.30, "momentum": 0.12, "activity": 0.16, "position": 0.22, "risk": 0.20},
            why=[
                "缩量回踩用于观察趋势结构是否仍保持。",
                "成交活跃度过高或过低都需要重新复核。",
                "该规则不判断结果，只展示条件是否满足。",
            ],
        ),
    }


def _research_wrapper_strategies() -> dict[str, StrategyDefinition]:
    """Research-only wrappers for sandbox and strategy-lab validation."""

    disclaimer = (
        STRATEGY_DEMO_DISCLAIMER
        + " Research wrapper only; validates local daily-bar hypotheses and does not produce orders."
    )
    return {
        "sandbox_long": StrategyDefinition(
            strategy_id="sandbox_long",
            name="Sandbox Long Research Wrapper",
            description="Daily-bar wrapper for the auto sandbox long observation rule.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">", "target_field": "ma20", "explain": "close is above MA20"},
                {"field": "close", "op": "<=", "target_field": "ma20", "multiplier": 1.20, "explain": "price is not extremely extended above MA20"},
                {"field": "activity_ratio_20", "op": ">=", "value": 0.5, "explain": "activity is not severely weak"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "explain": "close breaks MA20"},
                {"field": "close", "op": ">", "target_field": "ma20", "multiplier": 1.30, "explain": "price is overly extended versus MA20 and needs review"},
            ],
            required_indicators=["close", "ma20", "activity_ratio_20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.35, "activity": 0.20, "position": 0.20, "risk": 0.25},
            why=[
                "Shares the sandbox long filter shape so walk-forward can audit the idea.",
                "Uses daily data only; metadata filters remain upstream.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
        "sandbox_board": StrategyDefinition(
            strategy_id="sandbox_board",
            name="Sandbox Board Research Wrapper",
            description="Daily-bar approximation of the sandbox board observation rule.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">=", "target_field": "previous_close", "multiplier": 1.085, "explain": "close is near a standard-board limit-up proxy"},
                {"field": "activity_ratio_20", "op": ">=", "value": 0.8, "explain": "activity is not severely weak"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "previous_close", "explain": "next review day weakens versus previous close"},
                {"field": "close", "op": "<", "target_field": "ma5", "explain": "close breaks MA5"},
            ],
            required_indicators=["close", "previous_close", "ma5", "activity_ratio_20"],
            risk_line_type="ma5",
            score_weights={"momentum": 0.34, "activity": 0.26, "position": 0.12, "risk": 0.28},
            why=[
                "Provides a measurable daily-bar proxy for board-style research.",
                "It cannot model queues, sealed volume, intraday breaks, or tradability.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
        "strategy_lab_low_price": StrategyDefinition(
            strategy_id="strategy_lab_low_price",
            name="Strategy Lab Low Price Wrapper",
            description="Walk-forward wrapper for the low-price research family.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">", "value": 0, "explain": "close price is positive"},
                {"field": "close", "op": "<=", "value": 8, "explain": "low-price family price ceiling"},
                {"field": "activity_ratio_20", "op": ">=", "value": 0.8, "explain": "activity is acceptable for research review"},
                {"field": "close", "op": ">", "target_field": "ma5", "explain": "recent close is repairing above MA5"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "explain": "close breaks MA20"},
                {"field": "close", "op": ">", "value": 8, "explain": "price leaves the low-price research bucket"},
            ],
            required_indicators=["close", "ma5", "ma20", "activity_ratio_20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.22, "momentum": 0.20, "activity": 0.22, "position": 0.16, "risk": 0.20},
            why=[
                "Matches the strategy-lab low-price universe as a walk-forward-testable rule.",
                "Metadata blockers such as ST and delist status remain upstream.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
        "strategy_lab_long_term": StrategyDefinition(
            strategy_id="strategy_lab_long_term",
            name="Strategy Lab Long Term Wrapper",
            description="Walk-forward wrapper for the long-horizon trend/low-vol research family.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">", "target_field": "ma60", "explain": "close stays above MA60"},
                {"field": "ma20", "op": ">", "target_field": "ma60", "explain": "MA20 is above MA60"},
                {"field": "vol20", "op": "<", "value": 0.045, "explain": "20-day volatility is controlled"},
                {"field": "activity_ratio_20", "op": ">=", "value": 0.6, "explain": "activity is not severely weak"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma60", "explain": "close breaks MA60"},
                {"field": "ma20", "op": "<", "target_field": "ma60", "explain": "MA20 breaks below MA60"},
                {"field": "vol20", "op": ">", "value": 0.08, "explain": "volatility expands beyond the long-term review band"},
            ],
            required_indicators=["close", "ma20", "ma60", "vol20", "activity_ratio_20"],
            risk_line_type="ma60",
            score_weights={"trend": 0.38, "momentum": 0.12, "activity": 0.12, "position": 0.10, "risk": 0.28},
            why=[
                "Gives the long-term strategy-lab family a registry definition for out-of-sample checks.",
                "Uses price-only evidence; fundamentals are not inferred here.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
        "strategy_lab_limit_up": StrategyDefinition(
            strategy_id="strategy_lab_limit_up",
            name="Strategy Lab Limit-Up Wrapper",
            description="Daily-bar proxy wrapper for the limit-up research family.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">=", "target_field": "previous_close", "multiplier": 1.085, "explain": "close is near a standard-board limit-up proxy"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.0, "explain": "activity confirms the daily-bar move"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma5", "explain": "close breaks MA5"},
                {"field": "activity_ratio_20", "op": "<", "value": 0.7, "explain": "activity fades after the event"},
            ],
            required_indicators=["close", "previous_close", "ma5", "activity_ratio_20"],
            risk_line_type="ma5",
            score_weights={"momentum": 0.38, "activity": 0.28, "position": 0.08, "risk": 0.26},
            why=[
                "Connects limit-up research to walk-forward without pretending intraday queue data exists.",
                "Daily bars can only approximate limit-up behavior and tradability remains degraded.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
        "strategy_lab_sector_rotation": StrategyDefinition(
            strategy_id="strategy_lab_sector_rotation",
            name="Strategy Lab Sector Rotation Wrapper",
            description="Walk-forward wrapper for sector-rotation-selected symbols.",
            entry_conditions=[
                {"field": "data_reliability_score", "op": ">=", "value": 80, "explain": "local data passes the research gate"},
                {"field": "close", "op": ">", "target_field": "ma20", "explain": "symbol participates in a positive trend"},
                {"field": "ma20", "op": ">", "target_field": "ma60", "explain": "medium trend confirms the rotation candidate"},
                {"field": "activity_ratio_20", "op": ">=", "value": 1.0, "explain": "activity supports the rotation observation"},
                {"field": "vol20", "op": "<", "value": 0.06, "explain": "volatility is within the sector review band"},
            ],
            exit_conditions=[
                {"field": "close", "op": "<", "target_field": "ma20", "explain": "trend participation breaks down"},
                {"field": "activity_ratio_20", "op": "<", "value": 0.7, "explain": "activity support fades"},
            ],
            required_indicators=["close", "ma20", "ma60", "activity_ratio_20", "vol20"],
            risk_line_type="ma20",
            score_weights={"trend": 0.32, "momentum": 0.18, "activity": 0.22, "position": 0.08, "risk": 0.20},
            why=[
                "Allows sector-rotation-selected symbols to be validated by the shared walk-forward engine.",
                "Sector membership and ranking are supplied upstream; this wrapper only tests daily price behavior.",
            ],
            integration_type="research_wrapper",
            disclaimer=disclaimer,
        ),
    }


def _default_strategies() -> dict[str, StrategyDefinition]:
    strategies = _builtin_strategies()
    strategies.update(_research_wrapper_strategies())
    return strategies


ALIASES = {
    "ma_trend": "ma_breakout",
    "long": "sandbox_long",
    "board": "sandbox_board",
    "low_price_family": "strategy_lab_low_price",
    "long_term_family": "strategy_lab_long_term",
    "limit_up_family": "strategy_lab_limit_up",
    "sector_rotation_family": "strategy_lab_sector_rotation",
    "均线突破": "ma_breakout",
    "均线回踩观察": "ma_pullback",
    "量价突破观察": "volume_breakout",
    "MACD转强观察": "macd_strength",
    "超跌修复观察": "rsi_recovery",
    "趋势延续观察": "trend_continuation",
    "放量新高观察": "new_high_volume",
    "缩量回踩观察": "shrink_pullback",
}


def _config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    return Path(__file__).resolve().parents[1] / "config" / "strategy_demo.yaml"


def _registry_cache_key(path: Path) -> tuple[str, int, int, bool]:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        stat = resolved.stat()
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
    except OSError:
        mtime_ns = -1
        size = -1
    return (str(resolved), mtime_ns, size, yaml is None)


def builtin_strategy_catalog() -> dict[str, dict[str, Any]]:
    return {key: value.to_dict() for key, value in _default_strategies().items()}


def _strategy_from_mapping(strategy_id: str, payload: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> StrategyDefinition | None:
    if not strategy_id:
        return None
    merged = dict(base or {})
    merged.update({key: value for key, value in payload.items() if value is not None})
    entry_conditions = list(merged.get("entry_conditions") or merged.get("conditions") or [])
    exit_conditions = list(merged.get("exit_conditions") or [])
    if not entry_conditions:
        if base is not None and merged.get("enabled") is False:
            entry_conditions = list(base.get("entry_conditions") or base.get("conditions") or [])
            exit_conditions = list(base.get("exit_conditions") or [])
        else:
            return None
    return StrategyDefinition(
        strategy_id=strategy_id,
        name=str(merged.get("name") or strategy_id),
        description=str(merged.get("description") or ""),
        entry_conditions=[dict(item) for item in entry_conditions if isinstance(item, Mapping)],
        exit_conditions=[dict(item) for item in exit_conditions if isinstance(item, Mapping)],
        required_indicators=[str(item) for item in (merged.get("required_indicators") or [])],
        risk_line_type=str(merged.get("risk_line_type") or "ma20"),
        score_weights={str(key): float(value) for key, value in dict(merged.get("score_weights") or {}).items()},
        enabled=bool(merged.get("enabled", True)),
        why=[str(item) for item in (merged.get("why") or [])],
        integration_type=str(merged.get("integration_type") or merged.get("source") or "config"),
        disclaimer=str(merged.get("disclaimer") or STRATEGY_DEMO_DISCLAIMER),
    )


def load_strategy_registry(path: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    config_path = _config_path(path)
    cache_key = _registry_cache_key(config_path)
    cached = _REGISTRY_CACHE.get(cache_key)
    if cached is not None:
        registry, warning = cached
        return deepcopy(registry), warning

    registry: dict[str, StrategyDefinition] = _default_strategies()
    warning = ""
    if yaml is None:
        warning = "PyYAML 未安装，已使用内置策略。"
    elif config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            for item in data.get("strategies", []) or []:
                if not isinstance(item, Mapping):
                    continue
                strategy_id = str(item.get("id") or item.get("strategy_id") or "").strip()
                if not strategy_id:
                    continue
                base = registry.get(strategy_id).to_dict() if strategy_id in registry else None
                strategy = _strategy_from_mapping(strategy_id, item, base=base)
                if strategy is not None:
                    strategy.integration_type = "config" if strategy_id in registry else "custom_config"
                    registry[strategy_id] = strategy
        except Exception as exc:
            warning = f"策略配置读取失败，已使用内置策略：{exc}"
    payload = {key: value.to_dict() for key, value in registry.items() if value.enabled}
    _REGISTRY_CACHE[cache_key] = (deepcopy(payload), warning)
    return deepcopy(payload), warning


def _canonical_strategy_id(strategy_name: str | None) -> str:
    key = str(strategy_name or "ma_breakout").strip() or "ma_breakout"
    return ALIASES.get(key, key)


def resolve_strategy(strategy_name: str | None, path: str | Path | None = None) -> dict[str, Any]:
    registry, _warning = load_strategy_registry(path)
    strategy_id = _canonical_strategy_id(strategy_name)
    if strategy_id in registry:
        return dict(registry[strategy_id])
    fallback = dict(registry["ma_breakout"])
    fallback["strategy_warning"] = f"未知策略 {strategy_name}，已回退到 ma_breakout。"
    return fallback


def resolve_strategy_name(strategy_name: str | None, path: str | Path | None = None) -> str:
    return str(resolve_strategy(strategy_name, path=path).get("strategy_id", "ma_breakout"))


def list_strategy_definitions(path: str | Path | None = None) -> list[dict[str, Any]]:
    registry, _warning = load_strategy_registry(path)
    return [dict(item) for item in registry.values()]


def active_strategy_catalog(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    registry, _warning = load_strategy_registry(path)
    return registry


def get_strategy(strategy_id: str, path: str | Path | None = None) -> StrategyDefinition | None:
    """Return a StrategyDefinition by ID, or None if not found."""
    registry, _warning = load_strategy_registry(path)
    canonical_id = _canonical_strategy_id(strategy_id)
    data = registry.get(canonical_id)
    if data is None:
        return None
    return _strategy_from_mapping(canonical_id, data)


def strategy_registry_to_dict(path: str | Path | None = None) -> dict[str, Any]:
    registry, warning = load_strategy_registry(path)
    return {
        "strategies": list(registry.values()),
        "warning": warning,
        "disclaimer": STRATEGY_DEMO_DISCLAIMER,
    }


__all__ = [
    "STRATEGY_DEMO_DISCLAIMER",
    "StrategyDefinition",
    "active_strategy_catalog",
    "get_strategy",
    "builtin_strategy_catalog",
    "list_strategy_definitions",
    "load_strategy_registry",
    "resolve_strategy",
    "resolve_strategy_name",
    "strategy_registry_to_dict",
]
