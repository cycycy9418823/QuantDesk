from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "write_health_line.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("write_health_line_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_cal(root: Path, rows: list[tuple[str, str]]) -> None:
    path = root / "data" / "calendar" / "a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,is_open,source,note"]
    lines.extend(f"{day},{flag},fixture," for day, flag in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(root: Path, extra: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--root", str(root), "--json", "true", *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def strategy_fixture(root: Path, as_of: str, generated: str = "2026-09-18T20:03:44") -> None:
    write_json(
        root / "results" / "auto_paper_portfolio_latest.json",
        {"generated_at": generated, "candidate_intake": {"as_of": as_of}},
    )


def ledger_fixture(root: Path, rel: str, as_of: str, dates: list[str], extra: dict | None = None) -> None:
    payload = {
        "as_of": as_of,
        "trades": [{"date": day, "symbol": "000001"} for day in dates],
    }
    if extra:
        payload.update(extra)
    write_json(root / rel, payload)


def decision_fixture(root: Path, rel: str, as_of: str, rows: list[tuple[str, str]]) -> None:
    write_json(
        root / rel,
        {
            "as_of": as_of,
            "records": [{"decision_date": day, "result": result} for day, result in rows],
        },
    )


def test_t1_strategy_as_of_contract(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "策略账 ✓" in payload["line"]
    strategy_fixture(tmp_path, "2026-09-16")
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "策略账 ✗" in payload["line"]
    text = " ".join(payload["explains"])
    assert "2026-09-16" in text
    assert "2026-09-18T20:03:44" in text


def test_t2_ai_ledgers_count_and_missing(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    ledger_fixture(tmp_path, "state/ai_trader/ledger.json", "2026-09-18", ["2026-09-18"])
    ledger_fixture(tmp_path, "state/ai_trader_glm/ledger.json", "2026-09-18", ["2026-09-18"])
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-18", [("2026-09-18", "对")])
    decision_fixture(tmp_path, "state/ai_trader_glm/decision_latest.json", "2026-09-18", [("2026-09-18", "对")])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "AI账 ✓2笔" in payload["line"]
    (tmp_path / "state" / "ai_trader_glm" / "ledger.json").unlink()
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "AI账 ✗" in payload["line"]
    assert any("AI账本缺失" in item and "state/ai_trader_glm/ledger.json" in item for item in payload["explains"])


def test_t3_ai_ledger_fills_list_does_not_raise(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    ledger_fixture(
        tmp_path,
        "state/ai_trader/ledger.json",
        "2026-09-18",
        ["2026-09-18"],
        extra={"fills": [{"x": 1}]},
    )
    ledger_fixture(tmp_path, "state/ai_trader_glm/ledger.json", "2026-09-18", [])
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-18", [])
    decision_fixture(tmp_path, "state/ai_trader_glm/decision_latest.json", "2026-09-18", [])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "AI账 ✓1笔" in payload["line"]


def test_t4_review_mismatch_and_stale(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-14", "true"), ("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    ledger_fixture(tmp_path, "state/ai_trader/ledger.json", "2026-09-18", [])
    ledger_fixture(tmp_path, "state/ai_trader_glm/ledger.json", "2026-09-18", [])
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-18", [("2026-09-14", "陈述与数据不符")])
    decision_fixture(tmp_path, "state/ai_trader_glm/decision_latest.json", "2026-09-18", [])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-14"])
    assert "核对 ✗1条陈述不符" in payload["line"]
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-18", [("2026-09-14", "对")])
    decision_fixture(tmp_path, "state/ai_trader_glm/decision_latest.json", "2026-09-18", [("2026-09-14", "对")])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-14"])
    assert "核对 ✓" in payload["line"]
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-10", [("2026-09-14", "对")])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-14"])
    assert "核对 ✗" in payload["line"]
    assert any("核对记录截至 2026-09-10" in item for item in payload["explains"])


def test_t5_calendar_is_open_column(tmp_path: Path) -> None:
    mod = load_mod()
    write_cal(tmp_path, [("2026-10-01", "false"), ("2026-09-18", "true")])
    assert mod.is_trading_day(tmp_path, "2026-10-01") == (False, "")
    assert mod.is_trading_day(tmp_path, "2026-09-18") == (True, "")
    ok, note = mod.is_trading_day(tmp_path, "2026-09-20")
    assert ok is True
    assert note == "日历无 2026-09-20"


def test_t6_backup_explain_has_last_success(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    ledger_fixture(tmp_path, "state/ai_trader/ledger.json", "2026-09-18", [])
    ledger_fixture(tmp_path, "state/ai_trader_glm/ledger.json", "2026-09-18", [])
    decision_fixture(tmp_path, "state/ai_trader/decision_latest.json", "2026-09-18", [])
    decision_fixture(tmp_path, "state/ai_trader_glm/decision_latest.json", "2026-09-18", [])
    log = tmp_path / "logs" / "ops" / "backup_push.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"stamp": "snapshot_20260919T140001Z", "all_hashes_match": True}) + "\n",
        encoding="utf-8",
    )
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "备份 ✗" in payload["line"]
    assert any("上次成功 2026-09-19" in item for item in payload["explains"])


def test_t7_dry_run_does_not_write(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    status = tmp_path / "STATUS.md"
    status.write_text("# STATUS\nkeep\n", encoding="utf-8")
    log = tmp_path / "logs" / "ops" / "health_line.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("keep-log\n", encoding="utf-8")
    before_status = sha(status)
    before_log = sha(log)
    run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert sha(status) == before_status
    assert sha(log) == before_log


def test_t8_review_missing_file_wording(tmp_path: Path) -> None:
    write_cal(tmp_path, [("2026-09-18", "true")])
    strategy_fixture(tmp_path, "2026-09-18")
    ledger_fixture(tmp_path, "state/ai_trader/ledger.json", "2026-09-18", [])
    ledger_fixture(tmp_path, "state/ai_trader_glm/ledger.json", "2026-09-18", [])
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", "2026-09-18"])
    assert "核对 ✗" in payload["line"]
    assert any(
        "核对记录缺失" in item and "state/ai_trader/decision_latest.json" in item
        for item in payload["explains"]
    )


TARGET_DATE = "2026-09-18"
PREV_DATE = "2026-09-17"


def health_scenario(root: Path, *, daily: int, benchmark: str, slots: int, collected: int,
                    minute: dict | None) -> None:
    """Build one S3.3 health-line scenario (mirrors the recorded dry-run fixtures)."""
    write_cal(root, [("2026-09-16", "true"), (PREV_DATE, "true"), (TARGET_DATE, "true")])
    universe = root / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text(
        "symbol\n" + "\n".join(f"{i:06d}" for i in range(100)) + "\n", encoding="utf-8"
    )
    daily_dir = root / "data" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    for index in range(daily):
        (daily_dir / f"{index:06d}.csv").write_text(
            f"date,symbol,open\n{TARGET_DATE},{index:06d},10\n", encoding="utf-8"
        )
    bench_dir = root / "data" / "benchmark"
    bench_dir.mkdir(parents=True, exist_ok=True)
    for name in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        (bench_dir / name).write_text(f"date,close\n{benchmark},100\n", encoding="utf-8")
    day = root / "data" / "snapshots" / TARGET_DATE
    day.mkdir(parents=True, exist_ok=True)
    for slot in range(slots):
        (day / f"{slot:04d}.csv").write_text("symbol,price\n600000,10\n", encoding="utf-8")
    write_json(day / "manifest.json", {"collected_slot_count": collected})
    if minute is not None:
        write_json(root / "results" / "intraday" / "intraday_refresh_latest.json", minute)
    strategy_fixture(root, TARGET_DATE, generated=f"{TARGET_DATE}T20:03:44")
    for rel in ("state/ai_trader/ledger.json", "state/ai_trader_glm/ledger.json"):
        write_json(root / rel, {"as_of": TARGET_DATE, "trades": []})
    for rel in ("state/ai_trader/decision_latest.json", "state/ai_trader_glm/decision_latest.json"):
        write_json(root / rel, {"as_of": TARGET_DATE, "records": []})
    log = root / "logs" / "ops" / "backup_push.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"stamp": "20260918T140001Z", "all_hashes_match": True}) + "\n",
        encoding="utf-8",
    )


GOOD_MINUTE = {
    "target_date": TARGET_DATE, "requested_count": 90, "fresh_count": 90,
    "missing_count": 0, "provider_status": [],
}
BLOCKED_MINUTE = {
    "target_date": TARGET_DATE, "requested_count": 90, "fresh_count": 82,
    "missing_count": 8,
    "provider_status": [
        {"symbol": "000610", "status": "storage_blocked"},
        {"symbol": "000676", "status": "storage_blocked"},
    ],
}


def test_t9_nine_cells_all_green_dry_run(tmp_path: Path) -> None:
    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])
    assert payload["line"] == (
        "日线 ✓100/100 · 基准 ✓2026-09-17 · 快照 ✓48张/完成 48 · 分钟 ✓90/90 · "
        "策略账 ✓ · AI账 ✓0笔 · 核对 ✓ · 备份 ✓"
    )


def test_t10_daily_gap_is_visible_even_when_marked_cross(tmp_path: Path) -> None:
    health_scenario(tmp_path, daily=90, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])
    assert payload["line"].startswith("日线 ✗90/100(缺10) · 基准 ✓2026-09-17")
    assert "日线缺口 10/100" in payload["explains"]


def test_t11_benchmark_stale_names_the_day_it_stopped(tmp_path: Path) -> None:
    health_scenario(tmp_path, daily=100, benchmark="2026-09-16", slots=48, collected=48,
                    minute=GOOD_MINUTE)
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])
    assert payload["line"].startswith("日线 ✓100/100 · 基准 ✗")
    assert any(
        "基准停在哪天：应有 2026-09-17，实际 hs300=2026-09-16 zz500=2026-09-16 "
        "sh000001=2026-09-16" in item
        for item in payload["explains"]
    )


def test_t12_snapshot_partial_and_blocked_minute_are_visible(tmp_path: Path) -> None:
    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=0,
                    minute=BLOCKED_MINUTE)
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])
    assert "快照 ✗48张/完成 0" in payload["line"]
    assert "分钟 ✗82/90(缺8+拒写2)" in payload["line"]
    text = " ".join(payload["explains"])
    assert "快照未拉回 2026-09-18" in text
    assert "缺8，拒写2" in text
