"""Quality audit for local 1-minute intraday CSV files.

The audit is read-only. It decides whether imported minute bars are usable for
paper execution, and it never creates market data or places orders.
"""

from __future__ import annotations

# Compatibility markers: audit_intraday_file 疑似误导入日线 price-only.
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.shared import normalize_symbol, now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"
INTRADAY_QUALITY_AUDIT_LATEST_JSON = RESULTS_DIR / "intraday_quality_audit_latest.json"
OHLC_COMPARISON_TOLERANCE = 1e-8

DISCLAIMER = "仅用于本地纸面模拟的数据质量检查，不接券商、不真实下单、不构成投资建议。"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def assess_complete_session(
    frame: pd.DataFrame,
    *,
    symbol: str,
    target_date: str,
    price_basis: str = "",
    price_only_intraday: bool = False,
    minimum_coverage_ratio: float = 0.95,
    max_missing_gap_minutes: int = 5,
) -> dict[str, Any]:
    """Assess whether one raw minute frame can support formal replay evidence."""

    reasons: list[str] = []
    hard_reasons: list[str] = []
    target = str(target_date or "")[:10]
    normalized_symbol = normalize_symbol(symbol)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {
            "status": "missing_session",
            "price_basis": "unknown",
            "price_basis_values": [],
            "unique_session_minutes": 0,
            "session_coverage_ratio": 0.0,
            "first_time": "",
            "last_time": "",
            "morning_present": False,
            "afternoon_present": False,
            "max_missing_gap_minutes": 0,
            "formal_execution_eligible": False,
            "reliable_no_fill_eligible": False,
            "reason_codes": ["missing_intraday"],
        }

    work = frame.copy()
    timestamps = pd.to_datetime(work.get("datetime"), errors="coerce")
    if timestamps.isna().any():
        hard_reasons.append("invalid_datetime")
    if not timestamps.dropna().is_monotonic_increasing:
        hard_reasons.append("datetime_not_monotonic")
    work["_timestamp"] = timestamps
    work = work.loc[work["_timestamp"].dt.strftime("%Y-%m-%d") == target].copy()
    if work.empty:
        hard_reasons.append("target_date_missing")
    if "symbol" in work.columns:
        symbols = {normalize_symbol(item) for item in work["symbol"].astype(str)}
        if symbols - {normalized_symbol}:
            hard_reasons.append("symbol_mismatch")

    time_values = work["_timestamp"].dt.strftime("%H:%M:%S") if not work.empty else pd.Series(dtype=str)
    in_morning = time_values.between("09:30:00", "11:30:00")
    in_afternoon = time_values.between("13:00:00", "15:00:00")
    session = work.loc[in_morning | in_afternoon].copy()
    unique_times = session["_timestamp"].dropna().drop_duplicates().sort_values()
    unique_count = int(len(unique_times))
    coverage = round(unique_count / 240.0, 6)
    first_time = unique_times.iloc[0].strftime("%H:%M:%S") if unique_count else ""
    last_time = unique_times.iloc[-1].strftime("%H:%M:%S") if unique_count else ""
    if unique_count:
        text_times = unique_times.dt.strftime("%H:%M:%S")
        morning_present = bool(text_times.between("09:30:00", "11:30:00").any())
        afternoon_present = bool(text_times.between("13:00:00", "15:00:00").any())
    else:
        morning_present = False
        afternoon_present = False

    if unique_count != len(session):
        reasons.append("duplicate_session_minute")
    if coverage < float(minimum_coverage_ratio):
        reasons.append("session_coverage_below_0_95")
    if not morning_present:
        reasons.append("morning_session_missing")
    if not afternoon_present:
        reasons.append("afternoon_session_missing")
    if first_time and first_time > "09:35:00":
        reasons.append("opening_window_missing")
    if last_time and last_time < "14:55:00":
        reasons.append("closing_window_missing")
    known_basis = {"raw", "qfq", "hfq"}
    explicit_basis = str(price_basis or "").strip().lower()
    row_basis_values = [
        str(item).strip().lower()
        for item in session.get("price_basis", pd.Series(dtype=str)).tolist()
    ]
    row_basis_values = [item if item in known_basis else "unknown" for item in row_basis_values]
    observed_basis_values = sorted(set(row_basis_values))
    observed_basis = (
        observed_basis_values[0]
        if len(observed_basis_values) == 1
        else "mixed_or_unknown"
        if observed_basis_values
        else "unknown"
    )
    if not session.empty and "price_basis" in session.columns:
        # Row evidence is authoritative: unknown/conflicting rows must not be hidden
        # by a frame-level declaration.
        effective_basis = observed_basis
        if explicit_basis in known_basis and explicit_basis != observed_basis:
            effective_basis = "mixed_or_unknown"
    elif not session.empty and explicit_basis in known_basis:
        # Compatibility for direct callers whose frame predates the row-level column.
        effective_basis = explicit_basis
    else:
        effective_basis = observed_basis
    if effective_basis != "raw":
        hard_reasons.append("price_basis_not_raw")

    required = ["open", "high", "low", "close", "volume"]
    if set(required) - set(session.columns):
        hard_reasons.append("required_ohlcv_missing")
    elif not session.empty:
        numeric = session[required].apply(pd.to_numeric, errors="coerce")
        if numeric.isna().any().any():
            hard_reasons.append("invalid_ohlcv_numeric")
        else:
            if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
                hard_reasons.append("non_positive_price")
            if (numeric["volume"] < 0).any():
                hard_reasons.append("negative_volume")
            if (
                (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1)).any()
                or (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1)).any()
            ):
                hard_reasons.append("invalid_ohlc_envelope")
    if bool(price_only_intraday):
        hard_reasons.append("price_only_intraday")

    largest_missing_gap = 0
    if unique_count:
        text_times = unique_times.dt.strftime("%H:%M:%S")
        for mask in (
            text_times.between("09:30:00", "11:30:00"),
            text_times.between("13:00:00", "15:00:00"),
        ):
            segment = unique_times.loc[mask]
            if len(segment) > 1:
                diff_minutes = float(segment.diff().dt.total_seconds().div(60).max())
                gap = int(max(0.0, diff_minutes - 1.0))
                largest_missing_gap = max(largest_missing_gap, gap)
    if largest_missing_gap > int(max_missing_gap_minutes):
        reasons.append("unexplained_gap_over_5_minutes")

    reason_codes = list(dict.fromkeys(hard_reasons + reasons))
    status = "invalid_session" if hard_reasons else ("partial_session" if reasons else "complete_session")
    eligible = status == "complete_session"
    return {
        "status": status,
        "price_basis": effective_basis,
        "price_basis_values": observed_basis_values,
        "unique_session_minutes": unique_count,
        "session_coverage_ratio": coverage,
        "first_time": first_time,
        "last_time": last_time,
        "morning_present": morning_present,
        "afternoon_present": afternoon_present,
        "max_missing_gap_minutes": largest_missing_gap,
        "formal_execution_eligible": eligible,
        "reliable_no_fill_eligible": eligible,
        "reason_codes": reason_codes,
    }


def _audit_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    target_date: str,
    payload: dict[str, Any],
    price_basis: str = "",
) -> dict[str, Any]:
    warnings = list(payload.get("warnings") or [])
    errors = list(payload.get("errors") or [])
    status = "usable"
    can_execute = True
    reliable = True

    if frame.empty:
        return {
            "status": "missing",
            "row_count": 0,
            "first_time": "",
            "last_time": "",
            "can_execute_condition_plan": False,
            "is_reliable": False,
            "warnings": warnings,
            "errors": errors or ["文件存在，但没有目标日期分钟线。"],
            "next_action": "请导入目标日期的真实 1 分钟线 CSV。",
        }

    row_count = int(len(frame))
    times = pd.to_datetime(frame["datetime"], errors="coerce")
    if times.isna().any():
        errors.append("datetime 无法解析。")
    if not times.is_monotonic_increasing:
        warnings.append("datetime 不是升序；导入器会在写入时排序。")
    duplicate_count = int(frame.duplicated(subset=["symbol", "datetime"]).sum())
    if duplicate_count:
        warnings.append(f"存在 {duplicate_count} 条重复 datetime；导入器会保留最后一条。")
    if set(["open", "high", "low", "close"]) - set(frame.columns):
        errors.append("缺少 open/high/low/close 字段。")
    else:
        for col in ("open", "high", "low", "close"):
            values = pd.to_numeric(frame[col], errors="coerce")
            if values.isna().any():
                errors.append(f"{col} 存在非数字值。")
            if (values <= 0).any():
                errors.append(f"{col} 存在 0 或负数。")
        if not errors:
            high = pd.to_numeric(frame["high"], errors="coerce")
            low = pd.to_numeric(frame["low"], errors="coerce")
            open_ = pd.to_numeric(frame["open"], errors="coerce")
            close = pd.to_numeric(frame["close"], errors="coerce")
            tolerance = OHLC_COMPARISON_TOLERANCE
            if (((high + tolerance) < open_) | ((high + tolerance) < close) | ((high + tolerance) < low)).any():
                errors.append("high 小于 open/close/low，OHLC 异常。")
            if (((low - tolerance) > open_) | ((low - tolerance) > close) | ((low - tolerance) > high)).any():
                errors.append("low 大于 open/close/high，OHLC 异常。")
    if "volume" not in frame.columns or frame["volume"].isna().all():
        warnings.append("缺少 volume；不阻断价格触发，但成交量分析不可用。")
    if "amount" not in frame.columns or frame["amount"].isna().all():
        warnings.append("缺少 amount；不阻断价格触发，但成交额分析不可用。")
    # Provenance is authoritative. Equal OHLC values alone are also the valid
    # shape of a one-price limit session and must never imply synthesis.
    price_only = bool(payload.get("price_only_intraday"))
    if price_only:
        warnings.append("price-only 分钟线：已用 price 临时填充 OHLC，可靠性低于完整 OHLC。")
    first_time = str(frame["time"].iloc[0]) if "time" in frame.columns else ""
    last_time = str(frame["time"].iloc[-1]) if "time" in frame.columns else ""
    if row_count == 1 and (first_time in {"00:00:00", "", "00:00"}):
        errors.append("疑似误导入日线：目标日期只有 1 行且时间不是分钟级。")
    if first_time and first_time > "09:45:00":
        warnings.append("未覆盖开盘附近分钟线。")
    if last_time and last_time < "14:45:00":
        warnings.append("未覆盖收盘附近分钟线。")
    symbols = {normalize_symbol(item) for item in frame.get("symbol", pd.Series(dtype=str)).astype(str).tolist()}
    if normalize_symbol(symbol) not in symbols:
        errors.append("文件内 symbol 与目标股票不匹配。")
    dates = set(frame.get("date", pd.Series(dtype=str)).astype(str).str[:10].tolist())
    if target_date and target_date[:10] not in dates:
        errors.append("文件不包含目标日期。")

    if errors:
        status = "invalid"
        can_execute = False
        reliable = False
    elif row_count >= 180:
        status = "usable"
        can_execute = True
        reliable = not price_only
    elif row_count >= 30:
        status = "partial"
        can_execute = True
        reliable = False
        warnings.append("分钟线行数不足 180，仅可观察，执行结果需标记不可靠。")
    else:
        status = "insufficient"
        can_execute = False
        reliable = False
        warnings.append("分钟线行数少于 30，不建议执行条件计划。")

    formal_frame = frame
    if (
        str(price_basis or "").strip().lower() in {"raw", "qfq", "hfq"}
        and payload.get("original_price_basis_column_present") is False
        and "price_basis" in frame.columns
    ):
        formal_frame = frame.drop(columns=["price_basis"])
    formal = assess_complete_session(
        formal_frame,
        symbol=symbol,
        target_date=target_date,
        price_basis=price_basis,
        price_only_intraday=price_only,
    )
    if formal["formal_execution_eligible"] is not True:
        can_execute = False
        reliable = False
    next_action = "可执行条件交易计划。" if can_execute else "请重新导入目标日期的真实 1 分钟线 CSV。"
    if status == "partial":
        next_action = "仅可观察；当前分钟会话不满足正式执行证据门。"
    return {
        "status": status,
        "row_count": row_count,
        "first_time": first_time,
        "last_time": last_time,
        "can_execute_condition_plan": can_execute,
        "is_reliable": reliable,
        "warnings": warnings,
        "errors": errors,
        "next_action": next_action,
        "session_status": formal["status"],
        "price_basis": formal["price_basis"],
        "complete_session": formal["status"] == "complete_session",
        "unique_session_minutes": formal["unique_session_minutes"],
        "session_coverage_ratio": formal["session_coverage_ratio"],
        "morning_present": formal["morning_present"],
        "afternoon_present": formal["afternoon_present"],
        "max_missing_gap_minutes": formal["max_missing_gap_minutes"],
        "formal_execution_eligible": formal["formal_execution_eligible"],
        "reliable_no_fill_eligible": formal["reliable_no_fill_eligible"],
        "session_reason_codes": formal["reason_codes"],
    }


def audit_intraday_file(
    symbol: str,
    target_date: str,
    *,
    intraday_dir: Path | None = None,
    encoding: str = "auto",
    price_basis: str = "",
) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    from quantdesk.intraday.minute_storage import resolve_minute_read_path

    requested_path = Path(intraday_dir or INTRADAY_1M_DIR) / f"{symbol}.csv"
    path = resolve_minute_read_path(requested_path)
    if not path.exists():
        return {
            "symbol": symbol,
            "target_date": target_date[:10],
            "path": _display_path(path),
            "exists": False,
            "status": "missing",
            "row_count": 0,
            "first_time": "",
            "last_time": "",
            "can_execute_condition_plan": False,
            "is_reliable": False,
            "warnings": [],
            "errors": ["文件不存在。"],
            "next_action": "请尝试 provider，或导入目标日期的 1 分钟线 CSV。",
            "disclaimer": DISCLAIMER,
        }
    payload = read_intraday_csv(requested_path, default_symbol=symbol, target_date=target_date, encoding=encoding)
    frame = payload.get("frame")
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame()
    audit = _audit_frame(
        frame,
        symbol=symbol,
        target_date=target_date,
        payload=payload,
        price_basis=price_basis,
    )
    return {
        "symbol": symbol,
        "target_date": target_date[:10],
        "path": _display_path(Path(payload.get("path") or path)),
        "exists": True,
        "encoding": payload.get("encoding") or "",
        "price_only_intraday": bool(payload.get("price_only_intraday")),
        **audit,
        "disclaimer": DISCLAIMER,
    }


def audit_intraday_batch(
    symbols: list[str],
    target_date: str,
    *,
    intraday_dir: Path | None = None,
    output_path: Path | None = None,
    write: bool = True,
    encoding: str = "auto",
    price_basis: str = "unknown",
    price_basis_by_symbol: Mapping[str, str] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    output_path = Path(output_path or INTRADAY_QUALITY_AUDIT_LATEST_JSON)
    basis_by_symbol = {
        normalize_symbol(symbol): str(basis or "unknown").strip().lower()
        for symbol, basis in dict(price_basis_by_symbol or {}).items()
        if normalize_symbol(symbol)
    }
    items = [
        audit_intraday_file(
            symbol,
            target_date,
            intraday_dir=intraday_dir,
            encoding=encoding,
            price_basis=basis_by_symbol.get(symbol, price_basis),
        )
        for symbol in [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
    ]
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "intraday_quality_audit",
        "generated_at": now_text(),
        "run_id": str(run_id or ""),
        "target_date": target_date[:10],
        "total_symbols": len(items),
        "summary": {
            "usable": counts.get("usable", 0),
            "complete_session": sum(bool(item.get("complete_session")) for item in items),
            "formal_execution_eligible": sum(bool(item.get("formal_execution_eligible")) for item in items),
            "partial_session": sum(item.get("session_status") == "partial_session" for item in items),
            "invalid_session": sum(item.get("session_status") == "invalid_session" for item in items),
            "partial": counts.get("partial", 0),
            "insufficient": counts.get("insufficient", 0),
            "invalid": counts.get("invalid", 0),
            "missing": counts.get("missing", 0),
        },
        "items": items,
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_intraday_quality_audit(path: Path | None = None) -> dict[str, Any]:
    from quantdesk.shared import read_json

    path = Path(path or INTRADAY_QUALITY_AUDIT_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {"ok": True, "exists": False, "path": _display_path(path), "message": "尚未生成分钟线质量审计。", "disclaimer": DISCLAIMER}
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload
