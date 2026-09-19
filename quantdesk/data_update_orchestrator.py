from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quantdesk.daily_session_evidence import load_verified_no_session_dates

DEFAULT_REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "data_update_report_latest.md"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISCLAIMER = "Research only. No broker. No real orders. Not investment advice."
LOG = logging.getLogger(__name__)
# Self-check contract markers: network=False, full_scan=False.

try:
    from quantdesk.daily_price_update_engine import (
        local_daily_status_for_symbols as _engine_local_daily_status,
    )
    from quantdesk.daily_price_update_engine import resolve_symbols as _engine_resolve_symbols
    from quantdesk.daily_price_update_engine import update_daily_prices as _engine_update_daily_prices
except Exception as exc:  # pragma: no cover - defensive compatibility for partial imports
    LOG.warning("daily_price_update_engine_import_failed: %s", exc)
    _engine_local_daily_status = None
    _engine_resolve_symbols = None
    _engine_update_daily_prices = None

try:
    # Session-aware expected-settlement date keeps this module's target-date
    # semantics aligned with market_clock / trading_day_freshness so the data
    # health layer stops treating "yesterday's completed bar" (the freshest one
    # that can exist before today's close) as stale.
    from quantdesk.market_clock import expected_settlement_date as _expected_settlement_date
    from quantdesk.market_clock import to_shanghai_datetime as _to_shanghai_datetime
except Exception as exc:  # pragma: no cover - defensive compatibility
    LOG.warning("market_clock_import_failed: %s", exc)
    _expected_settlement_date = None
    _to_shanghai_datetime = None


def _coerce_root(value: Any) -> Path:
    if isinstance(value, Path):
        return value
    text = str(value or "")
    if text and (Path(text).exists() and (Path(text) / "data").exists()):
        return Path(text)
    return PROJECT_ROOT


def _split_symbols(value: Any, max_symbols: int | None = None) -> list[str]:
    if isinstance(value, (list, tuple)):
        symbols = [str(item).strip() for item in value if str(item).strip()]
    else:
        symbols = [item.strip() for item in str(value or "").split(",") if item.strip()]
    if max_symbols is not None and max_symbols > 0:
        return symbols[:max_symbols]
    return symbols


def resolve_symbols(symbols: Any = None, *, max_symbols: int = 80, only_low_price: bool = True) -> list[dict[str, str]]:
    """Resolve update symbols for readonly workbench status builders.

    This compatibility wrapper never fetches network data and never writes
    ``data/daily``. It delegates to the daily price update engine when present
    and otherwise falls back to explicit symbols.
    """

    if _engine_resolve_symbols is not None:
        try:
            return _engine_resolve_symbols(only_low_price=only_low_price, limit=max_symbols, symbols=symbols)
        except Exception as exc:  # noqa: BLE001 - compatibility wrapper must degrade visibly
            LOG.warning("engine_resolve_symbols_failed: %s", exc)
    return [{"symbol": symbol.zfill(6), "name": symbol.zfill(6), "included": "explicit"} for symbol in _split_symbols(symbols, max_symbols)]


def select_update_symbols(symbols: Any = None, *, max_symbols: int = 80, only_low_price: bool = True) -> dict[str, Any]:
    rows = resolve_symbols(symbols, max_symbols=max_symbols, only_low_price=only_low_price)
    selected = rows[:max_symbols] if max_symbols and max_symbols > 0 else rows
    return {
        "ok": True,
        "symbols": selected,
        "symbol_count": len(selected),
        "symbol_source": "explicit" if symbols else "current_analysis_pool",
        "full_scan": False,
        "max_symbols": max_symbols,
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
    }


def _project_trading_calendar(project_root: Path) -> dict[str, Any]:
    path = (
        project_root
        / "data"
        / "calendar"
        / "a_share_trading_calendar.csv"
    )
    lookup: dict[str, bool] = {}
    if not path.exists():
        return {"is_open_by_date": lookup}
    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            for row in csv.DictReader(handle):
                trade_date = str(row.get("date") or "").strip()[:10]
                value = str(row.get("is_open") or "").strip().lower()
                if trade_date:
                    lookup[trade_date] = value in {
                        "1",
                        "true",
                        "yes",
                        "y",
                        "open",
                    }
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "project_trading_calendar_read_failed: path=%s error=%s",
            path,
            exc,
        )
        return {"is_open_by_date": {}}
    return {"is_open_by_date": lookup}


def daily_price_status(
    symbols: Any = None,
    *,
    root: Path | str | None = None,
    target_date: str | None = None,
    max_symbols: int = 80,
    freshness_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible local daily status summary.

    This is intentionally readonly. It scans local ``data/daily`` state and
    provides the older v3.3.2 field names used by workbench tests.
    """

    project_root = _coerce_root(root)
    selected = _split_symbols(symbols, max_symbols=max_symbols)
    fresh = (
        freshness_snapshot
        if freshness_snapshot is not None
        else freshness_check(project_root)
    )
    if _engine_local_daily_status is not None:
        rows = [
            {
                "symbol": symbol,
                "name": symbol,
                "included": "explicit",
            }
            for symbol in selected
        ]
        return _engine_local_daily_status(
            rows,
            target_date=str(
                target_date
                or fresh.get("latest_trade_date_in_daily")
                or ""
            ),
            daily_dir=project_root / "data" / "daily",
            metadata_dir=project_root / "data" / "metadata",
            calendar=_project_trading_calendar(project_root),
        )
    global_latest = str(fresh.get("latest_trade_date_in_daily") or "")
    daily_dir = project_root / "data" / "daily"
    latest_by_symbol: dict[str, str] = {}
    for symbol in selected:
        path = daily_dir / f"{symbol}.csv"
        latest = _last_csv_date(path)
        if latest:
            latest_by_symbol[symbol] = latest.isoformat()
    dates = [value for value in latest_by_symbol.values() if value]
    latest_min = min(dates) if dates else ""
    latest_max = max(dates) if dates else ""
    target = str(target_date or latest_max or global_latest or "")
    (
        no_session_dates_by_symbol,
        _session_evidence,
        session_warnings,
    ) = load_verified_no_session_dates(
        selected,
        metadata_dir=project_root / "data" / "metadata",
    )
    effective_latest_by_symbol = {
        symbol: (
            target
            if value and target and (
                value >= target
                or target in no_session_dates_by_symbol.get(symbol, set())
            )
            else value
        )
        for symbol, value in latest_by_symbol.items()
    }
    fresh_symbols = [
        symbol
        for symbol, value in effective_latest_by_symbol.items()
        if value and target and value >= target
    ]
    stale_symbols = [
        symbol
        for symbol, value in effective_latest_by_symbol.items()
        if target and value and value < target
    ]
    missing_symbols = [symbol for symbol in selected if symbol not in latest_by_symbol]
    effective_dates = [
        value for value in effective_latest_by_symbol.values() if value
    ]
    coverage_date = (
        min(effective_dates)
        if effective_dates and len(effective_dates) == len(selected)
        else ""
    )
    verified_no_session_symbols = sorted(
        symbol
        for symbol, values in no_session_dates_by_symbol.items()
        if target in values
    )
    return {
        "latest_by_symbol": latest_by_symbol,
        "latest_local_date": latest_min or (global_latest if not selected else ""),
        "max_local_date": latest_max or (global_latest if not selected else ""),
        "coverage_date": coverage_date,
        "raw_bar_coverage_date": latest_min if dates and len(dates) == len(selected) else "",
        "effective_latest_by_symbol": effective_latest_by_symbol,
        "verified_no_session_symbols": verified_no_session_symbols,
        "verified_no_session_dates_by_symbol": {
            symbol: sorted(values)
            for symbol, values in no_session_dates_by_symbol.items()
        },
        "warnings": session_warnings,
        "fresh_symbols": fresh_symbols,
        "fresh_count": len(fresh_symbols) if selected else 0,
        "stale_count": len(stale_symbols),
        "missing_count": len(missing_symbols),
        "stale_symbols": stale_symbols,
        "missing_symbols": missing_symbols,
        "symbols_checked": selected,
    }


def detect_daily_update_gap(payload: dict[str, Any]) -> dict[str, Any]:
    latest_by_symbol = dict(payload.get("latest_by_symbol") or {})
    target = str(payload.get("target_complete_trade_date") or payload.get("effective_target_date") or payload.get("target_date") or "")
    dates = [str(value) for value in latest_by_symbol.values() if str(value or "")]
    latest_min = min(dates) if dates else str(payload.get("coverage_date") or payload.get("latest_local_date") or "")
    latest_max = max(dates) if dates else str(payload.get("max_local_date") or latest_min or "")
    symbols_checked = list(payload.get("symbols_checked") or latest_by_symbol.keys())
    updated_symbols = [symbol for symbol in symbols_checked if str(latest_by_symbol.get(symbol) or "") >= target] if target else []
    stale_symbols = list(payload.get("stale_symbols") or [symbol for symbol in symbols_checked if symbol not in updated_symbols])
    missing_symbols = list(payload.get("missing_symbols") or [])
    result = dict(payload)
    result.update(
        {
            "coverage_date": latest_min,
            "latest_date_min": latest_min,
            "latest_date_max": latest_max,
            "updated_symbols": updated_symbols,
            "fresh_count": int(payload.get("fresh_count") or len(updated_symbols)),
            "stale_count": int(payload.get("stale_count") or len(stale_symbols)),
            "missing_count": int(payload.get("missing_count") or len(missing_symbols)),
            "stale_symbols": stale_symbols,
            "missing_symbols": missing_symbols,
        }
    )
    return result


def update_daily_prices(**kwargs: Any) -> dict[str, Any]:
    """Compatibility wrapper for the bounded daily update engine."""

    if _engine_update_daily_prices is None:
        return {
            "status": "warning",
            "provider_results": [],
            "updated_symbols": 0,
            "failed_symbols": 0,
            "unchanged_symbols": 0,
            "warnings": ["daily_price_update_engine_unavailable"],
            "research_only": True,
            "no_broker": True,
            "no_live_trading": True,
        }
    return _engine_update_daily_prices(**kwargs)


def run_data_update_preflight(
    root: Path | str | None = None,
    *,
    target_date: str | None = None,
    auto_update: bool = False,
    scope: str = "",
    network: bool = False,
    max_symbols: int = 80,
    dry_run: bool = False,
    provider: str = "local_csv",
    timeout_seconds: int = 30,
    max_retries: int = 1,
    write_mode: str = "dry_run",
    preflight_only: bool = False,
    auto_health: bool = True,
    extra_warnings: list[str] | None = None,
    write: bool = True,
    output_dir: str | Path = "results/data_update",
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    """Run a structured data-update preflight without trading or broker behavior.

    The first positional argument is backward compatible: if it is an existing
    project root, use it as root; otherwise treat it as a comma-separated symbol
    list from ``scripts/run_data_update.py``.
    """
    project_root = _coerce_root(root)
    explicit_symbols = (
        []
        if isinstance(root, Path)
        else _split_symbols(root, max_symbols=None)
    )
    resolved_rows = resolve_symbols(
        explicit_symbols or None,
        max_symbols=0,
        only_low_price=True,
    )
    all_symbols = explicit_symbols or [
        str(row.get("symbol") or "").zfill(6)
        for row in resolved_rows
        if str(row.get("symbol") or "").strip()
    ]
    symbols = (
        all_symbols[:max_symbols]
        if max_symbols and max_symbols > 0
        else list(all_symbols)
    )
    truncated = len(all_symbols) > len(symbols)
    data_scope = "specified_symbols" if explicit_symbols else (scope or "current_analysis_pool")
    scope_source = "explicit_symbols" if explicit_symbols else "current_analysis_pool"
    fresh = freshness_check(project_root)
    daily_status = daily_price_status(
        symbols,
        root=project_root,
        target_date=target_date,
        max_symbols=max_symbols,
        freshness_snapshot=fresh,
    )
    target_complete = target_date or fresh.get("latest_trade_date_in_daily") or ""
    warnings = list(extra_warnings or [])
    if not network:
        warnings.append("network_false_provider_update_skipped")
    if dry_run:
        warnings.append("dry_run_no_data_daily_write")
    if write_mode not in {"dry_run", "staging", "append_new_date"}:
        warnings.append(f"unsupported_write_mode:{write_mode}; treated as dry_run")
    effective_write_mode = "append_new_date" if network and auto_update and not dry_run and write_mode == "dry_run" else write_mode
    update_allowed = bool(network and auto_update and not dry_run and effective_write_mode == "append_new_date")
    status = "dry_run" if dry_run else ("network_disabled_using_local" if not network else "provider_update_configured")
    if preflight_only:
        status = "preflight_only"
    daily_cache_exists = (project_root / "data" / "daily").exists()
    if not daily_cache_exists:
        warnings.append("data/daily missing; data update preflight degraded")
        status = "warning"
    output_path = Path(output_dir) / "data_update_latest.json"
    report_rel = Path(report_path)
    if report_rel.is_absolute():
        try:
            report_rel = report_rel.relative_to(project_root)
        except ValueError as exc:
            LOG.warning("report_path_relative_resolution_failed: path=%s root=%s error=%s", report_rel, project_root, exc)
    payload = {
        "task_id": "v424_data_update_preflight",
        "ok": True,
        "status": status,
        "freshness": fresh,
        "network_requested": bool(network),
        "network": bool(network),
        "provider": provider,
        "timeout_seconds": int(timeout_seconds),
        "max_retries": int(max_retries),
        "target_date": target_complete,
        "target_complete_trade_date": target_complete,
        "effective_target_date": target_complete,
        "target_date_source": "explicit" if target_date else "local_latest",
        "scope": data_scope,
        "data_scope": data_scope,
        "scope_label": data_scope,
        "scope_source": scope_source,
        "requested_count": len(all_symbols),
        "selected_count": len(symbols),
        "truncated": truncated,
        "coverage_scope": (
            "no_universe"
            if not symbols
            else "partial_pool"
            if truncated
            else "explicit_scope"
            if explicit_symbols
            else "current_analysis_pool"
        ),
        "scope_complete": bool(symbols) and not truncated,
        "full_pool_coverage": bool(
            symbols
            and not truncated
            and not explicit_symbols
            and not daily_status.get("is_stale")
        ),
        "data_freshness_status": (
            "no_universe"
            if not symbols
            else str(
                daily_status.get("data_freshness_status")
                or (
                    "stale"
                    if daily_status.get("stale_count")
                    else "fresh"
                )
            )
        ),
        "symbols_preview": symbols[:10],
        "symbols_omitted_count": max(0, len(symbols) - 10),
        "symbols_requested": explicit_symbols,
        "symbols": symbols,
        "symbols_checked": symbols,
        "symbol_count": len(symbols),
        "max_symbols": int(max_symbols),
        "auto_update_enabled": bool(auto_update),
        "update_allowed": update_allowed,
        "dry_run": bool(dry_run),
        "preflight_only": bool(preflight_only),
        "write_mode": effective_write_mode,
        "rows_written": 0,
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": len(symbols),
        "fresh_count": int(daily_status.get("fresh_count") or 0),
        "stale_count": int(daily_status.get("stale_count") or 0),
        "missing_count": int(daily_status.get("missing_count") or 0),
        "full_scan": False,
        "daily_cache_exists": daily_cache_exists,
        "warnings": list(dict.fromkeys(warnings)),
        "failed_reason": "",
        "skipped_reason": "network=false" if not network else ("dry_run" if dry_run else ""),
        "latest_by_symbol": daily_status.get("latest_by_symbol") or {},
        "latest_local_daily_date": daily_status.get("latest_local_date") or (fresh.get("latest_trade_date_in_daily", "") if symbols else ""),
        "latest_local_daily_max_date": daily_status.get("max_local_date") or (fresh.get("latest_trade_date_in_daily", "") if symbols else ""),
        "latest_date_min": daily_status.get("coverage_date") or (fresh.get("latest_trade_date_in_daily", "") if symbols else ""),
        "latest_date_max": daily_status.get("max_local_date") or (fresh.get("latest_trade_date_in_daily", "") if symbols else ""),
        "coverage_date": daily_status.get("coverage_date") or (fresh.get("latest_trade_date_in_daily", "") if symbols else ""),
        "raw_bar_coverage_date": daily_status.get("raw_bar_coverage_date") or "",
        "stale_symbols": daily_status.get("stale_symbols") or [],
        "missing_symbols": daily_status.get("missing_symbols") or [],
        "provider_chain": [provider] if network else [],
        "output_path": str(output_path).replace("\\", "/"),
        "report_path": str(report_rel).replace("\\", "/"),
        "suggested_next_action": "Run work-start gate with --network true after explicit user approval if local data is stale.",
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
        "disclaimer": DISCLAIMER,
    }
    if update_allowed and not preflight_only:
        update_payload = update_daily_prices(
            only_low_price=not bool(explicit_symbols),
            target_date=target_complete or None,
            network=True,
            dry_run=False,
            limit=max_symbols if not explicit_symbols else 0,
            rebuild_features=True,
            symbols=symbols if explicit_symbols else None,
            project_root=project_root,
            daily_dir=project_root / "data" / "daily",
            metadata_dir=project_root / "data" / "metadata",
        )
        after_status = daily_price_status(symbols, root=project_root, target_date=target_complete, max_symbols=max_symbols)
        provider_results = update_payload.get("provider_results") if isinstance(update_payload.get("provider_results"), list) else []
        providers = update_payload.get("providers") if isinstance(update_payload.get("providers"), list) else []
        updated_count = int(update_payload.get("updated_symbols") or 0)
        failed_count = int(update_payload.get("failed_symbols") or 0)
        unchanged_count = int(update_payload.get("unchanged_symbols") or 0)
        stale_after = int(update_payload.get("stale_count_after") if update_payload.get("stale_count_after") is not None else after_status.get("stale_count") or 0)
        missing_after = int(update_payload.get("missing_count_after") if update_payload.get("missing_count_after") is not None else after_status.get("missing_count") or 0)
        fresh_symbol_list_after = (
            list(update_payload.get("fresh_symbol_list_after") or [])
            if isinstance(update_payload.get("fresh_symbol_list_after"), list)
            else list(after_status.get("fresh_symbols") or [])
        )
        stale_symbol_list_after = (
            list(update_payload.get("stale_symbol_list_after") or [])
            if isinstance(update_payload.get("stale_symbol_list_after"), list)
            else list(after_status.get("stale_symbols") or [])
        )
        missing_symbol_list_after = (
            list(update_payload.get("missing_symbol_list_after") or [])
            if isinstance(update_payload.get("missing_symbol_list_after"), list)
            else list(after_status.get("missing_symbols") or [])
        )
        verified_no_session_symbol_list = list(
            update_payload.get("verified_no_session_symbol_list") or []
        )
        fresh_after = (
            len(fresh_symbol_list_after)
            if isinstance(update_payload.get("fresh_symbol_list_after"), list)
            else max(0, len(symbols) - stale_after)
        )
        update_warnings = [str(item) for item in update_payload.get("warnings") or [] if str(item or "")]
        failed_reason = ""
        if failed_count:
            reasons = update_payload.get("failed_symbol_reasons") if isinstance(update_payload.get("failed_symbol_reasons"), dict) else {}
            failed_reason = "; ".join(str(value) for value in reasons.values() if str(value)) or str(update_payload.get("message") or "")
        payload.update(
            {
                "status": str(update_payload.get("status") or ("warning" if stale_after or failed_count else "completed")),
                "provider": "daily_price_update_engine",
                "provider_chain": [str(item) for item in providers] if providers else [str(row.get("provider")) for row in provider_results if row.get("provider")],
                "daily_price_update": update_payload,
                "rows_written": updated_count,
                "success_count": updated_count,
                "failed_count": failed_count,
                "skipped_count": unchanged_count,
                "fresh_count": fresh_after,
                "stale_count": stale_after,
                "missing_count": missing_after,
                "latest_by_symbol": update_payload.get("latest_by_symbol_after") or after_status.get("latest_by_symbol") or payload.get("latest_by_symbol") or {},
                "latest_local_daily_date": update_payload.get("latest_local_date_after") or after_status.get("latest_local_date") or payload.get("latest_local_daily_date") or "",
                "latest_local_daily_max_date": update_payload.get("max_local_date_after") or after_status.get("max_local_date") or payload.get("latest_local_daily_max_date") or "",
                "latest_date_min": update_payload.get("coverage_date_after") or after_status.get("coverage_date") or payload.get("latest_date_min") or "",
                "latest_date_max": update_payload.get("max_local_date_after") or after_status.get("max_local_date") or payload.get("latest_date_max") or "",
                "coverage_date": update_payload.get("coverage_date_after") or after_status.get("coverage_date") or payload.get("coverage_date") or "",
                "raw_bar_coverage_date": update_payload.get("raw_bar_coverage_date_after") or update_payload.get("latest_local_date_after") or after_status.get("latest_local_date") or "",
                "fresh_symbols": fresh_symbol_list_after,
                "stale_symbols": stale_symbol_list_after,
                "missing_symbols": missing_symbol_list_after,
                "verified_no_session_symbols": verified_no_session_symbol_list,
                "failed_symbols": update_payload.get("failed_symbol_list") or [],
                "updated_symbols": update_payload.get("updated_symbol_list") or [],
                "warnings": list(dict.fromkeys([*payload.get("warnings", []), *update_warnings])),
                "failed_reason": failed_reason,
                "skipped_reason": "",
                "suggested_next_action": "Run data-health --network false to verify local daily freshness after the provider update.",
            }
        )
    return payload


def _last_csv_date(path: Path) -> date | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            chunk = handle.read().decode("utf-8-sig", errors="ignore")
        lines = [line for line in chunk.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        last = lines[-1].split(",", 1)[0]
        return datetime.strptime(last, "%Y-%m-%d").date()
    except Exception as exc:  # noqa: BLE001 - cache probe must not break status pages
        LOG.warning("last_csv_date_read_failed: path=%s error=%s", path, exc)
        return None


def _cached_latest_daily_date(root: Path) -> date | None:
    cache_paths = [
        root / "results/data_update/v424_app_open_refresh_latest.json",
        root / "results/daily_research/v424_open_app_result_page_latest.json",
        root / "results/data_update/v424_market_session_latest.json",
        root / "results/data_update/v424_post_close_daily_update_latest.json",
    ]
    today = datetime.now().date()
    for path in cache_paths:
        try:
            if not path.exists() or datetime.fromtimestamp(path.stat().st_mtime).date() != today:
                continue
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            candidates = [
                payload.get("current_data_date"),
                payload.get("latest_date_after"),
                payload.get("latest_local_trade_date"),
                (payload.get("open_app_result_page") or {}).get("current_data_date"),
                (payload.get("gate") or {}).get("session", {}).get("latest_local_trade_date"),
            ]
            for value in candidates:
                text = str(value or "")[:10]
                if not text:
                    continue
                parsed = datetime.strptime(text, "%Y-%m-%d").date()
                if parsed <= today:
                    return parsed
        except Exception as exc:  # noqa: BLE001 - stale cache entries are optional hints
            LOG.warning("cached_latest_daily_date_probe_failed: path=%s error=%s", path, exc)
            continue
    return None


def latest_daily_date(root: Path, max_files: int | None = None) -> tuple[date | None, int]:
    if max_files is None:
        cached = _cached_latest_daily_date(root)
        if cached:
            return cached, 0
    files = sorted((root / "data/daily").glob("*.csv"))
    if max_files:
        files = files[:max_files]
    latest = None
    scanned = 0
    for path in files:
        d = _last_csv_date(path)
        scanned += 1
        if d and (latest is None or d > latest):
            latest = d
    return latest, scanned


def latest_benchmark_date(root: Path) -> date | None:
    latest = None
    for path in (root / "data/benchmark").glob("*.csv"):
        d = _last_csv_date(path)
        if d and (latest is None or d > latest):
            latest = d
    return latest


def freshness_check(root: Path, today: date | None = None) -> dict[str, Any]:
    provided_today = today
    today = today or datetime.now().date()
    daily_date, scanned = latest_daily_date(root)
    bench_date = latest_benchmark_date(root)
    metadata_files = list((root / "data/metadata").glob("*.csv"))
    metadata_latest_mtime = max((p.stat().st_mtime for p in metadata_files), default=0)
    metadata_date = datetime.fromtimestamp(metadata_latest_mtime).date() if metadata_latest_mtime else None
    # Calendar-day staleness + lenient status are preserved unchanged so existing
    # consumers keep their semantics.
    stale_days = (today - daily_date).days if daily_date else None
    bench_stale = (today - bench_date).days if bench_date else None
    metadata_stale = (today - metadata_date).days if metadata_date else None
    status = "data_fresh" if stale_days is not None and stale_days <= 3 else "network_disabled_using_stale_local_with_warning"
    # The expected "latest COMPLETE daily bar" is session-aware: before today's
    # close it is yesterday's completed bar, NOT today's (which does not exist
    # yet). Previously this reported a bare calendar `today`, which made the data
    # health layer judge a perfectly fresh latest-completed bar as stale/red.
    expected_date = today
    if _expected_settlement_date is not None:
        try:
            if provided_today is None:
                ref_dt = None
            else:
                ref_dt = datetime.combine(provided_today, datetime.min.time()).replace(hour=12)
                if _to_shanghai_datetime is not None:
                    ref_dt = _to_shanghai_datetime(ref_dt)
            settled = _expected_settlement_date(ref_dt)
            if settled is not None:
                expected_date = settled
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("expected_settlement_date_failed: %s", exc)
            expected_date = today
    trading_day_staleness = max(0, (expected_date - daily_date).days) if daily_date else None
    return {
        "task_id": "v420_data_freshness",
        "status": status,
        "local_first": True,
        "freshness_required": True,
        "network_update_optional": True,
        "provider_timeout_seconds": 30,
        "max_retries": 1,
        "no_background_process": True,
        "stale_data_warning_required": status != "data_fresh",
        "latest_trade_date_in_daily": daily_date.isoformat() if daily_date else "",
        "latest_expected_trade_date": expected_date.isoformat(),
        "calendar_today": today.isoformat(),
        "data_staleness_days": stale_days,
        "trading_day_staleness": trading_day_staleness,
        "data_fresh_for_latest_trading_day": (trading_day_staleness == 0) if trading_day_staleness is not None else False,
        "benchmark_staleness_days": bench_stale,
        "metadata_staleness_days": metadata_stale,
        "daily_files_scanned": scanned,
    }


def update_attempt(root: Path, network: bool = False) -> dict[str, Any]:
    fresh = freshness_check(root)
    if fresh["status"] == "data_fresh":
        status = "data_fresh"
        fallback = ""
    elif not network:
        status = "network_disabled_using_stale_local_with_warning"
        fallback = "network disabled; using local stale data with explicit warning"
    else:
        status = "data_stale_update_failed_fallback_local"
        fallback = "provider update is intentionally bounded; fallback local data used"
    return {
        "task_id": "v420_data_update_attempt",
        "status": status,
        "attempted_network_update": bool(network),
        "provider_error_report": [] if not network else ["provider_update_not_configured_in_research_only_lab"],
        "fallback_reason": fallback,
        "freshness": fresh,
    }


def build_stale_data_warning(
    *,
    target_date: str | None = None,
    symbols: Any = None,
    max_symbols: int = 80,
) -> dict[str, Any]:
    root = PROJECT_ROOT
    selected = _split_symbols(symbols, max_symbols=max_symbols)
    latest, scanned = latest_daily_date(root, max_files=max_symbols)
    target_text = str(target_date or "")
    target = None
    if target_text:
        try:
            target = datetime.strptime(target_text[:10], "%Y-%m-%d").date()
        except Exception as exc:  # noqa: BLE001 - invalid user input should degrade to visible warning
            LOG.warning("stale_warning_target_date_parse_failed: target=%r error=%s", target_text, exc)
            target = None
    stale = bool(target and (latest is None or latest < target))
    status = "warning" if stale else "passed"
    message = (
        f"local data latest {latest.isoformat() if latest else 'missing'} is older than target {target.isoformat()}"
        if stale and target
        else "local daily data is available for the requested target or target is not specified"
    )
    return {
        "status": status,
        "stale": stale,
        "target_date": target.isoformat() if target else target_text,
        "latest_daily_date": latest.isoformat() if latest else "",
        "daily_files_scanned": scanned,
        "symbols": selected,
        "symbol_count": len(selected),
        "max_symbols": int(max_symbols or 0),
        "message": message,
        "research_only": True,
        "paper_only": True,
        "external_execution_locked": True,
    }


def build_workbench_data_update(project_root: Path | str | None = None) -> dict[str, Any]:
    """Build readonly data-update workbench payload.

    The workbench is intentionally observational: network defaults to false and
    post-close writes remain behind explicit bounded provider commands.
    """

    root = _coerce_root(project_root)
    fresh = freshness_check(root)
    symbol_selection = select_update_symbols(None, max_symbols=80)
    status = "passed" if fresh.get("status") == "data_fresh" else "warning"
    return {
        "task_id": "workbench_data_update",
        "ok": True,
        "status": status,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "network_default": False,
        "auto_write_default": False,
        "freshness": fresh,
        "symbol_selection": symbol_selection,
        "suggested_commands": [
            "python scripts/qd.py data-health --network false --json true --summary true",
            "python scripts/qd.py data-update --network false --scope current_analysis_pool --max-symbols 80",
        ],
        "warnings": [] if status == "passed" else ["local_daily_data_not_fresh_or_incomplete"],
        "research_only": True,
        "paper_only": True,
        "external_execution_locked": True,
        "candidate_upgrade_allowed": False,
        "not_investment_advice": True,
        "not_position_advice": True,
        "broker_route_locked": True,
        "live_execution_locked": True,
        "qmt_order_route_locked": True,
    }
