"""Research workflow cards for the daily QuantDesk review.

The cards are a research-only translation layer: they organize existing daily
workflow evidence into plain-language review cards, but they do not change
strategy formulas, ranking, backtest logic, paper-account state, or execution
boundaries.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.indicators import add_indicators, latest_indicator_snapshot
from quantdesk.shared import normalize_symbol
from quantdesk.utils import atomic_write_json, read_json_safe


DEFAULT_ARTIFACT_RELATIVE_PATH = Path("results") / "research" / "research_workflow_cards_latest.json"
SECTOR_ROTATION_RELATIVE_PATH = Path("results") / "sector_rotation_latest.json"
BACKTEST_RELATIVE_PATHS = [
    Path("results") / "backtest_latest.json",
    Path("results") / "backtest" / "latest.json",
    Path("results") / "backtest" / "backtest_latest.json",
]

BOUNDARY_BADGES = [
    "Research Only",
    "Paper Account Only",
    "No Broker Route",
    "Live Execution Locked",
    "QMT Order Route Locked",
    "Not Investment Advice",
    "Not Position Advice",
]

CARD_BOUNDARY = {
    "research_only": True,
    "paper_account_only": True,
    "no_broker_route": True,
    "live_execution_locked": True,
    "qmt_order_route_locked": True,
    "not_investment_advice": True,
    "not_position_advice": True,
}

RESEARCH_STATES = {
    "abandoned",
    "watch_only",
    "review_needed",
    "paper_simulation_candidate",
    "high_risk_warning",
    "degraded",
}

STRATEGY_EVIDENCE_STATES = {
    "current_evidence_best",
    "excluded_pending_research",
    "metric_conflict_unresolved",
    "degraded",
    "missing",
}

DATA_QUALITY_AREAS = [
    "adjustment_basis",
    "pit_st_delist_coverage",
    "suspension_limit_coverage",
    "benchmark_availability",
    "candidate_pool_completeness",
    "paper_holding_latest_price",
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _int(value: Any, default: int = 0) -> int:
    number = _number(value, None)
    if number is None:
        return default
    return int(number)


def _pct_text(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value * 100:.1f}%"


def _heat_score_scale(value: float | None) -> str:
    if value is None:
        return "unknown"
    if 0 <= value <= 1:
        return "ratio_0_1"
    if 1 < value <= 100:
        return "score_0_100"
    return "raw_unbounded"


def _heat_score_display(value: float | None) -> str:
    if value is None:
        return "unknown"
    scale = _heat_score_scale(value)
    if scale == "ratio_0_1":
        return f"{value * 100:.1f}%"
    if scale == "score_0_100":
        return f"{value:.1f}/100"
    return f"{value:.6g}"


def _price_text(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f}"


def _zone_text(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "unknown"
    return f"{low:.2f}-{high:.2f}"


def _first_value(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _project_root(project_root: Path | str | None = None) -> Path:
    return Path(project_root) if project_root is not None else Path.cwd()


def _read_project_json(relative_path: Path, project_root: Path | str | None = None) -> dict[str, Any]:
    payload = read_json_safe(_project_root(project_root) / relative_path, {})
    return payload if isinstance(payload, dict) else {}


def _read_first_project_json(relative_paths: list[Path], project_root: Path | str | None = None) -> tuple[dict[str, Any], str]:
    root = _project_root(project_root)
    for relative_path in relative_paths:
        target = root / relative_path
        payload = read_json_safe(target, {})
        if isinstance(payload, dict) and payload:
            return payload, str(relative_path)
    return {}, ""


def _read_daily_csv(symbol: str, project_root: Path | str | None = None) -> pd.DataFrame | None:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return None
    path = _project_root(project_root) / "data" / "daily" / f"{normalized}.csv"
    if not path.exists() or not path.is_file():
        return None
    frame = read_csv_frame(path)
    if frame.empty:
        return None
    if frame.empty or "close" not in frame.columns:
        return None
    if "date" in frame.columns:
        frame = frame.sort_values("date")
    close = pd.to_numeric(frame["close"], errors="coerce")
    if close.dropna().empty:
        return None
    frame = frame.copy()
    frame["close"] = close
    return frame.reset_index(drop=True)


def _candidate_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = _as_dict(payload.get("auto_strategy"))
    candidates = strategy.get("standard_candidates")
    if not isinstance(candidates, list):
        candidates = []
        for key in ("top_candidates", "long_candidates", "board_candidates", "candidates"):
            rows = strategy.get(key)
            if isinstance(rows, list):
                candidates.extend(rows)
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        symbol = _candidate_symbol(row)
        dedupe = symbol or f"row-{len(clean)}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        clean.append(row)
    return clean


def _candidate_symbol(row: dict[str, Any]) -> str:
    value = _first_value(row, ("symbol", "code", "ts_code", "ticker"), "")
    return normalize_symbol(value) or _string(value)


def _candidate_name(row: dict[str, Any]) -> str:
    return _string(_first_value(row, ("name", "stock_name", "display_name"), ""), "unknown")


def _candidate_sector(row: dict[str, Any]) -> str:
    return _string(
        _first_value(row, ("theme", "sector", "industry", "theme_name", "board", "concept"), ""),
        "unknown",
    )


def _candidate_close(row: dict[str, Any]) -> float | None:
    return _number(
        _first_value(
            row,
            (
                "latest_close",
                "close",
                "last_close",
                "price",
                "current_price",
                "entry_reference_price",
            ),
        ),
        None,
    )


def _candidate_volatility(row: dict[str, Any]) -> float | None:
    value = _number(_first_value(row, ("volatility", "volatility_20d", "atr_pct", "risk_pct"), None), None)
    if value is None:
        return None
    return value / 100.0 if value > 1 else value


def _candidate_score(row: dict[str, Any]) -> float | None:
    return _number(
        _first_value(
            row,
            (
                "score",
                "total_score",
                "combined_score",
                "long_score",
                "board_score",
                "data_confidence",
            ),
            None,
        ),
        None,
    )


def _source_paths(payload: dict[str, Any], *extra: str) -> list[str]:
    artifacts = _as_dict(payload.get("artifacts"))
    paths = [
        _string(payload.get("latest_path")),
        _string(payload.get("report_path")),
        _string(artifacts.get("daily_result")),
        _string(artifacts.get("daily_report")),
        _string(artifacts.get("low_price_report")),
    ]
    paths.extend(extra)
    deduped: list[str] = []
    for path in paths:
        if path and path not in deduped:
            deduped.append(path)
    return deduped


def _base_card(
    *,
    card_id: str,
    card_type: str,
    title: str,
    research_state: str,
    plain_summary: str,
    evidence: list[str] | None = None,
    counterpoints: list[str] | None = None,
    data_quality: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    source_paths: list[str] | None = None,
    generated_at: str,
) -> dict[str, Any]:
    state = research_state if research_state in RESEARCH_STATES else "degraded"
    return {
        "card_id": card_id,
        "card_type": card_type,
        "title": title,
        "research_state": state,
        "plain_summary": plain_summary,
        "evidence": list(evidence or []),
        "counterpoints": list(counterpoints or []),
        "data_quality": dict(data_quality or {}),
        "warnings": list(warnings or []),
        "source_paths": list(source_paths or []),
        "generated_at": generated_at,
        "boundary": dict(CARD_BOUNDARY),
    }


def _candidate_evidence(row: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    score = _candidate_score(row)
    if score is not None:
        evidence.append(f"综合研究分数/置信代理: {score:.2f}")
    sector = _candidate_sector(row)
    if sector != "unknown":
        evidence.append(f"题材/行业映射: {sector}")
    amount = _number(_first_value(row, ("amount_ma20", "amount", "turnover_amount", "activity_value"), None), None)
    if amount is not None:
        evidence.append(f"流动性代理可用: {amount:.0f}")
    trend = _number(_first_value(row, ("ret20", "return_20d", "return_60d", "momentum_20d"), None), None)
    if trend is not None:
        evidence.append(f"阶段强弱代理: {trend:.2f}")
    confidence = _number(_first_value(row, ("data_confidence", "reference_confidence", "confidence_score"), None), None)
    if confidence is not None:
        evidence.append(f"数据参考置信代理: {confidence:.2f}")
    if not evidence:
        evidence.append("候选进入研究池，但缺少可解释的结构化证据。")
    return evidence[:5]


def _candidate_warnings(row: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key in ("warnings", "warning_flags", "risk_flags", "missing_evidence"):
        value = row.get(key)
        if isinstance(value, list):
            warnings.extend(_string(item) for item in value if _string(item))
        elif _string(value):
            warnings.append(_string(value))
    verification = _string(row.get("verification_status"))
    if verification and verification.lower() not in {"ok", "passed", "verified"}:
        warnings.append(f"verification_status={verification}")
    if _candidate_close(row) is None:
        warnings.append("missing_latest_price")
    if _candidate_sector(row) == "unknown":
        warnings.append("missing_theme_or_sector")
    if not warnings:
        warnings.append("research_only_boundary")
    return list(dict.fromkeys(warnings))


def _candidate_data_quality(row: dict[str, Any]) -> dict[str, Any]:
    warnings = _candidate_warnings(row)
    return {
        "latest_price": "available" if _candidate_close(row) is not None else "missing",
        "theme_or_sector": "available" if _candidate_sector(row) != "unknown" else "missing",
        "reference_confidence": _first_value(row, ("data_confidence", "reference_confidence", "confidence_score"), "unknown"),
        "tradability_checks": _first_value(row, ("tradability_status", "verification_status"), "unknown"),
        "status": "degraded" if any("missing" in item or "unknown" in item for item in warnings) else "partial",
    }


def _data_driven_counterpoints(row: dict[str, Any], data_quality: dict[str, Any]) -> list[str]:
    counterpoints: list[str] = []
    if data_quality.get("latest_price") != "available":
        counterpoints.append("最新价格缺失，价格地图和纸面情景需要降级阅读。")
    else:
        counterpoints.append("最新价格虽可用，仍需确认与复权口径、交易日和数据源一致。")
    if data_quality.get("theme_or_sector") != "available":
        counterpoints.append("题材/行业映射缺失，不能据此推断主线候选关系。")
    else:
        counterpoints.append("题材/行业映射可用但可能来自缓存或代理字段，需要与板块证据交叉验证。")
    confidence = _number(data_quality.get("reference_confidence"), None)
    if confidence is None:
        counterpoints.append("参考置信度缺失，候选证据不能视为已验证。")
    elif confidence < 0.7:
        counterpoints.append(f"参考置信度偏低（{confidence:.2f}），需要更多来源确认。")
    verification = _string(data_quality.get("tradability_checks"), "unknown").lower()
    if verification not in {"ok", "passed", "verified"}:
        counterpoints.append(f"可交易性/校验状态为 {verification or 'unknown'}，停牌、涨跌停和流动性仍需复核。")
    if _candidate_close(row) is None:
        counterpoints.append("候选行本身缺少 close/latest_close 字段，不能直接做价格复盘。")
    fallback = [
        "复权口径、PIT/ST/退市覆盖需要在数据质量卡中同步确认。",
        "候选进入研究池不等于 alpha 证明，需要保留反方证据。",
        "纸面风险预算只用于情景观察，不代表真实资金安排。",
    ]
    for item in fallback:
        if len(counterpoints) >= 3:
            break
        counterpoints.append(item)
    return list(dict.fromkeys(counterpoints))[:6]


def _indicator_snapshot_for_daily(daily_df: pd.DataFrame | None) -> tuple[dict[str, Any], list[str]]:
    if daily_df is None or daily_df.empty:
        return {}, ["daily_csv_missing_or_invalid"]
    try:
        enhanced = add_indicators(daily_df.copy())
        snapshot = latest_indicator_snapshot(enhanced)
    except Exception as exc:
        return {}, [f"indicator_snapshot_failed:{exc}"]
    warnings = [
        _string(item)
        for item in _as_list(snapshot.get("indicator_warnings"))
        if _string(item)
    ]
    return snapshot, warnings


def _fallback_price_map(row: dict[str, Any]) -> dict[str, Any]:
    close = _candidate_close(row)
    if close is None:
        return {
            "basis_price": "unknown",
            "support_zone": "unknown",
            "pressure_zone": "unknown",
            "risk_zone": "unknown",
            "strength_weakness_confirmation_zone": "unknown",
            "ma20": "unknown",
            "atr14": "unknown",
            "boll_upper": "unknown",
            "boll_lower": "unknown",
            "high_60d": "unknown",
            "low_60d": "unknown",
            "indicators_used": [],
            "scenario_note": "缺少最新价格，价格地图降级为 unknown。",
        }
    return {
        "basis_price": _price_text(close),
        "basis_price_source": "candidate_row",
        "support_zone": _zone_text(close * 0.95, close * 0.99),
        "pressure_zone": _zone_text(close * 1.03, close * 1.08),
        "risk_zone": f"below {_price_text(close * 0.92)}",
        "strength_weakness_confirmation_zone": {
            "stronger_if_above": _price_text(close * 1.02),
            "weaker_if_below": _price_text(close * 0.96),
        },
        "ma20": "unknown",
        "atr14": "unknown",
        "boll_upper": "unknown",
        "boll_lower": "unknown",
        "high_60d": "unknown",
        "low_60d": "unknown",
        "indicators_used": [],
        "warnings": ["daily_csv_missing_or_invalid", "price_map_fallback_candidate_close_proxy"],
        "scenario_note": "缺少可用本地日线，价格地图退化为候选价代理区间；不是执行指令。",
    }


def _build_price_map(row: dict[str, Any], daily_df: pd.DataFrame | None) -> dict[str, Any]:
    snapshot, indicator_warnings = _indicator_snapshot_for_daily(daily_df)
    close = _number(snapshot.get("close"), None) or _candidate_close(row)
    if close is None or not snapshot:
        return _fallback_price_map(row)
    ma20 = _number(snapshot.get("ma20"), close)
    atr14 = _number(snapshot.get("atr14"), close * 0.02)
    boll_upper = _number(snapshot.get("boll_upper"), close * 1.05)
    boll_lower = _number(snapshot.get("boll_lower"), close * 0.95)
    high_60d = _number(snapshot.get("high_60d"), None)
    low_60d = _number(snapshot.get("low_60d"), None)
    support_low = max(value for value in (low_60d, boll_lower) if value is not None)
    support_high = ma20
    pressure_low = ma20
    pressure_high = min(value for value in (high_60d, boll_upper) if value is not None)
    risk_line = ma20 - 2 * atr14
    return {
        "basis_price": _price_text(close),
        "basis_price_source": "data/daily",
        "support_zone": _zone_text(min(support_low, support_high), max(support_low, support_high)),
        "pressure_zone": _zone_text(min(pressure_low, pressure_high), max(pressure_low, pressure_high)),
        "risk_zone": f"below {_price_text(risk_line)}",
        "strength_weakness_confirmation_zone": {
            "stronger_if_above": _price_text(ma20),
            "weaker_if_below": _price_text(boll_lower),
        },
        "ma20": _price_text(ma20),
        "atr14": _price_text(atr14),
        "boll_upper": _price_text(boll_upper),
        "boll_lower": _price_text(boll_lower),
        "high_60d": _price_text(high_60d),
        "low_60d": _price_text(low_60d),
        "indicators_used": ["ma20", "atr14", "bollinger", "60d_high_low"],
        "indicator_snapshot": {
            "ma20": _price_text(ma20),
            "atr14": _price_text(atr14),
            "boll_upper": _price_text(boll_upper),
            "boll_lower": _price_text(boll_lower),
            "high_60d": _price_text(high_60d),
            "low_60d": _price_text(low_60d),
        },
        "warnings": indicator_warnings,
        "scenario_note": "基于 MA20/ATR14/BOLL/60日高低的技术区间，不是执行指令。",
    }


def _price_map(row: dict[str, Any], project_root: Path | str | None = None) -> dict[str, Any]:
    daily_df = _read_daily_csv(_candidate_symbol(row), project_root=project_root)
    return _build_price_map(row, daily_df)


def _paper_risk_budget_scenarios(row: dict[str, Any], portfolio_value: float | None) -> list[dict[str, Any]]:
    equity = portfolio_value or 100000.0
    volatility = _candidate_volatility(row)
    scenarios: list[dict[str, Any]] = []
    for exposure in (0.02, 0.05, 0.10):
        scenarios.append(
            {
                "exposure": _pct_text(exposure),
                "notional_proxy": round(equity * exposure, 2),
                "concentration_after_proxy": _pct_text(exposure),
                "volatility_proxy": _pct_text(exposure * volatility) if volatility is not None else "unknown",
                "drawdown_proxy": _pct_text(exposure * (volatility or 0.10) * 2),
                "scenario_boundary": "paper_account_scenario_only",
            }
        )
    return scenarios


def _candidate_research_state(row: dict[str, Any]) -> str:
    warnings = _candidate_warnings(row)
    if _candidate_close(row) is None:
        return "degraded"
    if any("suspend" in item.lower() or "limit" in item.lower() for item in warnings):
        return "high_risk_warning"
    if _candidate_score(row) is not None and len(_candidate_evidence(row)) >= 2:
        return "paper_simulation_candidate"
    return "review_needed"


def _sector_key(value: Any) -> str:
    return _string(value).lower().replace(" ", "")


def _load_sector_rotation(project_root: Path | str | None = None) -> dict[str, Any]:
    return _read_project_json(SECTOR_ROTATION_RELATIVE_PATH, project_root=project_root)


def _sector_rotation_lookup(project_root: Path | str | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any], str]:
    payload = _load_sector_rotation(project_root=project_root)
    lookup: dict[str, dict[str, Any]] = {}
    for group in ("sector_rotation", "rows", "sectors", "sector_rankings", "top_improving_sectors", "weakening_sectors"):
        for row in _as_list(payload.get(group)):
            if not isinstance(row, dict):
                continue
            name = _first_value(row, ("sector_name", "sector", "name", "industry", "theme"), "")
            key = _sector_key(name)
            if key and key not in lookup:
                lookup[key] = row
    source_path = str(SECTOR_ROTATION_RELATIVE_PATH) if payload else ""
    return lookup, payload, source_path


def _sector_rotation_metrics(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "heat_score": "unknown",
            "heat_score_scale": "unknown",
            "heat_score_display": "unknown",
            "sector_strength": "unknown",
            "relative_strength": "unknown",
            "rank_change": "unknown",
            "breadth": "unknown",
        }
    heat_score = _number(row.get("heat_score"), None)
    relative_strength = _number(
        _first_value(row, ("relative_strength", "sector_strength", "rs_20d", "rs_5d", "rs_60d"), None),
        None,
    )
    rank_change = _number(_first_value(row, ("rank_change", "rank_improvement_20d", "rank_improvement_5d"), None), None)
    breadth = _number(_first_value(row, ("breadth", "above_ma20_ratio", "up_ratio_5d", "up_ratio_1d"), None), None)
    return {
        "heat_score": round(heat_score, 6) if heat_score is not None else "unknown",
        "heat_score_scale": _heat_score_scale(heat_score),
        "heat_score_display": _heat_score_display(heat_score),
        "sector_strength": _string(_first_value(row, ("sector_status", "heat_status"), ""), "unknown"),
        "relative_strength": round(relative_strength, 6) if relative_strength is not None else "unknown",
        "rank_change": round(rank_change, 6) if rank_change is not None else "unknown",
        "breadth": round(breadth, 6) if breadth is not None else "unknown",
    }


def _mainline_stage(count: int, metrics: dict[str, Any]) -> str:
    heat = _number(metrics.get("heat_score"), None)
    rank_change = _number(metrics.get("rank_change"), None)
    if rank_change is not None and rank_change < 0:
        return "weakening"
    if heat is not None and ((heat > 70 and count >= 3) or (0 < heat <= 1 and count >= 3)):
        return "confirming"
    if count >= 3 or (heat is not None and (heat > 50 or 0 < heat <= 1)):
        return "emerging"
    return "unknown"


def _load_backtest(project_root: Path | str | None = None) -> dict[str, Any]:
    payload, _ = _read_first_project_json(BACKTEST_RELATIVE_PATHS, project_root=project_root)
    return payload


def _backtest_reference(project_root: Path | str | None = None) -> tuple[dict[str, Any], str]:
    payload, source_path = _read_first_project_json(BACKTEST_RELATIVE_PATHS, project_root=project_root)
    metrics_source = _as_dict(payload.get("metrics")) or _as_dict(payload.get("summary")) or payload
    metrics = {
        "annual_return": _number(
            _first_value(metrics_source, ("annual_return", "annualized_return", "annual_return_net", "annualized_return_net"), None),
            None,
        ),
        "total_return": _number(_first_value(metrics_source, ("total_return", "net_total_return", "total_return_net"), None), None),
        "max_drawdown": _number(_first_value(metrics_source, ("max_drawdown", "daily_mdd", "rolling_252d_max_drawdown"), None), None),
        "sharpe": _number(_first_value(metrics_source, ("sharpe", "sharpe_ratio", "Sharpe"), None), None),
    }
    if not any(value is not None for value in metrics.values()):
        return {}, source_path
    return metrics, source_path


def _metric_evidence(metrics: dict[str, Any]) -> list[str]:
    labels = {
        "annual_return": ("年化收益", "pct"),
        "total_return": ("总收益", "pct"),
        "max_drawdown": ("最大回撤", "pct"),
        "sharpe": ("夏普", "number"),
    }
    evidence: list[str] = []
    for key, (label, kind) in labels.items():
        value = _number(metrics.get(key), None)
        if value is not None:
            if kind == "pct":
                evidence.append(f"{label}: {value:.2%}（回测，不代表未来）")
            else:
                evidence.append(f"{label}: {value:.2f}")
    return evidence


def _strategy_counterpoints(count: int, metrics: dict[str, Any]) -> list[str]:
    counterpoints = ["候选数量不是 alpha 证明。"]
    sharpe = _number(metrics.get("sharpe"), None)
    max_dd = _number(metrics.get("max_drawdown"), None)
    annual_return = _number(metrics.get("annual_return"), None)
    if sharpe is not None and sharpe < 0.5:
        counterpoints.append(f"夏普比率偏低（{sharpe:.2f}），策略风险调整后收益不足。")
    if max_dd is not None and max_dd < -0.20:
        counterpoints.append(f"最大回撤较大（{max_dd:.2%}），需关注极端行情下的表现。")
    if annual_return is not None and annual_return < 0:
        counterpoints.append("年化收益为负，策略当前回测不产生正收益。")
    if not metrics:
        counterpoints.append("回测数据缺失，策略证据仅基于候选数量。")
    if count <= 0:
        counterpoints.append("本轮候选数量为 0，不能从该策略卡推断有效性。")
    for item in ("策略间指标冲突时需要保留 metric_conflict_unresolved 状态。", "数据质量降级会让策略证据同步降级。"):
        if len(counterpoints) >= 3:
            break
        counterpoints.append(item)
    return counterpoints[:6]


def _build_candidate_decision_cards(
    payload: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    generated_at: str,
) -> list[dict[str, Any]]:
    portfolio = _as_dict(payload.get("auto_paper_portfolio"))
    portfolio_summary = _as_dict(portfolio.get("summary"))
    portfolio_value = _number(
        _first_value(portfolio_summary, ("equity", "total_value", "portfolio_value", "cash"), None),
        None,
    )
    cards: list[dict[str, Any]] = []
    for index, row in enumerate(_candidate_rows(payload)[:20], start=1):
        symbol = _candidate_symbol(row) or f"candidate-{index}"
        name = _candidate_name(row)
        sector = _candidate_sector(row)
        state = _candidate_research_state(row)
        warnings = _candidate_warnings(row)
        evidence = _candidate_evidence(row)
        data_quality = _candidate_data_quality(row)
        card = _base_card(
            card_id=f"candidate-{symbol}",
            card_type="candidate_decision",
            title=f"{symbol} {name} 研究决策卡",
            research_state=state,
            plain_summary=f"{name} 当前是 {sector} 方向的研究候选，状态为 {state}，需要按证据和反方意见继续复核。",
            evidence=evidence,
            counterpoints=_data_driven_counterpoints(row, data_quality),
            data_quality=data_quality,
            warnings=warnings,
            source_paths=_source_paths(payload),
            generated_at=generated_at,
        )
        card["symbol"] = symbol
        card["name"] = name
        card["theme_or_sector"] = sector
        card["price_map"] = _price_map(row, project_root=project_root)
        card["paper_risk_budget_scenarios"] = _paper_risk_budget_scenarios(row, portfolio_value)
        card["next_review_checklist"] = [
            "确认复权口径和最新价格是否同一口径。",
            "确认 PIT/ST/退市、停牌和涨跌停覆盖是否足够。",
            "用纸面账户情景观察集中度、波动和回撤代理变化。",
            "记录反方证据是否足以降级为 watch_only 或 abandoned。",
        ]
        cards.append(card)
    if cards:
        return cards
    fallback = _base_card(
        card_id="candidate-none",
        card_type="candidate_decision",
        title="候选研究卡不可用",
        research_state="degraded",
        plain_summary="当前 payload 未提供可用候选，候选决策卡降级为 not available。",
        evidence=[],
        counterpoints=[
            "候选池为空时不能推断市场没有机会。",
            "可能是上游策略沙盒未运行、数据缺失或本轮被过滤。",
            "需要先恢复候选池和数据质量证据，再做纸面复核。",
        ],
        data_quality={"candidate_pool": "missing", "status": "degraded"},
        warnings=["missing_candidate_pool"],
        source_paths=_source_paths(payload),
        generated_at=generated_at,
    )
    fallback["symbol"] = "unknown"
    fallback["name"] = "unknown"
    fallback["theme_or_sector"] = "unknown"
    fallback["price_map"] = _price_map({}, project_root=project_root)
    fallback["paper_risk_budget_scenarios"] = _paper_risk_budget_scenarios({}, None)
    fallback["next_review_checklist"] = [
        "先恢复候选池生成结果。",
        "确认候选池数据完整性不是 missing。",
        "候选出现后再生成价格地图和纸面风险预算情景。",
    ]
    return [fallback]


def _sector_rotation_lookup_from_payload(sector_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for group in ("sector_rotation", "rows", "sectors", "sector_rankings", "top_improving_sectors", "weakening_sectors"):
        for row in _as_list(sector_payload.get(group)):
            if not isinstance(row, dict):
                continue
            name = _first_value(row, ("sector_name", "sector", "name", "industry", "theme"), "")
            key = _sector_key(name)
            if key and key not in lookup:
                lookup[key] = row
    return lookup


def _readiness_domain_status(readiness: dict[str, Any], domain: str) -> str:
    domains = _as_dict(readiness.get("domains"))
    item = _as_dict(domains.get(domain))
    return _string(item.get("status"), "missing") or "missing"


def _mainline_readiness_quality(readiness: dict[str, Any]) -> dict[str, str]:
    if not readiness:
        return {
            "extended_data_readiness": "missing",
            "sector_index": "missing",
            "sector_membership": "missing",
            "concept_theme": "missing",
            "moneyflow": "missing",
            "fundamentals": "missing",
            "events": "missing",
        }
    return {
        "extended_data_readiness": _string(readiness.get("status"), "missing") or "missing",
        "sector_index": _readiness_domain_status(readiness, "sector_index"),
        "sector_membership": _readiness_domain_status(readiness, "sector_membership"),
        "concept_theme": _readiness_domain_status(readiness, "concept_theme"),
        "moneyflow": _readiness_domain_status(readiness, "moneyflow"),
        "fundamentals": _readiness_domain_status(readiness, "fundamentals"),
        "events": _readiness_domain_status(readiness, "events"),
    }


def _extended_readiness_is_degraded(quality: dict[str, str]) -> bool:
    return any(
        quality.get(key) in {"missing", "staged_not_imported"}
        for key in ("sector_index", "sector_membership", "concept_theme", "moneyflow", "fundamentals", "events")
    )


def _missing_extended_data_readiness() -> dict[str, Any]:
    return {
        "status": "missing",
        "domains": {},
        "capabilities": {},
        "warnings": ["missing:extended_data_readiness"],
        "note": "research_workflow_cards consumes upstream readiness; it does not rescan large data directories.",
    }


def _build_mainline_cards_from_sector_data(
    payload: dict[str, Any],
    sector_payload: dict[str, Any],
    generated_at: str,
    extended_data_readiness: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates = _candidate_rows(payload)
    sector_counts: Counter[str] = Counter(_candidate_sector(row) for row in candidates)
    sector_counts.pop("unknown", None)
    rotation_lookup = _sector_rotation_lookup_from_payload(sector_payload)
    rotation_source = str(SECTOR_ROTATION_RELATIVE_PATH) if sector_payload else ""
    rotation_is_synthetic = _string(sector_payload.get("data_source")).lower() == "synthetic_test_data"
    readiness = _as_dict(extended_data_readiness)
    readiness_quality = _mainline_readiness_quality(readiness)
    cards: list[dict[str, Any]] = []
    for index, (sector, count) in enumerate(sector_counts.most_common(6), start=1):
        rotation_row = rotation_lookup.get(_sector_key(sector))
        rotation_metrics = _sector_rotation_metrics(rotation_row)
        sector_rotation_status = "missing"
        if rotation_row:
            sector_rotation_status = "proxy" if rotation_is_synthetic else "available"
        if count >= 3:
            strength = "medium"
        elif count == 2:
            strength = "low"
        else:
            strength = "low"
        if rotation_row and not rotation_is_synthetic and count >= 2:
            strength = "high"
        stage = _mainline_stage(count, rotation_metrics)
        warnings = ["mainline_candidate_only"]
        if not rotation_row:
            warnings.append("sector_rotation_missing_or_unmatched")
        if rotation_is_synthetic:
            warnings.append("sector_rotation_synthetic_test_data")
        if _extended_readiness_is_degraded(readiness_quality):
            warnings.append("extended_data_readiness_degraded")
        research_state = "review_needed" if stage in {"confirming", "emerging"} else "watch_only"
        counterpoints = ["候选集中可能来自同一筛选因子，并不代表资金确认。"]
        if sector_rotation_status == "missing":
            counterpoints.append("板块轮动数据缺失或未匹配，主线判断依赖候选计数，可信度降低。")
        if _extended_readiness_is_degraded(readiness_quality):
            counterpoints.append("extended data readiness degraded: sector/moneyflow/fundamental/event evidence may be missing or staged but not imported.")
        rank_change = _number(rotation_metrics.get("rank_change"), None)
        if rank_change is not None and rank_change < 0:
            counterpoints.append(f"板块排名下降 {abs(rank_change):.0f} 位，热度可能正在衰减。")
        if count < 3:
            counterpoints.append("候选样本偏少，不足以形成主线假设。")
        if len(counterpoints) < 3:
            counterpoints.append("若后续候选扩散失败或成交约束变差，应降级观察。")
        card = _base_card(
            card_id=f"mainline-{index}",
            card_type="market_mainline",
            title=f"主线候选：{sector}",
            research_state=research_state,
            plain_summary=(
                f"{sector} 在候选池中出现 {count} 次，"
                f"板块热度 {rotation_metrics['heat_score']}，综合判断为 {stage}（主线候选，非确定结论）。"
            ),
            evidence=[
                f"候选池相关数量: {count}",
                f"evidence_strength={strength}",
                f"stage={stage}",
                f"sector_rotation_status={sector_rotation_status}",
                f"heat_score={rotation_metrics['heat_score']}; heat_score_scale={rotation_metrics['heat_score_scale']}",
                f"relative_strength={rotation_metrics['relative_strength']}",
                f"rank_change={rotation_metrics['rank_change']}",
                f"breadth={rotation_metrics['breadth']}",
            ][:5],
            counterpoints=counterpoints[:6],
            data_quality={
                "theme_or_sector": "available",
                "sector_rotation": sector_rotation_status,
                "heat_score": rotation_metrics["heat_score"],
                "heat_score_scale": rotation_metrics["heat_score_scale"],
                "heat_score_display": rotation_metrics["heat_score_display"],
                "status": "proxy" if sector_rotation_status == "proxy" else ("partial" if sector_rotation_status == "available" else "missing"),
                **readiness_quality,
            },
            warnings=warnings,
            source_paths=_source_paths(payload, rotation_source),
            generated_at=generated_at,
        )
        card.update(
            {
                "theme": sector,
                "sector": sector,
                "evidence_strength": strength,
                "stage": stage,
                "sector_rotation": rotation_metrics,
                "supporting_evidence": [
                    f"候选池中 {count} 个样本映射到该方向。",
                    f"板块轮动证据状态: {sector_rotation_status}。",
                ],
                "confirmation_signals": [
                    "板块热度继续上升。",
                    "候选数量继续扩散。",
                    "排名正向移动。",
                ],
                "invalidation_signals": [
                    "板块热度下降。",
                    "候选数量回落。",
                    "排名负向移动。",
                ],
                "related_candidate_count": count,
                "heat_score": rotation_metrics["heat_score"],
                "heat_score_scale": rotation_metrics["heat_score_scale"],
                "heat_score_display": rotation_metrics["heat_score_display"],
                "relative_strength": rotation_metrics["relative_strength"],
                "rank_change": rotation_metrics["rank_change"],
                "breadth": rotation_metrics["breadth"],
            }
        )
        cards.append(card)
    if cards:
        return cards
    return [
        _base_card(
            card_id="mainline-unknown",
            card_type="market_mainline",
            title="主线候选：unknown",
            research_state="degraded",
            plain_summary="当前缺少足够候选或题材映射，主线候选不可用。",
            evidence=[],
            counterpoints=[
                "没有候选不等于没有市场主线。",
                "题材映射缺失会让主线候选判断失真。",
                "需要先补齐候选池、行业映射和数据质量证据。",
            ],
            data_quality={"theme_or_sector": "missing", "status": "degraded", **readiness_quality},
            warnings=[
                "missing_theme_or_sector_evidence",
                *(["extended_data_readiness_degraded"] if _extended_readiness_is_degraded(readiness_quality) else []),
            ],
            source_paths=_source_paths(payload),
            generated_at=generated_at,
        )
        | {
            "theme": "unknown",
            "sector": "unknown",
            "evidence_strength": "low",
            "stage": "unknown",
            "supporting_evidence": [],
            "confirmation_signals": ["候选池和题材映射恢复后再复核。"],
            "invalidation_signals": ["数据继续缺失。"],
            "related_candidate_count": 0,
        }
    ]


def _build_market_mainline_cards(
    payload: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    generated_at: str,
) -> list[dict[str, Any]]:
    sector_payload = _load_sector_rotation(project_root=project_root)
    readiness = _as_dict(payload.get("extended_data_readiness"))
    if not readiness:
        readiness = _missing_extended_data_readiness()
    return _build_mainline_cards_from_sector_data(payload, sector_payload, generated_at, extended_data_readiness=readiness)


def _build_strategy_evidence_cards(
    payload: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    generated_at: str,
) -> list[dict[str, Any]]:
    strategy = _as_dict(payload.get("auto_strategy"))
    counts = _as_dict(strategy.get("candidate_strategy_counts"))
    backtest_metrics, backtest_source = _backtest_reference(project_root=project_root)
    backtest_evidence = _metric_evidence(backtest_metrics)
    if not counts:
        summary = _as_dict(strategy.get("summary"))
        counts = {
            "long": _int(_first_value(summary, ("long_candidate_count", "long_count"), 0)),
            "board": _int(_first_value(summary, ("board_candidate_count", "board_count"), 0)),
        }
    cards: list[dict[str, Any]] = []
    for strategy_name in ("long", "board"):
        count = _int(counts.get(strategy_name), 0)
        status = "current_evidence_best" if count > 0 else "missing"
        research_state = "review_needed" if count > 0 else "degraded"
        warnings = [] if count else ["missing_strategy_candidates"]
        if not backtest_metrics:
            warnings.append("missing_backtest_latest_metrics")
        card = _base_card(
            card_id=f"strategy-{strategy_name}",
            card_type="strategy_evidence",
            title=f"策略证据卡：{strategy_name}",
            research_state=research_state,
            plain_summary=f"{strategy_name} 仅做聚合展示；本功能不改变策略公式、默认排序或回测逻辑。",
            evidence=([f"candidate_count={count}"] if count else []) + backtest_evidence,
            counterpoints=_strategy_counterpoints(count, backtest_metrics),
            data_quality={
                "candidate_count": count,
                "backtest_latest": "available" if backtest_metrics else "missing",
                "status": "partial" if count and backtest_metrics else "missing",
            },
            warnings=warnings,
            source_paths=_source_paths(payload, backtest_source),
            generated_at=generated_at,
        )
        card["strategy_evidence_status"] = status if status in STRATEGY_EVIDENCE_STATES else "degraded"
        card["backtest_metrics"] = backtest_metrics or {
            "annual_return": "unknown",
            "total_return": "unknown",
            "max_drawdown": "unknown",
            "sharpe": "unknown",
        }
        card["backtest_metrics"]["available"] = bool(backtest_metrics)
        card["backtest_metrics"]["disclaimer"] = "回测不代表未来收益。"
        cards.append(card)
    return cards


def _build_paper_holding_review_cards(payload: dict[str, Any], *, generated_at: str) -> list[dict[str, Any]]:
    portfolio = _as_dict(payload.get("auto_paper_portfolio"))
    positions = [row for row in _as_list(portfolio.get("positions")) if isinstance(row, dict)]
    if not positions:
        return [
            _base_card(
                card_id="paper-holding-none",
                card_type="paper_holding_review",
                title="纸面持仓复核不可用",
                research_state="degraded",
                plain_summary="当前未发现可复核的纸面持仓，持仓卡降级为 not available。",
                evidence=[],
                counterpoints=[
                    "没有纸面持仓不代表候选无研究价值。",
                    "纸面账户可能尚未初始化或本轮跳过复盘。",
                    "需要确认最新价格后再做持仓复核。",
                ],
                data_quality={"paper_holding_latest_price": "missing", "status": "degraded"},
                warnings=["missing_paper_holding_positions"],
                source_paths=_source_paths(payload),
                generated_at=generated_at,
            )
        ]
    cards: list[dict[str, Any]] = []
    for index, row in enumerate(positions[:20], start=1):
        symbol = normalize_symbol(_first_value(row, ("symbol", "code", "ts_code"), "")) or f"holding-{index}"
        latest_price = _number(_first_value(row, ("latest_price", "mark_price", "close"), None), None)
        latest_date = _string(_first_value(row, ("latest_date", "valuation_date", "date"), ""), "unknown")
        card = _base_card(
            card_id=f"paper-holding-{symbol}",
            card_type="paper_holding_review",
            title=f"{symbol} 纸面持仓复核",
            research_state="review_needed" if latest_price is not None else "degraded",
            plain_summary=f"{symbol} 仅做纸面账户复核，最新价格状态为 {'available' if latest_price is not None else 'missing'}。",
            evidence=[f"latest_price={_price_text(latest_price)}", f"latest_date={latest_date}"],
            counterpoints=[
                "纸面市值不代表真实成交结果。",
                "缺少最新价格时不能判断当日复盘质量。",
                "持仓复核必须与数据质量卡同时阅读。",
            ],
            data_quality={
                "paper_holding_latest_price": "available" if latest_price is not None else "missing",
                "latest_date": latest_date,
                "status": "partial" if latest_price is not None else "degraded",
            },
            warnings=[] if latest_price is not None else ["missing_latest_price"],
            source_paths=_source_paths(payload),
            generated_at=generated_at,
        )
        cards.append(card)
    return cards


def _quality_status(label: str, status: str, warnings: list[str]) -> dict[str, Any]:
    degraded = status in {"unknown", "missing", "proxy", "degraded"} or bool(warnings)
    return {
        "coverage_area": label,
        "status": status,
        "measured": status not in {"unknown", "missing"},
        "warnings": warnings,
        "is_degraded": degraded,
    }


def _build_data_quality_cards(payload: dict[str, Any], *, generated_at: str) -> list[dict[str, Any]]:
    candidates = _candidate_rows(payload)
    summary = _as_dict(payload.get("summary"))
    freshness = _as_dict(payload.get("data_freshness_gate"))
    daily_price = _as_dict(payload.get("daily_price_status"))
    portfolio = _as_dict(payload.get("auto_paper_portfolio"))
    positions = [row for row in _as_list(portfolio.get("positions")) if isinstance(row, dict)]
    guardrails = _as_dict(payload.get("guardrail_summary"))
    research_snapshot = _as_dict(payload.get("research_dashboard_snapshot"))
    pit_status = _as_dict(research_snapshot.get("pit_status"))

    checks = [
        _quality_status(
            "adjustment_basis",
            "unknown",
            ["adjustment_basis_not_in_daily_workflow_payload"],
        ),
        _quality_status(
            "pit_st_delist_coverage",
            "available" if pit_status.get("status") in {"ok", "available"} else "unknown",
            [] if pit_status.get("status") in {"ok", "available"} else ["pit_st_delist_coverage_unknown"],
        ),
        _quality_status(
            "suspension_limit_coverage",
            "proxy" if guardrails or daily_price else "unknown",
            ["suspension_limit_coverage_proxy"] if guardrails or daily_price else ["suspension_limit_coverage_unknown"],
        ),
        _quality_status(
            "benchmark_availability",
            "unknown",
            ["benchmark_availability_not_confirmed_in_cards"],
        ),
        _quality_status(
            "candidate_pool_completeness",
            "available" if candidates else "missing",
            [] if candidates else ["candidate_pool_missing"],
        ),
        _quality_status(
            "paper_holding_latest_price",
            "available"
            if positions and all(_first_value(row, ("latest_price", "mark_price", "close"), None) is not None for row in positions)
            else ("missing" if positions else "unknown"),
            []
            if positions and all(_first_value(row, ("latest_price", "mark_price", "close"), None) is not None for row in positions)
            else ["paper_holding_latest_price_missing_or_unknown"],
        ),
    ]
    if freshness.get("stale_mode") or daily_price.get("is_stale") or summary.get("daily_price_is_stale"):
        checks.append(_quality_status("daily_freshness", "degraded", ["daily_price_stale"]))

    cards: list[dict[str, Any]] = []
    for check in checks:
        state = "degraded" if check["is_degraded"] else "review_needed"
        card = _base_card(
            card_id=f"data-quality-{check['coverage_area']}",
            card_type="data_quality",
            title=f"数据质量卡：{check['coverage_area']}",
            research_state=state,
            plain_summary=f"{check['coverage_area']} 当前状态为 {check['status']}。",
            evidence=[f"status={check['status']}", f"measured={check['measured']}"],
            counterpoints=[
                "unknown/missing/proxy/degraded 不能当作 0 问题。",
                "数据质量降级会同步降低候选、主线候选和策略证据可信度。",
                "需要回到源 artifact 查证后再提高研究状态。",
            ],
            data_quality=check,
            warnings=list(check["warnings"]),
            source_paths=_source_paths(payload),
            generated_at=generated_at,
        )
        card["coverage_area"] = check["coverage_area"]
        card["status"] = check["status"]
        cards.append(card)
    return cards


def _next_review_checklist(payload: dict[str, Any]) -> list[str]:
    candidates = _candidate_rows(payload)
    checklist = [
        "先读数据质量卡，确认 unknown/missing/proxy/degraded 项。",
        "复核候选卡的证据与至少三条反方意见。",
        "把主线候选当作观察假设，等待确认/失效信号。",
        "纸面风险预算只用于情景观察，不作为真实资金指令。",
    ]
    if not candidates:
        checklist.insert(1, "候选池为空，先恢复上游候选生成和主题映射。")
    return checklist


def _dashboard_summary(
    *,
    candidate_cards: list[dict[str, Any]],
    mainline_cards: list[dict[str, Any]],
    data_quality_cards: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    degraded_count = sum(1 for card in data_quality_cards if card.get("research_state") == "degraded")
    return {
        "generated_at": generated_at,
        "research_state": "degraded" if degraded_count else "review_needed",
        "candidate_card_count": len(candidate_cards),
        "mainline_card_count": len(mainline_cards),
        "data_quality_card_count": len(data_quality_cards),
        "degraded_data_quality_count": degraded_count,
        "plain_summary": (
            "研究决策卡已生成；请先看数据质量和反方意见。"
            if candidate_cards and candidate_cards[0].get("card_id") != "candidate-none"
            else "研究决策卡已降级：候选池或关键数据不可用。"
        ),
        "boundary_badges": list(BOUNDARY_BADGES),
    }


def build_research_workflow_cards(
    payload: dict[str, Any] | None,
    *,
    project_root: Path | str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a research-only card artifact from an existing daily payload."""

    root = _project_root(project_root)
    source = dict(payload or {})
    generated = generated_at or _string(source.get("generated_at"), "") or _now_iso()
    extended_data_readiness = _as_dict(source.get("extended_data_readiness"))
    if not extended_data_readiness:
        extended_data_readiness = _missing_extended_data_readiness()
    source["extended_data_readiness"] = extended_data_readiness
    candidate_cards = _build_candidate_decision_cards(source, project_root=root, generated_at=generated)
    mainline_cards = _build_market_mainline_cards(source, project_root=root, generated_at=generated)
    paper_cards = _build_paper_holding_review_cards(source, generated_at=generated)
    strategy_cards = _build_strategy_evidence_cards(source, project_root=root, generated_at=generated)
    data_quality_cards = _build_data_quality_cards(source, generated_at=generated)
    artifact = {
        "task_type": "research_workflow_cards",
        "schema_version": "1.0",
        "generated_at": generated,
        "dashboard_summary": _dashboard_summary(
            candidate_cards=candidate_cards,
            mainline_cards=mainline_cards,
            data_quality_cards=data_quality_cards,
            generated_at=generated,
        ),
        "market_mainline_cards": mainline_cards,
        "candidate_decision_cards": candidate_cards,
        "paper_holding_review_cards": paper_cards,
        "strategy_evidence_cards": strategy_cards,
        "data_quality_cards": data_quality_cards,
        "extended_data_readiness": extended_data_readiness,
        "next_review_checklist": _next_review_checklist(source),
        "boundary_badges": list(BOUNDARY_BADGES),
    }
    return artifact


def card_context_for_llm(artifact: dict[str, Any]) -> dict[str, Any]:
    summary = _as_dict(artifact.get("dashboard_summary")) if isinstance(artifact, dict) else {}
    candidates = [card for card in _as_list(artifact.get("candidate_decision_cards")) if isinstance(card, dict)] if isinstance(artifact, dict) else []
    mainlines = [card for card in _as_list(artifact.get("market_mainline_cards")) if isinstance(card, dict)] if isinstance(artifact, dict) else []
    quality_cards = [card for card in _as_list(artifact.get("data_quality_cards")) if isinstance(card, dict)] if isinstance(artifact, dict) else []
    quality_issues = []
    for card in quality_cards:
        if card.get("research_state") == "degraded" or _as_list(card.get("warnings")):
            quality_issues.append(
                {
                    "coverage_area": card.get("coverage_area") or "unknown",
                    "status": card.get("status") or "unknown",
                    "warnings": _as_list(card.get("warnings"))[:5],
                }
            )
    candidate_context = [
        {
            "symbol": _string(card.get("symbol")),
            "name": _string(card.get("name")),
            "state": _string(card.get("research_state"), "degraded"),
            "evidence": _as_list(card.get("evidence"))[:5],
            "warnings": _as_list(card.get("warnings"))[:5],
        }
        for card in candidates[:5]
    ]
    mainline_context = [
        {
            "theme": _string(card.get("theme") or card.get("sector"), "unknown"),
            "stage": _string(card.get("stage"), "unknown"),
            "count": card.get("related_candidate_count", 0),
            "heat_score": card.get("heat_score", "unknown"),
            "relative_strength": card.get("relative_strength", "unknown"),
            "rank_change": card.get("rank_change", "unknown"),
        }
        for card in mainlines[:5]
    ]
    quality_issue_names = [
        _string(card.get("coverage_area"), "unknown")
        for card in quality_cards
        if card.get("research_state") == "degraded"
    ]
    return {
        "task_type": "research_workflow_cards",
        "research_state": summary.get("research_state") or "degraded",
        "candidate_count": summary.get("candidate_card_count", len(candidates)),
        "mainline_count": summary.get("mainline_card_count", len(mainlines)),
        "degraded_count": summary.get("degraded_data_quality_count", len(quality_issues)),
        "degraded_quality_count": summary.get("degraded_data_quality_count", len(quality_issues)),
        "candidates": candidate_context,
        "mainlines": mainline_context,
        "quality_issues": quality_issue_names,
        "top_candidates": [
            {
                "symbol": card.get("symbol") or "unknown",
                "name": card.get("name") or "unknown",
                "research_state": card.get("research_state") or "degraded",
                "theme_or_sector": card.get("theme_or_sector") or "unknown",
                "warnings": _as_list(card.get("warnings"))[:5],
            }
            for card in candidates[:5]
        ],
        "top_mainlines": [
            {
                "sector": card.get("sector") or card.get("theme") or "unknown",
                "stage": card.get("stage") or "unknown",
                "evidence_strength": card.get("evidence_strength") or "unknown",
                "related_candidate_count": card.get("related_candidate_count", 0),
                "sector_rotation": _as_dict(card.get("sector_rotation")),
            }
            for card in mainlines[:5]
        ],
        "quality_issue_details": quality_issues[:10],
        "boundary_badges": _as_list(artifact.get("boundary_badges")) or list(BOUNDARY_BADGES) if isinstance(artifact, dict) else list(BOUNDARY_BADGES),
    }


def write_research_workflow_cards_outputs(
    artifact: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root) if project_root is not None else Path.cwd()
    target = Path(output_path) if output_path is not None else root / DEFAULT_ARTIFACT_RELATIVE_PATH
    atomic_write_json(target, artifact)
    result = dict(artifact)
    try:
        display = str(target.relative_to(root))
    except ValueError:
        display = str(target)
    result["output_paths"] = {"latest_json": display}
    atomic_write_json(target, result)
    return result


def load_research_workflow_cards(
    path: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root) if project_root is not None else Path.cwd()
    target = Path(path) if path is not None else root / DEFAULT_ARTIFACT_RELATIVE_PATH
    payload = read_json_safe(target, {})
    return payload if isinstance(payload, dict) else {}


_DISPLAY_TOKEN_REPLACEMENTS = (
    ("verification_status=", "验证状态："),
    ("strategy_evidence_status=", "策略证据状态："),
    ("evidence_strength=", "证据强度："),
    ("coverage_area=", "覆盖范围："),
    ("research_state=", "研究状态："),
    ("warnings=", "警告："),
    ("price_map:", "价格观察区间："),
    ("stage=", "阶段："),
    ("state=", "状态："),
    ("status=", "状态："),
)


def _render_display_text(value: Any, default: str = "暂不可用") -> str:
    """Translate internal key/value markers before rendering operator copy."""

    text = _string(value, default)
    for raw, label in _DISPLAY_TOKEN_REPLACEMENTS:
        text = text.replace(raw, label)
    return text


def _render_bullets(items: list[Any], *, limit: int | None = None) -> list[str]:
    selected = items[:limit] if limit else items
    if not selected:
        return ["  - 暂不可用"]
    return [f"  - {_render_display_text(item)}" for item in selected]


def _render_card_line(card: dict[str, Any]) -> str:
    title = _string(card.get("title"), "未命名")
    state = _string(card.get("research_state"), "degraded")
    warnings = "，".join(
        _render_display_text(item)
        for item in _as_list(card.get("warnings"))
        if _string(item)
    ) or "无"
    summary = _render_display_text(card.get("plain_summary"))
    return f"- **{title}**｜研究状态：{state}｜警告：{warnings}｜{summary}"


def render_research_workflow_cards_markdown(artifact: dict[str, Any] | None) -> str:
    """Render an already-built artifact into Markdown.

    This function intentionally does not rebuild conclusions from raw market
    data. Missing or invalid artifacts render as degraded/not available.
    """

    if not isinstance(artifact, dict) or not artifact:
        return "\n".join(
            [
                "## 研究决策翻译",
                "",
                "- 状态：已降级 / 暂不可用",
                "- 产物：缺失",
                "- 边界：Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice",
            ]
        )

    summary = _as_dict(artifact.get("dashboard_summary"))
    lines = [
        "## 研究决策翻译",
        "",
        f"- 产物状态：{summary.get('research_state') or 'degraded'}",
        f"- 生成时间：{artifact.get('generated_at') or '-'}",
        f"- 摘要：{summary.get('plain_summary') or '暂不可用'}",
        f"- 边界标记：{', '.join(_as_list(artifact.get('boundary_badges')) or BOUNDARY_BADGES)}",
        "",
        "### 主线候选卡",
    ]
    for card in _as_list(artifact.get("market_mainline_cards"))[:5]:
        if isinstance(card, dict):
            lines.append(_render_card_line(card))
            lines.append(
                f"  - 阶段：{card.get('stage') or 'unknown'}；"
                f"证据强度：{card.get('evidence_strength') or 'unknown'}"
            )
    if len(lines) and lines[-1] == "### 主线候选卡":
        lines.append("- not available")

    lines += ["", "### 候选决策卡"]
    for card in _as_list(artifact.get("candidate_decision_cards"))[:5]:
        if isinstance(card, dict):
            lines.append(_render_card_line(card))
            lines.extend(_render_bullets(_as_list(card.get("evidence")), limit=5))
            price_map = _as_dict(card.get("price_map"))
            if price_map:
                lines.append(
                    "  - 价格观察区间："
                    f"支撑={price_map.get('support_zone') or 'unknown'}，"
                    f"压力={price_map.get('pressure_zone') or 'unknown'}，"
                    f"风险={price_map.get('risk_zone') or 'unknown'}"
                )
    if lines[-1] == "### 候选决策卡":
        lines.append("- not available")

    lines += ["", "### 策略证据卡"]
    for card in _as_list(artifact.get("strategy_evidence_cards")):
        if isinstance(card, dict):
            lines.append(_render_card_line(card))
            lines.append(
                f"  - 策略证据状态：{card.get('strategy_evidence_status') or 'missing'}"
            )
    if lines[-1] == "### 策略证据卡":
        lines.append("- not available")

    lines += ["", "### 数据质量卡"]
    for card in _as_list(artifact.get("data_quality_cards")):
        if isinstance(card, dict):
            lines.append(_render_card_line(card))
            lines.append(
                f"  - 覆盖范围：{card.get('coverage_area') or 'unknown'}；"
                f"状态：{card.get('status') or 'unknown'}"
            )
    if lines[-1] == "### 数据质量卡":
        lines.append("- not available")

    lines += ["", "### 下一步复核清单"]
    lines.extend(_render_bullets(_as_list(artifact.get("next_review_checklist"))))
    return "\n".join(lines)
