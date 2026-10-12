from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

import pandas as pd


class DataGrade(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SCORABLE = "SCORABLE"
    RESEARCH_READY = "RESEARCH_READY"
    BACKTEST_READY = "BACKTEST_READY"


DATA_GRADE_LABELS = {
    DataGrade.UNAVAILABLE.value: "不可用",
    DataGrade.OBSERVE_ONLY.value: "仅观察",
    DataGrade.SCORABLE.value: "可基础评分",
    DataGrade.RESEARCH_READY.value: "可策略研究",
    DataGrade.BACKTEST_READY.value: "可回测",
}


NETWORK_BLOCKED_CODE = "NETWORK_BLOCKED_OR_PERMISSION_DENIED"
NETWORK_REMOTE_DISCONNECTED = "NETWORK_REMOTE_DISCONNECTED"
PROVIDER_EMPTY_RESPONSE = "PROVIDER_EMPTY_RESPONSE"
PROVIDER_SCHEMA_MISMATCH = "PROVIDER_SCHEMA_MISMATCH"
PROVIDER_UNSUPPORTED_SYMBOL = "PROVIDER_UNSUPPORTED_SYMBOL"
BSE_MAPPING_MISSING = "BSE_MAPPING_MISSING"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

MARKET_SH = "SH"
MARKET_SZ = "SZ"
MARKET_STAR = "STAR"
MARKET_CHINEXT = "CHINEXT"
MARKET_BSE = "BSE"
MARKET_UNKNOWN = "UNKNOWN"


def classify_market(symbol: str) -> str:
    s = str(symbol or "").strip()
    if len(s) != 6 or not s.isdigit():
        return MARKET_UNKNOWN
    if s.startswith("920"):
        return MARKET_BSE
    if s.startswith("83") or s.startswith("87") or s.startswith("43"):
        return MARKET_BSE
    # 689xxx = 科创板存托凭证（如 689009），302xxx = 创业板新代码段（如 302132）。
    if s.startswith("688") or s.startswith("689"):
        return MARKET_STAR
    if s.startswith("300") or s.startswith("301") or s.startswith("302"):
        return MARKET_CHINEXT
    if s.startswith("60"):
        return MARKET_SH
    if s.startswith("00"):
        return MARKET_SZ
    return MARKET_UNKNOWN


@dataclass(frozen=True)
class DataGradeAssessment:
    data_grade: str
    data_grade_label: str
    reason: str
    row_count: int
    min_date: str
    max_date: str
    latest_date: str
    missing_fields: list[str]
    provider_status: str
    provider_error_type: str
    research_allowed: bool
    backtest_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_grade": self.data_grade,
            "data_grade_label": self.data_grade_label,
            "data_grade_reason": self.reason,
            "row_count": self.row_count,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "latest_date": self.latest_date,
            "missing_fields": list(self.missing_fields),
            "provider_status": self.provider_status,
            "provider_error_type": self.provider_error_type,
            "research_allowed": self.research_allowed,
            "backtest_allowed": self.backtest_allowed,
        }


def normalize_date_text(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y-%m-%d")


def classify_provider_error(error: object) -> str:
    text = str(error or "")
    lowered = text.lower()
    if "winerror 10013" in lowered or ("10013" in lowered and "permission" in lowered):
        return NETWORK_BLOCKED_CODE
    if "远程主机" in text and "拒绝" in text:
        return NETWORK_BLOCKED_CODE
    if "permission denied" in lowered or "forbidden by its access permissions" in lowered:
        return NETWORK_BLOCKED_CODE
    if "remotedisconnected" in lowered:
        return NETWORK_REMOTE_DISCONNECTED
    if "no value to decode" in lowered or "json" in lowered and "decode" in lowered:
        return PROVIDER_SCHEMA_MISMATCH
    if "keyerror" in lowered and "date" in lowered:
        return PROVIDER_SCHEMA_MISMATCH
    if "'date'" in lowered and ("keyerror" in lowered or "not in" in lowered):
        return PROVIDER_SCHEMA_MISMATCH
    if "empty" in lowered and ("daily" in lowered or "data" in lowered):
        return PROVIDER_EMPTY_RESPONSE
    if "cannot normalize" in lowered and "sina" in lowered:
        return BSE_MAPPING_MISSING
    if "unsupported" in lowered:
        return PROVIDER_UNSUPPORTED_SYMBOL
    if "connection" in lowered or "timeout" in lowered:
        return NETWORK_REMOTE_DISCONNECTED
    if not text:
        return ""
    return UNKNOWN_PROVIDER_ERROR


def failed_by_error_type_summary(results: list[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in results:
        if item.get("ok") or item.get("outcome") not in ("failed", None):
            continue
        if item.get("outcome") != "failed":
            continue
        error_type = str(item.get("provider_error_type") or UNKNOWN_PROVIDER_ERROR)
        summary[error_type] = summary.get(error_type, 0) + 1
    return summary


def failed_by_market_summary(results: list[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in results:
        if item.get("ok") or item.get("outcome") != "failed":
            continue
        market = classify_market(str(item.get("symbol", "")))
        summary[market] = summary.get(market, 0) + 1
    return summary


def bse_summary_from_results(results: list[Mapping[str, Any]]) -> dict[str, int]:
    total = 0
    success = 0
    failed = 0
    unsupported = 0
    for item in results:
        market = classify_market(str(item.get("symbol", "")))
        if market != MARKET_BSE:
            continue
        total += 1
        outcome = item.get("outcome", "")
        if outcome == "success" or (outcome == "cache_hit" and item.get("ok")):
            success += 1
        elif outcome == "failed":
            failed += 1
            error_type = str(item.get("provider_error_type") or "")
            if error_type in (PROVIDER_UNSUPPORTED_SYMBOL, BSE_MAPPING_MISSING, PROVIDER_SCHEMA_MISMATCH):
                unsupported += 1
    return {
        "bse_total": total,
        "bse_success_count": success,
        "bse_failed_count": failed,
        "bse_provider_unsupported_count": unsupported,
    }


def _as_int(value: object, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _date_span_days(min_date: str, max_date: str) -> int:
    start = pd.to_datetime(min_date, errors="coerce")
    end = pd.to_datetime(max_date, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return 0
    return max(0, int((pd.Timestamp(end) - pd.Timestamp(start)).days))


def assess_data_grade(
    *,
    row_count: int = 0,
    min_date: object = "",
    max_date: object = "",
    latest_date: object = "",
    provider_status: object = "",
    warning_level: object = "",
    missing_fields: list[str] | None = None,
    provider_errors: Mapping[str, object] | None = None,
    expected_latest_date: object = "",
    min_scorable_rows: int = 20,
    min_research_rows: int = 120,
    min_backtest_rows: int = 500,
) -> DataGradeAssessment:
    rows = max(0, _as_int(row_count))
    min_text = normalize_date_text(min_date)
    max_text = normalize_date_text(max_date or latest_date)
    latest_text = normalize_date_text(latest_date or max_date)
    expected_text = normalize_date_text(expected_latest_date)
    missing = [str(item) for item in (missing_fields or []) if str(item).strip()]
    level = str(warning_level or "").upper()
    provider = str(provider_status or "").strip()
    error_values = list((provider_errors or {}).values())
    provider_error_type = ""
    for error in error_values:
        provider_error_type = classify_provider_error(error)
        if provider_error_type == NETWORK_BLOCKED_CODE:
            break

    grade = DataGrade.UNAVAILABLE
    reason = "没有可用日线数据。"
    if rows <= 0 or level == "ERROR":
        grade = DataGrade.UNAVAILABLE
        if provider_error_type == NETWORK_BLOCKED_CODE:
            reason = "网络或权限阻止行情接口访问，当前股票数据不可用。"
        elif level == "ERROR":
            reason = "数据健康为 ERROR，暂不进入候选、研究或回测。"
    elif missing:
        grade = DataGrade.OBSERVE_ONLY
        reason = "关键字段缺失，仅适合观察，不进入严肃评分或回测。"
    elif rows < min_scorable_rows:
        grade = DataGrade.OBSERVE_ONLY
        reason = f"历史样本只有 {rows} 行，仅适合观察。"
    elif rows < min_research_rows:
        grade = DataGrade.SCORABLE
        reason = f"历史样本 {rows} 行，可做基础评分，但不适合策略研究。"
    elif rows < min_backtest_rows:
        grade = DataGrade.RESEARCH_READY
        reason = f"历史样本 {rows} 行，可做策略研究，但不足以作为完整回测样本。"
    else:
        grade = DataGrade.BACKTEST_READY
        reason = f"历史样本 {rows} 行，达到回测样本下限。"

    if grade != DataGrade.UNAVAILABLE and level == "WARNING":
        if grade == DataGrade.BACKTEST_READY:
            grade = DataGrade.RESEARCH_READY
            reason = "数据健康存在 WARNING，已从可回测降级为可策略研究。"
        elif grade == DataGrade.RESEARCH_READY:
            grade = DataGrade.SCORABLE
            reason = "数据健康存在 WARNING，已降级为基础评分。"

    if expected_text and latest_text:
        latest_ts = pd.to_datetime(latest_text, errors="coerce")
        expected_ts = pd.to_datetime(expected_text, errors="coerce")
        if pd.notna(latest_ts) and pd.notna(expected_ts) and latest_ts < expected_ts and grade not in {DataGrade.UNAVAILABLE, DataGrade.OBSERVE_ONLY}:
            grade = DataGrade.OBSERVE_ONLY
            reason = f"最新日线 {latest_text} 早于预期 {expected_text}，仅适合观察。"

    if grade in {DataGrade.RESEARCH_READY, DataGrade.BACKTEST_READY} and _date_span_days(min_text, max_text) < 180:
        grade = DataGrade.SCORABLE
        reason = "历史日期跨度不足 180 天，已降级为基础评分。"

    return DataGradeAssessment(
        data_grade=grade.value,
        data_grade_label=DATA_GRADE_LABELS[grade.value],
        reason=reason,
        row_count=rows,
        min_date=min_text,
        max_date=max_text,
        latest_date=latest_text,
        missing_fields=missing,
        provider_status=provider,
        provider_error_type=provider_error_type,
        research_allowed=grade in {DataGrade.RESEARCH_READY, DataGrade.BACKTEST_READY},
        backtest_allowed=grade == DataGrade.BACKTEST_READY,
    )


def assess_daily_frame_grade(
    df: pd.DataFrame,
    *,
    provider_status: str = "",
    warning_level: str = "",
    provider_errors: Mapping[str, object] | None = None,
    expected_latest_date: object = "",
    min_scorable_rows: int = 20,
    min_research_rows: int = 120,
    min_backtest_rows: int = 500,
    deep_validate: bool = False,
    min_deep_quality_score: float = 50.0,
) -> DataGradeAssessment:
    """评估日线 DataFrame 的数据等级。

    当 deep_validate=True 时，额外执行深度内容验证（离群值/逻辑错误/停牌伪装），
    若质量分数低于 min_deep_quality_score 则自动降级。
    """
    if df is None or df.empty:
        return assess_data_grade(
            row_count=0,
            provider_status=provider_status,
            warning_level=warning_level,
            provider_errors=provider_errors,
            expected_latest_date=expected_latest_date,
            min_scorable_rows=min_scorable_rows,
            min_research_rows=min_research_rows,
            min_backtest_rows=min_backtest_rows,
        )
    date_col = "date" if "date" in df.columns else "日期" if "日期" in df.columns else ""
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna() if date_col else pd.Series(dtype="datetime64[ns]")
    required = ["close"] if "close" in df.columns else ["收盘"] if "收盘" in df.columns else []
    missing = []
    if not required:
        missing.append("close")
    result = assess_data_grade(
        row_count=int(len(df)),
        min_date=dates.min() if not dates.empty else "",
        max_date=dates.max() if not dates.empty else "",
        latest_date=dates.max() if not dates.empty else "",
        provider_status=provider_status,
        warning_level=warning_level,
        missing_fields=missing,
        provider_errors=provider_errors,
        expected_latest_date=expected_latest_date,
        min_scorable_rows=min_scorable_rows,
        min_research_rows=min_research_rows,
        min_backtest_rows=min_backtest_rows,
    )
    # v3.7.0.1: 可选深度验证——若质量分数低于阈值则自动降级
    if deep_validate and result.backtest_allowed and not missing:
        try:
            from quantdesk.data_deep_validation import deep_validate_daily
            dv = deep_validate_daily(df)
            if dv.data_quality_score < min_deep_quality_score:
                if result.data_grade == DataGrade.BACKTEST_READY.value:
                    result = DataGradeAssessment(
                        data_grade=DataGrade.RESEARCH_READY.value,
                        data_grade_label=DATA_GRADE_LABELS[DataGrade.RESEARCH_READY.value],
                        reason=f"深度质量分数 {dv.data_quality_score:.0f} 低于 {min_deep_quality_score:.0f}，已从可回测降级。",
                        row_count=result.row_count,
                        min_date=result.min_date,
                        max_date=result.max_date,
                        latest_date=result.latest_date,
                        missing_fields=result.missing_fields,
                        provider_status=result.provider_status,
                        provider_error_type=result.provider_error_type,
                        research_allowed=True,
                        backtest_allowed=False,
                    )
        except ImportError:
            pass
    return result


def grade_distribution(items: list[Mapping[str, Any]]) -> dict[str, int]:
    summary = {grade.value: 0 for grade in DataGrade}
    for item in items:
        grade = str(item.get("data_grade") or item.get("grade") or "").upper()
        if grade in summary:
            summary[grade] += 1
    return summary


def utc_now_text() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

