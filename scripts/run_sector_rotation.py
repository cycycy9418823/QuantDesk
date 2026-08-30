from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.sector_rotation_engine import (  # noqa: E402
    load_sector_map,
    run_sector_rotation,
    write_sector_rotation_outputs,
)

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402
from scripts.run_factor_evaluation import build_synthetic_panel, load_local_daily_panel  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only QuantDesk sector rotation diagnostics.")
    parser.add_argument("--network", default="false", help="Defaults to false. This command never fetches market data.")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--windows", default="5,20,60")
    parser.add_argument("--benchmark-method", default="equal_weight_universe")
    parser.add_argument("--max-symbols", type=int, default=500)
    parser.add_argument("--max-days", type=int, default=320)
    parser.add_argument("--min-sector-symbols", type=int, default=3)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--daily-dir", default="data/daily")
    parser.add_argument("--sector-map-path", default="")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--synthetic-test-data", default="false", help="Test-only synthetic sector data; never used by default.")
    return parser.parse_args(argv)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _parse_windows(value: str) -> tuple[int, ...]:
    windows: list[int] = []
    for item in str(value or "5,20,60").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            number = int(item)
        except ValueError:
            continue
        if number > 0:
            windows.append(number)
    return tuple(windows or [5, 20, 60])


def _synthetic_sector_panel() -> tuple[pd.DataFrame, dict[str, str]]:
    panel = build_synthetic_panel(symbol_count=36, days=120)
    symbols = sorted(panel["symbol"].astype(str).unique())
    sectors = ["Healthcare", "Semiconductor", "Consumer", "Energy", "Industrial", "Finance"]
    sector_map = {symbol: sectors[index % len(sectors)] for index, symbol in enumerate(symbols)}
    frame = panel.copy().sort_values(["symbol", "date"]).reset_index(drop=True)
    frame["sector_name"] = frame["symbol"].astype(str).map(sector_map)
    for index, sector in enumerate(sectors):
        mask = frame["sector_name"] == sector
        dates = pd.to_datetime(frame.loc[mask, "date"])
        drift = (index - 2) * 0.0008
        trend = (dates.rank(method="dense").astype(float) / max(1.0, dates.nunique())) * drift
        frame.loc[mask, "close"] = pd.to_numeric(frame.loc[mask, "close"], errors="coerce").astype(float) * (1.0 + trend.to_numpy())
    return frame, sector_map


def run(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    windows = _parse_windows(args.windows)
    data_source = "local_daily"
    if truthy(args.network):
        warnings.append("network_requested_ignored: sector-rotation is offline and does not fetch market data")
    if truthy(args.synthetic_test_data):
        price_panel, sector_map = _synthetic_sector_panel()
        data_source = "synthetic_test_data"
        warnings.append("synthetic_test_data: test-only sample data; do not use as production research evidence")
    else:
        price_panel, load_warnings = load_local_daily_panel(
            args.daily_dir,
            max_symbols=args.max_symbols,
            max_days=args.max_days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        warnings.extend(load_warnings)
        sector_map = load_sector_map({"sector_map_path": args.sector_map_path} if args.sector_map_path else None)
        if not sector_map and (price_panel.empty or not any(column in price_panel.columns for column in ("sector", "industry", "theme", "primary_theme", "sector_name"))):
            warnings.append("missing_sector_map: no local sector/theme map found")
        if price_panel.empty:
            data_source = "none"
    payload = run_sector_rotation(
        price_panel,
        sector_map=sector_map,
        windows=windows,
        benchmark_method=args.benchmark_method,
        min_sector_symbols=args.min_sector_symbols,
    )
    payload["data_source"] = data_source if payload.get("data_source") != "none" else payload.get("data_source")
    payload["network"] = False
    payload["warnings"] = list(dict.fromkeys(str(item) for item in list(payload.get("warnings") or []) + warnings if item))
    if data_source == "synthetic_test_data":
        payload["status"] = "warning"
    if payload["warnings"]:
        payload["status"] = "warning"
    payload = write_sector_rotation_outputs(payload, output_dir=args.output_dir, report_dir=args.report_dir)
    payload["output_paths"] = {key: _rel(value) for key, value in (payload.get("output_paths") or {}).items()}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    wants_json = truthy(args.json)
    wants_summary = truthy(args.summary)
    if wants_summary:
        summary = build_cli_summary(payload, task_type="sector_rotation")
        if wants_json:
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
        else:
            print(format_human_summary(summary))
    elif wants_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(format_human_summary(build_cli_summary(payload, task_type="sector_rotation")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
