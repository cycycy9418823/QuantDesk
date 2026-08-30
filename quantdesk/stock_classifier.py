from __future__ import annotations

from typing import Any, Mapping

from quantdesk.schema_alias import canonical_field_name, normalize_strategy_item_fields
from quantdesk.strategy_features import build_strategy_feature_row, safe_float

CLASSIFIER_DISCLAIMER = "股票分类仅用于本地规则复核和模拟验证，不构成投资建议。"
FIELD_LABELS = {
    "close": "收盘价",
    "ma20": "20 日均线",
    "activity_value": "成交活跃度",
    "activity_avg_20": "20 日均成交活跃度",
    "amount_ma20": "20 日均成交额",
    "volume_ma20": "20 日均成交量",
    "liquidity_status": "成交活跃度状态",
    "data_confidence_status": "数据可信度状态",
    "data_reliability_score": "数据可信度评分",
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _symbol(stock: Mapping[str, Any]) -> str:
    return str(stock.get("symbol") or stock.get("code") or "").zfill(6)


def _strategy_category(strategy_result: Any) -> str:
    data = _as_mapping(strategy_result)
    status = str(data.get("status") or "")
    if not status and isinstance(data.get("detail"), Mapping):
        status = str(_as_mapping(data["detail"]).get("status") or "")
    return {
        "all_matched": "观察条件全部满足",
        "partial": "部分满足",
        "blocked": "已阻断",
        "missing_required": "缺少关键字段",
    }.get(status, str(data.get("status_label") or "部分满足"))


def _data_category(features: Mapping[str, Any], trust_assessment: Any) -> str:
    coverage = _as_mapping(features.get("feature_coverage"))
    trust = _as_mapping(trust_assessment)
    score = safe_float(features.get("data_reliability_score") or trust.get("data_reliability_score"))
    if score is not None and score < 60:
        return "数据可信度不足"
    if features.get("feature_freshness") == "mixed":
        return "混合数据"
    if features.get("feature_freshness") == "intraday_estimate_only":
        return "等待数据更新"
    if any(value == "demo_fallback" for value in coverage.values()):
        return "样例补全"
    if any(value in {"missing", "unknown"} for value in coverage.values()):
        return "数据部分缺失"
    return "数据完整"


def _trend_category(features: Mapping[str, Any]) -> str:
    close = safe_float(features.get("close"))
    ma20 = safe_float(features.get("ma20"))
    if close is None or ma20 is None:
        return "趋势字段不足"
    if close > ma20:
        return "站上20日均线"
    if ma20 * 0.98 <= close <= ma20 * 1.02:
        return "接近20日均线"
    return "跌破20日均线"


def _activity_category(features: Mapping[str, Any]) -> str:
    ratio = safe_float(features.get("activity_ratio_20"))
    if ratio is None:
        return "成交活跃度字段不足"
    if ratio >= 1.5:
        return "成交活跃度放大"
    if ratio >= 0.8:
        return "成交活跃度一般"
    return "成交活跃度不足"


def _liquidity_status(features: Mapping[str, Any]) -> tuple[str, str]:
    amount_ma20 = safe_float(features.get("amount_ma20") or features.get("activity_avg_20"))
    source = str(features.get("activity_source") or "")
    if amount_ma20 is not None and source in {"amount", "activity", "demo_fallback"}:
        if amount_ma20 >= 30_000_000:
            return "ok", "20 日均成交额处于可读范围，仅作数据质量观察。"
        if amount_ma20 >= 10_000_000:
            return "low", "20 日均成交额偏低，规则检查需谨慎。"
        return "very_low", "20 日均成交额很低，流动性观察不足。"
    if safe_float(features.get("volume_ma20") or features.get("avg_volume_20")) is not None:
        return "unknown", "缺少成交额字段，仅能参考成交量，成交活跃度无法完整判断。"
    return "unknown", "缺少成交额和成交量字段，无法判断成交活跃度。"


def _missing_field_diagnostics(features: Mapping[str, Any], strategy_result: Mapping[str, Any]) -> list[dict[str, str]]:
    features = normalize_strategy_item_fields(dict(features or {}))
    strategy_result = normalize_strategy_item_fields(dict(strategy_result or {}))
    coverage = _as_mapping(features.get("feature_coverage"))
    missing = {canonical_field_name(field) for field in (strategy_result.get("missing_fields") or [])}
    for key, value in coverage.items():
        canonical = canonical_field_name(str(key))
        if value in {"missing", "unknown"} and canonical in {"close", "ma20", "activity_value", "amount_ma20", "data_confidence_score"}:
            if features.get(canonical) in {None, ""}:
                missing.add(canonical)
    diagnostics = []
    for field in sorted(missing):
        diagnostics.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "impact": {
                    "activity_avg_20": "无法判断成交活跃度",
                    "amount_ma20": "无法判断成交活跃度",
                    "liquidity_status": "无法判断是否适合进入虚拟组合",
                    "data_confidence_status": "风控无法判断数据是否可靠",
                    "data_reliability_score": "数据可信度只能部分判断",
                    "ma20": "无法判断趋势是否站上 20 日均线",
                    "close": "无法完成价格条件检查",
                }.get(field, "影响部分规则检查"),
                "fix": {
                    "activity_avg_20": "检查日线 CSV 是否有 amount / volume 字段并重新生成报告",
                    "amount_ma20": "检查日线 CSV 是否有 amount / turnover / 成交额字段",
                    "liquidity_status": "重新生成低价学习池报告",
                    "data_confidence_status": "重新生成低价报告或检查日线字段",
                }.get(field, "检查本地日线字段并重新生成报告"),
            }
        )
    return diagnostics


def _risk_category(stock: Mapping[str, Any], trust_assessment: Any) -> str:
    trust = _as_mapping(trust_assessment)
    state = str(stock.get("action_state") or trust.get("action_state") or "")
    risk_text = " ".join(
        str(value)
        for value in [
            stock.get("data_warning_level"),
            stock.get("news_warning_level"),
            stock.get("action_label"),
            " ".join(stock.get("key_risks") or []) if isinstance(stock.get("key_risks"), list) else stock.get("key_risks"),
        ]
        if value
    )
    if state == "暂不参考" or "ERROR" in risk_text:
        return "暂不进入观察池"
    if "WARNING" in risk_text or "风险偏高" in risk_text:
        return "风险较高"
    if state in {"只观察", "虚拟验证"}:
        return "谨慎观察"
    return "低风险观察"


def _portfolio_category(stock: Mapping[str, Any], paper_status: Any) -> str:
    status = _as_mapping(paper_status)
    held = status.get(_symbol(stock)) or status.get(str(stock.get("code") or ""))
    if isinstance(held, Mapping):
        settlement = str(held.get("settlement_status") or held.get("status") or "")
        if "waiting" in settlement or "等待" in settlement:
            return "等待结算"
        return "虚拟持仓中"
    if str(stock.get("paper_portfolio_status") or "").lower() in {"held", "holding"}:
        return "虚拟持仓中"
    return "未进入虚拟组合"


def classify_stock(
    stock: Mapping[str, Any],
    strategy_result: Any = None,
    trust_assessment: Any = None,
    paper_status: Any = None,
) -> dict[str, Any]:
    features = normalize_strategy_item_fields(build_strategy_feature_row(dict(stock or {}), str(stock.get("data_mode") or "sample")))
    trust = trust_assessment or stock.get("trust_assessment") or {}
    strategy_category = _strategy_category(strategy_result or {})
    data_category = _data_category(features, trust)
    trend_category = _trend_category(features)
    activity_category = _activity_category(features)
    risk_category = _risk_category(stock, trust)
    portfolio_category = _portfolio_category(stock, paper_status)
    strategy_map = normalize_strategy_item_fields(dict(_as_mapping(strategy_result or {})))
    liquidity_status, liquidity_note = _liquidity_status(features)
    confidence_score = safe_float(features.get("data_reliability_score"), None)
    confidence_status = "blocked" if confidence_score is not None and confidence_score < 60 else "ok" if confidence_score is not None else "unknown"
    missing_diagnostics = _missing_field_diagnostics(features, strategy_map)
    block_reasons: list[str] = []
    warning_reasons: list[str] = []
    if strategy_map.get("status") == "all_matched" and missing_diagnostics:
        warning_reasons.append("部分字段缺失，但不影响当前策略模拟。")
    if strategy_map.get("status") == "all_matched":
        warning_reasons.append("已满足自动模拟条件。")
    if confidence_status == "blocked":
        block_reasons.append("data_confidence_status=blocked，数据可信度不足")
    if liquidity_status == "very_low":
        block_reasons.append("liquidity_status=very_low，成交活跃度过低")
    for item in missing_diagnostics:
        if item["field"] in {"close", "ma20", "activity_value", "activity_avg_20"}:
            block_reasons.append(f"缺少 {item['field']}（{item['label']}）")
    tags = [strategy_category, data_category, trend_category, activity_category, risk_category]
    if portfolio_category != "未进入虚拟组合":
        tags.append(portfolio_category)
    primary_category = strategy_category if strategy_category != "部分满足" else risk_category
    if strategy_map.get("status") == "all_matched":
        explanation = f"已满足自动模拟条件；{data_category}；{trend_category}；{activity_category}。请结合风险复核和人工确认。"
    else:
        explanation = f"{strategy_category}；{data_category}；{trend_category}；{activity_category}。请结合风险复核和人工确认。"
    return {
        "symbol": _symbol(stock),
        "name": str(stock.get("name") or ""),
        "primary_category": primary_category,
        "strategy_category": strategy_category,
        "data_category": data_category,
        "trend_category": trend_category,
        "activity_category": activity_category,
        "risk_category": risk_category,
        "portfolio_category": portfolio_category,
        "tags": tags,
        "explanation": explanation,
        "feature_freshness": features.get("feature_freshness"),
        "data_date_label": features.get("feature_freshness_label"),
        "liquidity_status": liquidity_status,
        "liquidity_note": liquidity_note,
        "data_confidence_status": confidence_status,
        "data_confidence_score": confidence_score,
        "missing_required_fields": [item["field"] for item in missing_diagnostics],
        "missing_field_diagnostics": missing_diagnostics,
        "block_reasons": list(dict.fromkeys(block_reasons)),
        "warning_reasons": list(dict.fromkeys(warning_reasons + list(features.get("feature_notes") or []))),
        "raw_data_status": {
            "csv_exists": bool(stock.get("csv_path") or stock.get("daily_cache_path") or stock.get("data_latest_date") or features.get("daily_bar_date")),
            "latest_date": str(stock.get("data_latest_date") or stock.get("latest_date") or features.get("daily_bar_date") or ""),
            "row_count": stock.get("row_count") or stock.get("history_row_count") or "",
            "has_ohlc": all(features.get(key) is not None for key in ("close",)),
            "has_amount_field": features.get("activity_source") == "amount",
            "has_volume_field": features.get("volume") is not None,
        },
        "derived_field_status": {
            "ma20": features.get("ma20") is not None,
            "amount_ma20": features.get("activity_avg_20") is not None and features.get("activity_source") == "amount",
            "volume_ma20": features.get("avg_volume_20") is not None,
            "liquidity_status": liquidity_status,
            "data_confidence_status": confidence_status,
        },
        "next_steps": [
            "重新生成低价报告",
            "检查 data/daily CSV 是否包含成交额/成交量",
            "如果只有价格字段，可以继续观察，但不进入自动虚拟组合",
            "如果字段已存在但仍缺失，说明字段名识别需要修复",
        ],
        "disclaimer": CLASSIFIER_DISCLAIMER,
    }


__all__ = ["CLASSIFIER_DISCLAIMER", "classify_stock"]
