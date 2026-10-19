"""S3 (INTEGRATE-0922) regression tests: fallback market map, visible minute
gaps, and the health-line benchmark / gap / snapshot / minute cells.

Offline only: providers are injected, every fixture lives under tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday_refresh import collector

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "scripts" / "ops" / "write_health_line.py"
SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET = "2026-09-18"
PREV = "2026-09-17"


def load_health():
    spec = importlib.util.spec_from_file_location("write_health_line_s3", HEALTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("600000", "sh"), ("000001", "sz"), ("300750", "sz"),
     ("920227", ""), ("430047", ""), ("830799", ""), ("60000", ""), ("abc", "")],
)
def test_sina_minute_prefix_covers_only_verified_markets(symbol: str, expected: str) -> None:
    assert collector._sina_minute_prefix(symbol) == expected


def _bse_scope_root(tmp_path: Path, symbols: list[str]) -> Path:
    (tmp_path / "state" / "ai_trader").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {s: {"qty": 100} for s in symbols}}), encoding="utf-8"
    )
    (tmp_path / "state" / "ai_trader" / "plan_latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "ai_trader" / "watchlist.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "universe" / "a_share_symbols.csv").write_text(
        "symbol\n" + "\n".join(symbols) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_bse_failure_stays_a_visible_gap(tmp_path: Path) -> None:
    """A 92xxxx symbol has no verified fallback: it must be counted, not hidden.

    Both providers are injected, and the snapshot fetcher is injected too, so a
    closed-session round can be exercised with ``network=True`` semantics
    without touching the real provider (the collector would otherwise fetch the
    full-market snapshot).
    """

    def fake_fetch(symbol: str, offset: int):
        return [], "provider_failed:mootdx:unpack requires a buffer of 4 bytes"

    def fake_snapshot():
        return [], ""

    root = _bse_scope_root(tmp_path, ["600000", "920227"])
    manifest = collector.collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 22, 15, 5, tzinfo=SHANGHAI),
        fetcher=fake_fetch,
        snapshot_fetcher=fake_snapshot,
        write_manifest=False,
    )

    assert manifest["market_session"] == "closed"
    assert manifest["requested_count"] == 2
    assert manifest["missing_count"] == 2
    assert manifest["status"] != "ready"
    assert "minute_fallback_unavailable:920227" in manifest["reason_codes"]
    # 600000 keeps its existing fallback path, so it must not carry that code.
    assert "minute_fallback_unavailable:600000" not in manifest["reason_codes"]


def _project(tmp_path: Path, *, universe: int, daily_days: int, benchmark_day: str,
             snapshot_slots: int, collected: int, minute: dict | None) -> Path:
    cal = tmp_path / "data" / "calendar" / "a_share_trading_calendar.csv"
    cal.parent.mkdir(parents=True, exist_ok=True)
    cal.write_text(
        "date,is_open,source,note\n" + "\n".join(
            f"{day},true,fixture," for day in ("2026-09-16", PREV, TARGET)
        ) + "\n",
        encoding="utf-8",
    )
    universe_file = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    universe_file.parent.mkdir(parents=True, exist_ok=True)
    universe_file.write_text(
        "symbol\n" + "\n".join(f"{i:06d}" for i in range(universe)) + "\n", encoding="utf-8"
    )
    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    for index in range(daily_days):
        (daily / f"{index:06d}.csv").write_text(
            f"date,symbol,open\n{TARGET},{index:06d},10\n", encoding="utf-8"
        )
    bench = tmp_path / "data" / "benchmark"
    bench.mkdir(parents=True, exist_ok=True)
    for name in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        (bench / name).write_text(f"date,close\n{benchmark_day},100\n", encoding="utf-8")
    day = tmp_path / "data" / "snapshots" / TARGET
    day.mkdir(parents=True, exist_ok=True)
    for slot in range(snapshot_slots):
        (day / f"{slot:04d}.csv").write_text("symbol,price\n600000,10\n", encoding="utf-8")
    (day / "manifest.json").write_text(
        json.dumps({"collected_slot_count": collected}), encoding="utf-8"
    )
    if minute is not None:
        path = tmp_path / "results" / "intraday" / "intraday_refresh_latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(minute), encoding="utf-8")
    return tmp_path


def test_daily_cell_always_shows_a_non_zero_gap(tmp_path: Path) -> None:
    root = _project(tmp_path, universe=100, daily_days=99, benchmark_day=PREV,
                    snapshot_slots=0, collected=0, minute=None)
    cell, extra = load_health().daily_cell(root, TARGET)
    assert cell == "日线 ✓99/100(缺1)"
    # A gap with no provider suspension list behind it is reported as
    # unexplained instead of being left silent.
    assert extra == "日线缺口原因未核验1只"


def test_daily_cell_marks_a_large_gap_as_cross(tmp_path: Path) -> None:
    root = _project(tmp_path, universe=100, daily_days=90, benchmark_day=PREV,
                    snapshot_slots=0, collected=0, minute=None)
    cell, extra = load_health().daily_cell(root, TARGET)
    assert cell == "日线 ✗90/100(缺10)"
    assert "日线缺口 10/100" in extra


def test_benchmark_cell_requires_the_previous_trading_day(tmp_path: Path) -> None:
    module = load_health()
    fresh = _project(tmp_path / "fresh", universe=100, daily_days=100, benchmark_day=PREV,
                     snapshot_slots=0, collected=0, minute=None)
    cell, extra = module.benchmark_cell(fresh, TARGET)
    assert cell == f"基准 ✓{PREV}" and extra == ""

    stale = _project(tmp_path / "stale", universe=100, daily_days=100,
                     benchmark_day="2026-09-16", snapshot_slots=0, collected=0, minute=None)
    cell, extra = module.benchmark_cell(stale, TARGET)
    assert cell == "基准 ✗"
    assert f"应有 {PREV}" in extra and "hs300=2026-09-16" in extra


def test_benchmark_cell_uses_the_calendar_not_the_weekend(tmp_path: Path) -> None:
    module = load_health()
    root = _project(tmp_path, universe=100, daily_days=100, benchmark_day=PREV,
                    snapshot_slots=0, collected=0, minute=None)
    # 2026-09-21 is a Monday; with the calendar open on 09-17/18/21 the previous
    # session must be 09-18, so a 09-17 file is a gap.
    (root / "data" / "calendar" / "a_share_trading_calendar.csv").write_text(
        "date,is_open,source,note\n2026-09-17,true,fixture,\n2026-09-18,true,fixture,\n"
        "2026-09-21,true,fixture,\n",
        encoding="utf-8",
    )
    assert module.previous_trading_day(root, "2026-09-21") == ("2026-09-18", "")
    cell, extra = module.benchmark_cell(root, "2026-09-21")
    assert cell == "基准 ✗"
    assert "应有 2026-09-18，实际 hs300=2026-09-17" in extra


def test_snapshot_cell_keeps_archive_and_unverified_counts_separate(tmp_path: Path) -> None:
    root = _project(tmp_path, universe=100, daily_days=100, benchmark_day=PREV,
                    snapshot_slots=48, collected=0, minute=None)
    cell, extra = load_health().snapshot_cell(root, TARGET)
    assert cell == "快照 ✗48张/覆盖完成 未知/字段核实 未知"
    assert "已归档 48 张" in extra and "仍未核实" in extra
    assert "快照未拉回" not in extra
    missing = _project(tmp_path / "missing", universe=100, daily_days=100, benchmark_day=PREV,
                       snapshot_slots=0, collected=0, minute=None)
    missing_cell, missing_extra = load_health().snapshot_cell(missing, TARGET)
    assert missing_cell.startswith("快照 ✗0张")
    assert "快照未拉回" in missing_extra


def test_minute_cell_counts_gaps_and_storage_blocks(tmp_path: Path) -> None:
    minute = {
        "target_date": TARGET,
        "requested_count": 90,
        "fresh_count": 82,
        "missing_count": 8,
        "provider_status": [
            {"symbol": "000610", "status": "storage_blocked"},
            {"symbol": "000676", "status": "storage_blocked"},
        ],
    }
    root = _project(tmp_path, universe=100, daily_days=100, benchmark_day=PREV,
                    snapshot_slots=0, collected=0, minute=minute)
    cell, extra = load_health().minute_cell(root, TARGET)
    assert cell == "分钟 ✗82/90(缺8+拒写2)"
    assert "拒写2" in extra


def test_minute_cell_missing_receipt_is_visible(tmp_path: Path) -> None:
    root = _project(tmp_path, universe=100, daily_days=100, benchmark_day=PREV,
                    snapshot_slots=0, collected=0, minute=None)
    cell, extra = load_health().minute_cell(root, TARGET)
    assert cell == "分钟 ✗"
    assert "分钟回执缺失" in extra
