from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.config import load_config  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.daily_cache_index import (  # noqa: E402
    build_daily_cache_index,
    classify_cache_freshness_from_index,
    daily_cache_index_path,
    get_index_entry,
    is_index_entry_current,
    load_daily_cache_index,
    save_daily_cache_index,
    summarize_daily_cache_index,
    upsert_daily_cache_index_entry,
)
from quantdesk.data_providers import fetch_daily_eastmoney, fetch_daily_sina, normalize_cn_symbol_for_sina  # noqa: E402
from quantdesk.data_quality import (  # noqa: E402
    NETWORK_BLOCKED_CODE,
    assess_daily_frame_grade,
    assess_data_grade,
    bse_summary_from_results,
    classify_provider_error,
    failed_by_error_type_summary,
    failed_by_market_summary,
    grade_distribution,
)
from quantdesk.daily_price_providers import (  # noqa: E402
    DAILY_COLUMN_ALIASES,
    STANDARD_DAILY_COLUMNS,
    _with_fetch_metadata,
    merge_daily_frames as merge_price_frames,
    normalize_daily_df,
)
from quantdesk.manual_confirmation_queue import list_manual_confirmation_items  # noqa: E402
from quantdesk.market_clock import (  # noqa: E402
    AFTERNOON_END,
    is_cn_a_share_trading_day,
    load_cn_trading_calendar,
    previous_cn_a_share_trading_day,
    to_shanghai_datetime,
)
from quantdesk.paper.paper_trading import load_paper_portfolio  # noqa: E402
from quantdesk.price_basis import classify_frame_price_basis  # noqa: E402
from quantdesk.update_task_lock import (  # noqa: E402
    DEFAULT_LOCK_FILENAME,
    UpdateLockHeld,
    acquire_update_lock,
    release_update_lock,
)
from quantdesk.utils import atomic_write_json  # noqa: E402
from quantdesk.watchlist import list_watchlist  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DAILY_CACHE_DIR = DATA_DIR / "daily"
DEFAULT_UPDATE_STATUS_PATH = DATA_DIR / "cache" / "update_status.json"
DEFAULT_RUNNING_STATUS_PATH = DATA_DIR / "cache" / "update_status_running.json"
DEFAULT_CACHE_INVENTORY_PATH = DATA_DIR / "cache" / "cache_inventory.json"
DEFAULT_UPDATE_LOCK_PATH = DATA_DIR / "cache" / DEFAULT_LOCK_FILENAME
DEFAULT_DAILY_CACHE_INDEX_PATH = daily_cache_index_path()
DEFAULT_UNIVERSE_PATH = DATA_DIR / "universe" / "a_share_symbols.csv"
LONG_POOL_CANDIDATES = [DATA_DIR / "state" / "long_pool.csv", PROJECT_ROOT / "state" / "long_pool.csv"]
DEFAULT_FALLBACK_SYMBOLS = ["600000", "000001", "600519", "300750"]
LOG = logging.getLogger(__name__)

COL_DATE = "\u65e5\u671f"
COL_OPEN = "\u5f00\u76d8"
COL_CLOSE = "\u6536\u76d8"
COL_HIGH = "\u6700\u9ad8"
COL_LOW = "\u6700\u4f4e"
COL_VOLUME = "\u6210\u4ea4\u91cf"
COL_AMOUNT = "\u6210\u4ea4\u989d"
COL_PCT_CHG = "\u6da8\u8dcc\u5e45"
COL_CHG = "\u6da8\u8dcc\u989d"
COL_AMPLITUDE = "\u632f\u5e45"
COL_TURNOVER = "\u6362\u624b\u7387"
COL_SYMBOL = "\u80a1\u7968\u4ee3\u7801"

NUMERIC_COLUMNS = [COL_OPEN, COL_CLOSE, COL_HIGH, COL_LOW, COL_VOLUME, COL_AMOUNT, COL_PCT_CHG, COL_CHG, COL_AMPLITUDE, COL_TURNOVER]
COLUMN_ALIASES = {
    "date": COL_DATE,
    "open": COL_OPEN,
    "close": COL_CLOSE,
    "high": COL_HIGH,
    "low": COL_LOW,
    "volume": COL_VOLUME,
    "amount": COL_AMOUNT,
    "change": COL_CHG,
    "pct_chg": COL_PCT_CHG,
    "change_pct": COL_PCT_CHG,
    "turnover": COL_TURNOVER,
}


class DailyProviderChainError(RuntimeError):
    def __init__(self, provider_errors: dict[str, str]) -> None:
        self.provider_errors = provider_errors
        super().__init__("; ".join(f"{key}: {value}" for key, value in provider_errors.items()))


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_workers(value: object) -> int:
    try:
        workers = int(value or 1)
    except (TypeError, ValueError):
        workers = 1
    return max(1, min(8, workers))


def parse_yyyymmdd(value: str) -> datetime:
    return datetime.strptime(str(value), "%Y%m%d")


def expected_latest_trading_day_text(end_date: str) -> str:
    """Return the latest expected A-share trading day for a requested end date.

    If *end_date* is today and the A-share market has not yet closed
    (before 15:30 CST -- 30 min buffer after AFTERNOON_END for data
    provider delay), the expected latest is the *previous* trading day
    because today's daily bar is not yet available from data providers.
    """

    target = parse_yyyymmdd(end_date).date()
    calendar = load_cn_trading_calendar()
    now_sh = to_shanghai_datetime()
    today_sh = now_sh.date()
    market_close_buffer = (datetime.combine(today_sh, AFTERNOON_END) + timedelta(minutes=30)).time()
    if target == today_sh and is_cn_a_share_trading_day(target, calendar) and now_sh.time() < market_close_buffer:
        expected = previous_cn_a_share_trading_day(target, calendar)
    elif is_cn_a_share_trading_day(target, calendar):
        expected = target
    else:
        expected = previous_cn_a_share_trading_day(target + timedelta(days=1), calendar)
    return expected.strftime("%Y%m%d")


def display_date(value: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y-%m-%d")


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    if digits:
        return digits.zfill(6)
    return ""


def parse_symbol_text(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").replace(";", ",").replace("\n", ",").split(",")
    result: list[str] = []
    for item in raw_items:
        symbol = normalize_symbol(item)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def _daily_evidence_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Translate names without filtering rows or discarding preflight evidence."""
    aliases = {key: DAILY_COLUMN_ALIASES.get(value, value) for key, value in COLUMN_ALIASES.items()}
    aliases.update(DAILY_COLUMN_ALIASES)
    aliases["price_basis"] = "adjust"
    return frame.rename(columns={column: aliases.get(str(column).strip().lower(), str(column).strip())
                                 for column in frame.columns}).copy()


def normalize_daily_bars(
    df: pd.DataFrame,
    symbol: str,
    source: str = "",
    *,
    adjust_mode: str | None = None,
) -> pd.DataFrame:
    """Preserve original evidence before cleaning; a caller label is not provenance.

    ``adjust_mode`` represents an explicit request, only when the response has no
    basis column. Acquisition metadata belongs to the actual fetch boundary.
    """
    result = _daily_evidence_columns(df) if df is not None else pd.DataFrame()
    if adjust_mode is not None and "adjust" not in result.columns:
        adjust_token = str(adjust_mode).strip().lower()
        result["adjust"] = "raw" if adjust_token in {"", "none", "raw", "不复权"} else adjust_token
    output, warnings = normalize_daily_df(result, symbol)
    output.attrs["warnings"] = list(dict.fromkeys([*result.attrs.get("warnings", []), *warnings]))
    return output


def _normalize_fetched_daily_bars(
    frame: pd.DataFrame, symbol: str, *, source: str, adjust_mode: str,
) -> pd.DataFrame:
    prepared = _daily_evidence_columns(frame)
    prepared = _with_fetch_metadata(prepared, source=source, captured_at=now_text())
    return normalize_daily_bars(prepared, symbol, adjust_mode=adjust_mode)


def normalize_daily_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    return normalize_daily_bars(df, symbol)


def read_daily_cache(path: Path, symbol: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        raw = read_csv_frame(path)
        warnings = [str(item) for item in raw.attrs.get("warnings", []) if item]
        # A lossy read is not an absent cache: merging only the surviving rows
        # could erase history or a skipped row's conflicting price evidence.
        if raw.empty or any(item.startswith(("csv_parse_failed:", "csv_bad_lines_skipped:",
                                             "csv_read_failed:", "csv_missing:", "csv_empty:"))
                            for item in warnings):
            raise ValueError("daily_cache_incomplete_read:" + "|".join(warnings))
        normalized = normalize_daily_bars(raw, symbol)
        if normalized.empty:
            raise ValueError("daily_cache_no_valid_rows")
        normalized.attrs["warnings"] = list(dict.fromkeys([*warnings, *normalized.attrs.get("warnings", [])]))
        return normalized
    except Exception as exc:
        LOG.warning("read_daily_cache failed for %s: %s", symbol, exc)
        rejected = pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)
        rejected.attrs.update(
            observed_price_basis="mixed_or_unknown", price_basis_reason="unknown_price_basis",
            warnings=[f"daily_cache_read_failed:{symbol}:{type(exc).__name__}:{exc}"],
        )
        return rejected


def write_daily_cache(path: Path, df: pd.DataFrame) -> None:
    evidence = _daily_evidence_columns(df)
    if list(evidence.columns).count("adjust") > 1:
        raise ValueError("ambiguous_daily_adjust_columns")
    _basis, reason = classify_frame_price_basis(evidence)
    if reason:
        # CSV cannot retain attrs. Never persist apparently clean surviving rows
        # after the original response has failed its price-basis check.
        raise ValueError(f"daily_cache_write_price_basis:{reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    output = evidence.copy()
    date_col = "date" if "date" in output.columns else COL_DATE
    if date_col in output.columns:
        output[date_col] = pd.to_datetime(output[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        output.to_csv(temp_path, index=False, encoding="utf-8-sig")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def merge_daily_frames(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    return merge_price_frames(
        _daily_evidence_columns(existing) if existing is not None else None,
        _daily_evidence_columns(fresh) if fresh is not None else None,
    )


def load_symbols_file(path: str | Path | None) -> list[str]:
    if not path:
        return []
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if not target.exists():
        return []
    text = target.read_text(encoding="utf-8", errors="ignore")
    return parse_symbol_text(text)


def load_universe_symbols(path: str | Path | None) -> list[dict[str, str]]:
    if not path:
        return []
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if not target.exists():
        return []
    df = read_csv_frame(target, dtype=str)
    if df.empty and any("csv_read_failed" in str(item) for item in df.attrs.get("warnings", [])):
        return [{"symbol": symbol, "source": "universe_file"} for symbol in load_symbols_file(target)]
    if df.empty:
        return []
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get(COL_SYMBOL))
        if not symbol or any(item["symbol"] == symbol for item in rows):
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(row.get("name") or row.get("名称") or row.get("股票简称") or "").strip(),
                "source": str(row.get("source") or "universe_file").strip() or "universe_file",
            }
        )
    return rows


def _add_symbol(rows: list[dict[str, str]], symbol: object, source: str) -> None:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return
    if any(row["symbol"] == normalized for row in rows):
        return
    rows.append({"symbol": normalized, "source": source})


def collect_symbols(
    *,
    cli_symbols: object = None,
    symbols_file: str | Path | None = None,
    config: dict[str, Any] | None = None,
    limit: int | None = None,
    include_local_state: bool = True,
) -> list[dict[str, str]]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    rows: list[dict[str, str]] = []
    explicit_symbols = parse_symbol_text(cli_symbols)
    explicit_file_symbols = load_symbols_file(symbols_file)

    for symbol in explicit_symbols:
        _add_symbol(rows, symbol, "cli_symbols")
    for symbol in explicit_file_symbols:
        _add_symbol(rows, symbol, "symbols_file")

    explicit_only = bool(rows) and not include_local_state
    if explicit_only:
        return rows[:limit] if limit and limit > 0 else rows

    for symbol in parse_symbol_text(data_cfg.get("fallback_symbols") or []):
        _add_symbol(rows, symbol, "config_fallback_symbols")

    if include_local_state:
        try:
            for item in list_watchlist():
                _add_symbol(rows, item.get("symbol") or item.get("code"), "watchlist")
        except Exception:
            pass
        try:
            portfolio = load_paper_portfolio()
            for position in portfolio.get("positions", []) or []:
                _add_symbol(rows, position.get("symbol") or position.get("code"), "paper_portfolio")
        except Exception:
            pass
        try:
            for item in list_manual_confirmation_items():
                _add_symbol(rows, item.get("symbol") or item.get("code"), "manual_confirmation_queue")
        except Exception:
            pass
        for path in LONG_POOL_CANDIDATES:
            if not path.exists():
                continue
            try:
                df = read_csv_frame(path, dtype=str)
                for _, row in df.iterrows():
                    _add_symbol(rows, row.get("code") or row.get("symbol") or row.get(COL_SYMBOL), "long_pool")
            except Exception:
                continue

    if not rows:
        for symbol in DEFAULT_FALLBACK_SYMBOLS:
            _add_symbol(rows, symbol, "default_fallback_symbols")

    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def resolve_data_paths(config: dict[str, Any] | None = None) -> tuple[Path, Path]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    daily_dir = Path(str(data_cfg.get("daily_cache_dir") or DEFAULT_DAILY_CACHE_DIR))
    status_path = Path(str(data_cfg.get("update_status_path") or DEFAULT_UPDATE_STATUS_PATH))
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir
    if not status_path.is_absolute():
        status_path = PROJECT_ROOT / status_path
    return daily_dir, status_path


def resolve_cache_index_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    index_path = Path(str(data_cfg.get("daily_cache_index_path") or DEFAULT_DAILY_CACHE_INDEX_PATH))
    if not index_path.is_absolute():
        index_path = PROJECT_ROOT / index_path
    return index_path


def resolve_cache_inventory_path(
    config: dict[str, Any] | None = None,
    *,
    status_path: Path | None = None,
    daily_dir: Path | None = None,
) -> Path:
    """Keep test/custom roots from writing the production inventory snapshot."""

    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    configured = str(data_cfg.get("cache_inventory_path") or "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path
    resolved_status = Path(status_path).resolve() if status_path is not None else None
    default_status = DEFAULT_UPDATE_STATUS_PATH.resolve()
    if resolved_status is not None and resolved_status.parent != default_status.parent:
        return resolved_status.parent / "cache_inventory.json"
    resolved_daily = Path(daily_dir).resolve() if daily_dir is not None else None
    if resolved_daily is not None and resolved_daily != DEFAULT_DAILY_CACHE_DIR.resolve():
        return resolved_daily.parent / "cache" / "cache_inventory.json"
    return DEFAULT_CACHE_INVENTORY_PATH


def resolve_universe_path(config: dict[str, Any] | None = None, override: str | Path | None = None) -> Path:
    if override:
        path = Path(override)
    else:
        cfg = config if config is not None else load_config()
        data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
        path = Path(str(data_cfg.get("a_share_universe_path") or DEFAULT_UNIVERSE_PATH))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_retry_failed_symbols(status_path: Path) -> list[str]:
    if not status_path.exists():
        return []
    try:
        import json

        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result: list[str] = []
    for item in raw.get("results", []) or []:
        if isinstance(item, dict) and not bool(item.get("ok")):
            symbol = normalize_symbol(item.get("symbol"))
            if symbol and symbol not in result:
                result.append(symbol)
    for symbol in parse_symbol_text((raw.get("errors") or {}).keys() if isinstance(raw.get("errors"), dict) else []):
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def load_resume_processed_symbols(status_path: Path) -> set[str]:
    if not status_path.exists():
        return set()
    try:
        import json

        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    processed: set[str] = set()
    for item in raw.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        symbol = normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        if bool(item.get("ok")) or item.get("outcome") in {"success", "cache_hit", "skipped"}:
            processed.add(symbol)
    return processed


def provider_summary_from_results(results: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in results:
        provider = str(item.get("provider_used") or item.get("outcome") or "unknown")
        summary[provider] = summary.get(provider, 0) + 1
    return summary


def build_cache_inventory(status: dict[str, Any]) -> dict[str, Any]:
    """Build a lightweight cache inventory from the current update run.

    This intentionally avoids a full scan of ``data/daily`` so the UI can read
    a small status file on startup instead of touching thousands of CSV files.
    """
    results = [item for item in status.get("results", []) or [] if isinstance(item, dict)]
    min_dates = [str(item.get("min_date") or "") for item in results if item.get("min_date")]
    max_dates = [str(item.get("max_date") or item.get("latest_date") or "") for item in results if item.get("max_date") or item.get("latest_date")]
    latest_distribution: dict[str, int] = {}
    for value in max_dates:
        latest_distribution[value] = latest_distribution.get(value, 0) + 1
    distribution = status.get("data_grade_distribution")
    if not isinstance(distribution, dict) or not distribution:
        distribution = grade_distribution(results)
    csv_count = sum(1 for item in results if item.get("output_path") or item.get("path"))
    return {
        "generated_at": now_text(),
        "csv_count": csv_count,
        "min_date": min(min_dates) if min_dates else "",
        "max_date": max(max_dates) if max_dates else "",
        "expected_latest_trading_day": status.get("expected_latest_trading_day", ""),
        "data_grade_distribution": distribution,
        "latest_date_distribution": latest_distribution,
        "provider_summary": status.get("provider_summary", {}),
        "history_short_count": status.get("history_short_count", 0),
        "failed_count": status.get("failed_count", 0),
        "universe_total": status.get("universe_total", 0),
        "update_mode": status.get("update_mode", ""),
    }


def write_cache_inventory(status: dict[str, Any], path: Path = DEFAULT_CACHE_INVENTORY_PATH) -> None:
    # A resume run intentionally contains only the unfinished subset.  Writing
    # that subset as the global inventory makes a healthy full-A snapshot look
    # like a tiny universe after a crash. Preserve the last complete snapshot;
    # an explicit non-resume scan can rebuild it from the full universe.
    if bool(status.get("resume")):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            status["cache_inventory_preserved"] = True
            status["cache_inventory_path"] = str(path)
            return
        status.setdefault("warnings", []).append(
            "cache_inventory_not_rewritten_on_resume:explicit_rebuild_required"
        )
        status["cache_inventory_preserved"] = False
        status["cache_inventory_path"] = str(path)
        return
    inventory = build_cache_inventory(status)
    atomic_write_json(path, inventory)
    status["cache_inventory_path"] = str(path)


def running_status_path_from_status_path(status_path: Path) -> Path:
    return status_path.with_name("update_status_running.json")


def build_running_update_status(
    status: dict[str, Any],
    *,
    running: bool,
    current_symbol: str = "",
    current_name: str = "",
    last_symbol: str = "",
    last_outcome: str = "",
    last_message: str = "",
) -> dict[str, Any]:
    total = int(status.get("total_symbols") or 0)
    processed = int(status.get("processed_count") or 0)
    if total > 0:
        progress_percent = round(min(100.0, max(0.0, processed * 100.0 / total)), 2)
    else:
        progress_percent = 100.0 if not running else 0.0
    return {
        "running": bool(running),
        "started_at": status.get("started_at", ""),
        "updated_at": now_text(),
        "finished_at": status.get("finished_at", "") if not running else "",
        "update_mode": status.get("update_mode", ""),
        # v2.6.9: optional wrapper-script tag.  Backwards compatible: when no
        # wrapper sets it, this mirrors the existing universe_mode label so
        # the legacy UI still sees a non-empty string.
        "mode": status.get("mode", "") or status.get("universe_mode", ""),
        "all_a_share": bool(status.get("all_a_share", False)),
        "universe_total": status.get("universe_total", 0),
        "total_symbols": total,
        "processed_count": processed,
        "success_count": status.get("success_count", 0),
        "skipped_count": status.get("skipped_count", 0),
        "failed_count": status.get("failed_count", 0),
        "already_fresh_count": status.get("already_fresh_count", 0),
        "date_fresh_count": status.get("date_fresh_count", 0),
        "history_short_count": status.get("history_short_count", 0),
        "index_hit_count": status.get("index_hit_count", 0),
        "index_miss_count": status.get("index_miss_count", 0),
        "index_stale_count": status.get("index_stale_count", 0),
        "index_path": status.get("index_path", ""),
        "index_rebuilt": bool(status.get("index_rebuilt", False)),
        "current_symbol": current_symbol,
        "current_name": current_name,
        "last_symbol": last_symbol,
        "last_outcome": last_outcome,
        "last_message": last_message,
        "expected_latest_trading_day": status.get("expected_latest_trading_day", ""),
        "workers": status.get("workers", 1),
        "progress_percent": progress_percent,
    }


def write_running_update_status(
    running_path: Path,
    status: dict[str, Any],
    *,
    running: bool = True,
    current_symbol: str = "",
    current_name: str = "",
    last_symbol: str = "",
    last_outcome: str = "",
    last_message: str = "",
) -> None:
    payload = build_running_update_status(
        status,
        running=running,
        current_symbol=current_symbol,
        current_name=current_name,
        last_symbol=last_symbol,
        last_outcome=last_outcome,
        last_message=last_message,
    )
    atomic_write_json(running_path, payload)


def finalize_running_update_status(running_path: Path, status: dict[str, Any], *, last_message: str = "") -> None:
    write_running_update_status(
        running_path,
        status,
        running=False,
        last_symbol="",
        last_outcome="finished",
        last_message=last_message or "update finished",
    )


def provider_error_type_from_errors(provider_errors: dict[str, object] | None) -> str:
    for value in (provider_errors or {}).values():
        error_type = classify_provider_error(value)
        if error_type == NETWORK_BLOCKED_CODE:
            return error_type
        if error_type:
            return error_type
    return ""


def fetch_hist_daily(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    # Eastmoney daily provider wraps AKShare stock_zh_a_hist.
    return fetch_daily_eastmoney(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)


def fetch_sina_daily(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    sina_symbol = normalize_cn_symbol_for_sina(symbol)
    if not sina_symbol:
        raise ValueError(f"cannot normalize {symbol} for Sina daily")
    return fetch_daily_sina(symbol=sina_symbol, start_date=start_date, end_date=end_date, adjust=adjust)


def _is_bse_symbol(symbol: str) -> bool:
    """Return whether *symbol* belongs to the Beijing exchange prefixes.

    The legacy Eastmoney/Sina chain does not address these symbols.  The
    maintained mootdx provider already has an exchange-aware mapping, so the
    fallback is deliberately limited to BSE codes instead of broadening the
    provider surface for every symbol.
    """

    return normalize_symbol(symbol).startswith(("920", "83", "87", "43"))


def _fetch_mootdx_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from quantdesk.daily_price_providers import fetch_mootdx_daily

    frame, _warnings = fetch_mootdx_daily(
        symbol,
        start_date,
        end_date,
        network=True,
    )
    return frame


def _fetch_tencent_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    from quantdesk.daily_price_providers import fetch_tencent_daily

    frame, _warnings = fetch_tencent_daily(
        symbol,
        start_date,
        end_date,
        network=True,
    )
    return frame


def fetch_daily_provider_chain(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str,
    *,
    expected_end_date: str | None = None,
) -> tuple[pd.DataFrame, str, dict[str, str], dict[str, str]]:
    provider_errors: dict[str, str] = {}
    extra: dict[str, str] = {}
    try:
        frame = fetch_hist_daily(symbol, start_date, end_date, adjust)
        normalized = _normalize_fetched_daily_bars(frame, symbol, source="eastmoney_hist", adjust_mode=adjust)
        if normalized.empty:
            raise ValueError("eastmoney_hist returned empty daily data")
        return normalized, "eastmoney_hist", provider_errors, extra
    except Exception as exc:
        provider_errors["eastmoney_hist"] = str(exc)

    try:
        frame = fetch_sina_daily(symbol, start_date, end_date, adjust)
        normalized = _normalize_fetched_daily_bars(frame, symbol, source="sina_daily", adjust_mode=adjust)
        if normalized.empty:
            raise ValueError("sina_daily returned empty daily data")
        return normalized, "sina_daily", provider_errors, extra
    except Exception as exc:
        provider_errors["sina_daily"] = str(exc)

    retry_end = expected_end_date or ""
    if retry_end and retry_end != end_date:
        try:
            frame = fetch_sina_daily(symbol, start_date, retry_end, adjust)
            normalized = _normalize_fetched_daily_bars(frame, symbol, source="sina_daily", adjust_mode=adjust)
            if normalized.empty:
                raise ValueError("sina_daily returned empty daily data")
            extra["retry_end_date_used"] = retry_end
            return normalized, "sina_daily", provider_errors, extra
        except Exception as exc:
            provider_errors["sina_daily_retry_expected_latest_trading_day"] = str(exc)

    # BSE symbols are not addressable by the legacy Eastmoney/Sina adapters.
    # Use the already-maintained exchange-aware chain only for that market and
    # keep the canonical output raw, as required by the daily cache contract.
    if _is_bse_symbol(symbol):
        for provider_name, fetcher in (
            ("mootdx_daily", _fetch_mootdx_daily),
            ("tencent_daily", _fetch_tencent_daily),
        ):
            try:
                frame = fetcher(symbol, start_date, end_date)
                normalized = _normalize_fetched_daily_bars(
                    frame,
                    symbol,
                    source=provider_name,
                    adjust_mode="",
                )
                if normalized.empty:
                    raise ValueError(f"{provider_name} returned empty daily data")
                observed_adjust = {
                    str(value).strip().lower()
                    for value in normalized.get("adjust", pd.Series(dtype="string")).dropna().tolist()
                    if str(value).strip()
                }
                if observed_adjust and observed_adjust != {"raw"}:
                    raise ValueError(
                        "canonical_raw_required:"
                        f"{provider_name}_adjust={','.join(sorted(observed_adjust))}"
                    )
                return normalized, provider_name, provider_errors, extra
            except Exception as exc:
                provider_errors[provider_name] = str(exc)

    raise DailyProviderChainError(provider_errors)


def update_one_symbol(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    adjust: str,
    daily_dir: Path,
    force: bool = False,
    dry_run: bool = False,
    update_mode: str = "incremental",
    min_history_rows: int = 0,
    lookback_days: int = 10,
    daily_cache_index: dict[str, Any] | None = None,
    use_cache_index: bool = True,
) -> tuple[str, dict[str, Any]]:
    cache_path = daily_dir / f"{symbol}.csv"
    fetch_start = start_date
    skipped_reason = ""
    expected_end_date = expected_latest_trading_day_text(end_date)
    expected_end_display = display_date(expected_end_date)
    cache_latest_date = ""
    cache_min_date = ""
    cache_row_count = 0
    history_short = False
    date_fresh_but_history_short = False
    cache_fresh = False
    cache_index_status = "disabled" if not use_cache_index or daily_cache_index is None else "miss"
    cache_index_reason = ""
    cache_index_refreshable = False
    index_entry = None
    if use_cache_index and daily_cache_index is not None and not force:
        index_entry = get_index_entry(daily_cache_index, symbol)
        if not index_entry:
            cache_index_status = "miss"
        else:
            index_csv_path = cache_path if cache_path.exists() else None
            if not is_index_entry_current(index_entry, index_csv_path):
                cache_index_status = "stale"
                cache_index_reason = "index entry does not match local csv metadata"
            else:
                freshness = classify_cache_freshness_from_index(index_entry, expected_end_date, min_history_rows)
                cache_index_reason = str(freshness.get("reason") or "")
                cache_row_count = int(freshness.get("row_count") or 0)
                cache_latest_date = str(freshness.get("latest_date") or "")
                cache_min_date = str(freshness.get("first_date") or "")
                history_short = bool(freshness.get("history_short", False))
                cache_fresh = bool(freshness.get("cache_fresh", False))
                date_fresh_but_history_short = bool(cache_fresh and history_short)
                cache_index_status = str(freshness.get("status") or "miss")
                if cache_fresh and (not history_short or update_mode == "incremental"):
                    if history_short:
                        skipped_reason = (
                            f"local cache is already up to expected latest trading day {expected_end_display}; "
                            "history is short and should be expanded with --backfill"
                        )
                    else:
                        skipped_reason = f"local cache is already up to expected latest trading day {expected_end_display}"
                    cache_index_status = "hit"
    if skipped_reason:
        assessment = assess_data_grade(
            row_count=cache_row_count,
            min_date=cache_min_date,
            max_date=cache_latest_date,
            latest_date=cache_latest_date,
            provider_status="local_daily_cache",
            expected_latest_date=expected_end_display,
            min_backtest_rows=max(500, min_history_rows or 0),
        )
        cache_index_refreshable = False
        return "cache_hit", {"symbol": symbol, "ok": True, "provider_used": "local_daily_cache", "path": str(cache_path), "output_path": str(cache_path), "reason": skipped_reason, "latest_date": cache_latest_date, "skipped": True, "provider_errors": {}, "expected_latest_trading_day": expected_end_display, "cache_latest_date": cache_latest_date, "cache_fresh": True, "row_count": cache_row_count, "min_date": cache_min_date, "max_date": cache_latest_date, "history_short": history_short, "date_fresh_but_history_short": date_fresh_but_history_short, "already_fresh": True, "cache_index_status": cache_index_status, "cache_index_reason": cache_index_reason, "cache_index_refreshable": cache_index_refreshable, **assessment.to_dict()}
    existing = read_daily_cache(cache_path, symbol)
    cache_row_count = int(len(existing)) if existing is not None and not existing.empty else 0
    history_short = bool(min_history_rows and cache_row_count > 0 and cache_row_count < min_history_rows)
    if not force and not existing.empty:
        dates = pd.to_datetime(existing["date"] if "date" in existing.columns else existing[COL_DATE], errors="coerce")
        latest = dates.max()
        earliest = dates.min()
        if pd.notna(earliest):
            cache_min_date = earliest.strftime("%Y-%m-%d")
        if pd.notna(latest):
            cache_latest_date = latest.strftime("%Y-%m-%d")
            cache_fresh = latest.normalize() >= pd.Timestamp(parse_yyyymmdd(expected_end_date).date())
            date_fresh_but_history_short = bool(cache_fresh and history_short)
            if update_mode == "backfill" and history_short:
                fetch_start = start_date
            elif update_mode == "incremental":
                refresh_from = latest - timedelta(days=max(0, int(lookback_days or 0)))
                fetch_start = max(pd.Timestamp(start_date), refresh_from).strftime("%Y%m%d")
            else:
                next_day = latest + timedelta(days=1)
                fetch_start = max(pd.Timestamp(start_date), next_day).strftime("%Y%m%d")
            if cache_fresh and (not history_short or update_mode == "incremental"):
                if history_short:
                    skipped_reason = (
                        f"local cache is already up to expected latest trading day {expected_end_display}; "
                        "history is short and should be expanded with --backfill"
                    )
                else:
                    skipped_reason = f"local cache is already up to expected latest trading day {expected_end_display}"
    if dry_run:
        assessment = assess_daily_frame_grade(
            existing,
            provider_status="dry_run",
            expected_latest_date=expected_end_display,
            min_backtest_rows=max(500, min_history_rows or 0),
        )
        return "skipped", {"symbol": symbol, "ok": True, "provider_used": "dry_run", "path": str(cache_path), "output_path": str(cache_path), "reason": "dry_run", "skipped": True, "provider_errors": {}, "expected_latest_trading_day": expected_end_display, "cache_latest_date": cache_latest_date, "cache_fresh": cache_fresh, "row_count": cache_row_count, "min_date": cache_min_date, "max_date": cache_latest_date, "history_short": history_short, "date_fresh_but_history_short": date_fresh_but_history_short, "cache_index_status": cache_index_status, "cache_index_reason": cache_index_reason, "cache_index_refreshable": False, **assessment.to_dict()}
    if skipped_reason:
        date_col = "date" if "date" in existing.columns else COL_DATE
        dates = pd.to_datetime(existing[date_col], errors="coerce")
        latest_text = dates.max().strftime("%Y-%m-%d")
        min_text = dates.min().strftime("%Y-%m-%d")
        assessment = assess_daily_frame_grade(
            existing,
            provider_status="local_daily_cache",
            expected_latest_date=expected_end_display,
            min_backtest_rows=max(500, min_history_rows or 0),
        )
        return "cache_hit", {"symbol": symbol, "ok": True, "provider_used": "local_daily_cache", "path": str(cache_path), "output_path": str(cache_path), "reason": skipped_reason, "latest_date": latest_text, "skipped": True, "provider_errors": {}, "expected_latest_trading_day": expected_end_display, "cache_latest_date": latest_text, "cache_fresh": True, "row_count": int(len(existing)), "min_date": min_text, "max_date": latest_text, "history_short": history_short, "date_fresh_but_history_short": date_fresh_but_history_short, "already_fresh": True, "cache_index_status": cache_index_status, "cache_index_reason": cache_index_reason, "cache_index_refreshable": False, **assessment.to_dict()}

    normalized, provider_used, provider_errors, provider_extra = fetch_daily_provider_chain(symbol, fetch_start, end_date, adjust, expected_end_date=expected_end_date)
    if normalized.empty:
        raise RuntimeError(f"{symbol} returned empty daily data")
    merged = merge_daily_frames(existing, normalized)
    write_daily_cache(cache_path, merged)
    merged_dates = pd.to_datetime(merged["date"] if "date" in merged.columns else merged[COL_DATE], errors="coerce")
    latest_date = merged_dates.max().strftime("%Y-%m-%d")
    min_date = merged_dates.min().strftime("%Y-%m-%d")
    merged_row_count = int(len(merged))
    merged_history_short = bool(min_history_rows and merged_row_count < min_history_rows)
    merged_latest_ts = merged_dates.max()
    merged_cache_fresh = bool(pd.notna(merged_latest_ts) and merged_latest_ts.normalize() >= pd.Timestamp(parse_yyyymmdd(expected_end_date).date()))
    merged_date_fresh_but_history_short = bool(merged_cache_fresh and merged_history_short)
    assessment = assess_daily_frame_grade(
        merged,
        provider_status=provider_used,
        provider_errors=provider_errors,
        expected_latest_date=expected_end_display,
        min_backtest_rows=max(500, min_history_rows or 0),
    )
    return "success", {"symbol": symbol, "ok": True, "provider_used": provider_used, "path": str(cache_path), "output_path": str(cache_path), "fetch_start": fetch_start, "row_count": merged_row_count, "latest_date": latest_date, "min_date": min_date, "max_date": latest_date, "skipped": False, "provider_errors": provider_errors, "expected_latest_trading_day": expected_end_display, "cache_latest_date": cache_latest_date, "cache_fresh": merged_cache_fresh, "history_short": merged_history_short, "date_fresh_but_history_short": merged_date_fresh_but_history_short, "date_fresh": merged_cache_fresh, "cache_index_status": cache_index_status, "cache_index_reason": cache_index_reason, "cache_index_refreshable": not dry_run, **assessment.to_dict(), **provider_extra}


def run_update(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config or None)
    daily_dir, status_path = resolve_data_paths(config)
    cache_inventory_path = resolve_cache_inventory_path(
        config,
        status_path=status_path,
        daily_dir=daily_dir,
    )
    cache_index_path = resolve_cache_index_path(config)
    cache_index_enabled = not bool(getattr(args, "no_cache_index", False))
    retry_failed = bool(getattr(args, "retry_failed", False))
    retry_symbols = load_retry_failed_symbols(status_path) if retry_failed else []
    all_a_share = bool(getattr(args, "all_a_share", False))
    resume = bool(getattr(args, "resume", False))
    universe_override = str(getattr(args, "universe_file", "") or "").strip()
    symbols_file_arg = str(getattr(args, "symbols_file", "") or "").strip()
    update_mode_tag = str(getattr(args, "update_mode_tag", "") or "").strip()
    rebuild_cache_index = bool(getattr(args, "rebuild_cache_index", False))
    update_mode_flags = any(
        bool(getattr(args, name, False))
        for name in ("symbols", "symbols_file", "all_a_share", "universe_file", "retry_failed", "backfill", "incremental", "dry_run")
    )
    if rebuild_cache_index and not update_mode_flags:
        rebuilt_index = build_daily_cache_index(daily_dir)
        save_daily_cache_index(rebuilt_index, cache_index_path)
        return {
            "ok": True,
            "message": "daily cache index rebuilt",
            "index_rebuilt": True,
            "index_path": str(cache_index_path),
            "update_status_path": str(status_path),
            "symbol_count": int(rebuilt_index.get("symbol_count") or len(rebuilt_index.get("symbols", {})) or 0),
            "updated_at": rebuilt_index.get("updated_at", ""),
            "summary": summarize_daily_cache_index(rebuilt_index),
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "processed_count": 0,
            "total_symbols": 0,
            "results": [],
            "warnings": [],
            "suggested_actions": [],
        }
    universe_path = resolve_universe_path(config, universe_override) if (all_a_share or universe_override) else resolve_universe_path(config)
    daily_cache_index = load_daily_cache_index(cache_index_path) if cache_index_enabled else None
    cache_index_dirty = False

    if retry_failed and not retry_symbols:
        base_symbol_rows: list[dict[str, str]] = []
        universe_mode = "retry_failed"
    elif retry_failed:
        base_symbol_rows = [{"symbol": symbol, "source": "retry_failed"} for symbol in retry_symbols]
        universe_mode = "retry_failed"
    elif all_a_share:
        base_symbol_rows = load_universe_symbols(universe_path)
        universe_mode = "all_a_share"
        if not base_symbol_rows:
            base_symbol_rows = collect_symbols(config=config, include_local_state=True)
            universe_mode = "all_a_share_fallback_local"
    elif universe_override:
        base_symbol_rows = load_universe_symbols(universe_path)
        universe_mode = "universe_file"
    else:
        cli_symbols = getattr(args, "symbols", "")
        symbols_file = symbols_file_arg
        has_explicit_symbols = bool(parse_symbol_text(cli_symbols) or load_symbols_file(symbols_file))
        include_local_state = bool(getattr(args, "include_local_universe", False) or (not has_explicit_symbols and getattr(args, "include_local_state", True)))
        base_symbol_rows = collect_symbols(cli_symbols=cli_symbols, symbols_file=symbols_file, config=config, include_local_state=include_local_state)
        universe_mode = "explicit_symbols" if has_explicit_symbols else "local_universe"

    universe_total = len(base_symbol_rows)
    resume_processed = load_resume_processed_symbols(status_path) if resume else set()
    resume_skipped_in_universe = sum(1 for row in base_symbol_rows if row["symbol"] in resume_processed)
    if resume_processed:
        symbol_rows = [row for row in base_symbol_rows if row["symbol"] not in resume_processed]
    else:
        symbol_rows = list(base_symbol_rows)
    if args.limit and args.limit > 0:
        symbol_rows = symbol_rows[: args.limit]
    expected_latest = expected_latest_trading_day_text(args.end_date)
    update_mode = "backfill" if bool(getattr(args, "backfill", False)) else "incremental" if bool(getattr(args, "incremental", False)) else "default"
    status_update_mode = "incremental" if update_mode == "default" else update_mode
    min_history_rows = max(0, int(getattr(args, "min_history_rows", 0) or 0))
    lookback_days = max(0, int(getattr(args, "lookback_days", 10) or 0))
    workers = normalize_workers(getattr(args, "workers", 1))
    running_status_path = running_status_path_from_status_path(status_path)
    status: dict[str, Any] = {
        "started_at": now_text(),
        "finished_at": "",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "expected_latest_trading_day": display_date(expected_latest),
        "adjust": args.adjust,
        "daily_cache_dir": str(daily_dir),
        "update_status_path": str(status_path),
        "cache_inventory_path": str(cache_inventory_path),
        "dry_run": bool(args.dry_run),
        "force": bool(args.force),
        "retry_failed": retry_failed,
        "resume": resume,
        "update_mode": status_update_mode,
        "min_history_rows": min_history_rows,
        "lookback_days": lookback_days,
        "workers": workers,
        "index_path": str(cache_index_path),
        "index_hit_count": 0,
        "index_miss_count": 0,
        "index_stale_count": 0,
        "index_rebuilt": rebuild_cache_index,
        "history_short_count": 0,
        "date_fresh_but_history_short": 0,
        "date_fresh_count": 0,
        "already_fresh_count": 0,
        "all_a_share": all_a_share,
        "universe_mode": universe_mode,
        # v2.6.9: optional wrapper-script tag (e.g. low_price_universe_daily_update).
        # Falls back to universe_mode so the running status always has a label.
        "mode": update_mode_tag or universe_mode,
        "universe_file": str(universe_path),
        "universe_total": universe_total,
        "resume_skipped_count": resume_skipped_in_universe,
        "processed_count": 0,
        "remaining_count": max(universe_total - resume_skipped_in_universe - len(symbol_rows), 0),
        "total_symbols": len(symbol_rows),
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "provider_summary": {},
        "data_grade_distribution": {},
        "latest_dates": {},
        "errors": {},
        "last_error": "",
        "last_error_type": "",
        "network_blocked_or_permission_denied": False,
        "warnings": [],
        "provider_unavailable": False,
        "suggested_actions": [],
        "symbols": symbol_rows,
        "results": [],
        "failed_by_error_type": {},
        "failed_by_market": {},
        "bse_total": 0,
        "bse_success": 0,
        "bse_failed": 0,
    }
    if not symbol_rows:
        if bool(getattr(args, "retry_failed", False)):
            status["warnings"].append("no failed symbols to retry")
        else:
            status["warnings"].append("no symbols to update")
    write_running_update_status(running_status_path, status, running=True, last_message="update started")

    def _apply_success(row: dict[str, str], outcome: str, detail: dict[str, Any]) -> str:
        symbol = row["symbol"]
        if outcome == "success":
            status["success_count"] += 1
            status["latest_dates"][symbol] = detail.get("latest_date", "")
            message = f"OK {symbol} latest={detail.get('latest_date', '')} rows={detail.get('row_count', '')}"
        else:
            status["skipped_count"] += 1
            if detail.get("latest_date"):
                status["latest_dates"][symbol] = detail.get("latest_date", "")
            message = f"SKIP {symbol} {detail.get('reason', '')}"
        status["results"].append({**row, **detail, "outcome": outcome})
        if detail.get("history_short"):
            status["history_short_count"] += 1
        if detail.get("date_fresh_but_history_short"):
            status["date_fresh_but_history_short"] += 1
        if detail.get("date_fresh") or detail.get("cache_fresh"):
            status["date_fresh_count"] += 1
        if detail.get("already_fresh") or (outcome == "cache_hit" and detail.get("cache_fresh")):
            status["already_fresh_count"] += 1
        return message

    def _record_index_stats(detail: dict[str, Any]) -> None:
        nonlocal cache_index_dirty
        status_text = str(detail.get("cache_index_status") or "").strip().lower()
        if status_text == "hit":
            status["index_hit_count"] += 1
        elif status_text in {"stale", "stale_entry", "stale_date"}:
            status["index_stale_count"] += 1
        elif status_text:
            status["index_miss_count"] += 1
        if not cache_index_enabled or daily_cache_index is None:
            return
        if not detail.get("cache_index_refreshable"):
            return
        symbol = normalize_symbol(detail.get("symbol"))
        csv_path_text = str(detail.get("output_path") or detail.get("path") or "")
        if not symbol or not csv_path_text:
            return
        csv_path = Path(csv_path_text)
        if not csv_path.is_absolute():
            csv_path = PROJECT_ROOT / csv_path
        entry = upsert_daily_cache_index_entry(daily_cache_index, symbol, csv_path, metadata=detail)
        if entry is not None:
            cache_index_dirty = True

    def _apply_failure(row: dict[str, str], exc: Exception) -> str:
        symbol = row["symbol"]
        status["failed_count"] += 1
        status["errors"][symbol] = str(exc)
        provider_errors = getattr(exc, "provider_errors", None)
        if not isinstance(provider_errors, dict):
            provider_errors = {"all_failed": str(exc)}
        error_type = provider_error_type_from_errors(provider_errors) or classify_provider_error(exc)
        failed_grade = assess_data_grade(
            row_count=0,
            provider_status="all_failed",
            warning_level="ERROR",
            provider_errors=provider_errors,
            expected_latest_date=status.get("expected_latest_trading_day", ""),
        )
        status["last_error"] = str(exc)
        status["last_error_type"] = error_type
        if error_type == NETWORK_BLOCKED_CODE:
            status["network_blocked_or_permission_denied"] = True
        status["results"].append({**row, "symbol": symbol, "ok": False, "provider_used": "all_failed", "row_count": 0, "latest_date": "", "output_path": "", "skipped": False, "provider_errors": provider_errors, "provider_error_type": error_type, "outcome": "failed", **failed_grade.to_dict()})
        return f"FAIL {symbol} {exc}"

    def _run_symbol(row: dict[str, str]) -> tuple[dict[str, str], str, dict[str, Any] | None, Exception | None]:
        symbol = row["symbol"]
        try:
            outcome, detail = update_one_symbol(symbol, start_date=args.start_date, end_date=args.end_date, adjust=args.adjust, daily_dir=daily_dir, force=args.force, dry_run=args.dry_run, update_mode=update_mode, min_history_rows=min_history_rows, lookback_days=lookback_days, daily_cache_index=daily_cache_index, use_cache_index=cache_index_enabled)
            if workers > 1 and args.sleep and args.sleep > 0:
                time.sleep(args.sleep)
            return row, outcome, detail, None
        except Exception as exc:
            if workers > 1 and args.sleep and args.sleep > 0:
                time.sleep(args.sleep)
            return row, "failed", None, exc

    def _record_progress(row: dict[str, str], outcome: str, message: str) -> None:
        status["processed_count"] = len(status["results"])
        status["remaining_count"] = max(universe_total - resume_skipped_in_universe - status["processed_count"], 0)
        write_running_update_status(
            running_status_path,
            status,
            running=True,
            current_symbol=row.get("symbol", ""),
            current_name=row.get("name", ""),
            last_symbol=row.get("symbol", ""),
            last_outcome=outcome,
            last_message=message,
        )

    if workers <= 1:
        for row in symbol_rows:
            _row, outcome, detail, exc = _run_symbol(row)
            if exc is None and detail is not None:
                message = _apply_success(_row, outcome, detail)
                _record_index_stats(detail)
            else:
                message = _apply_failure(_row, exc or RuntimeError("unknown error"))
            print(message)
            _record_progress(_row, outcome, message)
            if args.sleep and args.sleep > 0:
                time.sleep(args.sleep)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_symbol, row) for row in symbol_rows]
            for future in as_completed(futures):
                _row, outcome, detail, exc = future.result()
                if exc is None and detail is not None:
                    message = _apply_success(_row, outcome, detail)
                    _record_index_stats(detail)
                else:
                    message = _apply_failure(_row, exc or RuntimeError("unknown error"))
                print(message)
                _record_progress(_row, outcome, message)
    status["provider_summary"] = provider_summary_from_results(status["results"])
    status["data_grade_distribution"] = grade_distribution(status["results"])
    status["failed_by_error_type"] = failed_by_error_type_summary(status["results"])
    status["failed_by_market"] = failed_by_market_summary(status["results"])
    status.update(bse_summary_from_results(status["results"]))
    if status["success_count"] == 0 and status["failed_count"] > 0:
        status["provider_unavailable"] = True
        if status.get("network_blocked_or_permission_denied"):
            status["warnings"].append("network blocked or permission denied; AKShare providers could not be reached")
            status["suggested_actions"].append("Check firewall, proxy/TUN, Windows network permission, or run from an allowed network.")
        status["warnings"].append("all symbols failed; check network, AKShare, or provider response")
        status["suggested_actions"].append("Check network/proxy/TUN, retry later, or import local CSV daily bars.")
    elif status["success_count"] > 0 and status["failed_count"] > 0:
        status["warnings"].append("some symbols failed; successful rows were kept")
    status["finished_at"] = now_text()
    status["updated_count"] = status["success_count"]
    status["remaining_count"] = max(universe_total - resume_skipped_in_universe - status["processed_count"], 0)
    if cache_index_enabled and daily_cache_index is not None and (cache_index_dirty or rebuild_cache_index):
        save_daily_cache_index(daily_cache_index, cache_index_path)
    write_cache_inventory(status, cache_inventory_path)
    atomic_write_json(status_path, status)
    finalize_running_update_status(running_status_path, status, last_message="update_market_data finished")
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update local QuantDesk daily-bar cache from provider chain.")
    parser.add_argument("--config", default="", help="Optional config path.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols, e.g. 600000,000001.")
    parser.add_argument("--symbols-file", default="", help="Optional file containing symbols.")
    parser.add_argument("--all-a-share", action="store_true", help="Use data/universe/a_share_symbols.csv as the update universe.")
    parser.add_argument("--universe-file", default="", help="CSV universe file with symbol/code column.")
    parser.add_argument("--start-date", default="20230101", help="Start date in YYYYMMDD format.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="End date in YYYYMMDD format.")
    parser.add_argument("--adjust", default="qfq", help="AKShare adjust mode, default qfq.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of symbols to update.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Sleep seconds between symbols.")
    parser.add_argument("--force", action="store_true", help="Re-download the requested date range.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve symbols and write status without network calls.")
    parser.add_argument("--include-local-universe", action="store_true", help="Merge watchlist, paper portfolio, long pool, and config fallback symbols with explicit --symbols.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry only symbols that failed in the previous update_status.json.")
    parser.add_argument("--resume", action="store_true", help="Skip symbols already marked ok/skipped in the previous update_status.json.")
    parser.add_argument("--backfill", action="store_true", help="Backfill long history; fresh short caches are not skipped.")
    parser.add_argument("--incremental", action="store_true", help="Run daily incremental update with a lookback window.")
    parser.add_argument("--min-history-rows", type=int, default=0, help="Minimum expected local history rows before a cache is considered complete.")
    parser.add_argument("--lookback-days", type=int, default=10, help="Incremental update overlap window in calendar days.")
    parser.add_argument("--workers", type=int, default=1, help="Optional limited parallel workers for symbol updates, clamped to 1-8. Default 1 keeps legacy serial behavior.")
    parser.add_argument("--rebuild-cache-index", action="store_true", help="Rebuild data/cache/daily_cache_index.json from local CSV files without network access.")
    parser.add_argument("--no-cache-index", action="store_true", help="Disable the local daily-cache index acceleration layer and fall back to CSV reads.")
    # v2.6.9: wrapper scripts (e.g. scripts/update_low_price_universe_daily.py)
    # set a short tag that is stamped on update_status_running.json's "mode"
    # field.  This is purely informational so the UI can label the running
    # task; the legacy update_mode (incremental / backfill / default) and
    # universe_mode fields are unchanged.
    parser.add_argument("--update-mode-tag", default="", help="Optional short tag stamped on update_status_running.json::mode. Used by wrapper scripts to label the task track (e.g. low_price_universe_daily_update). Does not change update behavior.")
    return parser.parse_args(argv)


def _update_lock_path_from_args(args: argparse.Namespace) -> Path:
    """Resolve the lock path next to ``update_status.json``.

    Keeps the lock file in the same ``data/cache/`` directory as the
    other status artifacts so test runs that override the config also
    keep their lock isolated from production runs.
    """

    config = load_config(getattr(args, "config", "") or None)
    _, status_path = resolve_data_paths(config)
    return status_path.with_name(DEFAULT_LOCK_FILENAME)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rebuild_only = bool(getattr(args, "rebuild_cache_index", False)) and not any(
        bool(getattr(args, name, False))
        for name in ("symbols", "symbols_file", "all_a_share", "universe_file", "retry_failed", "backfill", "incremental", "dry_run")
    )
    lock_path = _update_lock_path_from_args(args)
    lock_acquired = False
    if not rebuild_only:
        try:
            command = "update_market_data.py " + " ".join(argv) if argv else "update_market_data.py " + " ".join(sys.argv[1:])
            acquire_update_lock(lock_path, command=command)
            lock_acquired = True
        except UpdateLockHeld as held:
            print(
                "已有市场数据更新任务正在运行 / another update_market_data task is running: "
                + json.dumps(held.lock_info, ensure_ascii=False)
            )
            return 2
    try:
        status = run_update(args)
    finally:
        if lock_acquired:
            release_update_lock(lock_path)
    print(f"update_market_data finished: success={status['success_count']} failed={status['failed_count']} skipped={status['skipped_count']} status={status['update_status_path']}")
    if getattr(args, "rebuild_cache_index", False) and status.get("index_rebuilt"):
        return 0
    if status["success_count"] > 0 or status.get("dry_run"):
        return 0
    if status.get("retry_failed") and status["total_symbols"] == 0:
        return 0
    if status["total_symbols"] == 0:
        return 1
    return 1 if status["failed_count"] > 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
