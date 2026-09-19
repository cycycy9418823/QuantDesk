"""First-run data bootstrap for QuantDesk clean zips.

The bootstrap command creates a tiny local learning universe and, only when the
user explicitly enables network access, attempts to fetch daily bars for that
small set. It never scans the full A-share market and never fabricates market
data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_price_providers import provider_availability
from quantdesk.daily_price_update_engine import DEFAULT_PROVIDERS, update_daily_prices
from quantdesk.learning_universe import (
    OUTPUT_COLUMNS,
    low_price_universe_path,
    save_low_price_universe,
)
from quantdesk.shared import now_text, write_json
from quantdesk.trading_calendar import resolve_target_complete_trade_date
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_JSON = PROJECT_ROOT / "results" / "data_bootstrap" / "data_bootstrap_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "data_bootstrap_report_latest.md"
DEMO_SYMBOLS = ("600000", "000001", "600519", "300750")
DISCLAIMER = (
    "Local learning and paper-simulation bootstrap only. QuantDesk does not "
    "connect to brokers, place real orders, or provide investment advice."
)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _normalize_symbols(symbols: str | list[str] | tuple[str, ...] | None, *, preset: str) -> list[str]:
    if symbols:
        raw = symbols.split(",") if isinstance(symbols, str) else list(symbols)
    elif preset == "low_price_demo":
        raw = list(DEMO_SYMBOLS)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        text = "".join(ch for ch in str(item or "").strip() if ch.isdigit())
        if len(text) >= 6:
            text = text[-6:]
        if text and text not in out:
            out.append(text)
    return out


def _demo_universe_frame(symbols: list[str]) -> pd.DataFrame:
    names = {
        "600000": "SPDB",
        "000001": "Ping An Bank",
        "600519": "Kweichow Moutai",
        "300750": "CATL",
    }
    rows = [
        {
            "symbol": symbol,
            "name": names.get(symbol, symbol),
            "price": pd.NA,
            "amount": pd.NA,
            "included": True,
            "in_buffer": False,
            "excluded_reason": "",
            "source": "data_bootstrap_demo",
            "updated_at": now_text(),
        }
        for symbol in symbols
    ]
    return pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS))


def _provider_dependency_summary() -> list[dict[str, Any]]:
    return [provider_availability(name) for name in DEFAULT_PROVIDERS]


def _is_demo_safe_to_overwrite(universe_path: Path, selected: list[str]) -> bool:
    """Return True only when replacing the on-disk universe with a demo frame is safe.

    A demo overwrite is safe only if the existing file is itself demo-scale:
    it has no more rows than the demo set we are about to write AND every row
    that carries a ``source`` is a ``data_bootstrap_demo`` row. Any file that is
    larger, or that carries a non-demo provenance, is treated as real production
    data and must NOT be clobbered by the tiny demo universe. Unreadable/empty
    files are safe to (re)seed.
    """

    existing = read_csv_frame(universe_path, dtype=str)
    if existing.empty:
        return True
    # More rows than the demo set → real data.
    if len(existing) > max(len(selected), len(DEMO_SYMBOLS)):
        return False
    # Any non-demo provenance → real data.
    if "source" in existing.columns:
        sources = {str(v).strip() for v in existing["source"].dropna().tolist()}
        sources.discard("")
        if sources - {"data_bootstrap_demo"}:
            return False
    return True


def build_data_bootstrap(
    *,
    preset: str = "low_price_demo",
    symbols: str | list[str] | tuple[str, ...] | None = None,
    start_date: str = "2023-01-01",
    end_date: str = "",
    adjust: str = "qfq",
    network: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    selected = _normalize_symbols(symbols, preset=preset)
    warnings: list[str] = []
    failed_symbols: list[str] = []
    failed_reason = ""
    created_universe = False
    update_payload: dict[str, Any] = {}
    target_info = resolve_target_complete_trade_date(end_date or None)
    end = str(target_info.get("target_complete_trade_date") or target_info.get("target_date") or now_text()[:10])[:10]
    warnings.extend(str(item) for item in (target_info.get("target_date_warnings") or []) if item)

    if not selected:
        failed_reason = "no_symbols_selected"
        warnings.append("No demo symbols were selected. Pass --symbols or use --preset low_price_demo.")
        status = "warning"
    else:
        universe_path = low_price_universe_path()
        explicit_symbols = bool(symbols)
        blocked_overwrite = False
        if universe_path.exists() and not _is_demo_safe_to_overwrite(universe_path, selected):
            # Guard: never let a tiny demo universe clobber a real, larger
            # production universe on disk. This is the P0 data-accident source
            # (2026-07-02) where data-bootstrap shrank the live universe to a
            # single demo row and the shrink then cascaded into features.
            blocked_overwrite = True
            warnings.append(
                "refused_to_overwrite_real_universe: the existing "
                f"{_display_path(universe_path)} looks like real data (more rows "
                "than the demo set or a non-demo source); data-bootstrap will not "
                "replace it with demo rows. Delete it explicitly if you truly want "
                "to re-seed the demo universe."
            )
        if not blocked_overwrite and (explicit_symbols or not universe_path.exists()):
            save_low_price_universe(_demo_universe_frame(selected), universe_path)
            created_universe = True
        elif not blocked_overwrite:
            warnings.append("A local universe already exists; data-bootstrap did not overwrite it.")
        status = "initialized" if not network else "warning"

        if network:
            if start_date:
                warnings.append("start_date is accepted for audit, but the current daily update chain performs target-date incremental refresh.")
            if adjust:
                warnings.append("adjust is accepted for audit, but the current provider update chain does not directly apply it yet.")
            try:
                update_payload = update_daily_prices(
                    only_low_price=True,
                    target_date=end,
                    providers=list(DEFAULT_PROVIDERS),
                    network=True,
                    dry_run=False,
                    rebuild_features=False,
                    symbols=selected,
                )
                failed_symbols = list(update_payload.get("failed_symbol_list") or [])
                updated_count = int(update_payload.get("updated_symbols") or 0)
                if failed_symbols:
                    status = "warning"
                    failed_reason = str(update_payload.get("failed_reason") or "provider_failed_or_incomplete")
                elif updated_count > 0:
                    status = "passed"
                else:
                    status = "warning"
                    failed_reason = str(update_payload.get("message") or "provider_returned_no_new_daily_data")
            except Exception as exc:  # noqa: BLE001
                status = "warning"
                failed_reason = f"provider_error:{exc}"
                warnings.append(f"Network daily-data fetch failed: {exc}")
        else:
            warnings.append("network=false: created or checked the local demo universe only; no real market data was fetched.")

    provider_summary = _provider_dependency_summary()
    if network and all(str(row.get("status")) != "available" for row in provider_summary):
        warnings.append("Optional data providers are not available. Install requirements-data.txt before retrying network data updates.")
        failed_reason = failed_reason or "provider_not_available"

    updated_daily_count = int(update_payload.get("updated_symbols") or 0)
    universe_ready = created_universe or low_price_universe_path().exists()
    suggested = (
        "python scripts/qd.py data-health --network false --scope current_analysis_pool"
        if universe_ready
        else "python scripts/qd.py data-bootstrap --preset low_price_demo --network false"
    )
    payload = {
        "ok": status != "failed",
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "data_bootstrap",
        "status": status,
        "preset": preset,
        "symbols": selected,
        "symbol_count": len(selected),
        "network": bool(network),
        "adjust": adjust,
        "start_date": start_date,
        "end_date": end,
        "created_universe": bool(created_universe),
        "universe_path": _display_path(low_price_universe_path()),
        "updated_daily_count": updated_daily_count,
        "daily_ready": bool(updated_daily_count > 0),
        "bootstrap_mode": "network_daily_update" if network else "offline_universe_only",
        "next_required_step": "data-health" if network and updated_daily_count > 0 else "network_data_update_or_manual_import",
        "failed_symbols": failed_symbols,
        "failed_reason": failed_reason,
        "provider_status": provider_summary,
        "daily_update": update_payload,
        "warnings": warnings,
        "suggested_next_action": suggested,
        "output_paths": {
            "json": _display_path(RESULT_JSON),
            "report": _display_path(REPORT_MD),
        },
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_data_bootstrap(payload)
    return payload


def build_data_bootstrap_report(payload: dict[str, Any]) -> str:
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    failed = payload.get("failed_symbols") if isinstance(payload.get("failed_symbols"), list) else []
    warning_lines = [f"- {item}" for item in warnings] or ["- none"]
    return "\n".join(
        [
            "# QuantDesk Data Bootstrap",
            "",
            DISCLAIMER,
            "",
            f"- status: `{payload.get('status')}`",
            f"- preset: `{payload.get('preset')}`",
            f"- network: `{str(payload.get('network')).lower()}`",
            f"- symbols: `{', '.join(payload.get('symbols') or []) or '-'}`",
            f"- created_universe: `{payload.get('created_universe')}`",
            f"- universe_path: `{payload.get('universe_path')}`",
            f"- updated_daily_count: `{payload.get('updated_daily_count')}`",
            f"- daily_ready: `{payload.get('daily_ready')}`",
            f"- bootstrap_mode: `{payload.get('bootstrap_mode')}`",
            f"- failed_symbols: `{', '.join(failed) or '-'}`",
            f"- failed_reason: `{payload.get('failed_reason') or '-'}`",
            "",
            "## Suggested Next Action",
            "",
            f"`{payload.get('suggested_next_action')}`",
            "",
            "## Warnings",
            *warning_lines,
            "",
            "Offline mode only creates or checks local structure. It does not fetch real market data. To fetch daily bars, install `requirements-data.txt` and explicitly pass `--enable-network`.",
        ]
    ) + "\n"


def write_data_bootstrap(payload: dict[str, Any]) -> dict[str, str]:
    write_json(RESULT_JSON, payload)
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(build_data_bootstrap_report(payload), encoding="utf-8")
    return {"json": _display_path(RESULT_JSON), "report": _display_path(REPORT_MD)}
