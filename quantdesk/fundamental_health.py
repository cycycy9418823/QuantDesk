from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from quantdesk.integrations.registry import check_optional_import

FUNDAMENTAL_HEALTH_DISCLAIMER = "财务红线仅用于本地辅助复核，不构成投资建议，也不接券商接口。"
FUNDAMENTAL_PROVIDER_UNAVAILABLE_LABEL = "财务数据源未启用"
FUNDAMENTAL_FINANCIAL_KEYWORDS = {"银行", "保险", "证券", "券商", "信托"}


@dataclass
class FundamentalHealthAssessment:
    symbol: str
    name: str
    fundamental_status: str
    fundamental_status_label: str
    red_flags: list[str]
    warnings: list[str]
    missing_fields: list[str]
    provider_status: str
    data_date: str
    explanation: str
    disclaimer: str


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in {None, ""}:
            return value
    return None


def _safe_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    value = _first_value(data, keys)
    return str(value or "")


def _industry(data: Mapping[str, Any]) -> str:
    return _text(data, ("industry", "industry_name", "sector", "板块", "行业"))


def _contains_any(text: str, keywords: set[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def check_fundamental_health(
    stock_data: Mapping[str, Any] | Any,
    *,
    provider_status: str | None = None,
) -> FundamentalHealthAssessment:
    data = _as_mapping(stock_data)
    provider = provider_status or ("available" if check_optional_import("akshare").get("installed") else "provider_unavailable")
    symbol = str(_first_value(data, ("symbol", "code")) or "").zfill(6)
    name = str(_first_value(data, ("name",)) or "")
    data_date = str(_first_value(data, ("data_date", "report_date", "quote_date", "date")) or "")

    if provider != "available":
        return FundamentalHealthAssessment(
            symbol=symbol,
            name=name,
            fundamental_status="provider_unavailable",
            fundamental_status_label=FUNDAMENTAL_PROVIDER_UNAVAILABLE_LABEL,
            red_flags=[],
            warnings=["财务数据源未启用，当前仅能做占位提示。"],
            missing_fields=["net_profit_yoy", "operating_cash_flow_yoy", "debt_to_asset", "revenue_yoy", "st_flag"],
            provider_status="provider_unavailable",
            data_date=data_date,
            explanation="财务数据源未启用，当前只显示财务红线占位提示，不做真实财务判断。",
            disclaimer=FUNDAMENTAL_HEALTH_DISCLAIMER,
        )

    red_flags: list[str] = []
    warnings: list[str] = []
    missing_fields: list[str] = []

    industry = _industry(data)
    st_flag = _text(data, ("st_flag", "is_st", "st_status", "risk_flag", "special_treatment"))
    delist_flag = _text(data, ("delist_risk", "delisting_risk", "退市风险", "delist_status"))
    profit_yoy = _safe_float(_first_value(data, ("net_profit_yoy", "profit_yoy", "net_profit_growth", "profit_growth")))
    cash_flow_yoy = _safe_float(_first_value(data, ("operating_cash_flow_yoy", "cash_flow_yoy", "ocf_yoy")))
    revenue_yoy = _safe_float(_first_value(data, ("revenue_yoy", "营业收入同比", "revenue_growth")))
    debt_to_asset = _safe_float(_first_value(data, ("debt_to_asset", "asset_liability_ratio", "资产负债率")))
    operating_cash_flow = _safe_float(_first_value(data, ("operating_cash_flow", "ocf", "经营现金流")))

    if st_flag and _contains_any(st_flag.upper(), {"ST", "*ST"}) or _contains_any(delist_flag, {"退市", "风险"}):
        red_flags.append("ST / 退市风险标签")
    if profit_yoy is None:
        missing_fields.append("net_profit_yoy")
    elif profit_yoy <= -30:
        red_flags.append("净利润明显下滑")
    elif profit_yoy <= -10:
        warnings.append("净利润同比下滑")
    if revenue_yoy is None:
        missing_fields.append("revenue_yoy")
    elif revenue_yoy <= -20:
        red_flags.append("营收明显下滑")
    elif revenue_yoy <= -10:
        warnings.append("营收同比下滑")
    if cash_flow_yoy is None and operating_cash_flow is None:
        missing_fields.append("operating_cash_flow")
    if cash_flow_yoy is not None and cash_flow_yoy < 0:
        warnings.append("经营现金流为负")
    if operating_cash_flow is not None and operating_cash_flow < 0:
        warnings.append("经营现金流为负")
    if debt_to_asset is None:
        missing_fields.append("debt_to_asset")
    elif debt_to_asset >= 95:
        red_flags.append("资产负债率异常偏高")
    elif debt_to_asset >= 85:
        if _contains_any(industry, FUNDAMENTAL_FINANCIAL_KEYWORDS):
            warnings.append("金融行业资产负债率偏高，仅作提示")
        else:
            warnings.append("资产负债率偏高")

    if not red_flags and not warnings and missing_fields:
        status = "missing"
        label = "财务数据缺失"
        explanation = "财务字段缺失较多，当前只能做保守提示。"
    elif red_flags:
        status = "red_flag"
        label = "财务红线"
        explanation = "发现财务红线提示，需要更严格人工确认。"
    elif warnings:
        status = "warning"
        label = "财务风险提示"
        explanation = "财务字段存在提醒项，适合继续观察和复核。"
    else:
        status = "ok"
        label = "财务状态正常"
        explanation = "未见明显财务红线提示。"

    return FundamentalHealthAssessment(
        symbol=symbol,
        name=name,
        fundamental_status=status,
        fundamental_status_label=label,
        red_flags=red_flags,
        warnings=warnings,
        missing_fields=missing_fields,
        provider_status="available",
        data_date=data_date,
        explanation=explanation,
        disclaimer=FUNDAMENTAL_HEALTH_DISCLAIMER,
    )


def fundamental_health_to_dict(assessment: FundamentalHealthAssessment) -> dict[str, Any]:
    return asdict(assessment)


def summarize_fundamental_health_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": 0,
        "ok": 0,
        "warning": 0,
        "red_flag": 0,
        "provider_unavailable": 0,
        "missing": 0,
        "unknown": 0,
    }
    for row in rows or []:
        status = str(row.get("fundamental_status") or row.get("fundamental_health_status") or "").strip().lower()
        if status not in summary:
            status = "unknown"
        summary["total"] += 1
        summary[status] += 1
    return summary


__all__ = [
    "FUNDAMENTAL_HEALTH_DISCLAIMER",
    "FUNDAMENTAL_PROVIDER_UNAVAILABLE_LABEL",
    "FundamentalHealthAssessment",
    "check_fundamental_health",
    "fundamental_health_to_dict",
    "summarize_fundamental_health_rows",
]
