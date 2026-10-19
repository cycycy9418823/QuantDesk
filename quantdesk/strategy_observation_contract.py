"""Read-only strategy observation context for future agent use.

The context is a local aggregation of existing QuantDesk outputs.  It never
connects to providers, never recalculates strategy decisions, and never
modifies paper portfolio state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.agent.agent_policy_guard import detect_forbidden_trading_advice
from quantdesk.data_update_orchestrator import build_stale_data_warning
from quantdesk.research.backtest_statistical_validation import summarize_statistical_validation
from quantdesk.research.sample_provenance import research_sample_context
from quantdesk.research.feature_quality_audit import summarize_feature_quality
from quantdesk.research.parameter_sensitivity_audit import summarize_parameter_sensitivity
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
STATE_DIR = PROJECT_ROOT / "state"

DATA_HEALTH_DASHBOARD_LATEST_JSON = RESULTS_DIR / "data_health" / "data_health_dashboard_latest.json"
PROVIDER_NETWORK_SMOKE_LATEST_JSON = RESULTS_DIR / "provider_network_smoke_latest.json"
AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
DAILY_WORKFLOW_LATEST_JSON = RESULTS_DIR / "daily_workflow_latest.json"
CONDITIONAL_TRADE_PLANS_LATEST_JSON = RESULTS_DIR / "conditional_trade_plans_latest.json"
CONDITIONAL_EXECUTION_LATEST_JSON = RESULTS_DIR / "conditional_execution_latest.json"
INTRADAY_GAP_STATUS_LATEST_JSON = RESULTS_DIR / "intraday_gap_status_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
CHANLUN_FRACTALS_LATEST_JSON = RESULTS_DIR / "chanlun_fractals_latest.json"
CHANLUN_BI_LATEST_JSON = RESULTS_DIR / "chanlun_bi_latest.json"
CHANLUN_SEGMENT_LATEST_JSON = RESULTS_DIR / "chanlun_segment_latest.json"
STRATEGY_VALIDATION_LATEST_JSON = RESULTS_DIR / "strategy_validation_latest.json"
PAPER_VS_BACKTEST_CONSISTENCY_LATEST_JSON = RESULTS_DIR / "paper_vs_backtest_consistency_latest.json"
FEATURE_QUALITY_AUDIT_LATEST_JSON = RESULTS_DIR / "feature_quality_audit_latest.json"
BACKTEST_STATISTICAL_VALIDATION_LATEST_JSON = RESULTS_DIR / "backtest_statistical_validation_latest.json"
PARAMETER_SENSITIVITY_LATEST_JSON = RESULTS_DIR / "parameter_sensitivity_latest.json"

STRATEGY_OBSERVATION_CONTEXT_LATEST_JSON = RESULTS_DIR / "strategy_observation_context_latest.json"

ALLOWED_AGENT_ACTIONS = [
    "explain",
    "summarize",
    "risk_notes",
    "data_gap_notes",
    "next_observation_steps",
    "experiment_hypotheses",
]
FORBIDDEN_AGENT_ACTIONS = [
    "buy_recommendation",
    "sell_recommendation",
    "position_sizing",
    "target_price",
    "stop_loss_price",
    "real_order",
]


def build_observation_disclaimer() -> str:
    return "仅用于本地学习研究，不构成投资建议；不接券商、不真实下单、不保存真实账户信息。"


def _load_payload(path: Path, label: str, warnings: list[str], data_gaps: list[str]) -> dict[str, Any]:
    payload = read_json(path, {})
    if isinstance(payload, dict) and payload:
        return payload
    rel = _rel(path)
    warnings.append(f"{label} 缺失或为空：{rel}")
    data_gaps.append(label)
    return {}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(path)


def normalize_strategy_signal(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "buy": "observation_candidate",
        "add": "observation_add_candidate",
        "sell": "risk_observation",
        "reduce_half": "risk_observation",
        "sell_all": "risk_observation",
        "stop_loss": "risk_threshold_observation",
        "take_profit": "profit_protection_observation",
        "hold": "hold_observation",
        "block": "blocked_observation",
        "no_fill": "no_fill",
        "missing_intraday": "missing_intraday",
    }
    return mapping.get(text, text or "unknown_observation")


def normalize_trigger_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"missing_intraday", "missing", "no_intraday"}:
        return "cannot_verify_intraday_trigger"
    if text in {"no_fill", "hold"}:
        return "not_triggered"
    if text in {"ok", "triggered", "filled", "reliable_simulated_event"}:
        return "reliable_simulated_event"
    if text in {"failed", "data_unreliable", "partial"}:
        return "data_unreliable"
    return text or "unknown"


def normalize_data_health_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("data_health_summary"), dict) or isinstance(payload.get("freshness"), dict):
        summary = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
        freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
        intraday = payload.get("intraday") if isinstance(payload.get("intraday"), dict) else {}
        provider = payload.get("provider_failure_stats") if isinstance(payload.get("provider_failure_stats"), dict) else {}
        grade = payload.get("data_grade_trend") if isinstance(payload.get("data_grade_trend"), dict) else {}
        return {
            "status": summary.get("status") or payload.get("status") or "unknown",
            "observation_only": (summary.get("status") or payload.get("status")) != "passed",
            "freshness_status": summary.get("freshness_status") or freshness.get("status") or "missing",
            "stale_count": int(summary.get("stale_count") if summary.get("stale_count") is not None else freshness.get("stale_count") or 0),
            "missing_daily_count": int(summary.get("missing_daily_count") if summary.get("missing_daily_count") is not None else freshness.get("missing_count") or 0),
            "max_trading_day_lag": int(freshness.get("max_trading_day_lag") or 0),
            "intraday_coverage_rate": summary.get("intraday_coverage_rate") if summary.get("intraday_coverage_rate") is not None else intraday.get("coverage_rate"),
            "provider_failure_summary": summary.get("provider_failure_summary") if isinstance(summary.get("provider_failure_summary"), dict) else {
                "failed_count": int(provider.get("failed_count") or 0),
                "skipped_count": int(provider.get("skipped_count") or 0),
                "offline_skipped_count": int(provider.get("offline_skipped_count") or 0),
            },
            "data_grade_counts": summary.get("data_grade_counts") if isinstance(summary.get("data_grade_counts"), dict) else (grade.get("grade_counts") if isinstance(grade.get("grade_counts"), dict) else {}),
            "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
            "suggested_next_action": payload.get("suggested_next_action") or "",
            "risk_disclaimer": payload.get("disclaimer") or build_observation_disclaimer(),
        }
    level = str(payload.get("health_level") or payload.get("status") or "unknown")
    cards = payload.get("cards") if isinstance(payload.get("cards"), dict) else {}
    return {
        "status": level,
        "observation_only": level not in {"green", "ok"},
        "summary_text": str(payload.get("summary_text") or payload.get("message") or ""),
        "cards": cards,
        "beginner_next_steps": payload.get("beginner_next_steps") if isinstance(payload.get("beginner_next_steps"), list) else [],
        "risk_disclaimer": payload.get("risk_disclaimer") or build_observation_disclaimer(),
    }


def normalize_chanlun_structure_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {"exists": False, "status": "missing", "observation_only": True}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    return {
        "exists": True,
        "schema_version": payload.get("schema_version"),
        "symbol": payload.get("symbol"),
        "timeframe": payload.get("timeframe"),
        "status": diagnostics.get("status") or payload.get("status") or "available",
        "counts": {
            "fractals": len(payload.get("fractals") or []),
            "bi": len(payload.get("bi_list") or []),
            "segments": len(payload.get("segments") or []),
        },
        "diagnostics": diagnostics,
        "observation_only": True,
        "disclaimer": payload.get("disclaimer") or "缠论结构仅用于学习观察，不构成投资建议。",
    }


def normalize_strategy_validation_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"exists": False, "status": "missing", "observation_only": True}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "exists": True,
        "status": payload.get("status") or summary.get("status") or "unknown",
        "ok": bool(payload.get("ok")),
        "target_date": payload.get("target_date"),
        "lookahead_status": summary.get("lookahead_status")
        or (payload.get("lookahead") if isinstance(payload.get("lookahead"), dict) else {}).get("status"),
        "execution_assumption_status": summary.get("execution_assumption_status")
        or (payload.get("execution_assumptions") if isinstance(payload.get("execution_assumptions"), dict) else {}).get("status"),
        "calendar_quality": payload.get("calendar_quality") or summary.get("calendar_quality"),
        "issue_count": int(payload.get("issue_count") or 0),
        "warning_count": int(payload.get("warning_count") or 0),
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        "checked_at": payload.get("checked_at"),
        "observation_only": True,
        "risk_note": (
            "当前策略验证存在风险，学习复盘报告不应被解读为可靠策略结论。"
            if str(payload.get("status") or "").lower() == "failed"
            else ""
        ),
    }


def normalize_consistency_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"exists": False, "status": "missing", "observation_only": True}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    status = str(payload.get("status") or summary.get("consistency_status") or "unknown")
    return {
        "exists": True,
        "status": status,
        "ok": bool(payload.get("ok")),
        "target_date": payload.get("target_date"),
        "issue_count": int(payload.get("issue_count") or summary.get("issue_count") or 0),
        "warning_count": int(payload.get("warning_count") or summary.get("warning_count") or 0),
        "manifest_available": bool(summary.get("manifest_available")),
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        "checked_at": payload.get("checked_at"),
        "observation_only": True,
        "risk_note": (
            "当前纸面模拟与回测存在一致性风险，本报告仅作学习观察。"
            if status in {"failed", "warning"}
            else ""
        ),
    }


def normalize_feature_quality_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"exists": False, "status": "missing", "observation_only": True, "high_missing_features": []}
    summary = summarize_feature_quality(payload)
    return {
        "exists": True,
        "status": summary.get("status") or "unknown",
        "row_count": summary.get("row_count"),
        "feature_count": summary.get("feature_count"),
        "high_missing_features": summary.get("high_missing_features") or [],
        "high_missing_feature_count": summary.get("high_missing_feature_count") or 0,
        "warning_count": summary.get("warning_count") or 0,
        "observation_only": True,
        "risk_note": (
            "部分特征字段缺失率较高，策略观察结论可信度下降。"
            if int(summary.get("high_missing_feature_count") or 0) > 0
            else ""
        ),
    }


def normalize_statistical_validation_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"exists": False, "status": "missing", "observation_only": True}
    summary = summarize_statistical_validation(payload)
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    p_value = summary.get("p_value")
    risk_notes: list[str] = [summary["sample_limitation"]]
    if "insufficient_sample" in warnings or int(summary.get("sample_size") or 0) < 8:
        risk_notes.append("当前统计样本不足，不能据此判断策略稳定性。")
    if summary.get("permutation_status") != "passed":
        risk_notes.append("当前样本中未显示出明显优于随机基线的证据。")
    return {
        "exists": True,
        "status": summary.get("status") or "unknown",
        "sample_size": summary.get("sample_size"),
        **research_sample_context(summary),
        "p_value": p_value,
        "permutation_status": summary.get("permutation_status"),
        "bootstrap_status": summary.get("bootstrap_status"),
        "bootstrap_ci_summary": summary.get("bootstrap_ci_summary") or {},
        "warnings": warnings,
        "risk_notes": risk_notes,
        "observation_only": True,
    }


def normalize_parameter_sensitivity_for_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {
            "exists": False,
            "status": "missing",
            "observation_only": True,
            "robustness_score": None,
            "top_overfit_warnings": [],
        }
    summary = summarize_parameter_sensitivity(payload)
    overfit = payload.get("overfit_warnings") if isinstance(payload.get("overfit_warnings"), list) else []
    warning_types = [
        str(item.get("type") or "")
        for item in overfit
        if isinstance(item, dict) and item.get("type")
    ]
    risk_notes: list[str] = []
    score = summary.get("robustness_score")
    try:
        score_value = float(score) if score is not None else None
    except Exception:
        score_value = None
    if summary.get("status") in {"failed", "warning"} or (score_value is not None and score_value < 0.7):
        risk_notes.append("当前策略结果对参数较敏感，存在过拟合风险，不能据此推断未来表现。")
    if summary.get("insufficient_sample") or any(item in {"insufficient_trades", "insufficient_windows", "insufficient_sample"} for item in warning_types):
        risk_notes.append("当前样本不足，无法可靠判断参数鲁棒性。")
    return {
        "exists": True,
        "status": summary.get("status") or "unknown",
        "robustness_score": score,
        "combination_count": summary.get("combination_count"),
        "warning_count": summary.get("warning_count"),
        "top_overfit_warnings": summary.get("top_overfit_warnings") or [],
        "warning_types": warning_types,
        "insufficient_sample": bool(summary.get("insufficient_sample")),
        "observation_only": True,
        "risk_notes": risk_notes,
        "disclaimer": summary.get("disclaimer") or "参数敏感性审计仅用于历史样本稳定性诊断，不构成投资建议。",
    }


def _provider_chain(provider_payload: dict[str, Any], data_health_payload: dict[str, Any]) -> list[Any]:
    chain = provider_payload.get("provider_chain")
    if isinstance(chain, list) and chain:
        return chain
    steps = provider_payload.get("steps")
    if isinstance(steps, list) and steps:
        return [
            {
                "provider": item.get("provider") or item.get("provider_used") or "",
                "category": item.get("category") or item.get("label") or "",
                "status": item.get("status") or "",
                "ok": bool(item.get("ok")),
                "failed_reason": short_error_text(item.get("failed_reason") or ""),
                "skipped_reason": short_error_text(item.get("skipped_reason") or ""),
            }
            for item in steps
            if isinstance(item, dict)
        ]
    trace = data_health_payload.get("data_source_trace")
    if isinstance(trace, dict):
        return [{"category": key, "trace": value} for key, value in trace.items()]
    return []


def _intraday_flags(intraday_payload: dict[str, Any], execution_payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    missing_count = int(intraday_payload.get("missing_count") or intraday_payload.get("missing_intraday_count") or 0)
    execution_summary = execution_payload.get("summary") if isinstance(execution_payload.get("summary"), dict) else {}
    missing_exec = int(execution_summary.get("missing_intraday_count") or 0)
    missing_items = intraday_payload.get("missing_symbols") or execution_payload.get("missing_intraday") or []
    pool_payload = intraday_payload.get("verified_minute_pool") if isinstance(intraday_payload.get("verified_minute_pool"), dict) else {}
    execution_gate = execution_summary.get("intraday_quality_gate") if isinstance(execution_summary.get("intraday_quality_gate"), dict) else {}
    verified_count = int(
        intraday_payload.get("verified_minute_pool_available_count")
        or execution_summary.get("verified_completed_intraday_count")
        or 0
    )
    local_unverified_count = int(execution_summary.get("local_unverified_intraday_count") or 0)
    minute_pool_status = pool_payload.get("status") or execution_gate.get("pool_status") or "missing"
    pool_passed = bool(
        pool_payload.get("status") == "passed"
        or execution_gate.get("pool_passed")
        or minute_pool_status == "passed"
    )
    cannot_verify = bool(missing_count or missing_exec or missing_items)
    return (
        {
            "status": "cannot_verify_intraday_trigger" if cannot_verify else "available",
            "target_trade_date": intraday_payload.get("target_trade_date") or intraday_payload.get("target_date"),
            "available_count": intraday_payload.get("available_count"),
            "missing_count": missing_count or missing_exec,
            "missing_symbols": missing_items if isinstance(missing_items, list) else [],
            "verified_completed_intraday_count": verified_count,
            "local_unverified_intraday_count": local_unverified_count,
            "verified_minute_pool": {
                "status": minute_pool_status,
                "passed": pool_passed,
                "available_count": verified_count,
                "missing_count": int(intraday_payload.get("verified_minute_pool_missing_count") or 0),
                "target_date": pool_payload.get("target_date") or execution_gate.get("quality_target_date") or "",
                "job_path": pool_payload.get("job_path") or execution_gate.get("pool_job_path") or "",
                "source_snapshot": pool_payload.get("source_snapshot") or execution_gate.get("pool_source_snapshot") or "",
                "latest_quality_status": pool_payload.get("latest_quality_status") or execution_gate.get("pool_latest_quality_status") or "",
                "latest_snapshot_path": pool_payload.get("latest_snapshot_path") or execution_gate.get("pool_latest_snapshot_path") or "",
                "warnings": [
                    str(item)
                    for item in [
                        *(pool_payload.get("warnings") if isinstance(pool_payload.get("warnings"), list) else []),
                        *(execution_gate.get("warnings") if isinstance(execution_gate.get("warnings"), list) else []),
                    ]
                    if item
                ],
                "research_only": True,
                "paper_only": True,
            },
            "execution_summary": execution_summary,
            "explanation": (
                "缺少分钟线，无法验证盘中触发；系统不会用日线或实时行情假装成交。"
                if cannot_verify
                else "已有分钟线状态可用于盘中触发观察。"
            ),
        },
        cannot_verify,
    )


def build_strategy_observation_context(
    symbol: str = "",
    target_date: str = "",
    *,
    date: str = "",
    network: bool = False,
    write: bool = False,
    output_path: Path | None = None,
    data_health_payload: dict[str, Any] | None = None,
    provider_payload: dict[str, Any] | None = None,
    strategy_payload: dict[str, Any] | None = None,
    workflow_payload: dict[str, Any] | None = None,
    conditional_plan_payload: dict[str, Any] | None = None,
    execution_payload: dict[str, Any] | None = None,
    intraday_payload: dict[str, Any] | None = None,
    portfolio_payload: dict[str, Any] | None = None,
    fractal_payload: dict[str, Any] | None = None,
    bi_payload: dict[str, Any] | None = None,
    segment_payload: dict[str, Any] | None = None,
    validation_payload: dict[str, Any] | None = None,
    consistency_payload: dict[str, Any] | None = None,
    feature_quality_payload: dict[str, Any] | None = None,
    statistical_validation_payload: dict[str, Any] | None = None,
    parameter_sensitivity_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    data_gaps: list[str] = []
    normalized_symbol = normalize_symbol(symbol)
    target = (target_date or date or now_text()[:10]).strip()

    data_health = data_health_payload if isinstance(data_health_payload, dict) else _load_payload(DATA_HEALTH_DASHBOARD_LATEST_JSON, "data_health", warnings, data_gaps)
    provider = provider_payload if isinstance(provider_payload, dict) else _load_payload(PROVIDER_NETWORK_SMOKE_LATEST_JSON, "provider_network_smoke", warnings, data_gaps)
    strategy = strategy_payload if isinstance(strategy_payload, dict) else _load_payload(AUTO_STRATEGY_LATEST_JSON, "auto_strategy", warnings, data_gaps)
    workflow = workflow_payload if isinstance(workflow_payload, dict) else _load_payload(DAILY_WORKFLOW_LATEST_JSON, "daily_workflow", warnings, data_gaps)
    plans = conditional_plan_payload if isinstance(conditional_plan_payload, dict) else _load_payload(CONDITIONAL_TRADE_PLANS_LATEST_JSON, "conditional_trade_plan", warnings, data_gaps)
    execution = execution_payload if isinstance(execution_payload, dict) else _load_payload(CONDITIONAL_EXECUTION_LATEST_JSON, "conditional_execution", warnings, data_gaps)
    intraday = intraday_payload if isinstance(intraday_payload, dict) else _load_payload(INTRADAY_GAP_STATUS_LATEST_JSON, "intraday_gap_status", warnings, data_gaps)
    portfolio = portfolio_payload if isinstance(portfolio_payload, dict) else read_json(AUTO_PAPER_PORTFOLIO_LATEST_JSON, {}) or read_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, {})
    if not isinstance(portfolio, dict) or not portfolio:
        warnings.append("auto_paper_portfolio 缺失或为空")
        data_gaps.append("auto_paper_portfolio")
        portfolio = {}

    fractals = fractal_payload if isinstance(fractal_payload, dict) else read_json(CHANLUN_FRACTALS_LATEST_JSON, {})
    bi = bi_payload if isinstance(bi_payload, dict) else read_json(CHANLUN_BI_LATEST_JSON, {})
    segments = segment_payload if isinstance(segment_payload, dict) else read_json(CHANLUN_SEGMENT_LATEST_JSON, {})
    validation_raw = (
        validation_payload
        if isinstance(validation_payload, dict)
        else read_json(STRATEGY_VALIDATION_LATEST_JSON, {})
    )
    validation_summary = normalize_strategy_validation_for_observation(validation_raw if isinstance(validation_raw, dict) else {})
    consistency_raw = (
        consistency_payload
        if isinstance(consistency_payload, dict)
        else read_json(PAPER_VS_BACKTEST_CONSISTENCY_LATEST_JSON, {})
    )
    consistency_summary = normalize_consistency_for_observation(consistency_raw if isinstance(consistency_raw, dict) else {})
    feature_quality_raw = (
        feature_quality_payload
        if isinstance(feature_quality_payload, dict)
        else read_json(FEATURE_QUALITY_AUDIT_LATEST_JSON, {})
    )
    feature_quality_summary = normalize_feature_quality_for_observation(feature_quality_raw if isinstance(feature_quality_raw, dict) else {})
    statistical_validation_raw = (
        statistical_validation_payload
        if isinstance(statistical_validation_payload, dict)
        else read_json(BACKTEST_STATISTICAL_VALIDATION_LATEST_JSON, {})
    )
    statistical_validation_summary = normalize_statistical_validation_for_observation(
        statistical_validation_raw if isinstance(statistical_validation_raw, dict) else {}
    )
    parameter_sensitivity_raw = (
        parameter_sensitivity_payload
        if isinstance(parameter_sensitivity_payload, dict)
        else read_json(PARAMETER_SENSITIVITY_LATEST_JSON, {})
    )
    parameter_sensitivity_summary = normalize_parameter_sensitivity_for_observation(
        parameter_sensitivity_raw if isinstance(parameter_sensitivity_raw, dict) else {}
    )
    if validation_summary.get("status") == "failed":
        warnings.append("当前策略验证存在风险，学习复盘报告不应被解读为可靠策略结论。")

    if consistency_summary.get("status") in {"failed", "warning"}:
        warnings.append("当前纸面模拟与回测存在一致性风险，本报告仅作学习观察。")
    if int(feature_quality_summary.get("high_missing_feature_count") or 0) > 0:
        warnings.append("部分特征字段缺失率较高，策略观察结论可信度下降。")
    for note in statistical_validation_summary.get("risk_notes") or []:
        warnings.append(str(note))
    for note in parameter_sensitivity_summary.get("risk_notes") or []:
        warnings.append(str(note))

    health = normalize_data_health_for_observation(data_health)
    stale_data_warning = build_stale_data_warning(
        target_date=target,
        symbols=[normalized_symbol] if normalized_symbol else [],
        max_symbols=1 if normalized_symbol else 80,
    )
    if stale_data_warning.get("stale"):
        warnings.append("本地日线数据未更新到目标完整交易日，学习复盘可信度下降。")
    if int(health.get("stale_count") or 0) > 0 or int(health.get("max_trading_day_lag") or 0) > 2:
        warnings.append("本地日线数据可能过期，学习复盘可信度下降。")
    coverage = health.get("intraday_coverage_rate")
    try:
        coverage_value = float(coverage) if coverage is not None else None
    except Exception:
        coverage_value = None
    if coverage_value is not None and coverage_value < 0.5:
        warnings.append("分钟线覆盖率不足，盘中触发验证能力有限。")
    provider_failure_summary = health.get("provider_failure_summary") if isinstance(health.get("provider_failure_summary"), dict) else {}
    if int(provider_failure_summary.get("failed_count") or 0) > 0:
        warnings.append("近期数据源失败较多，请先检查 provider 状态。")
    intraday_status, cannot_verify = _intraday_flags(intraday, execution)
    provider_chain = _provider_chain(provider, data_health)
    source_trace = data_health.get("data_source_trace") if isinstance(data_health.get("data_source_trace"), dict) else {}
    observation_only = bool(health.get("observation_only") or cannot_verify or data_gaps)

    context: dict[str, Any] = {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "status": "warning" if warnings or data_gaps else "passed",
        "ok": True,
        "symbol": normalized_symbol,
        "symbols": [normalized_symbol] if normalized_symbol else [],
        "date": target,
        "target_date": target,
        "purpose": "learning_observation",
        "not_investment_advice": True,
        "network": bool(network),
        "observation_only": observation_only,
        "cannot_verify_intraday_trigger": cannot_verify,
        "data_health": health,
        "data_health_summary": health,
        "stale_data_warning": stale_data_warning,
        "provider_chain": provider_chain,
        "data_source_trace": source_trace,
        "daily_features": {
            "source": "existing_outputs_only",
            "workflow_summary": workflow.get("summary") if isinstance(workflow.get("summary"), dict) else {},
        },
        "strategy_outputs": {
            "auto_strategy": strategy,
            "daily_workflow": workflow,
            "conditional_trade_plan": plans,
            "conditional_execution": execution,
            "normalized_trigger_status": normalize_trigger_status(intraday_status.get("status")),
        },
        "intraday_status": intraday_status,
        "paper_portfolio_status": {
            "exists": bool(portfolio),
            "summary": portfolio.get("summary") if isinstance(portfolio.get("summary"), dict) else {},
            "cash": portfolio.get("cash"),
            "position_count": len(portfolio.get("positions") or []) if isinstance(portfolio.get("positions"), list) else None,
            "readonly": True,
        },
        "validation_summary": validation_summary,
        "consistency_summary": consistency_summary,
        "feature_quality_summary": feature_quality_summary,
        "statistical_validation_summary": statistical_validation_summary,
        "parameter_sensitivity_summary": parameter_sensitivity_summary,
        "chanlun": {
            "fractals": normalize_chanlun_structure_for_observation(fractals if isinstance(fractals, dict) else {}),
            "bi": normalize_chanlun_structure_for_observation(bi if isinstance(bi, dict) else {}),
            "segments": normalize_chanlun_structure_for_observation(segments if isinstance(segments, dict) else {}),
        },
        "warnings": warnings,
        "data_gaps": sorted(set(data_gaps)),
        "allowed_agent_actions": ALLOWED_AGENT_ACTIONS,
        "forbidden_agent_actions": FORBIDDEN_AGENT_ACTIONS,
        "disclaimer": build_observation_disclaimer(),
        "generated_at": now_text(),
    }
    validation = validate_observation_context(context)
    context["validation"] = validation
    if write:
        write_json(output_path or STRATEGY_OBSERVATION_CONTEXT_LATEST_JSON, context)
    return context


def validate_observation_context(context: dict[str, Any]) -> dict[str, Any]:
    forbidden_terms = detect_forbidden_trading_advice(context)
    required = ["schema_version", "symbol", "date", "data_health", "strategy_outputs", "provider_chain", "chanlun", "warnings", "disclaimer"]
    missing = [key for key in required if key not in context]
    return {
        "ok": not missing and not forbidden_terms,
        "missing_required_fields": missing,
        "forbidden_terms": forbidden_terms,
        "policy": "observation_only",
    }
