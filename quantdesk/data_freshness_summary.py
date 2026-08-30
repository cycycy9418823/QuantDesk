"""Local daily data freshness summary for QuantDesk.

The module only inspects local daily cache files. It never downloads data,
never scans the full A-share universe by default, and uses the QuantDesk
trading calendar to compute trading-day lag.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, short_error_text, truthy, write_json
from quantdesk.trading_calendar import calendar_info, resolve_target_complete_trade_date, trade_days_between
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
DEFAULT_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
LOW_PRICE_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
DEFAULT_OUTPUT_PATH = RESULTS_DIR / "data_health" / "data_freshness_summary_latest.json"
DISCLAIMER = "仅用于本地数据健康诊断，不构成投资建议。"
MISSING_DAILY_WARNING = "未找到本地日线数据，无法判断数据新鲜度；请先运行 data-update 离线检查/显式联网补数，或导入本地 data/daily。"


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _load_low_price_symbols(max_symbols: int = 50, *, universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV) -> list[str]:
    path = _project_path(universe_path)
    if not path.exists() or not path.is_file():
        return []
    limit = max(0, int(max_symbols or 0)) or 50
    symbols: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if "included" in row and not truthy(row.get("included")):
                    continue
                symbol = normalize_symbol(row.get("symbol") or row.get("code"))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
                if len(symbols) >= limit:
                    break
    except Exception:
        return []
    return symbols


def normalize_data_scope(symbol_source: Any = "", *, explicit_symbols: Any = None) -> str:
    """Return the user-facing scope name used by workbench status cards."""

    if _split_symbols(explicit_symbols):
        return "specified_symbols"
    source = str(symbol_source or "").strip().lower()
    if source in {"explicit", "specified_symbols"}:
        return "specified_symbols"
    if "low_price" in source:
        return "low_price_included"
    if source in {"analysis_universe", "analysis"}:
        return "analysis_universe"
    if source in {"local_cache", "local_symbols_only", "local"}:
        return "local_cache"
    return "unknown"


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] in {"-", "/"}:
        return text[:10].replace("/", "-")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except Exception:
        return text[:10]


def _latest_date_from_csv(path: Path, *, target_date: str = "") -> tuple[str, int, str]:
    if not path.exists() or not path.is_file():
        return "", 0, "missing"
    latest = ""
    row_count = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row_count += 1
                day = _date_text(row.get("date") or row.get("trade_date") or row.get("latest_date") or row.get("datetime"))
                if not day:
                    continue
                if target_date and day > target_date:
                    continue
                if not latest or day > latest:
                    latest = day
    except Exception as exc:  # noqa: BLE001
        return "", row_count, f"parse_error:{short_error_text(exc)}"
    return latest, row_count, "available" if latest else "missing_date"


def compute_trading_day_lag(latest_date: str, target_date: str, *, calendar_path: str | Path | None = None) -> int | None:
    latest = _date_text(latest_date)
    target = _date_text(target_date)
    if not latest or not target:
        return None
    if latest >= target:
        return 0
    days = trade_days_between(latest, target, calendar_path=Path(calendar_path) if calendar_path else None)
    if days and days[0] == latest:
        return max(0, len(days) - 1)
    return len(days)


def inspect_symbol_latest_date(
    symbol: str,
    *,
    daily_dir: str | Path = DEFAULT_DAILY_DIR,
    target_date: str = "",
    stale_threshold_trading_days: int = 2,
    calendar_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    base = _project_path(daily_dir)
    candidates = [
        base / f"{normalized}.csv",
        base / f"{normalized}_qfq.csv",
    ]
    existing = next((path for path in candidates if path.exists()), None)
    if not existing:
        return {
            "latest_date": "",
            "trading_day_lag": None,
            "status": "missing",
            "path": "",
            "row_count": 0,
            "reason": "daily_file_missing",
        }
    latest, row_count, load_status = _latest_date_from_csv(existing, target_date=_date_text(target_date))
    lag = compute_trading_day_lag(latest, target_date, calendar_path=calendar_path)
    if not latest:
        status = "missing"
    elif lag is not None and lag > int(stale_threshold_trading_days):
        status = "stale"
    else:
        status = "fresh"
    return {
        "latest_date": latest,
        "trading_day_lag": lag,
        "status": status,
        "path": _rel(existing),
        "row_count": row_count,
        "reason": "" if load_status == "available" else load_status,
    }


def inspect_daily_cache_freshness(
    symbols: Any = None,
    *,
    target_date: str = "",
    daily_dir: str | Path = DEFAULT_DAILY_DIR,
    max_symbols: int = 50,
    stale_threshold_trading_days: int = 2,
    low_price_universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV,
    calendar_path: str | Path | None = None,
) -> dict[str, Any]:
    explicit = _split_symbols(symbols)
    selected = explicit or _load_low_price_symbols(max_symbols=max_symbols, universe_path=low_price_universe_path)
    selected = selected[: max(0, int(max_symbols or 0)) or 50] if not explicit else selected
    base = _project_path(daily_dir)
    rows = {
        symbol: inspect_symbol_latest_date(
            symbol,
            daily_dir=base,
            target_date=target_date,
            stale_threshold_trading_days=stale_threshold_trading_days,
            calendar_path=calendar_path,
        )
        for symbol in selected
    }
    warnings: list[str] = []
    if not base.exists():
        warnings.append(MISSING_DAILY_WARNING)
    if not explicit and not selected:
        warnings.append("未传入 symbols，且低价学习池没有可用 included=true 样本；不会默认扫描全 A。")
    return {
        "symbols": rows,
        "selected_symbols": selected,
        "symbol_source": "explicit" if explicit else "low_price_included_sample",
        "daily_dir": _rel(base),
        "daily_dir_exists": base.exists(),
        "warnings": warnings,
    }


def summarize_stale_symbols(symbols: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stale = [symbol for symbol, row in symbols.items() if row.get("status") == "stale"]
    missing = [symbol for symbol, row in symbols.items() if row.get("status") == "missing"]
    fresh = [symbol for symbol, row in symbols.items() if row.get("status") == "fresh"]
    max_lag = max([int(row.get("trading_day_lag") or 0) for row in symbols.values()] or [0])
    return {
        "fresh_symbols": fresh,
        "stale_symbols": stale,
        "missing_symbols": missing,
        "fresh_count": len(fresh),
        "stale_count": len(stale),
        "missing_count": len(missing),
        "max_trading_day_lag": max_lag,
    }


def summarize_symbol_date_scope(symbols: dict[str, dict[str, Any]], *, target_date: str = "") -> dict[str, Any]:
    """Summarize date coverage without implying that newer symbols regressed.

    ``coverage_date`` is the date every checked symbol covers.  If any checked
    symbol is missing a local daily file, coverage is unknown.  ``latest_date_max``
    is the newest date present in the checked scope, so a partial update can show
    both coverage_date=2026-05-15 and latest_date_max=2026-05-20.
    """

    normalized_target = _date_text(target_date)
    latest_by_symbol = {
        symbol: _date_text(row.get("latest_date"))
        for symbol, row in symbols.items()
        if isinstance(row, dict)
    }
    present = [day for day in latest_by_symbol.values() if day]
    missing_symbols = [symbol for symbol, day in latest_by_symbol.items() if not day]
    latest_date_min = min(present) if present else ""
    latest_date_max = max(present) if present else ""
    coverage_date = latest_date_min if present and not missing_symbols else ""
    if normalized_target:
        updated_symbols = [symbol for symbol, day in latest_by_symbol.items() if day and day >= normalized_target]
    else:
        updated_symbols = [symbol for symbol, day in latest_by_symbol.items() if day and day == latest_date_max]
    latest_updated_symbols = [symbol for symbol, day in latest_by_symbol.items() if day and day == latest_date_max]
    return {
        "latest_by_symbol": latest_by_symbol,
        "latest_date_min": latest_date_min,
        "latest_date_max": latest_date_max,
        "coverage_date": coverage_date,
        "latest_local_daily_date": coverage_date or latest_date_min,
        "updated_symbols": updated_symbols,
        "latest_updated_symbols": latest_updated_symbols,
    }


def build_freshness_warning(summary: dict[str, Any]) -> str:
    if int(summary.get("symbol_count") or 0) == 0:
        return "当前没有可分析股票，无法更新 current_analysis_pool；请先运行 data-bootstrap 或指定 --symbols。"
    if int(summary.get("missing_count") or 0):
        return "部分股票缺少本地日线数据，请先补数据再做策略验证。"
    if int(summary.get("stale_count") or 0):
        return "部分股票本地日线数据可能过期，请先运行 data-update 离线检查/显式联网补数，或导入更新后的 data/daily。"
    return ""


def build_data_freshness_summary(
    symbols: Any = None,
    *,
    target_date: str = "",
    network: bool = False,
    daily_dir: str | Path = DEFAULT_DAILY_DIR,
    max_symbols: int = 50,
    stale_threshold_trading_days: int = 2,
    low_price_universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV,
    calendar_path: str | Path | None = None,
    write: bool = False,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> dict[str, Any]:
    target_info = resolve_target_complete_trade_date(_date_text(target_date) or None)
    target = str(target_info.get("target_complete_trade_date") or target_info.get("target_date") or now_text()[:10])
    inspected = inspect_daily_cache_freshness(
        symbols,
        target_date=target,
        daily_dir=daily_dir,
        max_symbols=max_symbols,
        stale_threshold_trading_days=stale_threshold_trading_days,
        low_price_universe_path=low_price_universe_path,
        calendar_path=calendar_path,
    )
    symbol_rows = inspected["symbols"]
    counts = summarize_stale_symbols(symbol_rows)
    counts["symbol_count"] = len(symbol_rows)
    date_scope = summarize_symbol_date_scope(symbol_rows, target_date=target)
    data_scope = normalize_data_scope(inspected.get("symbol_source"), explicit_symbols=symbols)
    cal = calendar_info(target_date=target)
    warnings = [*inspected.get("warnings", []), *[str(item) for item in (target_info.get("target_date_warnings") or []) if item]]
    warning = build_freshness_warning(counts)
    if warning:
        warnings.append(warning)
    no_universe = len(symbol_rows) == 0
    no_data = bool(symbol_rows) and not date_scope["latest_date_max"]
    if no_universe:
        freshness_status = "no_universe"
        warning_kind = "no_universe"
        warnings.append("warning_kind=no_universe: 当前没有可分析股票。")
    elif no_data:
        freshness_status = "no_data"
        warning_kind = "data_missing"
        warnings.append("warning_kind=no_data: 当前股票池没有可用本地日线。")
    elif counts["stale_count"] or counts["missing_count"]:
        freshness_status = "stale"
        warning_kind = "data_stale" if counts["stale_count"] else "data_missing"
    else:
        freshness_status = "fresh"
        warning_kind = ""
    status = "warning" if warnings or freshness_status != "fresh" else "passed"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "data_freshness_summary",
        "status": status,
        "freshness_status": freshness_status,
        "warning_kind": warning_kind,
        "target_date": target,
        "data_scope": data_scope,
        "symbols_requested": _split_symbols(symbols),
        "symbols_checked": inspected.get("selected_symbols") or [],
        "calendar_quality": cal.get("calendar_quality"),
        "symbol_count": len(symbol_rows),
        "fresh_count": counts["fresh_count"],
        "stale_count": counts["stale_count"],
        "missing_count": counts["missing_count"],
        "latest_date_min": date_scope["latest_date_min"],
        "latest_date_max": date_scope["latest_date_max"],
        "coverage_date": date_scope["coverage_date"],
        "latest_local_daily_date": date_scope["latest_local_daily_date"],
        "latest_updated_symbols": date_scope["latest_updated_symbols"][:50],
        "updated_symbols": date_scope["updated_symbols"][:50],
        "stale_symbols": counts["stale_symbols"][:50],
        "missing_symbols": counts["missing_symbols"][:50],
        "max_trading_day_lag": counts["max_trading_day_lag"],
        "stale_threshold_trading_days": int(stale_threshold_trading_days),
        "network": bool(network),
        "full_scan": False,
        "daily_dir": inspected.get("daily_dir"),
        "daily_dir_exists": inspected.get("daily_dir_exists"),
        "symbol_source": inspected.get("symbol_source"),
        "selected_symbols": inspected.get("selected_symbols"),
        "symbols": symbol_rows,
        "warnings": [short_error_text(item) for item in warnings if item],
        "suggested_next_action": (
            "python scripts/qd.py data-bootstrap --preset low_price_demo --network false"
            if no_universe
            else (
            "python scripts/qd.py data-update --symbols "
            + (",".join(inspected.get("selected_symbols") or []) or "600000,000001")
            + f" --target-date {target} --enable-network；或导入本地 data/daily/{{symbol}}.csv 后重试。"
            if status == "warning"
            else "可继续运行 strategy-validate、consistency-check 或 observation-report。"
            )
        ),
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_data_freshness_summary(payload, output_path)
    return payload


def write_data_freshness_summary(payload: dict[str, Any], output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    return write_json(_project_path(output_path), payload)
