"""Bounded startup data update for the local QuantDesk app.

This entrypoint is intentionally narrow:
- explicit network authorization is required via ``--enable-network``;
- the scope stays on the current analysis pool / low-price included symbols;
- no broker, order, live execution, or position advice path is touched.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.daily_price_update_engine import update_daily_prices
from quantdesk.daily_candidate_generator import generate_daily_recommendations
from quantdesk.market_clock import expected_settlement_date, to_shanghai_datetime
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json


BOUNDARY: dict[str, bool] = {
    "research_only": True,
    "paper_only": True,
    "no_broker": True,
    "no_live_trading": True,
    "no_auto_execution": True,
    "no_buy_sell_advice": True,
    "no_position_advice": True,
    "no_real_money_allocation": True,
}


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_providers(value: str | None) -> list[str] | None:
    if not value:
        return None
    providers = [item.strip() for item in value.split(",") if item.strip()]
    return providers or None


def resolve_startup_target_date(target_date: str | None = None) -> tuple[str | None, str]:
    if target_date:
        return target_date, "explicit"
    expected = expected_settlement_date(to_shanghai_datetime())
    if expected is None:
        return None, "unresolved"
    return expected.isoformat(), "latest_completed_market_session"


def read_candidate_ledger_symbols(root: Path, *, limit: int = 0) -> list[str]:
    path = root / "data" / "tracking" / "candidate_ledger.csv"
    if not path.exists():
        return []
    rows: list[tuple[str, str]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = normalize_symbol(row.get("ts_code") or row.get("symbol") or row.get("code"))
                if not symbol:
                    continue
                rows.append((str(row.get("entry_date") or ""), symbol))
    except Exception:
        return []
    # Candidate ledgers are usually newest first, but sorting by entry_date
    # keeps startup refresh aligned with the current visible candidate pool.
    rows.sort(key=lambda item: item[0], reverse=True)
    symbols: list[str] = []
    seen: set[str] = set()
    for _, symbol in rows:
        if symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
        if limit and len(symbols) >= limit:
            break
    return symbols


def _status_from_update(update_result: dict[str, Any], *, enable_network: bool) -> str:
    update_status = str(update_result.get("status") or "").strip()
    warning_kind = str(update_result.get("warning_kind") or "").strip()
    total_symbols = int(update_result.get("total_symbols") or 0)
    stale_after = int(update_result.get("stale_count_after") or 0)
    missing_after = int(update_result.get("missing_count_after") or 0)
    failed_symbols = int(update_result.get("failed_symbols") or 0)
    if not enable_network:
        return "skipped_network_disabled"
    if warning_kind == "no_universe" or total_symbols == 0:
        return "completed_with_warnings"
    if update_status in {"completed", "warning"} and not (stale_after or missing_after or failed_symbols):
        return "completed"
    if update_status in {"completed", "warning", "partial", "failed", "blocked", "skipped_network_false"}:
        return "completed_with_warnings"
    return update_status or "completed_with_warnings"


def _compact_update(update_result: dict[str, Any]) -> dict[str, Any]:
    provider_results = update_result.get("provider_results")
    if not isinstance(provider_results, list):
        provider_results = []
    return {
        "status": update_result.get("status", ""),
        "data_freshness_status": update_result.get("data_freshness_status", ""),
        "target_date": update_result.get("target_date", ""),
        "latest_local_date_before": update_result.get("latest_local_date_before", ""),
        "latest_local_date_after": update_result.get("latest_local_date_after", ""),
        "total_symbols": update_result.get("total_symbols", 0),
        "processed_symbols": update_result.get("processed_symbols", []),
        "updated_symbols": update_result.get("updated_symbols", 0),
        "unchanged_symbols": update_result.get("unchanged_symbols", 0),
        "failed_symbols": update_result.get("failed_symbols", 0),
        "stale_count_before": update_result.get("stale_count_before", 0),
        "stale_count_after": update_result.get("stale_count_after", 0),
        "missing_count_before": update_result.get("missing_count_before", 0),
        "missing_count_after": update_result.get("missing_count_after", 0),
        "provider_results": provider_results,
        "failed_symbol_reasons": update_result.get("failed_symbol_reasons", {}),
        "warnings": update_result.get("warnings", []),
        "errors": update_result.get("errors", []),
        "message": update_result.get("message", ""),
        "process_note": update_result.get("process_note", ""),
        "daily_features": update_result.get("daily_features", {}),
    }


def build_startup_payload(
    *,
    root: Path,
    enable_network: bool,
    max_symbols: int,
    target_date: str | None,
    providers: list[str] | None,
    rebuild_features: bool,
    dry_run: bool,
    update_result: dict[str, Any],
    candidate_generation: dict[str, Any] | None = None,
    target_date_source: str = "",
    symbol_source: str = "",
    requested_symbols: list[str] | None = None,
) -> dict[str, Any]:
    compact = _compact_update(update_result)
    status = _status_from_update(update_result, enable_network=enable_network)
    return {
        "task_id": "startup_data_update",
        "status": status,
        "startup_update_attempted": bool(enable_network and not dry_run),
        "provider_network_authorized": bool(enable_network),
        "network": bool(enable_network),
        "dry_run": bool(dry_run or not enable_network),
        "scope": "current_analysis_pool",
        "symbol_source": symbol_source,
        "requested_symbols": list(requested_symbols or []),
        "only_low_price": True,
        "full_scan": False,
        "max_symbols": max(0, int(max_symbols or 0)),
        "target_date": compact.get("target_date") or target_date or "",
        "target_date_source": target_date_source,
        "providers": providers or update_result.get("providers", []),
        "rebuild_features": bool(rebuild_features),
        "updated_symbols": compact.get("updated_symbols", 0),
        "failed_symbols": compact.get("failed_symbols", 0),
        "stale_count_after": compact.get("stale_count_after", 0),
        "missing_count_after": compact.get("missing_count_after", 0),
        "latest_local_date_after": compact.get("latest_local_date_after", ""),
        "message": compact.get("message", ""),
        "update_result": compact,
        "candidate_generation": candidate_generation or {
            "status": "skipped",
            "candidate_count": 0,
            "new_ledger_rows": 0,
            "message_for_ui": "本次启动只更新行情，未运行每日研究候选生成。",
        },
        "path": "results/startup_update_latest.json",
        "updated_at": now_text(),
        "disclaimer": "Startup update is bounded to local research data. It is not investment advice, not position advice, and never connects to brokers or places orders.",
        **BOUNDARY,
    }


def write_startup_payload(root: Path, payload: dict[str, Any]) -> Path:
    return atomic_write_json(root / "results" / "startup_update_latest.json", payload)


def run_startup_data_update(
    root: Path,
    *,
    enable_network: bool,
    max_symbols: int = 80,
    target_date: str | None = None,
    providers: list[str] | None = None,
    rebuild_features: bool = True,
    dry_run: bool = False,
    generate_candidates: bool = False,
    candidate_max_scan_symbols: int = 300,
    candidate_max_candidates: int = 18,
) -> dict[str, Any]:
    max_symbols = max(0, int(max_symbols or 0))
    network = bool(enable_network and not dry_run)
    effective_target_date, target_date_source = resolve_startup_target_date(target_date)
    candidate_symbols = read_candidate_ledger_symbols(root, limit=max_symbols)
    symbol_source = "candidate_ledger" if candidate_symbols else "low_price_learning_symbols"
    update_result = update_daily_prices(
        only_low_price=True,
        target_date=effective_target_date,
        providers=providers,
        workers=4,
        network=network,
        dry_run=not network,
        limit=max_symbols,
        rebuild_features=bool(rebuild_features),
        symbols=candidate_symbols or None,
    )
    candidate_generation: dict[str, Any] | None = None
    if generate_candidates and not dry_run:
        try:
            candidate_generation = generate_daily_recommendations(
                root,
                max_scan_symbols=max(1, int(candidate_max_scan_symbols or 1)),
                max_candidates=max(1, int(candidate_max_candidates or 1)),
                append_ledger=True,
            )
        except Exception as exc:  # noqa: BLE001
            candidate_generation = {
                "task_id": "daily_candidate_generation",
                "status": "failed",
                "candidate_count": 0,
                "new_ledger_rows": 0,
                "message_for_ui": f"每日研究候选生成失败：{type(exc).__name__}: {exc}",
                "error": str(exc),
                **BOUNDARY,
            }
    payload = build_startup_payload(
        root=root,
        enable_network=enable_network,
        max_symbols=max_symbols,
        target_date=effective_target_date,
        providers=providers,
        rebuild_features=rebuild_features,
        dry_run=dry_run,
        update_result=update_result,
        candidate_generation=candidate_generation,
        target_date_source=target_date_source,
        symbol_source=symbol_source,
        requested_symbols=candidate_symbols,
    )
    write_startup_payload(root, payload)
    return payload


def build_failure_payload(root: Path, exc: BaseException, *, enable_network: bool, max_symbols: int) -> dict[str, Any]:
    message = f"{type(exc).__name__}: {exc}"
    return {
        "task_id": "startup_data_update",
        "status": "failed",
        "startup_update_attempted": bool(enable_network),
        "provider_network_authorized": bool(enable_network),
        "network": bool(enable_network),
        "dry_run": not bool(enable_network),
        "scope": "current_analysis_pool",
        "only_low_price": True,
        "full_scan": False,
        "max_symbols": max(0, int(max_symbols or 0)),
        "target_date": "",
        "updated_symbols": 0,
        "failed_symbols": 0,
        "stale_count_after": 0,
        "missing_count_after": 0,
        "latest_local_date_after": "",
        "message": message,
        "error": message,
        "traceback": traceback.format_exc(limit=8),
        "path": "results/startup_update_latest.json",
        "updated_at": now_text(),
        "disclaimer": "Startup update failed visibly. The app may open with local cached data only.",
        **BOUNDARY,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded QuantDesk startup data update.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--enable-network", action="store_true", help="Explicitly allow provider network fetches.")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--target-date", default="")
    parser.add_argument("--providers", default="")
    parser.add_argument("--rebuild-features", default="true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--generate-candidates", default="false", help="Run bounded local daily candidate generation after data update.")
    parser.add_argument("--candidate-max-scan-symbols", type=int, default=300)
    parser.add_argument("--candidate-max-candidates", type=int, default=18)
    parser.add_argument("--json", default="true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    providers = _split_providers(args.providers)
    try:
        payload = run_startup_data_update(
            root,
            enable_network=bool(args.enable_network),
            max_symbols=args.max_symbols,
            target_date=args.target_date or None,
            providers=providers,
            rebuild_features=parse_bool(args.rebuild_features),
            dry_run=bool(args.dry_run),
            generate_candidates=parse_bool(args.generate_candidates),
            candidate_max_scan_symbols=args.candidate_max_scan_symbols,
            candidate_max_candidates=args.candidate_max_candidates,
        )
        exit_code = 0
    except Exception as exc:  # noqa: BLE001
        payload = build_failure_payload(root, exc, enable_network=bool(args.enable_network), max_symbols=args.max_symbols)
        write_startup_payload(root, payload)
        exit_code = 1

    if parse_bool(args.json):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(
            "startup_update status={status} updated={updated} stale_after={stale} missing_after={missing}".format(
                status=payload.get("status", ""),
                updated=payload.get("updated_symbols", 0),
                stale=payload.get("stale_count_after", 0),
                missing=payload.get("missing_count_after", 0),
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
