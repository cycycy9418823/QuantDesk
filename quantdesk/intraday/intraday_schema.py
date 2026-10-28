"""Standard schema helpers for local A-share 1-minute intraday bars.

This module only normalizes user-provided local CSV data. It does not download
market data and it never places real orders.
"""

from __future__ import annotations

# Compatibility markers: normalize_intraday_frame 代码 gb18030 order_book_id 股票代码 price_only_intraday.
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.minute_storage import resolve_minute_read_path
from quantdesk.intraday.minute_bar_lifecycle import LIFECYCLE_COLUMNS, select_bar_observations
from quantdesk.shared import normalize_symbol, now_text, safe_float

STANDARD_COLUMNS = [
    "symbol",
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "price_basis",
    "source",
    "updated_at",
]

REQUIRED_PRICE_COLUMNS = ["open", "high", "low", "close"]

COLUMN_ALIASES = {
    "symbol": "symbol",
    "ticker": "symbol",
    "code": "symbol",
    "代码": "symbol",
    "证券代码": "symbol",
    "datetime": "datetime",
    "date_time": "datetime",
    "trade_time": "datetime",
    "timestamp": "datetime",
    "minute": "datetime",
    "时间": "datetime",
    "日期时间": "datetime",
    "成交时间": "datetime",
    "date": "date",
    "trade_date": "date",
    "日期": "date",
    "time": "time",
    "时刻": "time",
    "open": "open",
    "开盘": "open",
    "open_price": "open",
    "high": "high",
    "最高": "high",
    "high_price": "high",
    "low": "low",
    "最低": "low",
    "low_price": "low",
    "close": "close",
    "收盘": "close",
    "close_price": "close",
    "price": "price",
    "价格": "price",
    "volume": "volume",
    "vol": "volume",
    "成交量": "volume",
    "amount": "amount",
    "turnover": "amount",
    "成交额": "amount",
    "price_basis": "price_basis",
    "adjust": "price_basis",
    "adjustment": "price_basis",
    "复权": "price_basis",
    "source": "source",
    "来源": "source",
    "updated_at": "updated_at",
    "更新时间": "updated_at",
}

COLUMN_ALIASES.update(
    {
        # Common Chinese exports.
        "代码": "symbol",
        "股票代码": "symbol",
        "证券代码": "symbol",
        "名称": "name",
        "股票名称": "name",
        "交易时间": "datetime",
        "日期": "date",
        "时间": "time",
        "时刻": "time",
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "最新价": "price",
        "价格": "price",
        # JQ / GM / RQ style exports.
        "order_book_id": "symbol",
        "orderbook_id": "symbol",
        "eob": "datetime",
        "bar_time": "datetime",
        "money": "amount",
        # Sina / Tencent minute exports use "day" for the bar timestamp.
        "day": "datetime",
    }
)

SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030")


def normalize_intraday_columns(columns: list[Any]) -> dict[Any, str]:
    mapping: dict[Any, str] = {}
    used: set[str] = set()
    for column in columns:
        text = str(column).strip()
        key = text.lower()
        target = COLUMN_ALIASES.get(text) or COLUMN_ALIASES.get(key) or key
        if target not in used:
            mapping[column] = target
            used.add(target)
    return mapping


def resolve_source_datetime_series(frame: pd.DataFrame | None) -> pd.Series | None:
    """Return the raw datetime-like series a provider frame would normalize on.

    Uses the SAME column aliases as normalize_intraday_frame (datetime/day/时间/
    交易时间/成交时间/eob/bar_time ... plus a date+time pair) so source-order
    checks see exactly the column normalization will key on. Returns None when no
    time information can be resolved (caller must treat order as unknown, never
    silently assume forward order).
    """

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    mapping = normalize_intraday_columns(list(frame.columns))
    renamed = frame.rename(columns=mapping)
    if "datetime" in renamed.columns:
        return renamed["datetime"]
    if "date" in renamed.columns and "time" in renamed.columns:
        combined = (
            renamed["date"].map(_normalize_date_text)
            + " "
            + renamed["time"].map(_normalize_time_text)
        )
        return combined
    if "time" in renamed.columns:
        return renamed["time"]
    if "date" in renamed.columns:
        return renamed["date"]
    return None


def source_time_order_state(frame: pd.DataFrame | None) -> bool | None:
    """Tri-state original-order check on a raw provider frame.

    Returns True when the resolved source datetime series is monotonically
    non-decreasing, False when it is out of order, and None when the order cannot
    be determined (no resolvable time column, or nothing parseable). None means
    "unknown" — the caller must NOT treat it as valid and must fail closed for
    formal signals.
    """

    series = resolve_source_datetime_series(frame)
    if series is None:
        return None
    parsed = pd.to_datetime(series, errors="coerce")
    ordered = parsed.dropna().tolist()
    if len(ordered) == 0:
        return None
    if len(ordered) < 2:
        return True
    return all(ordered[i] <= ordered[i + 1] for i in range(len(ordered) - 1))


def _as_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def _normalize_price_basis_text(value: Any) -> str:
    text = _as_text(value).strip().lower()
    if text in {"raw", "qfq", "hfq"}:
        return text
    return "unknown"


def _normalize_date_text(value: Any) -> str:
    text = _as_text(value).replace("/", "-").replace(".", "-")
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and ("-" not in text or len(text) <= 8):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def _normalize_time_text(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:00"
        if len(parts) >= 3:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2][:2].zfill(2)}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) == 4:
        return f"{digits[:2]}:{digits[2:4]}:00"
    if len(digits) >= 6:
        return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    return text


def _normalize_datetime_text(value: Any, *, target_date: str = "") -> str:
    text = _as_text(value).replace("/", "-")
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14 and ("-" not in text or " " not in text):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    if len(digits) == 12 and ("-" not in text or " " not in text):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:00"
    if len(digits) in {3, 4, 5, 6} and target_date:
        return f"{target_date[:10]} {_normalize_time_text(text)}"
    return text


def _series_looks_like_datetime(values: Any) -> bool:
    try:
        sample = [str(item).strip() for item in list(values.dropna().head(5)) if str(item).strip()]
    except Exception:
        return False
    for text in sample:
        digits = "".join(ch for ch in text if ch.isdigit())
        if "-" in text or "/" in text or len(digits) >= 8:
            return True
    return False


def normalize_intraday_frame(
    frame: pd.DataFrame | None,
    *,
    default_symbol: str = "",
    source: str = "manual_import",
    target_date: str = "",
    price_basis: str = "",
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if frame is None or not isinstance(frame, pd.DataFrame):
        return {"ok": False, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": warnings, "errors": ["输入不是表格数据。"]}
    if frame.empty:
        return {"ok": True, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": ["输入文件没有分钟线记录。"], "errors": []}

    df = frame.rename(columns=normalize_intraday_columns(list(frame.columns))).copy()
    try:
        if not df.attrs.get("lifecycle_materialized"):
            df = select_bar_observations(df)
    except ValueError as exc:
        return {"ok": False, "frame": pd.DataFrame(columns=STANDARD_COLUMNS),
                "warnings": warnings, "errors": [str(exc)]}
    if df.empty:
        return {"ok": True, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": warnings, "errors": []}
    if "symbol" not in df.columns:
        df["symbol"] = default_symbol
    df["symbol"] = df["symbol"].map(normalize_symbol)
    if default_symbol:
        df.loc[df["symbol"].astype(str).str.len() == 0, "symbol"] = normalize_symbol(default_symbol)
    elif df["symbol"].astype(str).str.len().eq(0).all():
        errors.append("缺少 symbol/代码 字段；如 CSV 没有 symbol，请在 CLI/API 中显式指定 --symbol。")

    if "datetime" not in df.columns:
        if "date" in df.columns and "time" in df.columns:
            df["datetime"] = df["date"].map(_normalize_date_text) + " " + df["time"].map(_normalize_time_text)
        elif "time" in df.columns and _series_looks_like_datetime(df["time"]):
            df["datetime"] = df["time"].map(lambda value: _normalize_datetime_text(value, target_date=target_date))
        elif "time" in df.columns and target_date:
            df["datetime"] = target_date[:10] + " " + df["time"].map(_normalize_time_text)
        elif "date" in df.columns:
            df["datetime"] = df["date"].map(_normalize_date_text)
        else:
            errors.append("缺少 datetime/date/time 字段，无法识别分钟线时间。")

    if "datetime" in df.columns:
        df["datetime"] = df["datetime"].map(lambda value: _normalize_datetime_text(value, target_date=target_date))

    price_only = False
    if "price_only_intraday" in df.columns:
        provenance_values = {
            str(value).strip().lower()
            for value in df["price_only_intraday"].dropna().tolist()
            if str(value).strip()
        }
        true_values = {"1", "true", "yes", "y", "on"}
        false_values = {"0", "false", "no", "n", "off"}
        unknown_values = provenance_values - true_values - false_values
        if provenance_values & true_values:
            price_only = True
        if unknown_values:
            # Persisted provenance is a fail-close signal.  An unrecognised
            # value must never be silently treated as explicit full OHLCV.
            price_only = True
            warnings.append(
                "price_only_intraday provenance contains unknown values: "
                + ",".join(sorted(unknown_values))
            )
        if (provenance_values & true_values) and (
            provenance_values & false_values
        ):
            warnings.append("price_only_intraday provenance is mixed")
    if "price" in df.columns and not any(col in df.columns for col in REQUIRED_PRICE_COLUMNS):
        for col in REQUIRED_PRICE_COLUMNS:
            df[col] = df["price"]
        price_only = True
        warnings.append("输入只有 price 字段，已临时填充 open/high/low/close；该分钟线成交可靠性需谨慎。")
    if "close" in df.columns:
        missing_ohlc = [col for col in ("open", "high", "low") if col not in df.columns]
        if missing_ohlc:
            # Provenance, not value shape, defines price-only data. A real
            # one-price limit session may legitimately have O=H=L=C in every
            # minute; only fields synthesized here are marked price-only.
            price_only = True
        for col in ("open", "high", "low"):
            if col not in df.columns:
                df[col] = df["close"]
                warnings.append(f"缺少字段：{col}，已用 close 临时填充以兼容只含分钟 close 的回放数据。")

    for col in REQUIRED_PRICE_COLUMNS:
        if col not in df.columns:
            errors.append(f"缺少字段：{col}")
            df[col] = pd.NA
    for col in ("volume", "amount"):
        if col not in df.columns:
            df[col] = pd.NA
            warnings.append(f"缺少可选字段：{col}")
    explicit_basis = _normalize_price_basis_text(price_basis)
    if "price_basis" not in df.columns:
        df["price_basis"] = explicit_basis
    else:
        df["price_basis"] = df["price_basis"].map(_normalize_price_basis_text)
        if explicit_basis != "unknown":
            df.loc[df["price_basis"] == "unknown", "price_basis"] = explicit_basis
    if "source" not in df.columns:
        df["source"] = source
    if "updated_at" not in df.columns:
        df["updated_at"] = now_text()

    df["_dt"] = pd.to_datetime(df.get("datetime"), errors="coerce")
    invalid_time = int(df["_dt"].isna().sum())
    if invalid_time:
        warnings.append(f"{invalid_time} 行 datetime 无法解析，已跳过。")
    df = df.dropna(subset=["_dt"]).copy()

    for col in REQUIRED_PRICE_COLUMNS + ["volume", "amount"]:
        if col in df.columns:
            if "evidence_hash" in df.columns:
                # Python's float conversion matches observation_hash; pandas'
                # string parser can round the same decimal differently.
                def evidence_number(value: Any) -> float:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return float("nan")
                df[col] = df[col].map(evidence_number)
            else:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=REQUIRED_PRICE_COLUMNS).copy()
    if df.empty:
        return {"ok": False, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": warnings, "errors": errors or ["没有可用的分钟线价格记录。"], "price_only_intraday": price_only}

    df["date"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df["time"] = df["_dt"].dt.strftime("%H:%M:%S")
    df["datetime"] = df["_dt"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df = df.sort_values(["symbol", "_dt"]).drop_duplicates(subset=["symbol", "datetime"], keep="last")
    source_row_count = int(len(df))
    latest_available_dt = df["_dt"].max() if source_row_count else None
    latest_available_datetime = latest_available_dt.strftime("%Y-%m-%d %H:%M:%S") if latest_available_dt is not None else ""
    latest_available_date = latest_available_dt.strftime("%Y-%m-%d") if latest_available_dt is not None else ""
    if target_date:
        df = df[df["date"] == target_date[:10]].copy()
    for col in STANDARD_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[STANDARD_COLUMNS + [col for col in LIFECYCLE_COLUMNS if col in df.columns]].reset_index(drop=True)
    basis_values = sorted(
        {
            _normalize_price_basis_text(item)
            for item in df.get("price_basis", pd.Series(dtype=str)).tolist()
        }
    )
    observed_basis = (
        basis_values[0]
        if len(basis_values) == 1
        else "mixed_or_unknown"
        if basis_values
        else "unknown"
    )
    return {
        "ok": not errors,
        "frame": df,
        "warnings": warnings,
        "errors": errors,
        "price_only_intraday": price_only,
        "price_basis": observed_basis,
        "price_basis_values": basis_values,
        "row_count": int(len(df)),
        "source_row_count": source_row_count,
        "latest_available_datetime": latest_available_datetime,
        "latest_available_date": latest_available_date,
    }


def read_intraday_csv(
    path: Path,
    *,
    default_symbol: str = "",
    target_date: str = "",
    encoding: str = "auto",
    as_of: str = "",
) -> dict[str, Any]:
    requested_path = Path(path)
    path = resolve_minute_read_path(requested_path)
    if not path.exists():
        return {"ok": False, "exists": False, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": [], "errors": ["文件不存在。"], "path": str(path)}
    encodings = SUPPORTED_ENCODINGS if not encoding or encoding == "auto" else (encoding,)
    raw = None
    used_encoding = ""
    read_errors: list[str] = []
    read_warnings: list[str] = []
    for enc in encodings:
        # Validate the recorded evidence before numeric inference can round a
        # price or strip leading zeroes from a symbol. Normalization follows.
        frame = read_csv_frame(path, encoding=enc, dtype=str, keep_default_na=False)
        warnings = list(frame.attrs.get("warnings") or [])
        if any(item.startswith("csv_read_failed:") for item in warnings):
            read_errors.extend(warnings)
            continue
        raw = frame
        read_warnings = warnings
        used_encoding = enc
        break
    if raw is None:
        return {"ok": False, "exists": True, "frame": pd.DataFrame(columns=STANDARD_COLUMNS), "warnings": [], "errors": [f"CSV 不可读：{'; '.join(read_errors)}"], "path": str(path)}
    input_paths = [path]
    if "evidence_hash" in raw.columns:
        try:
            raw = select_bar_observations(raw, as_of=as_of)
        except ValueError as exc:
            return {"ok": False, "exists": True, "frame": pd.DataFrame(columns=STANDARD_COLUMNS),
                    "warnings": [], "errors": [str(exc)], "path": str(path)}
        # Preserve known raw historical observations. Unknown-basis legacy
        # evidence stays separate; it cannot acquire raw from the new segment.
        from quantdesk.intraday.minute_bar_lifecycle import LIFECYCLE_DIR
        if path.parent.name == LIFECYCLE_DIR and not as_of:
            candidates = list(dict.fromkeys([requested_path, path.parent.parent / path.name]))
            older = []
            for candidate in candidates:
                if candidate == path or not candidate.is_file():
                    continue
                old_raw = read_csv_frame(candidate, encoding=used_encoding)
                if ("price_basis" not in old_raw.columns or
                        set(old_raw["price_basis"].astype(str).str.strip().str.lower()) != {"raw"}):
                    read_warnings.append(f"historical_segment_basis_unverified:{candidate.name}")
                    continue
                normalized_old = normalize_intraday_frame(old_raw, default_symbol=default_symbol)
                if not normalized_old.get("ok"):
                    read_warnings.append(f"historical_segment_invalid:{candidate.name}")
                    continue
                older.append(normalized_old["frame"])
                input_paths.append(candidate)
            if older:
                raw = pd.concat([*older, raw], ignore_index=True).drop_duplicates(["symbol", "datetime"], keep="last")
                raw.attrs["lifecycle_materialized"] = True
                read_warnings.append("historical_raw_observations_retained_with_unverified_finality")
    if as_of:
        try:
            if not raw.attrs.get("lifecycle_materialized"):
                raw = select_bar_observations(raw, as_of=as_of)
            # It is now a materialized as-of view, not a full revision log.
            # Keep metadata but avoid validating a partial chain a second time.
            raw.attrs["lifecycle_materialized"] = True
        except ValueError as exc:
            return {"ok": False, "exists": True, "frame": pd.DataFrame(columns=STANDARD_COLUMNS),
                    "warnings": [], "errors": [str(exc)], "path": str(path)}
    normalized_input_columns = set(
        normalize_intraday_columns(list(raw.columns)).values()
    )
    payload = normalize_intraday_frame(raw, default_symbol=default_symbol, target_date=target_date)
    payload["original_price_basis_column_present"] = (
        "price_basis" in normalized_input_columns
    )
    payload["warnings"] = read_warnings + list(payload.get("warnings") or [])
    if path != requested_path:
        payload["requested_path"] = str(requested_path)
        if path.parent.name == "lifecycle_v1":
            payload["warnings"].append(f"minute_lifecycle_segment_selected:{requested_path.name}")
        else:
            payload["warnings"].append(f"legacy_price_basis_unknown:{requested_path.name}; legacy rows not merged")
    payload["exists"] = True
    payload["path"] = str(path)
    payload["encoding"] = used_encoding
    payload["input_paths"] = [str(item) for item in input_paths]
    import hashlib
    payload["input_hashes"] = {str(item): hashlib.sha256(item.read_bytes()).hexdigest() for item in input_paths}
    return payload


def validate_intraday_payload(payload: dict[str, Any], *, target_date: str = "") -> dict[str, Any]:
    frame = payload.get("frame")
    warnings = list(payload.get("warnings") or [])
    errors = list(payload.get("errors") or [])
    if errors:
        status = "invalid_schema"
    elif not isinstance(frame, pd.DataFrame) or frame.empty:
        status = "stale_intraday" if target_date else "missing_intraday"
    elif len(frame) < 2:
        status = "insufficient_intraday"
        warnings.append("目标日期分钟线行数过少，不能代表完整盘中回放。")
    else:
        status = "available"
    latest_datetime = ""
    target_rows = 0
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        target_rows = int(len(frame))
        latest_datetime = str(frame["datetime"].iloc[-1])
    latest_available_datetime = str(payload.get("latest_available_datetime") or latest_datetime)
    latest_available_date = str(payload.get("latest_available_date") or latest_available_datetime[:10])
    return {
        "ok": status == "available",
        "status": status,
        "target_date_rows": target_rows,
        "latest_datetime": latest_datetime,
        "latest_available_datetime": latest_available_datetime,
        "latest_available_date": latest_available_date,
        "source_row_count": int(payload.get("source_row_count") or target_rows or 0),
        "warnings": warnings,
        "errors": errors,
        "price_only_intraday": bool(payload.get("price_only_intraday")),
    }


def intraday_status_for_file(path: Path, *, symbol: str, target_date: str = "") -> dict[str, Any]:
    payload = read_intraday_csv(path, default_symbol=symbol, target_date=target_date)
    if not payload.get("exists"):
        return {
            "ok": False,
            "status": "missing_intraday",
            "target_date_rows": 0,
            "latest_datetime": "",
            "warnings": [],
            "errors": payload.get("errors") or ["文件不存在。"],
            "price_only_intraday": False,
            "symbol": normalize_symbol(symbol),
            "path": str(path),
            "exists": False,
        }
    status = validate_intraday_payload(payload, target_date=target_date)
    return {
        **status,
        "symbol": normalize_symbol(symbol),
        "path": payload.get("path") or str(path),
        "exists": bool(payload.get("exists")),
    }


def frame_latest_close(frame: pd.DataFrame) -> float | None:
    if frame is None or frame.empty or "close" not in frame.columns:
        return None
    return safe_float(frame["close"].iloc[-1], None)


# --- Intraday minute quality chain (offline, research-only) ------------------
#
# The helpers below are pure functions: they never open a socket, never write
# market data, and never place orders. They turn a normalized minute frame into
# a uniform quality-gate contract so downstream research code can tell full
# minute OHLCV apart from a price-only trend fallback.

# A-share continuous trading session (local time). Sina/AKShare minute feeds may
# emit a leading 09:30 or 09:31 bar and a trailing 15:00 close-auction bar; both
# are in-session. Anything before 09:30, inside the 11:30-13:00 lunch break, or
# after 15:00 is out of session and must be filtered.
_MORNING_START = "09:30"
_MORNING_END = "11:30"
_AFTERNOON_START = "13:00"
_AFTERNOON_END = "15:00"


def _hhmm(value: Any) -> str:
    text = str(value or "")
    # datetime like "2026-05-15 09:30:00" -> "09:30"; time like "09:30:00" -> "09:30"
    if " " in text:
        text = text.split(" ", 1)[1]
    return text[:5]


def _in_trading_session(hhmm: str) -> bool:
    if not hhmm or ":" not in hhmm:
        return False
    if _MORNING_START <= hhmm <= _MORNING_END:
        return True
    if _AFTERNOON_START <= hhmm <= _AFTERNOON_END:
        return True
    return False


def filter_intraday_trading_session(
    frame: pd.DataFrame | None,
    *,
    target_date: str = "",
) -> tuple[pd.DataFrame, int]:
    """Drop out-of-session minute rows and report how many were filtered.

    Keeps 09:30-11:30 and 13:00-15:00 inclusive (so the 15:00 close-auction bar
    and a leading 09:30/09:31 bar survive). The common 14:57 -> 15:00 auction
    jump is *not* an out-of-session row, so it is never counted as filtered.
    """

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty or "datetime" not in frame.columns:
        return (frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()), 0
    df = frame.copy()
    hhmm = df["datetime"].map(_hhmm)
    in_session = hhmm.map(_in_trading_session)
    if target_date:
        same_day = df["datetime"].map(lambda v: str(v)[:10] == target_date[:10])
        in_session = in_session & same_day
    filtered_count = int((~in_session).sum())
    kept = df[in_session].reset_index(drop=True)
    return kept, filtered_count


def _count_valid_ohlcv_rows(df: pd.DataFrame) -> tuple[int, list[str]]:
    """Count rows with a fully valid OHLCV tuple and collect quality warnings.

    A row is valid only when open/high/low/close/volume are all present and
    numeric, every price is positive, the high/low envelope is consistent
    (high >= low, high >= max(open, close), low <= min(open, close)), and volume
    is a non-negative number (0 is allowed for a no-trade minute; NaN or negative
    is not). Anything else is not a real minute OHLCV bar and must not be counted.
    """

    warnings: list[str] = []
    required = ("open", "high", "low", "close", "volume")
    missing_cols = [col for col in required if col not in df.columns]
    if missing_cols:
        warnings.append(f"缺少 OHLCV 字段：{missing_cols}，不能标记为完整分钟 OHLCV。")
        return 0, warnings

    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    v = pd.to_numeric(df["volume"], errors="coerce")

    present = o.notna() & h.notna() & lo.notna() & c.notna()
    positive = (o > 0) & (h > 0) & (lo > 0) & (c > 0)
    envelope = (h >= lo) & (h >= o) & (h >= c) & (lo <= o) & (lo <= c)
    volume_ok = v.notna() & (v >= 0)

    price_present_rows = int(present.sum())
    if price_present_rows == 0:
        warnings.append("open/high/low/close 全为空，无有效分钟 OHLCV。")
    if int((present & ~positive).sum()) > 0:
        warnings.append("存在非正价格（<=0）的分钟行，判定为无效 OHLCV。")
    if int((present & positive & ~envelope).sum()) > 0:
        warnings.append("存在 high/low 关系非法的分钟行（high<low 或未包住 open/close），判定为无效 OHLCV。")
    if int((present & ~volume_ok).sum()) > 0 or bool(v.notna().sum() == 0):
        warnings.append("volume 为空/负数/非数值，判定为无效 OHLCV，不能标记为完整分钟数据。")

    valid = present & positive & envelope & volume_ok
    return int(valid.sum()), warnings


# Sentinel: caller did not pass source_time_order_valid at all (infer from frame).
# This is distinct from an explicit None, which means "order cannot be determined"
# and must fail closed.
_SOURCE_ORDER_UNSET = object()


def build_intraday_quality_gate(
    frame: pd.DataFrame | None,
    *,
    provider: str,
    target_date: str = "",
    price_only: bool = False,
    source: str = "",
    session_filtered_count: int = 0,
    assume_sorted: bool = False,
    source_time_order_valid: Any = _SOURCE_ORDER_UNSET,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return the uniform intraday quality-gate contract for a provider frame.

    Full Sina OHLCV -> minute-K and volume research allowed. Tencent price-only
    -> price-trend observation only. Reverse time order, target-date mismatch,
    invalid OHLCV values (missing/NaN prices, non-positive prices, illegal
    high/low envelope, missing/negative volume), or fewer than two valid rows all
    make the result signal-ineligible and ``ohlcv_complete=False``. Nothing is
    fabricated: a missing frame fails closed, a missing amount only warns.

    ``source_time_order_valid`` lets a caller that already sorted the frame report
    the *original* provider order, so a real provider that streamed rows in
    reverse is still caught even though the normalized frame is now sorted.
    """

    warnings: list[str] = list(extra_warnings or [])
    source = source or provider
    gate: dict[str, Any] = {
        "provider": provider,
        "source": source,
        "status": "failed",
        "target_date": target_date[:10],
        "rows": 0,
        "latest_datetime": "",
        "supports_target_date": False,
        "data_quality": "missing",
        "ohlcv_complete": False,
        "price_only_intraday": bool(price_only),
        "volume_semantics": "cumulative_or_unknown" if price_only else "unknown",
        "signal_capability": "none",
        "signal_eligible": False,
        "ohlcv_signal_eligible": False,
        "session_filtered_count": int(session_filtered_count),
        "source_time_order_valid": True,
        "failed_reason": "",
        "skipped_reason": "",
        "warnings": warnings,
    }

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        gate["status"] = "missing" if frame is None else "failed"
        gate["failed_reason"] = "provider 未返回目标日期分钟线"
        return gate

    df = frame
    rows = int(len(df))
    gate["rows"] = rows

    datetimes = df["datetime"].astype(str).tolist() if "datetime" in df.columns else []
    latest_dt = datetimes[-1] if datetimes else ""
    gate["latest_datetime"] = latest_dt

    dates = {str(v)[:10] for v in datetimes if str(v).strip()}
    if target_date:
        supports = bool(dates) and dates == {target_date[:10]}
    else:
        supports = bool(dates)
    gate["supports_target_date"] = supports
    if target_date and not supports:
        off_dates = sorted(d for d in dates if d != target_date[:10])
        warnings.append(f"provider 返回的分钟线日期 {off_dates} 与目标日期 {target_date[:10]} 不一致，不能用作目标日信号。")

    # Time-order integrity. Three cases:
    #  * caller did not pass the flag (sentinel) -> infer from this frame's order;
    #  * caller passed True/False -> trust the reported ORIGINAL source order
    #    (this frame may already be sorted, so we cannot re-derive it here);
    #  * caller passed an explicit None -> order is UNKNOWN (no resolvable time
    #    column / unparseable), which must fail closed rather than assume forward.
    order_unknown = False
    if source_time_order_valid is _SOURCE_ORDER_UNSET:
        reverse = False
        if len(datetimes) >= 2:
            reverse = any(datetimes[i] > datetimes[i + 1] for i in range(len(datetimes) - 1))
    elif source_time_order_valid is None:
        order_unknown = True
        reverse = False
    else:
        reverse = not bool(source_time_order_valid)
    if order_unknown:
        gate["source_time_order_valid"] = None
        warnings.append("无法确定分钟线原始时间顺序（无可识别时间列或无法解析），fail-close，不判定为信号可用。")
    else:
        gate["source_time_order_valid"] = not reverse
        if reverse:
            warnings.append("分钟线时间倒序（reverse time order），已判定为不可用于信号。")

    # OHLCV value integrity (columns present + positive prices + legal high/low +
    # non-negative volume). datetime+close only, all-NaN prices, or missing/negative
    # volume never count as complete minute OHLCV, even when not flagged price_only.
    valid_ohlcv_rows, ohlcv_warnings = _count_valid_ohlcv_rows(df)
    warnings.extend(ohlcv_warnings)
    ohlcv_columns_ok = valid_ohlcv_rows > 0

    # `price_only` is explicit normalization/provider provenance. Never infer
    # it from O=H=L=C: a genuine one-price limit session has the same shape.
    price_only = bool(price_only)
    gate["price_only_intraday"] = price_only

    has_amount = "amount" in df.columns and pd.to_numeric(df["amount"], errors="coerce").notna().any()
    if not has_amount:
        warnings.append("缺少 amount 成交额字段，只作 warning，不伪造。")

    # Minimum-sample semantics mirror validate_intraday_payload: fewer than two
    # valid rows is too few to represent a full intraday replay.
    sufficient_rows = valid_ohlcv_rows >= 2
    if not sufficient_rows:
        warnings.append("目标日期有效分钟线行数过少（<2），不足以代表完整盘中回放，不判定为信号可用。")

    # ohlcv_complete must mean "a real, target-date, correctly-ordered, >=2-row
    # valid OHLCV frame" — not merely that the columns exist. An UNKNOWN source
    # order (order_unknown) fails closed exactly like a reverse order.
    ohlcv_complete = (
        ohlcv_columns_ok
        and supports
        and not reverse
        and not order_unknown
        and sufficient_rows
        and not price_only
    )
    from quantdesk.intraday.minute_bar_lifecycle import frame_bar_evidence
    bar_evidence = frame_bar_evidence(df)
    gate["bar_evidence"] = bar_evidence
    gate["finalized_signal_eligible"] = ohlcv_complete and bar_evidence["signal_eligible"]
    gate["validation_scope"] = "minute_lifecycle_v1" if bar_evidence["lifecycle_present"] else "legacy_fields_only"
    # Field completeness is observable even for legacy rows. It cannot supply
    # missing finality, units or provenance to any signal consumer.
    eligible = ohlcv_complete and bar_evidence["signal_eligible"]
    warnings.extend(bar_evidence["reason_codes"])

    if price_only:
        gate.update({
            "status": "ok",
            "data_quality": "price_only",
            "ohlcv_complete": False,
            "volume_semantics": "cumulative_or_unknown",
            "signal_capability": "price_trend_only",
            "signal_eligible": False,
            "ohlcv_signal_eligible": False,
        })
        return gate

    if not ohlcv_complete:
        gate.update({
            "status": "ok",
            "data_quality": "incomplete_ohlcv",
            "ohlcv_complete": False,
            "volume_semantics": "per_minute" if ohlcv_columns_ok else "unknown",
            "signal_capability": "none",
            "signal_eligible": False,
            "ohlcv_signal_eligible": False,
        })
        return gate

    gate.update({
        "status": "ok",
        "data_quality": "full_ohlcv",
        "ohlcv_complete": True,
        "volume_semantics": "per_minute" if eligible else "unknown",
        "signal_capability": "full_minute_ohlcv" if eligible else "none",
        "signal_eligible": bool(eligible),
        "ohlcv_signal_eligible": bool(eligible),
    })
    return gate


def diagnose_intraday_price_conflict(
    *,
    sina_close: float | None,
    tencent_price: float | None,
    abs_tolerance: float = 0.02,
    pct_tolerance: float = 0.003,
) -> dict[str, Any]:
    """Pure diagnostic comparing the latest full Sina minute close vs a Tencent
    snapshot price. Tolerance is max(0.02 CNY, 0.3%). It only reports a
    wait/degrade reason; it never creates an order or mutates any state.
    """

    sina = safe_float(sina_close, None)
    tencent = safe_float(tencent_price, None)
    if sina is None or tencent is None or sina <= 0 or tencent <= 0:
        return {
            "status": "insufficient_data",
            "conflict": False,
            "sina_close": sina,
            "tencent_price": tencent,
            "tolerance": None,
            "difference": None,
            "wait_or_degrade_reason": "缺少新浪完整分钟收盘价或腾讯快照价，无法比较，暂不判定冲突。",
        }
    tolerance = max(abs_tolerance, sina * pct_tolerance)
    difference = abs(sina - tencent)
    conflict = difference > tolerance
    return {
        "status": "source_price_conflict" if conflict else "ok",
        "conflict": bool(conflict),
        "sina_close": sina,
        "tencent_price": tencent,
        "tolerance": round(tolerance, 6),
        "difference": round(difference, 6),
        "wait_or_degrade_reason": (
            f"新浪完整分钟收盘价 {sina} 与腾讯快照价 {tencent} 差异 {round(difference, 4)} 超过容差 {round(tolerance, 4)}，"
            "建议等待数据一致或降级为价格趋势观察，不据此创建纸面订单。"
            if conflict else ""
        ),
    }
