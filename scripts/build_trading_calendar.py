from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import truthy  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
DEFAULT_START_DATE = "2020-01-01"
DEFAULT_END_DATE = "2027-12-31"

# A-share markets do not open on adjusted make-up weekends.  The ranges below
# mark weekday exchange holidays; 2027 is a provisional offline planning range.
OFFLINE_HOLIDAY_RANGES: list[tuple[str, str, str, str]] = [
    ("2020-01-01", "2020-01-01", "new_year", "official_exchange_holiday"),
    ("2020-01-24", "2020-02-02", "spring_festival_extended", "official_exchange_holiday"),
    ("2020-04-04", "2020-04-06", "qingming", "official_exchange_holiday"),
    ("2020-05-01", "2020-05-05", "labor_day", "official_exchange_holiday"),
    ("2020-06-25", "2020-06-27", "dragon_boat", "official_exchange_holiday"),
    ("2020-10-01", "2020-10-08", "national_day_mid_autumn", "official_exchange_holiday"),
    ("2021-01-01", "2021-01-03", "new_year", "official_exchange_holiday"),
    ("2021-02-11", "2021-02-17", "spring_festival", "official_exchange_holiday"),
    ("2021-04-03", "2021-04-05", "qingming", "official_exchange_holiday"),
    ("2021-05-01", "2021-05-05", "labor_day", "official_exchange_holiday"),
    ("2021-06-12", "2021-06-14", "dragon_boat", "official_exchange_holiday"),
    ("2021-09-19", "2021-09-21", "mid_autumn", "official_exchange_holiday"),
    ("2021-10-01", "2021-10-07", "national_day", "official_exchange_holiday"),
    ("2022-01-01", "2022-01-03", "new_year", "official_exchange_holiday"),
    ("2022-01-31", "2022-02-06", "spring_festival", "official_exchange_holiday"),
    ("2022-04-03", "2022-04-05", "qingming", "official_exchange_holiday"),
    ("2022-04-30", "2022-05-04", "labor_day", "official_exchange_holiday"),
    ("2022-06-03", "2022-06-05", "dragon_boat", "official_exchange_holiday"),
    ("2022-09-10", "2022-09-12", "mid_autumn", "official_exchange_holiday"),
    ("2022-10-01", "2022-10-07", "national_day", "official_exchange_holiday"),
    ("2023-01-01", "2023-01-02", "new_year", "official_exchange_holiday"),
    ("2023-01-21", "2023-01-27", "spring_festival", "official_exchange_holiday"),
    ("2023-04-05", "2023-04-05", "qingming", "official_exchange_holiday"),
    ("2023-04-29", "2023-05-03", "labor_day", "official_exchange_holiday"),
    ("2023-06-22", "2023-06-24", "dragon_boat", "official_exchange_holiday"),
    ("2023-09-29", "2023-10-06", "mid_autumn_national_day", "official_exchange_holiday"),
    ("2024-01-01", "2024-01-01", "new_year", "official_exchange_holiday"),
    ("2024-02-09", "2024-02-17", "spring_festival", "official_exchange_holiday"),
    ("2024-04-04", "2024-04-06", "qingming", "official_exchange_holiday"),
    ("2024-05-01", "2024-05-05", "labor_day", "official_exchange_holiday"),
    ("2024-06-10", "2024-06-10", "dragon_boat", "official_exchange_holiday"),
    ("2024-09-15", "2024-09-17", "mid_autumn", "official_exchange_holiday"),
    ("2024-10-01", "2024-10-07", "national_day", "official_exchange_holiday"),
    ("2025-01-01", "2025-01-01", "new_year", "official_exchange_holiday"),
    ("2025-01-28", "2025-02-04", "spring_festival", "official_exchange_holiday"),
    ("2025-04-04", "2025-04-06", "qingming", "official_exchange_holiday"),
    ("2025-05-01", "2025-05-05", "labor_day", "official_exchange_holiday"),
    ("2025-05-31", "2025-06-02", "dragon_boat", "official_exchange_holiday"),
    ("2025-10-01", "2025-10-08", "national_day_mid_autumn", "official_exchange_holiday"),
    ("2026-01-01", "2026-01-03", "new_year", "official_exchange_holiday"),
    ("2026-02-15", "2026-02-23", "spring_festival", "official_exchange_holiday"),
    ("2026-04-04", "2026-04-06", "qingming", "official_exchange_holiday"),
    ("2026-05-01", "2026-05-05", "labor_day", "official_exchange_holiday"),
    ("2026-06-19", "2026-06-21", "dragon_boat", "official_exchange_holiday"),
    ("2026-09-25", "2026-09-27", "mid_autumn", "official_exchange_holiday"),
    ("2026-10-01", "2026-10-07", "national_day", "official_exchange_holiday"),
    ("2027-01-01", "2027-01-03", "new_year", "provisional_2027"),
    ("2027-02-05", "2027-02-13", "spring_festival", "provisional_2027"),
    ("2027-04-03", "2027-04-05", "qingming", "provisional_2027"),
    ("2027-05-01", "2027-05-05", "labor_day", "provisional_2027"),
    ("2027-06-09", "2027-06-11", "dragon_boat", "provisional_2027"),
    ("2027-09-15", "2027-09-17", "mid_autumn", "provisional_2027"),
    ("2027-10-01", "2027-10-07", "national_day", "provisional_2027"),
]


def _parse_date(value: str) -> date:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _date_range(start: date, end: date) -> list[date]:
    rows: list[date] = []
    current = start
    while current <= end:
        rows.append(current)
        current += timedelta(days=1)
    return rows


def _holiday_map() -> dict[str, tuple[str, str]]:
    holidays: dict[str, tuple[str, str]] = {}
    for start_text, end_text, name, source in OFFLINE_HOLIDAY_RANGES:
        for day in _date_range(_parse_date(start_text), _parse_date(end_text)):
            holidays[day.isoformat()] = (name, source)
    return holidays


def build_offline_calendar(start_date: str, end_date: str, *, source: str = "offline") -> list[dict[str, Any]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    holidays = _holiday_map()
    rows: list[dict[str, Any]] = []
    for day in _date_range(start, end):
        iso = day.isoformat()
        holiday = holidays.get(iso)
        is_weekend = day.weekday() >= 5
        is_open = (not is_weekend) and holiday is None
        if holiday:
            note, row_source = holiday
        elif is_weekend:
            note, row_source = "weekend", "offline_weekend_rule"
        else:
            note, row_source = "regular_trade_day", "offline_weekday_rule"
        if iso.startswith("2027") and "provisional" not in row_source:
            row_source = f"{row_source}_provisional_2027"
            note = f"{note}; provisional_2027"
        if source == "manual":
            row_source = row_source.replace("offline", "manual")
        rows.append({"date": iso, "is_open": str(bool(is_open)).lower(), "source": row_source, "note": note})
    return rows


def build_akshare_calendar(start_date: str, end_date: str, *, network: bool) -> list[dict[str, Any]]:
    if not network:
        raise RuntimeError("network=false; akshare calendar build is not allowed")
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"akshare is not installed: {exc}") from exc
    try:
        frame = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"akshare trade calendar request failed: {exc}") from exc
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        raise RuntimeError("akshare returned empty or unsupported trade calendar")
    open_days = {str(value)[:10] for value in frame["trade_date"].astype(str).tolist()}
    rows: list[dict[str, Any]] = []
    for day in _date_range(_parse_date(start_date), _parse_date(end_date)):
        iso = day.isoformat()
        rows.append(
            {
                "date": iso,
                "is_open": str(iso in open_days).lower(),
                "source": "akshare_tool_trade_date_hist_sina",
                "note": "akshare_trade_day" if iso in open_days else "akshare_closed_day",
            }
        )
    return rows


def write_calendar(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "is_open", "source", "note"])
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build QuantDesk local A-share trading calendar CSV.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--network", default="false")
    parser.add_argument("--source", choices=["offline", "manual", "akshare"], default="offline")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    network = truthy(args.network)
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    if args.source == "akshare":
        rows = build_akshare_calendar(args.start_date, args.end_date, network=network)
    else:
        rows = build_offline_calendar(args.start_date, args.end_date, source=args.source)
    if not rows:
        raise RuntimeError("calendar build returned no rows")
    write_calendar(rows, output)
    open_days = sum(1 for row in rows if truthy(row.get("is_open")))
    closed_days = len(rows) - open_days
    provisional = any("provisional" in str(row.get("source", "")) or "provisional" in str(row.get("note", "")) for row in rows)
    quality = "local_calendar_provisional" if provisional else "local_calendar"
    return {
        "ok": True,
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "open_days": open_days,
        "closed_days": closed_days,
        "source": args.source,
        "calendar_quality": quality,
        "output": str(output),
        "network": network,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        payload = run(_parse_args(argv))
    except Exception as exc:  # noqa: BLE001
        print(f"calendar build failed: {exc}", file=sys.stderr)
        return 1
    print("QuantDesk Trading Calendar Build")
    print(f"date range: {payload['start_date']} -> {payload['end_date']}")
    print(f"open days: {payload['open_days']}")
    print(f"closed days: {payload['closed_days']}")
    print(f"source: {payload['source']}")
    print(f"calendar_quality: {payload['calendar_quality']}")
    print(f"output: {payload['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
