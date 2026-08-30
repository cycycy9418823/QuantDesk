"""Data confidence scoring for low-price learning items (v2.7.3).

Produces a simple numeric score and status label indicating whether a stock's
local daily-bar cache is sufficient for strategy simulation.  This is a data
quality indicator only — it does not constitute investment advice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol
from quantdesk.utils import safe_float, safe_int


def calculate_data_confidence(item: dict[str, Any]) -> dict[str, Any]:
    """Score a single low-price report item for data completeness.

    Returns a dict with:
    - data_confidence_score (int 0-100)
    - data_confidence_status ("ok" / "warning" / "blocked")
    - data_confidence_note (str)
    - missing_required_fields (list[str])
    - missing_optional_fields (list[str])
    - block_reasons (list[str])
    - warning_reasons (list[str])
    """

    score = 100
    missing_required: list[str] = []
    missing_optional: list[str] = []
    block_reasons: list[str] = []
    warning_reasons: list[str] = []

    # Required: close
    if item.get("latest_close") is None and safe_float(item.get("close")) is None:
        score = 0
        missing_required.append("close")
        block_reasons.append("缺少收盘价，无法进行任何模拟。")

    # Optional but important: open/high/low
    for field in ("open", "high", "low"):
        # Check if the item reports these as missing
        if item.get(f"has_{field}_field") is False:
            score -= 20
            missing_optional.append(field)
            warning_reasons.append(f"缺少 {field} 字段。")

    # Amount / volume
    has_amount = item.get("has_amount_field", True)
    has_volume = item.get("has_volume_field", True)
    liquidity = str(item.get("liquidity_status") or "")
    if not has_amount and not has_volume:
        score -= 20
        missing_optional.append("amount/volume")
        warning_reasons.append("缺少成交额和成交量字段，无法判断流动性。")
    elif liquidity == "very_low":
        score -= 30
        warning_reasons.append("20 日均成交活跃度极低。")

    # Row count
    row_count = safe_int(item.get("row_count") or item.get("rows"), 0) or 0
    if row_count < 60:
        score -= 30
        warning_reasons.append(f"历史行数不足 60（当前 {row_count}），指标计算可能不准确。")

    # Staleness
    is_stale = item.get("is_stale", False)
    if is_stale:
        score -= 20
        warning_reasons.append("本地日线滞后于理论交易日。")

    score = max(score, 0)
    if block_reasons:
        status = "blocked"
    elif score >= 80:
        status = "ok"
    elif score >= 60:
        status = "warning"
    else:
        status = "blocked"

    notes: list[str] = block_reasons + warning_reasons
    note = "；".join(notes) if notes else "数据完整度良好。"

    return {
        "data_confidence_score": score,
        "data_confidence_status": status,
        "data_confidence_note": note,
        "missing_required_fields": missing_required,
        "missing_optional_fields": missing_optional,
        "block_reasons": block_reasons,
        "warning_reasons": warning_reasons,
    }


CONFIDENCE_LABELS = {
    "research_ready": "高可信参考",
    "partial": "部分可信参考",
    "degraded": "降级参考",
    "missing": "证据缺失",
}

VERIFICATION_LABELS = {
    "daily_research_ready": "日线研究候选",
    "daily_partial": "日线证据部分可用",
    "daily_degraded": "日线证据降级",
    "daily_unavailable": "待补数据后复核",
    "intraday_unverified": "分钟线待核验",
}


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", "-", "nan", "None", "null"):
            return value
    return None


def _project_daily_path(symbol: str, project_root: str | Path | None) -> Path | None:
    code = normalize_symbol(symbol)
    if not code:
        return None
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[1]
    return root / "data" / "daily" / f"{code}.csv"


def _evidence(
    kind: str,
    severity: str,
    message: str,
    suggested_action: str,
) -> dict[str, str]:
    return {
        "kind": kind,
        "severity": severity,
        "message": message,
        "suggested_action": suggested_action,
    }


def _has_daily_evidence(item: dict[str, Any], *, symbol: str, project_root: str | Path | None) -> bool:
    quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
    row_count = safe_int(quality.get("row_count") or item.get("row_count") or item.get("rows"), 0) or 0
    if row_count > 0:
        return True
    price = safe_float(_first_present(item, ("latest_close", "current_price", "close", "price")), None)
    if price is not None:
        return True
    daily_path = _project_daily_path(symbol, project_root)
    return bool(daily_path and daily_path.exists() and daily_path.is_file())


def build_reference_confidence(
    item: dict[str, Any],
    *,
    target_date: str | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a research-only evidence confidence payload for one candidate.

    The payload is deliberately about evidence sufficiency. It does not include
    buy/sell advice, target prices, or position instructions.
    """

    symbol = normalize_symbol(str(item.get("symbol") or item.get("ts_code") or item.get("code") or ""))
    quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
    score = 100
    missing_evidence: list[dict[str, str]] = []
    confidence_reasons: list[str] = []
    degradation_reasons: list[str] = []

    daily_ok = _has_daily_evidence(item, symbol=symbol, project_root=project_root)
    price = safe_float(_first_present(item, ("latest_close", "current_price", "close", "price")), None)
    if not daily_ok or price is None:
        score = min(score, 25)
        degradation_reasons.append("缺少可验证的本地日线或最新收盘价。")
        missing_evidence.append(
            _evidence(
                "missing_daily_file",
                "error",
                f"缺少可验证的本地日线证据：data/daily/{symbol or '<symbol>'}.csv。",
                f"补齐 data/daily/{symbol or '<symbol>'}.csv 后再复核。",
            )
        )
    else:
        confidence_reasons.append("本地日线与最新价格字段可用于研究复核。")

    row_count = safe_int(quality.get("row_count") or item.get("row_count") or item.get("rows"), None)
    if row_count is not None and row_count < 60:
        score -= 20
        degradation_reasons.append(f"有效日线行数不足 60（当前 {row_count}）。")
        missing_evidence.append(
            _evidence(
                "short_daily_history",
                "warning",
                f"有效日线行数不足 60（当前 {row_count}），长期指标需要降级解读。",
                "补齐更长历史日线后再进行严肃复核。",
            )
        )

    tone = str(quality.get("tone") or "").lower()
    if tone in {"warn", "warning", "missing", "field_gap", "degraded"}:
        score -= 15 if tone in {"warn", "warning", "field_gap"} else 35
        reasons = quality.get("missing_reasons") if isinstance(quality.get("missing_reasons"), list) else []
        message = str(reasons[0]) if reasons else str(quality.get("status") or "数据质量降级")
        degradation_reasons.append(message)
        missing_evidence.append(
            _evidence(
                "daily_quality_warning",
                "warning" if tone != "missing" else "error",
                message,
                "检查 data/daily 字段完整性、日期新鲜度和本地数据导入结果。",
            )
        )

    amount = safe_float(_first_present(item, ("daily_amount", "amount_ma20", "amount", "turnover_amount")), None)
    if amount is None or amount <= 0:
        score -= 10
        degradation_reasons.append("缺少成交额/成交量证据，流动性判断降级。")
        missing_evidence.append(
            _evidence(
                "missing_liquidity_evidence",
                "warning",
                "缺少成交额/成交量证据，流动性判断只能降级展示。",
                "补齐 amount 或 volume 字段以改善流动性复核。",
            )
        )
    else:
        confidence_reasons.append("成交额字段可用于流动性观察。")

    intraday_required = bool(item.get("intraday_required"))
    minute_available = item.get("minute_data_available")
    if minute_available is None and intraday_required and symbol and target_date:
        try:
            from quantdesk.market_intelligence_utils import intraday_file_exists

            minute_available = intraday_file_exists(symbol, str(target_date), project_root=project_root)
        except Exception:
            minute_available = False
    if intraday_required and not bool(minute_available):
        score -= 20
        degradation_reasons.append("缺少目标日 1m 分钟线，盘中触发验证未完成。")
        missing_evidence.append(
            _evidence(
                "missing_1m_data",
                "warning",
                f"缺少 {target_date or '目标日'} 的 1m 分钟线，盘中触发只能标为未核验。",
                "如需盘中触发复核，补齐 data/intraday/1m 下对应日期或股票文件。",
            )
        )
    elif intraday_required:
        confidence_reasons.append("目标日分钟线存在，可用于盘中触发复核。")

    score = max(0, min(100, int(round(score))))
    has_error = any(row.get("severity") == "error" for row in missing_evidence)
    if has_error:
        status = "missing"
        legacy_status = "blocked"
        verification_status = "daily_unavailable"
    elif intraday_required and not bool(minute_available):
        status = "partial" if score >= 60 else "degraded"
        legacy_status = "warning" if status == "partial" else "blocked"
        verification_status = "intraday_unverified"
    elif score >= 85 and not missing_evidence:
        status = "research_ready"
        legacy_status = "ok"
        verification_status = "daily_research_ready"
    elif score >= 60:
        status = "partial"
        legacy_status = "warning"
        verification_status = "daily_partial"
    else:
        status = "degraded"
        legacy_status = "blocked"
        verification_status = "daily_degraded"

    note_parts = confidence_reasons or ["已有部分本地证据可复核。"]
    if degradation_reasons:
        note_parts.extend(degradation_reasons[:3])

    return {
        "data_confidence": {
            "status": status,
            "score": score,
            "label": CONFIDENCE_LABELS[status],
            "note": "；".join(note_parts),
        },
        "data_confidence_status": legacy_status,
        "data_confidence_score": score,
        "data_confidence_note": "；".join(note_parts),
        "verification_status": verification_status,
        "verification_status_label": VERIFICATION_LABELS[verification_status],
        "missing_evidence": missing_evidence,
        "confidence_reasons": confidence_reasons,
        "degradation_reasons": degradation_reasons,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_investment_advice": True,
        "no_position_advice": True,
    }
