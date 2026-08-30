from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import normalize_symbol, safe_float, truthy, write_json as shared_write_json  # noqa: E402

DAILY_DIR = PROJECT_ROOT / "data" / "daily"
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"
RESULTS_DIR = PROJECT_ROOT / "results" / "universe"
REPORTS_DIR = PROJECT_ROOT / "reports" / "universe"
LOW_PRICE_UNIVERSE = UNIVERSE_DIR / "low_price_learning_symbols.csv"

BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "external_execution_locked": True,
    "candidate_upgrade_allowed": False,
    "not_investment_advice": True,
    "not_position_advice": True,
    "broker_route_locked": True,
    "live_execution_locked": True,
    "qmt_order_route_locked": True,
    "no_broker": True,
    "no_live_trading": True,
    "no_auto_execution": True,
    "no_buy_sell_advice": True,
    "no_target_price": True,
    "no_real_money_allocation": True,
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    shared_write_json(path, payload)


def _write_md(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# QuantDesk Build Universe",
        "",
        "Research-only. No broker, no live trading, no auto execution, no buy/sell advice, no target price, no real-money allocation.",
        "",
        f"- status: {payload.get('status')}",
        f"- mode: {payload.get('mode')}",
        f"- symbol_count: {payload.get('symbol_count')}",
        f"- daily_file_count: {payload.get('daily_file_count')}",
        f"- demo_universe_only: {str(payload.get('demo_universe_only')).lower()}",
        f"- latest_date: {payload.get('latest_date') or '-'}",
        f"- warnings: {', '.join(payload.get('warnings') or []) or '-'}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _last_daily_row(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as handle:
            header_line = handle.readline().decode("utf-8-sig", errors="replace").strip()
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            tail = handle.read().decode("utf-8-sig", errors="replace")
        lines = [line for line in tail.splitlines() if line.strip()]
        if not lines:
            return {}
        last_line = lines[-1]
        if last_line == header_line and len(lines) > 1:
            last_line = lines[-2]
        header = next(csv.reader([header_line]))
        values = next(csv.reader([last_line]))
        return {str(k or "").strip(): str(v or "").strip() for k, v in zip(header, values)}
    except Exception:
        return {}


def _cell(row: dict[str, str], *names: str) -> str:
    lookup = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = lookup.get(name.lower())
        if value not in {None, ""}:
            return str(value)
    return ""


def _to_float(value: str) -> float | None:
    return safe_float(value)


def _daily_file_count() -> int:
    return sum(1 for _path in DAILY_DIR.glob("*.csv"))


def _daily_symbols(symbols: set[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if symbols is None:
        paths = sorted(DAILY_DIR.glob("*.csv"))
    else:
        wanted = sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)})
        paths = [DAILY_DIR / f"{symbol}.csv" for symbol in wanted]
    for path in paths:
        if not path.exists():
            continue
        symbol = normalize_symbol(path.stem)
        if not symbol:
            continue
        last = _last_daily_row(path)
        close = _to_float(_cell(last, "close", "qfq_close", "adj_close"))
        date = _cell(last, "date", "trade_date", "datetime")
        rows.append(
            {
                "symbol": symbol,
                "name": symbol,
                "daily_path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "latest_date": date,
                "close": close,
                "has_daily_file": True,
            }
        )
    return rows


def _low_price_symbols_from_csv() -> set[str]:
    if not LOW_PRICE_UNIVERSE.exists():
        return set()
    out: set[str] = set()
    try:
        with LOW_PRICE_UNIVERSE.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if truthy(str(row.get("included", ""))):
                    symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("ts_code"))
                    if symbol:
                        out.add(symbol)
    except Exception:
        return set()
    return out


def build_universe(*, mode: str, price_limit: float | None, network: bool) -> dict[str, Any]:
    warnings: list[str] = []
    daily_file_count = _daily_file_count()
    if network:
        warnings.append("network_requested_but_not_used_build_universe_is_local_read_only")
    if mode == "full_a":
        daily_rows = _daily_symbols()
        selected = daily_rows
        source = "data_daily_all_local_csv"
    elif mode == "low_price":
        csv_symbols = _low_price_symbols_from_csv()
        selected = _daily_symbols(csv_symbols)
        source = "low_price_learning_symbols_included_intersect_data_daily"
        if price_limit is not None:
            daily_rows = _daily_symbols()
            filtered = [row for row in daily_rows if row.get("close") is not None and float(row["close"]) <= price_limit]
            if len(filtered) > len(selected):
                selected = filtered
                source = "data_daily_price_filter_fallback"
                warnings.append("low_price_learning_symbols_sparse_used_local_price_filter_fallback")
    else:
        selected = []
        source = "unknown"
        warnings.append(f"unsupported_mode:{mode}")
    latest_dates = sorted({str(row.get("latest_date") or "") for row in selected if row.get("latest_date")})
    symbol_count = len(selected)
    demo_universe_only = symbol_count <= 4
    if demo_universe_only:
        warnings.append("universe_symbol_count_at_or_below_demo_threshold")
    payload = {
        "task_type": "build_universe",
        "status": "completed" if selected and not demo_universe_only else "completed_with_warnings",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "network": bool(network),
        "source": source,
        "full_scan": mode == "full_a",
        "symbol_count": symbol_count,
        "daily_file_count": daily_file_count,
        "demo_universe_only": demo_universe_only,
        "price_limit": price_limit,
        "latest_date": latest_dates[-1] if latest_dates else "",
        "sample_symbols": selected[:20],
        "warnings": warnings,
        "data_daily_modified": False,
        "canonical_equity_overwritten": False,
        **BOUNDARY,
    }
    _write_json(RESULTS_DIR / "build_universe_latest.json", payload)
    _write_json(RESULTS_DIR / f"build_universe_{mode}_latest.json", payload)
    _write_md(REPORTS_DIR / "build_universe_latest.md", payload)
    _write_md(REPORTS_DIR / f"build_universe_{mode}_latest.md", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local research-only universe summary from existing data/daily files.")
    parser.add_argument("--mode", default="low_price", choices=["full_a", "low_price"], help="Default low_price reads only the local included=true learning pool; use full_a explicitly for an all-local-daily summary.")
    parser.add_argument("--price-limit", type=float, default=None)
    parser.add_argument("--network", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = build_universe(mode=args.mode, price_limit=args.price_limit, network=truthy(args.network))
    wants_json = truthy(args.json) or truthy(args.summary)
    if wants_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if payload.get("symbol_count", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
