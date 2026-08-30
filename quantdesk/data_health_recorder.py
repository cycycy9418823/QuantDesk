"""Data-health recorder for the daily QuantDesk report.

QuantDesk v2.6.4-data-health-recorder-split moves the data-health
accumulators and stateless classifiers out of
``scripts/daily_quant_report.py`` so they can be imported and
unit-tested independently of the full report pipeline.

This module keeps the **exact** behavior that used to live in
``scripts/daily_quant_report.py``:

- Module-level ``DATA_HEALTH_ROWS`` list and ``DATA_HEALTH_COLUMNS`` names.
- ``DataFetchStatus`` dataclass used for each recorded row.
- Classification thresholds ``DATA_STALE_WARNING_DAYS``,
  ``DATA_STALE_ERROR_DAYS`` and ``DATA_MIN_HISTORY_ROWS_WARNING``.
- Public helpers ``reset_data_health``, ``record_data_health``,
  ``data_health_frame``, ``data_health_summary``,
  ``data_health_reason_summary``, ``attach_data_health``,
  ``data_quality_risk_note``, ``data_risk_flag``,
  ``expected_latest_trading_date``, ``data_freshness_status_from_quote``,
  ``classify_data_warning`` and ``_health_reference_datetime``.

``scripts/daily_quant_report.py`` re-exports every public name from
this module so existing tests, scripts and imports continue to work.

No new scoring, no new strategy, no broker connection, no automated
order placement, no investment advice, and no prediction of future
price direction lives here.  The module is read-only with respect to
scoring logic; it only records data-health metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from typing import Any

import pandas as pd

from quantdesk import explanations as explanation_text
from quantdesk.data_quality import assess_data_grade
from quantdesk.market_clock import (
    DEFAULT_CALENDAR_NOTE,
    expected_settlement_date,
    get_market_session,
    previous_cn_a_share_trading_day,
    settlement_status_from_quote,
)

# ---------------------------------------------------------------------------
# Classification thresholds.
# ---------------------------------------------------------------------------

DATA_STALE_WARNING_DAYS = 3
DATA_STALE_ERROR_DAYS = 10
DATA_MIN_HISTORY_ROWS_WARNING = 120


# ---------------------------------------------------------------------------
# Dataclass describing a single data-health row.
# ---------------------------------------------------------------------------


@dataclass
class DataFetchStatus:
    symbol: str
    name: str
    requested_source: str
    effective_source: str
    adjusted: str
    fresh_fetch_success: bool
    cache_present: bool
    cache_merged: bool
    served_from_cache: bool
    fallback_used: bool
    fallback_from: str
    fallback_to: str
    latest_date: str
    expected_latest_date: str
    stale_days: int
    market_session: str
    expected_reason: str
    data_freshness_status: str
    calendar_note: str
    is_market_holiday: bool
    row_count: int
    error_message: str
    warning_level: str
    warning_message: str
    data_grade: str
    data_grade_label: str
    data_grade_reason: str
    research_allowed: bool
    backtest_allowed: bool
    fetched_at: str


# Module state shared with ``scripts.daily_quant_report`` via re-export.
DATA_HEALTH_ROWS: list[DataFetchStatus] = []

DATA_HEALTH_COLUMNS = [
    field.name for field in DataFetchStatus.__dataclass_fields__.values()
]


# ---------------------------------------------------------------------------
# Stateless time / freshness classifiers.
# ---------------------------------------------------------------------------


def reset_data_health() -> None:
    DATA_HEALTH_ROWS.clear()


def _health_reference_datetime(end_date: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(end_date, datetime):
        return end_date
    if isinstance(end_date, pd.Timestamp) and not pd.isna(end_date):
        return end_date.to_pydatetime()
    parsed = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(parsed):
        return datetime.now()
    target = pd.Timestamp(parsed).date()
    today = datetime.now().date()
    if target == today:
        return datetime.now()
    return datetime.combine(target, dt_time(15, 1))


def expected_latest_trading_date(end_date: str | datetime | pd.Timestamp) -> pd.Timestamp:
    """Return expected latest complete A-share daily bar date.

    The calculation uses QuantDesk's local CN A-share trading calendar. Public
    holidays such as the 2026 Labor Day break are treated as休市日, so
    2026-05-05 expects 2026-04-30 instead of incorrectly expecting same-day
    daily bars.
    """

    expected = expected_settlement_date(_health_reference_datetime(end_date))
    if expected is None:
        parsed = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(parsed):
            return pd.Timestamp(datetime.now().date())
        return pd.Timestamp(parsed).normalize()
    return pd.Timestamp(expected).normalize()


def data_freshness_status_from_quote(
    latest_date: str,
    end_date: str | datetime | pd.Timestamp,
) -> dict[str, object]:
    reference = _health_reference_datetime(end_date)
    status = settlement_status_from_quote(latest_date or None, reference)
    session = str(status.get("market_session") or "")
    expected_text = str(status.get("expected_settlement_date") or "")
    if latest_date and latest_date == expected_text:
        if status.get("is_market_holiday"):
            status["status"] = "market_holiday_using_last_trading_day"
            status["message"] = "当前为 A 股休市日，使用最近交易日完整日线。"
        elif session in {"pre_open", "morning_session", "lunch_break", "afternoon_session"}:
            status["status"] = "waiting_for_close"
            status["message"] = "当前未收盘，使用上一交易日完整日线。"
        else:
            status["status"] = "latest_complete_daily_bar"
            status["message"] = "最新日线符合 A 股交易日历预期。"
    elif latest_date and session == "after_close" and expected_text:
        latest_ts = pd.to_datetime(latest_date, errors="coerce")
        expected_ts = pd.to_datetime(expected_text, errors="coerce")
        if pd.notna(latest_ts) and pd.notna(expected_ts) and latest_ts < expected_ts and not status.get("is_market_holiday"):
            previous = pd.Timestamp(previous_cn_a_share_trading_day(reference.date())).normalize()
            if pd.Timestamp(latest_ts).normalize() == previous:
                status["status"] = "provider_not_updated_after_close"
                status["message"] = f"今日已收盘，但行情源尚未更新到 {expected_text} 完整日线。"
            else:
                status["status"] = "stale"
                status["message"] = "数据早于预期交易日，可能过期。"
    elif str(status.get("status") or "") == "quote_stale":
        status["status"] = "stale"
        status["message"] = "数据早于预期交易日，可能过期。"
    if not latest_date:
        status["status"] = "no_data"
        status["message"] = "没有任何可用日线数据，该股票不能作为候选依据。"
    return status


def classify_data_warning(
    *,
    row_count: int,
    fresh_fetch_success: bool,
    served_from_cache: bool,
    fallback_used: bool,
    stale_days: int,
    error_message: str,
    data_freshness_status: str = "",
) -> tuple[str, str]:
    if row_count <= 0:
        return "ERROR", "没有任何可用日线数据，该股票不能作为候选依据。"
    if data_freshness_status in {"market_holiday_using_last_trading_day", "using_last_trading_day", "latest_complete_daily_bar"}:
        if not fresh_fetch_success and served_from_cache:
            return "WARNING", "本次抓取失败，当前使用缓存数据。"
        if fallback_used:
            return "INFO", "主数据源失败，已使用备用数据源。"
        return "OK", "当前数据符合 A 股交易日历预期。"
    if data_freshness_status == "waiting_for_close":
        return "OK", "当前未收盘，使用上一交易日完整日线。"
    if data_freshness_status == "waiting_for_quote_update":
        return "INFO", "今日已收盘，但行情源尚未更新到今日。"
    if data_freshness_status == "provider_not_updated_after_close":
        return "INFO", "今日已收盘，但行情源尚未更新到预期完整日线。"
    if data_freshness_status == "stale":
        if stale_days > DATA_STALE_ERROR_DAYS:
            return "ERROR", "数据严重早于预期交易日，可能不适合作为今日决策依据。"
        return "WARNING", "数据早于预期交易日，可能过期。"
    if stale_days > DATA_STALE_ERROR_DAYS:
        return "ERROR", "数据严重过期，可能不适合作为今日决策依据。"
    if stale_days > DATA_STALE_WARNING_DAYS:
        return "WARNING", "数据可能过期，使用前请核对最新行情。"
    if not fresh_fetch_success and served_from_cache:
        return "WARNING", "本次抓取失败，当前使用缓存数据。"
    if fallback_used:
        return "INFO", "主数据源失败，已使用备用数据源。"
    if error_message:
        return "WARNING", "数据抓取出现错误，请检查错误摘要。"
    return "OK", "数据状态正常。"


# ---------------------------------------------------------------------------
# Recorder that mutates the module-level ``DATA_HEALTH_ROWS`` list.
# ---------------------------------------------------------------------------


def record_data_health(
    *,
    symbol: str,
    name: str = "",
    adjust: str,
    requested_source: str = "eastmoney",
    effective_source: str,
    merged: pd.DataFrame,
    end_date: str,
    fresh_fetch_success: bool,
    cache_present: bool,
    cache_merged: bool,
    served_from_cache: bool,
    fallback_used: bool,
    fallback_from: str = "",
    fallback_to: str = "",
    error_message: str,
    min_history_rows: int = 0,
) -> None:
    expected = expected_latest_trading_date(end_date)
    expected_latest_date = expected.strftime("%Y-%m-%d")
    if merged.empty or "日期" not in merged.columns:
        latest_date = ""
        min_date = ""
        max_date = ""
        row_count = 0
        stale_days = 9999
    else:
        dates = pd.to_datetime(merged["日期"], errors="coerce").dropna()
        last = dates.max()
        first = dates.min()
        latest_date = last.strftime("%Y-%m-%d") if pd.notna(last) else ""
        min_date = first.strftime("%Y-%m-%d") if pd.notna(first) else ""
        max_date = latest_date
        row_count = int(len(merged))
        stale_days = int(max(0, (expected - last).days)) if pd.notna(expected) and pd.notna(last) else 9999
    freshness = data_freshness_status_from_quote(latest_date, end_date)
    data_freshness_status = str(freshness.get("status") or "")
    market_session = str(freshness.get("market_session") or get_market_session(_health_reference_datetime(end_date)))
    expected_reason = str(freshness.get("message") or "")
    calendar_note = str(freshness.get("calendar_note") or DEFAULT_CALENDAR_NOTE)
    is_market_holiday = bool(freshness.get("is_market_holiday"))
    warning_level, warning_message = classify_data_warning(
        row_count=row_count,
        fresh_fetch_success=fresh_fetch_success,
        served_from_cache=served_from_cache,
        fallback_used=fallback_used,
        stale_days=stale_days,
        error_message=error_message,
        data_freshness_status=data_freshness_status,
    )
    if min_history_rows and 0 < row_count < min_history_rows:
        warning_level = "WARNING"
        warning_message = f"本地历史日线只有 {row_count} 行，少于建议的 {min_history_rows} 行；请先执行 backfill 建库。"
    elif warning_level in {"OK", "INFO"} and expected_reason:
        warning_message = expected_reason
    grade = assess_data_grade(
        row_count=row_count,
        min_date=min_date,
        max_date=max_date,
        latest_date=latest_date,
        provider_status=effective_source,
        warning_level=warning_level,
        provider_errors={"error": error_message} if error_message else {},
        expected_latest_date=expected_latest_date,
    )
    DATA_HEALTH_ROWS.append(
        DataFetchStatus(
            symbol=symbol,
            name=name,
            requested_source=requested_source,
            effective_source=effective_source,
            adjusted=adjust,
            fresh_fetch_success=fresh_fetch_success,
            cache_present=cache_present,
            cache_merged=cache_merged,
            served_from_cache=served_from_cache,
            fallback_used=fallback_used,
            fallback_from=fallback_from,
            fallback_to=fallback_to,
            latest_date=latest_date,
            expected_latest_date=expected_latest_date,
            stale_days=stale_days,
            market_session=market_session,
            expected_reason=expected_reason,
            data_freshness_status=data_freshness_status,
            calendar_note=calendar_note,
            is_market_holiday=is_market_holiday,
            row_count=row_count,
            error_message=error_message,
            warning_level=warning_level,
            warning_message=warning_message,
            data_grade=grade.data_grade,
            data_grade_label=grade.data_grade_label,
            data_grade_reason=grade.reason,
            research_allowed=grade.research_allowed,
            backtest_allowed=grade.backtest_allowed,
            fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )


# ---------------------------------------------------------------------------
# Aggregations over recorded rows.
# ---------------------------------------------------------------------------


def data_health_frame() -> pd.DataFrame:
    if not DATA_HEALTH_ROWS:
        return pd.DataFrame(columns=DATA_HEALTH_COLUMNS)
    return pd.DataFrame([status.__dict__ for status in DATA_HEALTH_ROWS], columns=DATA_HEALTH_COLUMNS)


def data_health_summary(health: pd.DataFrame) -> dict[str, int]:
    if health.empty:
        return {
            "total": 0,
            "fresh_fetch_success": 0,
            "ok": 0,
            "info": 0,
            "warning": 0,
            "error": 0,
            "cache_present": 0,
            "cache_merged": 0,
            "served_from_cache": 0,
            "fallback_used": 0,
            "stale": 0,
            "no_data": 0,
            "market_holiday_using_last_trading_day": 0,
            "waiting_for_close": 0,
            "waiting_for_quote_update": 0,
            "UNAVAILABLE": 0,
            "OBSERVE_ONLY": 0,
            "SCORABLE": 0,
            "RESEARCH_READY": 0,
            "BACKTEST_READY": 0,
        }
    level = health.get("warning_level", pd.Series("", index=health.index)).astype(str).str.upper()
    freshness = health.get("data_freshness_status", pd.Series("", index=health.index)).astype(str)
    summary = {
        "total": int(len(health)),
        "fresh_fetch_success": int(health.get("fresh_fetch_success", pd.Series(False, index=health.index)).fillna(False).sum()),
        "ok": int(level.eq("OK").sum()),
        "info": int(level.eq("INFO").sum()),
        "warning": int(level.eq("WARNING").sum()),
        "error": int(level.eq("ERROR").sum()),
        "cache_present": int(health.get("cache_present", pd.Series(False, index=health.index)).fillna(False).sum()),
        "cache_merged": int(health.get("cache_merged", pd.Series(False, index=health.index)).fillna(False).sum()),
        "served_from_cache": int(health.get("served_from_cache", pd.Series(False, index=health.index)).fillna(False).sum()),
        "fallback_used": int(health.get("fallback_used", pd.Series(False, index=health.index)).fillna(False).sum()),
        "stale": int((pd.to_numeric(health.get("stale_days", pd.Series(9999, index=health.index)), errors="coerce").fillna(9999) > DATA_STALE_WARNING_DAYS).sum()),
        "no_data": int((pd.to_numeric(health.get("row_count", pd.Series(0, index=health.index)), errors="coerce").fillna(0) <= 0).sum()),
        "market_holiday_using_last_trading_day": int(freshness.eq("market_holiday_using_last_trading_day").sum()),
        "waiting_for_close": int(freshness.eq("waiting_for_close").sum()),
        "waiting_for_quote_update": int(freshness.eq("waiting_for_quote_update").sum()),
    }
    grades = health.get("data_grade", pd.Series("", index=health.index)).astype(str).str.upper()
    for grade in ("UNAVAILABLE", "OBSERVE_ONLY", "SCORABLE", "RESEARCH_READY", "BACKTEST_READY"):
        summary[grade] = int(grades.eq(grade).sum())
    return summary


def data_health_reason_summary(health: pd.DataFrame) -> dict[str, int]:
    keys = [
        "latest_complete_daily_bar",
        "market_holiday_using_last_trading_day",
        "waiting_for_close",
        "waiting_for_quote_update",
        "provider_not_updated_after_close",
        "mixed_freshness",
        "stale",
        "no_data",
        "cache_only",
        "fallback_used",
        "fetch_failed",
        "other",
    ]
    summary = {key: 0 for key in keys}
    if health.empty:
        return summary
    for _, row in health.iterrows():
        status = str(row.get("data_freshness_status") or "")
        if status in {"using_last_trading_day", "latest_complete_daily_bar"}:
            status = "market_holiday_using_last_trading_day" if bool(row.get("is_market_holiday", False)) else "latest_complete_daily_bar"
        if status in summary:
            summary[status] += 1
        elif status:
            level = str(row.get("warning_level") or "").upper()
            if level not in {"OK", "INFO", ""}:
                summary["other"] += 1
        if bool(row.get("served_from_cache", False)):
            summary["cache_only"] += 1
        if bool(row.get("fallback_used", False)):
            summary["fallback_used"] += 1
        if not bool(row.get("fresh_fetch_success", False)) and str(row.get("error_message") or ""):
            summary["fetch_failed"] += 1
    return summary


# ---------------------------------------------------------------------------
# Note text and row-level helpers used by the report and the UI.
# ---------------------------------------------------------------------------


def data_quality_risk_note(summary: dict[str, int]) -> str:
    if summary.get("warning", 0) > 0 or summary.get("error", 0) > 0:
        return "本报告存在数据质量警告，使用前请先检查 Data Quality 区块。"
    return ""


def data_risk_flag(row: pd.Series) -> str:
    return explanation_text.data_risk_flag(row)


def _coerce_bool_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "0", "false", "no", "n", "off", "none", "null", "<na>"}:
            return False
        if text in {"1", "true", "yes", "y", "on"}:
            return True
    return bool(value)


def attach_data_health(features: pd.DataFrame, health: pd.DataFrame) -> pd.DataFrame:
    features = features.copy()
    features["code_key"] = features["code"].astype(str).str.zfill(6)
    if health.empty:
        features["data_warning_level"] = "OK"
        features["data_risk_flag"] = ""
        features["data_invalid"] = False
        features["data_grade"] = "SCORABLE"
        features["data_grade_label"] = "可基础评分"
        features["data_grade_reason"] = "当前报告缺少 Data Health 明细，按基础评分处理。"
        features["research_allowed"] = False
        features["backtest_allowed"] = False
        features["data_effective_source"] = ""
        features["data_latest_date"] = ""
        features["data_expected_latest_date"] = ""
        features["data_freshness_status"] = ""
        features["data_expected_reason"] = ""
        features["data_calendar_note"] = ""
        return features.drop(columns=["code_key"])

    health = health.copy()
    health["code_key"] = health["symbol"].astype(str).str.zfill(6)
    health["data_warning_level"] = health["warning_level"].fillna("OK").astype(str).str.upper()
    health["data_risk_flag"] = health.apply(data_risk_flag, axis=1)
    health["data_invalid"] = health["data_warning_level"].eq("ERROR")
    health = health.rename(
        columns={
            "effective_source": "data_effective_source",
            "latest_date": "data_latest_date",
            "expected_latest_date": "data_expected_latest_date",
            "data_freshness_status": "data_freshness_status",
            "expected_reason": "data_expected_reason",
            "calendar_note": "data_calendar_note",
        }
    )
    merge_columns = [
        "code_key",
        "data_warning_level",
        "data_grade",
        "data_grade_label",
        "data_risk_flag",
        "data_invalid",
        "data_grade",
        "data_grade_label",
        "data_grade_reason",
        "research_allowed",
        "backtest_allowed",
        "data_effective_source",
        "data_latest_date",
        "data_expected_latest_date",
        "data_freshness_status",
        "data_expected_reason",
        "data_calendar_note",
    ]
    for column in merge_columns:
        if column not in health.columns:
            health[column] = ""
    health = health[merge_columns].drop_duplicates("code_key", keep="last")
    existing_health_columns = [
        "data_warning_level",
        "data_grade",
        "data_grade_label",
        "data_risk_flag",
        "data_invalid",
        "data_grade",
        "data_grade_label",
        "data_grade_reason",
        "research_allowed",
        "backtest_allowed",
        "data_effective_source",
        "data_latest_date",
        "data_expected_latest_date",
        "data_freshness_status",
        "data_expected_reason",
        "data_calendar_note",
    ]
    features = features.drop(columns=[column for column in existing_health_columns if column in features.columns])
    merged = features.merge(health, on="code_key", how="left").drop(columns=["code_key"])
    merged["data_warning_level"] = merged["data_warning_level"].fillna("OK")
    merged["data_risk_flag"] = merged["data_risk_flag"].fillna("")
    merged["data_invalid"] = merged["data_invalid"].fillna(False).astype(bool)
    merged["data_grade"] = merged["data_grade"].fillna("SCORABLE")
    merged["data_grade_label"] = merged["data_grade_label"].fillna("可基础评分")
    merged["data_grade_reason"] = merged["data_grade_reason"].fillna("")
    merged["research_allowed"] = merged["research_allowed"].map(_coerce_bool_value).astype(bool)
    merged["backtest_allowed"] = merged["backtest_allowed"].map(_coerce_bool_value).astype(bool)
    merged["data_effective_source"] = merged["data_effective_source"].fillna("")
    merged["data_latest_date"] = merged["data_latest_date"].fillna("")
    merged["data_expected_latest_date"] = merged["data_expected_latest_date"].fillna("")
    merged["data_freshness_status"] = merged["data_freshness_status"].fillna("")
    merged["data_expected_reason"] = merged["data_expected_reason"].fillna("")
    merged["data_calendar_note"] = merged["data_calendar_note"].fillna("")
    return merged
