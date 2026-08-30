from __future__ import annotations

import importlib
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_session_evidence import build_baostock_session_evidence
from quantdesk.data_provider_registry import dependency_available, list_data_providers
from quantdesk.interprocess_lock import interprocess_file_lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
LOG = logging.getLogger(__name__)
_BAOSTOCK_SESSION_LOCK = threading.Lock()
# baostock keeps its session in module-global state, so two workers that log in
# concurrently clobber each other and the loser sees "用户未登录". A threading
# lock only covers threads inside one process; the weekly pools fan out with
# *spawned subprocesses*, where it provides no protection at all. Serialize the
# whole login->query->logout window across processes with a file lock so a
# concurrent run degrades to waiting instead of to random symbol failures.
# Lives under data/.locks/ (the project's existing ".locks" convention) rather
# than state/: state/ is a tripwire-watched root for real portfolio state, and a
# provider session lock is transport infrastructure, not state.
_BAOSTOCK_SESSION_LOCK_PATH = PROJECT_ROOT / "data" / ".locks" / "baostock_session.lock"
_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS = 300.0
STANDARD_DAILY_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "change",
    "pct_chg",
    "adjust",
    "source",
    "updated_at",
]

DAILY_COLUMN_ALIASES = {
    "date": "date",
    "日期": "date",
    "交易日期": "date",
    "symbol": "symbol",
    "code": "symbol",
    "股票代码": "symbol",
    "代码": "symbol",
    "open": "open",
    "开盘": "open",
    "开盘价": "open",
    "high": "high",
    "最高": "high",
    "最高价": "high",
    "low": "low",
    "最低": "low",
    "最低价": "low",
    "close": "close",
    "收盘": "close",
    "收盘价": "close",
    "volume": "volume",
    "成交量": "volume",
    "amount": "amount",
    "成交额": "amount",
    "change": "change",
    "涨跌额": "change",
    "pct_chg": "pct_chg",
    "涨跌幅": "pct_chg",
    "adjust": "adjust",
    "复权": "adjust",
    "复权类型": "adjust",
    "source": "source",
    "updated_at": "updated_at",
}


@dataclass
class DailyProviderResult:
    provider: str
    status: str
    attempted: int = 0
    updated: int = 0
    failed: int = 0
    latest_date: str = ""
    failed_reason: str = ""
    warnings: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["warnings"] = payload["warnings"] or []
        return payload


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_symbol(value: Any) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    for prefix in ("sh.", "sz.", "bj.", "sh", "sz", "bj"):
        if lower.startswith(prefix):
            text = text[len(prefix):]
            break
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def compact_date(value: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y%m%d")


def display_date(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y-%m-%d")


def daily_price_provider_status(*, check_network: bool = False) -> list[dict[str, object]]:
    return [
        provider
        for provider in list_data_providers(check_network=check_network)
        if provider.get("type") == "daily_price_provider"
    ]


def _network_disabled_frame(provider_name: str, symbol: str) -> tuple[pd.DataFrame, list[str]]:
    return pd.DataFrame(), [f"network=false; {provider_name} provider fetch skipped for {normalize_symbol(symbol)}"]


def provider_availability(provider_name: str) -> dict[str, Any]:
    if provider_name == "local_existing":
        return {"name": provider_name, "status": "available", "failed_reason": "", "network_required": False}
    if provider_name == "current_project_default":
        script_exists = (PROJECT_ROOT / "scripts" / "update_market_data.py").exists()
        return {
            "name": provider_name,
            "status": "available" if script_exists else "unavailable",
            "failed_reason": "" if script_exists else "scripts/update_market_data.py is missing",
            "network_required": True,
        }
    if provider_name == "akshare_daily":
        installed = dependency_available("akshare")
        return {
            "name": provider_name,
            "status": "available" if installed else "unavailable",
            "failed_reason": "" if installed else "未安装 akshare",
            "network_required": True,
        }
    if provider_name == "ashare_daily":
        installed = dependency_available("Ashare")
        return {
            "name": provider_name,
            "status": "available" if installed else "unavailable",
            "failed_reason": "" if installed else "未安装 Ashare",
            "network_required": True,
        }
    if provider_name == "mootdx_daily":
        installed = dependency_available("mootdx")
        return {
            "name": provider_name,
            "status": "available" if installed else "unavailable",
            "failed_reason": "" if installed else "未安装 mootdx",
            "network_required": True,
        }
    if provider_name == "baostock_daily":
        installed = dependency_available("baostock")
        return {
            "name": provider_name,
            "status": "available" if installed else "unavailable",
            "failed_reason": "" if installed else "baostock is not installed",
            "network_required": True,
            "implemented": True,
            "placeholder": False,
        }
    if provider_name == "tencent_daily":
        return {
            "name": provider_name,
            "status": "available",
            "failed_reason": "",
            "network_required": True,
        }
    if provider_name == "sina_daily":
        return {
            "name": provider_name,
            "status": "available",
            "failed_reason": "",
            "network_required": True,
            "implemented": True,
            "placeholder": False,
        }
    return {
        "name": provider_name,
        "status": "unavailable",
        "failed_reason": f"unknown provider: {provider_name}",
        "network_required": False,
    }


def normalize_daily_df(df: pd.DataFrame | None, symbol: str, *, source: str = "") -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    if df is None or df.empty:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS), ["provider returned empty dataframe"]
    renamed = df.rename(columns={col: DAILY_COLUMN_ALIASES.get(str(col).strip(), str(col).strip()) for col in df.columns}).copy()
    renamed = renamed.loc[:, ~renamed.columns.duplicated()]
    if "date" not in renamed.columns:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS), ["missing date column"]
    if "close" not in renamed.columns:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS), ["missing close column"]
    normalized = pd.DataFrame()
    normalized["date"] = pd.to_datetime(renamed["date"], errors="coerce")
    normalized["symbol"] = normalize_symbol(symbol)
    for column in ("open", "high", "low", "close", "volume", "amount", "change", "pct_chg"):
        if column in renamed.columns:
            normalized[column] = pd.to_numeric(renamed[column], errors="coerce")
        else:
            normalized[column] = pd.NA
            if column in {"open", "high", "low"}:
                warnings.append(f"missing {column}; fallback to close where possible")
            elif column in {"volume", "amount"}:
                warnings.append(f"missing {column}")
    close_numeric = pd.to_numeric(normalized["close"], errors="coerce")
    for column in ("open", "high", "low"):
        current = pd.to_numeric(normalized[column], errors="coerce")
        fill_mask = current.isna() & close_numeric.notna()
        filled_count = int(fill_mask.sum())
        if filled_count:
            warnings.append(f"ohlc_filled_from_close:{column}:{filled_count}_rows")
        normalized[column] = current.where(~fill_mask, close_numeric)
    if "adjust" in renamed.columns:
        normalized["adjust"] = renamed["adjust"].astype("string").str.strip().str.lower()
    else:
        source_lower = str(source or "").lower()
        if "akshare" in source_lower or "local_existing" in source_lower or "tushare" in source_lower:
            normalized["adjust"] = "raw"
        elif "tencent" in source_lower:
            normalized["adjust"] = "qfq"
        else:
            normalized["adjust"] = pd.NA
            warnings.append("missing adjust")
    normalized["source"] = source or (renamed["source"] if "source" in renamed.columns else "")
    normalized["updated_at"] = now_text()
    normalized = normalized.dropna(subset=["date", "close"])
    normalized = normalized[normalized["close"] > 0]
    normalized = normalized.sort_values("date").drop_duplicates("date", keep="last")
    normalized["date"] = normalized["date"].dt.strftime("%Y-%m-%d")
    for column in STANDARD_DAILY_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = pd.NA
    return normalized[STANDARD_DAILY_COLUMNS].reset_index(drop=True), warnings


def read_local_daily(symbol: str, *, daily_dir: Path | None = None) -> pd.DataFrame:
    frame, _warnings = read_local_daily_with_warnings(symbol, daily_dir=daily_dir)
    return frame


def _compact_warning(value: Any, *, max_len: int = 240) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)].rstrip() + "..."


def read_local_daily_status_dates_with_warnings(
    symbol: str,
    *,
    daily_dir: Path | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Read only the columns needed by the local freshness status scan.

    Freshness needs valid trading dates plus a bounded target-session truth
    signal.  Parsing only ``date``, ``close``, ``volume`` and ``adjust`` keeps
    the scan cheap while allowing callers to distinguish a real positive-
    volume session from a raw zero-volume suspension without reopening every
    full OHLCV frame.
    """

    base_dir = daily_dir or DAILY_DIR
    normalized_symbol = normalize_symbol(symbol)
    path = base_dir / f"{normalized_symbol}.csv"
    if not path.exists():
        return pd.DataFrame(columns=["date"]), []

    required_columns = {"date", "close", "volume", "adjust"}
    frame = read_csv_frame(
        path,
        usecols=lambda column: DAILY_COLUMN_ALIASES.get(str(column).strip(), str(column).strip())
        in required_columns,
    )
    read_warnings = [
        f"local_daily_cache_read_warning:{normalized_symbol}:{_compact_warning(item)}"
        for item in frame.attrs.get("warnings") or []
    ]
    if frame.empty and read_warnings:
        read_warnings.append(
            f"local_daily_cache_read_failed:{normalized_symbol}:{_compact_warning('; '.join(read_warnings))}"
        )

    renamed = frame.rename(
        columns={column: DAILY_COLUMN_ALIASES.get(str(column).strip(), str(column).strip()) for column in frame.columns}
    ).copy()
    renamed = renamed.loc[:, ~renamed.columns.duplicated()]
    normalize_warnings: list[str] = []
    if frame.empty:
        normalize_warnings.append("provider returned empty dataframe")
    elif "date" not in renamed.columns:
        normalize_warnings.append("missing date column")
    elif "close" not in renamed.columns:
        normalize_warnings.append("missing close column")

    if normalize_warnings:
        normalized = pd.DataFrame(columns=["date"])
    else:
        dates = pd.to_datetime(renamed["date"], errors="coerce")
        closes = pd.to_numeric(renamed["close"], errors="coerce")
        valid = dates.notna() & closes.notna() & (closes > 0)
        normalized = pd.DataFrame({"date": dates[valid]})
        if "volume" in renamed.columns:
            volumes = pd.to_numeric(renamed["volume"], errors="coerce")
            normalized["volume"] = volumes[valid].to_numpy()
        if "adjust" in renamed.columns:
            normalized["adjust"] = renamed.loc[valid, "adjust"].to_numpy()
        normalized = normalized.sort_values("date").drop_duplicates("date", keep="last")
        normalized["date"] = normalized["date"].dt.strftime("%Y-%m-%d")
        normalized = normalized.reset_index(drop=True)

    visible_warnings = read_warnings + [
        f"local_daily_cache_normalize_warning:{normalized_symbol}:{_compact_warning(item)}"
        for item in normalize_warnings
    ]
    if normalized.empty and not visible_warnings:
        visible_warnings.append(f"local_daily_cache_no_valid_rows:{normalized_symbol}")
    normalized.attrs["warnings"] = visible_warnings
    return normalized, visible_warnings


def read_local_daily_with_warnings(symbol: str, *, daily_dir: Path | None = None) -> tuple[pd.DataFrame, list[str]]:
    base_dir = daily_dir or DAILY_DIR
    normalized_symbol = normalize_symbol(symbol)
    path = base_dir / f"{normalized_symbol}.csv"
    if not path.exists():
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS), []
    frame = read_csv_frame(path)
    read_warnings = [
        f"local_daily_cache_read_warning:{normalized_symbol}:{_compact_warning(item)}"
        for item in frame.attrs.get("warnings") or []
    ]
    if frame.empty and read_warnings:
        read_warnings.append(f"local_daily_cache_read_failed:{normalized_symbol}:{_compact_warning('; '.join(read_warnings))}")
    normalized, warnings = normalize_daily_df(frame, symbol, source="local_existing")
    visible_warnings = [f"local_daily_cache_normalize_warning:{normalized_symbol}:{_compact_warning(item)}" for item in warnings]
    visible_warnings = read_warnings + visible_warnings
    if normalized.empty and not visible_warnings:
        visible_warnings.append(f"local_daily_cache_no_valid_rows:{normalized_symbol}")
    normalized.attrs["warnings"] = visible_warnings
    return normalized, visible_warnings


def latest_date_for_symbol(symbol: str, *, daily_dir: Path | None = None) -> str:
    frame = read_local_daily(symbol, daily_dir=daily_dir)
    if frame.empty:
        return ""
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return "" if dates.empty else dates.max().strftime("%Y-%m-%d")


def merge_daily_frames(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in (existing, fresh) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)
    raw_merged = pd.concat(frames, ignore_index=True)
    symbol = str(raw_merged["symbol"].dropna().iloc[-1] if "symbol" in raw_merged.columns and not raw_merged["symbol"].dropna().empty else "")
    normalized_frames: list[pd.DataFrame] = []
    input_warnings: list[str] = []
    for frame in frames:
        input_warnings.extend(str(item) for item in (frame.attrs.get("warnings") or []) if item)
        normalized_frame, _warnings = normalize_daily_df(frame, symbol, source="")
        input_warnings.extend(_warnings)
        if not normalized_frame.empty:
            normalized_frames.append(normalized_frame)
    if not normalized_frames:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)
    normalized = pd.concat(normalized_frames, ignore_index=True).sort_values("date")
    if normalized.empty:
        return normalized
    warnings: list[str] = []

    def _missing(value: Any) -> bool:
        if value is None:
            return True
        try:
            return bool(pd.isna(value))
        except (TypeError, ValueError):
            return False

    combined_rows: list[dict[str, Any]] = []
    for _date, group in normalized.groupby("date", sort=True):
        base = group.iloc[-1].to_dict()
        if "adjust" in group.columns:
            ordered_adjust: list[str] = []
            for value in reversed(group["adjust"].dropna().astype(str).str.strip().str.lower().tolist()):
                if value and value not in ordered_adjust:
                    ordered_adjust.append(value)
            if len(ordered_adjust) > 1:
                base_symbol = str(base.get("symbol") or symbol or "").strip()
                warnings.append(f"adjust_mismatch:{base_symbol}:{_date}:{','.join(ordered_adjust)}")
        for column in STANDARD_DAILY_COLUMNS:
            if column not in group.columns or not _missing(base.get(column)):
                continue
            values = group[column].dropna()
            if not values.empty:
                base[column] = values.iloc[-1]
        combined_rows.append(base)
    repaired = pd.DataFrame(combined_rows)
    repaired, _warnings = normalize_daily_df(repaired, symbol, source="")
    repaired.attrs["warnings"] = input_warnings + warnings + _warnings
    return repaired


def write_daily(symbol: str, frame: pd.DataFrame, *, daily_dir: Path | None = None) -> None:
    """Atomically replace one canonical daily CSV.

    The temporary file lives beside the target so os.replace cannot cross
    filesystems. A failed write leaves the previous canonical file intact.
    """

    normalized, _warnings = normalize_daily_df(frame, symbol, source="")
    base_dir = daily_dir or DAILY_DIR
    path = base_dir / f"{normalize_symbol(symbol)}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            normalized.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def fetch_current_project_default(symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("current_project_default", symbol)
    try:
        import scripts.update_market_data as update_market_data

        frame, provider_used, provider_errors, extra = update_market_data.fetch_daily_provider_chain(
            normalize_symbol(symbol),
            compact_date(start_date),
            compact_date(end_date),
            "",
            expected_end_date=compact_date(end_date),
        )
        normalized, warnings = normalize_daily_df(frame, symbol, source=f"current_project_default:{provider_used}")
        if provider_errors:
            warnings.append("fallback errors: " + "; ".join(f"{key}={value}" for key, value in provider_errors.items()))
        if extra:
            warnings.append("extra: " + str(extra))
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def fetch_akshare_daily(symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("akshare_daily", symbol)
    if not dependency_available("akshare"):
        raise RuntimeError("未安装 akshare")
    try:
        ak = importlib.import_module("akshare")
        frame = ak.stock_zh_a_hist(
            symbol=normalize_symbol(symbol),
            period="daily",
            start_date=compact_date(start_date),
            end_date=compact_date(end_date),
            adjust="",
        )
        return normalize_daily_df(frame, symbol, source="akshare_daily")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def _import_ashare_module() -> Any:
    for name in ("Ashare", "ashare"):
        try:
            return importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - optional provider module may be absent
            LOG.warning("ashare_module_import_attempt_failed: module=%s error=%s", name, exc)
            continue
    raise RuntimeError("未安装 Ashare")


def fetch_ashare_daily(symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("ashare_daily", symbol)
    module = _import_ashare_module()
    get_price = getattr(module, "get_price", None)
    if not callable(get_price):
        raise RuntimeError("Ashare module does not expose get_price")
    try:
        frame = get_price(normalize_symbol(symbol), frequency="1d", start_date=start_date, end_date=end_date)
        return normalize_daily_df(frame, symbol, source="ashare_daily")
    except TypeError:
        frame = get_price(normalize_symbol(symbol), frequency="1d")
        normalized, warnings = normalize_daily_df(frame, symbol, source="ashare_daily")
        if not normalized.empty:
            dates = pd.to_datetime(normalized["date"], errors="coerce")
            normalized = normalized.loc[(dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))]
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def fetch_mootdx_daily(symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("mootdx_daily", symbol)
    try:
        from quantdesk.providers.mootdx_provider import fetch_daily_bars

        payload = fetch_daily_bars(symbol, start_date, end_date, network=True)
        if not payload.get("ok"):
            reason = (
                payload.get("failed_reason")
                or payload.get("skipped_reason")
                or "; ".join(payload.get("errors") or [])
                or payload.get("status")
                or "mootdx_daily failed"
            )
            raise RuntimeError(str(reason))
        frame = pd.DataFrame(payload.get("data") or [])
        normalized, warnings = normalize_daily_df(frame, symbol, source="mootdx_daily")
        warnings.extend(payload.get("warnings") or [])
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def fetch_tencent_daily(symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("tencent_daily", symbol)
    try:
        from quantdesk.providers.tencent_provider import fetch_daily_bars

        payload = fetch_daily_bars(
            symbol,
            start_date,
            end_date,
            network=True,
            adjust="none",
        )
        if not payload.get("ok"):
            reason = payload.get("failed_reason") or "; ".join(payload.get("errors") or []) or payload.get("status") or "tencent_daily failed"
            raise RuntimeError(str(reason))
        effective_adjust = str(
            payload.get("effective_adjust") or payload.get("adjust") or ""
        ).strip().lower()
        if effective_adjust not in {"raw", "none", "bfq", "no"}:
            raise RuntimeError(
                "canonical_raw_required:"
                f"tencent_effective_adjust={effective_adjust or 'unknown'}"
            )
        frame = pd.DataFrame(payload.get("data") or [])
        normalized, warnings = normalize_daily_df(frame, symbol, source="tencent_daily")
        warnings.extend(payload.get("warnings") or [])
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def daily_provider_supports_symbol(provider_name: str, symbol: str) -> bool:
    """Return whether a daily provider can address this symbol at all.

    ``sina_daily`` and ``baostock_daily``只映射沪深前缀，对北交所 920xxx 会直接抛
    ``provider_unsupported_symbol``。派发前先问一次能力，避免把注定失败的请求算成
    "provider 失败"，从而把真实原因（该源不支持该板）淹没在通用失败计数里。
    """

    clean = normalize_symbol(symbol)
    if not clean:
        return False
    name = str(provider_name or "").strip()
    if name in {"sina_daily", "baostock_daily"}:
        # 302 是深市较新号段（例：302132 中航成飞，本地全集由
        # baostock.query_stock_basic 提供）。baostock 自身的映射用
        # startswith(("0","3")) 已覆盖它，因此把它算作不支持会误伤一只真实在市标的。
        return clean.startswith(
            (
                "600", "601", "603", "605", "688", "689",
                "000", "001", "002", "003", "300", "301", "302",
            )
        )
    return True


def _baostock_daily_symbol(symbol: str) -> str:
    clean = normalize_symbol(symbol)
    if clean.startswith("6"):
        return f"sh.{clean}"
    if clean.startswith(("0", "3")):
        return f"sz.{clean}"
    raise RuntimeError(
        f"provider_unsupported_symbol:baostock_daily:{clean or symbol}"
    )


def _fetch_baostock_daily_locked(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    network: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Return real trading bars plus explicit BaoStock suspension evidence.

    Rows with tradestatus=0 prove that the symbol did not trade; they are never
    written as price bars. The update engine may use only the structured attrs
    below to explain otherwise-missing exchange sessions.
    """

    if not network:
        return _network_disabled_frame("baostock_daily", symbol)
    bs = None
    logged_in = False
    try:
        bs = importlib.import_module("baostock")
        login = bs.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise RuntimeError(
                "baostock_login_failed:"
                + str(getattr(login, "error_msg", "") or "unknown")
            )
        logged_in = True
        fields = (
            "date,code,open,high,low,close,volume,amount,"
            "tradestatus,isST"
        )
        query = bs.query_history_k_data_plus(
            _baostock_daily_symbol(symbol),
            fields,
            start_date=display_date(start_date),
            end_date=display_date(end_date),
            frequency="d",
            adjustflag="3",
        )
        if str(getattr(query, "error_code", "")) != "0":
            raise RuntimeError(
                "baostock_query_failed:"
                + str(getattr(query, "error_msg", "") or "unknown")
            )
        rows: list[list[Any]] = []
        while query.next():
            rows.append(list(query.get_row_data()))
        frame = pd.DataFrame(rows, columns=list(getattr(query, "fields", [])))
        if frame.empty:
            return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS), [
                "baostock returned no rows"
            ]
        required_columns = {"date", "code", "tradestatus"}
        missing_columns = sorted(required_columns - set(frame.columns))
        if missing_columns:
            raise RuntimeError(
                "baostock_missing_evidence_columns:"
                + ",".join(missing_columns)
            )
        provider_code = _baostock_daily_symbol(symbol)
        returned_codes = {
            str(value or "").strip().lower()
            for value in frame["code"].tolist()
            if str(value or "").strip()
        }
        if returned_codes != {provider_code}:
            raise RuntimeError(
                "baostock_symbol_mismatch:"
                f"expected={provider_code}:"
                f"returned={','.join(sorted(returned_codes)) or '-'}"
            )
        status = frame.get("tradestatus", pd.Series("", index=frame.index)).astype(str).str.strip()
        dates = frame.get("date", pd.Series("", index=frame.index)).map(display_date)
        suspended_dates = sorted(
            {day for day, state in zip(dates.tolist(), status.tolist()) if day and state == "0"}
        )
        session_evidence = build_baostock_session_evidence(
            symbol=normalize_symbol(symbol),
            provider_code=provider_code,
            query_start_date=display_date(start_date),
            query_end_date=display_date(end_date),
            records=[
                {"trade_date": day, "tradestatus": state}
                for day, state in zip(dates.tolist(), status.tolist())
            ],
        )
        traded = frame.loc[status == "1"].copy()
        traded["adjust"] = "raw"
        normalized, warnings = normalize_daily_df(
            traded,
            symbol,
            source="baostock_daily",
        )
        normalized.attrs["suspension_evidence"] = session_evidence
        normalized.attrs["suspended_dates"] = suspended_dates
        normalized.attrs["suspension_evidence_source"] = "baostock.tradestatus"
        normalized.attrs["suspension_evidence_verified"] = True
        if suspended_dates:
            warnings.append(
                "baostock_explicit_suspension_dates:"
                + ",".join(suspended_dates)
            )
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc
    finally:
        if bs is not None and logged_in:
            try:
                bs.logout()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("baostock_logout_failed: %s", exc)


def fetch_baostock_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    network: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    # Thread lock first (cheap, same-process), then the cross-process file lock:
    # both are required because the daily pools fan out with spawned subprocesses
    # that a threading.Lock cannot see.
    with _BAOSTOCK_SESSION_LOCK:
        try:
            with interprocess_file_lock(
                _BAOSTOCK_SESSION_LOCK_PATH,
                timeout_seconds=_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS,
            ):
                return _fetch_baostock_daily_locked(
                    symbol,
                    start_date,
                    end_date,
                    network=network,
                )
        except TimeoutError as exc:
            # Surface the contention as a provider failure rather than crashing
            # the batch: the caller's fallback chain can still try other sources.
            raise RuntimeError(
                "baostock_session_lock_busy:"
                f"{_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS:.0f}s"
            ) from exc


def _sina_daily_symbol(symbol: str) -> str:
    clean = normalize_symbol(symbol)
    if clean.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh{clean}"
    if clean.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return f"sz{clean}"
    raise RuntimeError(
        f"provider_unsupported_symbol:sina_daily:{clean or symbol}"
    )


def fetch_sina_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    network: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    if not network:
        return _network_disabled_frame("sina_daily", symbol)
    try:
        from quantdesk.data_providers import fetch_daily_sina as fetcher

        raw = fetcher(
            symbol=_sina_daily_symbol(symbol),
            start_date=compact_date(start_date),
            end_date=compact_date(end_date),
            adjust="",
        )
        frame = pd.DataFrame(raw).copy()
        if "date" not in frame.columns and not frame.empty:
            index_name = str(frame.index.name or "index")
            frame = frame.reset_index()
            if index_name in frame.columns:
                frame = frame.rename(columns={index_name: "date"})
            elif "index" in frame.columns:
                frame = frame.rename(columns={"index": "date"})
        frame["adjust"] = "raw"
        normalized, warnings = normalize_daily_df(
            frame,
            symbol,
            source="sina_daily",
        )
        return normalized, warnings
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc

def fetch_daily(provider_name: str, symbol: str, start_date: str, end_date: str, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    if provider_name == "local_existing":
        frame, warnings = read_local_daily_with_warnings(symbol)
        if not frame.empty:
            dates = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame.loc[(dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))]
        return frame, warnings
    if provider_name == "current_project_default":
        return fetch_current_project_default(symbol, start_date, end_date, network=network)
    if provider_name == "akshare_daily":
        return fetch_akshare_daily(symbol, start_date, end_date, network=network)
    if provider_name == "ashare_daily":
        return fetch_ashare_daily(symbol, start_date, end_date, network=network)
    if provider_name == "mootdx_daily":
        return fetch_mootdx_daily(symbol, start_date, end_date, network=network)
    if provider_name == "baostock_daily":
        return fetch_baostock_daily(symbol, start_date, end_date, network=network)
    if provider_name == "tencent_daily":
        return fetch_tencent_daily(symbol, start_date, end_date, network=network)
    if provider_name == "sina_daily":
        return fetch_sina_daily(symbol, start_date, end_date, network=network)
    raise RuntimeError(f"unknown provider: {provider_name}")


def ashare_daily_available() -> bool:
    return provider_availability("ashare_daily").get("status") == "available"
