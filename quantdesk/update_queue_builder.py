from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.market_session_detector import detect_market_session
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def _read_symbols_from_csv(path: Path, columns: tuple[str, ...] = ("symbol", "stock_symbol")) -> set[str]:
    symbols: set[str] = set()
    if not path.exists():
        return symbols
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for column in columns:
                    value = str(row.get(column) or "").strip()
                    digits = "".join(ch for ch in value if ch.isdigit())
                    if digits:
                        symbols.add(digits.zfill(6)[-6:])
    except Exception:
        return symbols
    return symbols


def priority_symbol_groups(root: Path) -> dict[str, set[str]]:
    p0 = set()
    p0 |= _read_symbols_from_csv(root / "results/daily_research/v420_daily_stock_candidates_latest.csv")
    p0 |= _read_symbols_from_csv(root / "results/daily_research/v420_daily_sector_candidates_latest.csv")
    p0 |= _read_symbols_from_csv(root / "results/holdings_research/v410_top_recurring_holdings_latest.csv")
    p0 |= _read_symbols_from_csv(root / "results/holdings_research/v410_research_only_stock_review_table_latest.csv")
    p1 = set()
    p1 |= _read_symbols_from_csv(root / "results/holdings/canonical_selected_symbols_latest.csv")
    p1 |= _read_symbols_from_csv(root / "data/universe/stock_theme_map.csv")
    return {"P0": p0, "P1": p1}


def _missing_days(latest: str, expected: str) -> int:
    try:
        latest_day = date.fromisoformat(latest)
        expected_day = date.fromisoformat(expected)
    except Exception:
        return 9999
    return max(0, (expected_day - latest_day).days)


def fast_last_date(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            chunk = handle.read().decode("utf-8-sig", errors="ignore")
        lines = [line for line in chunk.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.lower().startswith("date,"):
                continue
            return line.split(",", 1)[0].strip()
    except Exception:
        return ""
    return ""


def latest_date_distribution(root: Path) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for path in sorted((root / "data/daily").glob("*.csv")):
        counter[fast_last_date(path) or "<blank>"] += 1
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def build_update_queue(
    root: Path,
    *,
    max_symbols: int = 300,
    priority_only: bool = True,
    resume: bool = False,
    start_after: str = "",
    expected_date: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    session = detect_market_session(root)
    expected = expected_date or session["expected_latest_completed_trade_date"]
    groups = priority_symbol_groups(root)
    all_daily = {path.stem for path in (root / "data/daily").glob("*.csv")}
    priority_symbols = groups["P0"] | groups["P1"]
    candidates = sorted(priority_symbols if priority_only else all_daily)
    if start_after:
        normalized = "".join(ch for ch in start_after if ch.isdigit()).zfill(6)[-6:]
        candidates = [symbol for symbol in candidates if symbol > normalized]
    rows: list[dict[str, Any]] = []
    for symbol in candidates:
        path = root / "data/daily" / f"{symbol}.csv"
        if not path.exists():
            continue
        latest = fast_last_date(path)
        already_latest = bool(latest and latest >= expected)
        if symbol in groups["P0"]:
            group = "P0"
            reason = "daily_candidates_or_combo50_priority"
        elif symbol in groups["P1"]:
            group = "P1"
            reason = "selected_or_sector_mapped_priority"
        else:
            group = "P2"
            reason = "remaining_data_daily_symbol"
        row = {
            "symbol": symbol,
            "latest_local_date": latest,
            "expected_latest_date": expected,
            "missing_days_count": _missing_days(latest, expected),
            "priority_group": group,
            "reason": reason,
            "provider_preference": "akshare_then_tencent",
            "already_latest": already_latest,
            "update_required": not already_latest,
        }
        rows.append(row)
    rows.sort(key=lambda row: (row["priority_group"], not row["update_required"], -int(row["missing_days_count"]), row["symbol"]))
    limited = rows[: max(0, int(max_symbols))]
    payload = {
        "task_id": "v426_update_queue",
        "status": "completed",
        "expected_latest_date": expected,
        "priority_only": bool(priority_only),
        "resume": bool(resume),
        "start_after": start_after,
        "max_symbols": int(max_symbols),
        "total_data_daily_symbols": len(all_daily),
        "total_candidate_queue_symbols": len(rows),
        "selected_queue_symbols": len(limited),
        "update_required_total": sum(1 for row in rows if row["update_required"]),
        "update_required_selected": sum(1 for row in limited if row["update_required"]),
        "latest_date_distribution": latest_date_distribution(root),
        "queue": limited,
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v426_update_queue_latest.json", payload)
    write_md(root / "reports/data_update/v426_update_queue_latest.md", "# v426 Update Queue\n\n" + md_table(limited[:50], ["symbol", "latest_local_date", "expected_latest_date", "missing_days_count", "priority_group", "update_required"]))
    csv_path = root / "results/data_update/v426_update_queue_latest.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        cols = ["symbol", "latest_local_date", "expected_latest_date", "missing_days_count", "priority_group", "reason", "provider_preference", "already_latest", "update_required"]
        writer = csv.DictWriter(handle, fieldnames=cols)
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in cols} for row in limited])
    return payload
