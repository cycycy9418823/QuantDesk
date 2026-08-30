from __future__ import annotations

import csv
from pathlib import Path


def _write_daily(path: Path, *dates: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "symbol", "close"])
        for day in dates:
            writer.writerow([day, path.stem, "10.0"])


def _write_universe(path: Path, *symbols: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name"])
        for symbol in symbols:
            writer.writerow([symbol, symbol])


def test_full_a_daily_coverage_uses_current_universe_not_raw_directory_count(
    tmp_path: Path,
) -> None:
    """Archive-only CSV files must not impersonate current-universe coverage."""

    from quantdesk.full_a_daily_coverage import build_full_a_daily_coverage

    universe = tmp_path / "universe" / "a_share_symbols.csv"
    daily = tmp_path / "daily"
    _write_universe(universe, "000001", "000002", "000003")
    _write_daily(daily / "000001.csv", "2026-08-21")
    _write_daily(daily / "000002.csv", "2026-08-28")
    _write_daily(daily / "999999.csv", "2026-08-28")  # not in current universe

    payload = build_full_a_daily_coverage(
        target_date="2026-08-28",
        universe_path=universe,
        daily_dir=daily,
    )

    assert payload["universe_total"] == 3
    assert payload["canonical_intersection"] == 2
    assert payload["at_target_date_count"] == 1
    assert payload["stale_count"] == 1
    assert payload["missing_count"] == 1
    assert payload["coverage_floor_date"] == "2026-08-21"
    assert payload["extra_files_outside_universe"] == 1
    assert payload["network"] is False
    assert payload["read_only"] is True
