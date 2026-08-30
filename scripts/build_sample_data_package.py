from __future__ import annotations

import argparse
import json
import math
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.version import APP_VERSION


def _business_dates(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def build_sample_data_package(
    manifest_path: str | Path = "assets/sample_data_manifest.json",
    output_zip: str | Path = "dist/quantdesk_sample_data_v3.5.0.zip",
) -> dict:
    manifest_file = PROJECT_ROOT / manifest_path
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    out_path = PROJECT_ROOT / output_zip
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = date.fromisoformat(manifest["date_range"]["start"])
    end = date.fromisoformat(manifest["date_range"]["end"])
    days = _business_dates(start, end)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", "QuantDesk synthetic sample data. Not real market data. Not investment advice.\n")
        archive.writestr("sample_data_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for idx, symbol in enumerate(manifest["symbols"]):
            rows: list[list[str | float]] = [["date", "open", "high", "low", "close", "volume", "symbol"]]
            base = 10.0 + idx
            for offset, day in enumerate(days):
                drift = offset * 0.015
                wave = math.sin((offset + idx) / 5.0) * 0.2
                close = round(base + drift + wave, 3)
                open_price = round(close * 0.995, 3)
                high = round(close * 1.01, 3)
                low = round(close * 0.99, 3)
                volume = 100000 + idx * 1000 + offset * 100
                rows.append([day.isoformat(), open_price, high, low, close, volume, symbol])
            text_rows: list[str] = []
            for row in rows:
                text_rows.append(",".join(str(item) for item in row))
            archive.writestr(f"data/daily/{symbol}.csv", "\n".join(text_rows) + "\n")
        calendar_rows = [["date", "is_open"]] + [[day.isoformat(), "1"] for day in days]
        archive.writestr("data/calendar/a_share_trading_calendar_sample.csv", "\n".join(",".join(row) for row in calendar_rows) + "\n")
        sector_rows = [["symbol", "industry"]]
        industries = ["Finance", "Technology", "Consumer", "Healthcare", "Industry"]
        for idx, symbol in enumerate(manifest["symbols"]):
            sector_rows.append([symbol, industries[idx % len(industries)]])
        archive.writestr("data/reference/industry_map_sample.csv", "\n".join(",".join(row) for row in sector_rows) + "\n")
    return {
        "ok": True,
        "status": "passed",
        "task_type": "sample_data_package",
        "app_version": APP_VERSION,
        "network": False,
        "synthetic_sample_data": True,
        "symbol_count": len(manifest["symbols"]),
        "output_path": str(out_path),
        "disclaimer": "Synthetic sample data only. Not real market data. Not investment advice.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="assets/sample_data_manifest.json")
    parser.add_argument("--output", default="dist/quantdesk_sample_data_v3.5.0.zip")
    parser.add_argument("--json", default="false")
    args = parser.parse_args(argv)
    payload = build_sample_data_package(args.manifest, args.output)
    if str(args.json).lower() in {"1", "true", "yes", "y"}:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"sample data package: {payload['output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
