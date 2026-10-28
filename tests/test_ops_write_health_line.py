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
    write_json(root / "state/auto_paper_portfolio.json", {
        "initial_cash": 100000, "cash": 100000, "positions": [],
        "valuation_date": as_of, "portfolio_review_valid": True,
        "market_value": 0, "total_equity": 100000,
    })
    write_json(
        root / "results" / "auto_paper_portfolio_latest.json",
        {"generated_at": generated, "candidate_intake": {"as_of": as_of},
         "ok": True, "valuation_date": as_of, "portfolio_review_valid": True,
         "summary": {"cash": 100000, "market_value": 0, "total_equity": 100000}},
    )


def ledger_fixture(root: Path, rel: str, as_of: str, dates: list[str], extra: dict | None = None) -> None:
    payload = {
        "as_of": as_of,
        "trades": [{"date": day, "symbol": "000001"} for day in dates],
    }
    if extra:
        payload.update(extra)
    write_json(root / rel, payload)
    write_json((root / rel).with_name("journal_latest.json"), {"as_of": as_of, "llm": {"used": True}})


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
    names = [f"{slot:04d}" for slot in range(slots)]
    for name in names:
        write_json(day / f"{name}.json", {"trade_date": TARGET_DATE, "slot_time": name,
                   "csv_sha256": sha(day / f"{name}.csv")})
    write_json(day / "manifest.json", {"trade_date": TARGET_DATE, "collected_slot_count": slots,
        "completed_slot_count": collected, "field_verified_complete_slot_count": collected,
        "partial_slot_count": max(0, slots - collected), "expected_slot_count": 48,
        "field_verified_complete_slots": names[:collected]})
    if minute is not None:
        write_json(root / "results" / "intraday" / "intraday_refresh_latest.json", minute)
    strategy_fixture(root, TARGET_DATE, generated=f"{TARGET_DATE}T20:03:44")
    for rel in ("state/ai_trader/ledger.json", "state/ai_trader_glm/ledger.json"):
        write_json(root / rel, {"as_of": TARGET_DATE, "trades": []})
        write_json((root / rel).with_name("journal_latest.json"), {"as_of": TARGET_DATE, "llm": {"used": True}})
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
        "日线 ✓100/100 · 基准 ✓2026-09-17 · 快照 ✓48张/覆盖完成 48/字段核实 48 · 分钟 ✓90/90 · "
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
    assert "快照 ✗48张/覆盖完成 0/字段核实 0" in payload["line"]
    assert "分钟 ✗82/90(缺8+拒写2)" in payload["line"]
    text = " ".join(payload["explains"])
    assert "快照 2026-09-18 已归档 48 张，完整字段或档案哈希仍未核实" in text
    assert "缺8，拒写2" in text


def write_pull_log(root: Path, rows: list[dict]) -> None:
    path = root / "logs" / "ops" / "snapshot_pull.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def pull_log_row(root: Path, target: str, **extra: object) -> dict:
    """一条和拉取程序写法一致的到达记录：绑定当时那一版数据的清单指纹。"""

    manifest = root / "data" / "snapshots" / target / "manifest.json"
    row: dict = {"date": target, "quote_date": target, "published": True,
                 "manifest_sha256": sha(manifest), "manifest_revision": None}
    row.update(extra)
    return row


def test_t13_yesterday_quote_received_today_keeps_its_own_date(tmp_path: Path) -> None:
    """S1 反例：昨天的行情今天才在本地完整可用，行情日期仍是昨天，收到时间单独显示。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_pull_log(tmp_path, [pull_log_row(
        tmp_path, TARGET_DATE, files=49, collected_slot_count=48, updated=True, ok=True,
        started_at="2026-09-19T10:11:15+08:00",
        download_finished_at="2026-09-19T10:12:00+08:00",
        pulled_at="2026-09-19T10:12:02+08:00",
    )])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    assert "快照 ✓48张/覆盖完成 48/字段核实 48（本地 09-19 10:12 收到）" in payload["line"]
    text = " ".join(payload["explains"])
    assert f"行情日期仍是 {TARGET_DATE}" in text
    assert "上游发布 未知" in text


def test_t14_failed_pull_attempt_reports_arrival_unknown(tmp_path: Path) -> None:
    """S1 反例：只有失败记录时不能显示“已收到”，本地到达时间标未知。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_pull_log(tmp_path, [{
        "date": TARGET_DATE, "stage": "tar", "ok": False, "published": False,
        "started_at": "2026-09-19T10:00:00+08:00",
        "download_finished_at": None, "pulled_at": None,
    }])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    assert "收到）" not in payload["line"]
    text = " ".join(payload["explains"])
    assert f"快照 {TARGET_DATE} 有拉取记录但没有一次发布完成，本地到达时间未知" in text


def test_t15_upstream_publish_stamp_is_shown_when_present(tmp_path: Path) -> None:
    """S1：清单里有上游发布时间就照实显示，没有才写未知。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_json(tmp_path / "data" / "snapshots" / TARGET_DATE / "manifest.json",
               {"collected_slot_count": 48, "upstream_published_at": f"{TARGET_DATE}T15:05:00"})
    write_pull_log(tmp_path, [pull_log_row(
        tmp_path, TARGET_DATE, ok=True,
        started_at="2026-09-19T10:11:15+08:00",
        download_finished_at="2026-09-19T10:12:00+08:00",
        pulled_at="2026-09-19T10:12:02+08:00",
    )])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    assert f"上游发布 {TARGET_DATE}T15:05:00" in " ".join(payload["explains"])


def test_t16_other_version_receipt_is_not_borrowed(tmp_path: Path) -> None:
    """S1 反例：新数据已换版但它的成功记录没写成 → 不借上一版到达时间，写未知。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    old_stamp = "2026-09-19T10:12:02+08:00"
    write_pull_log(tmp_path, [pull_log_row(
        tmp_path, TARGET_DATE, files=49, collected_slot_count=48, updated=True, ok=True,
        started_at="2026-09-19T10:11:15+08:00",
        download_finished_at="2026-09-19T10:12:00+08:00",
        pulled_at=old_stamp,
        manifest_sha256="a" * 64,          # 上一版（A）的指纹
    )])
    # 当前数据已经是另一版：清单里带 revision=2，指纹与 A 的记录不同。
    write_json(tmp_path / "data" / "snapshots" / TARGET_DATE / "manifest.json",
               {"collected_slot_count": 48, "revision": 2})

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    assert old_stamp[:16] not in payload["line"]
    assert f"本地 {old_stamp[5:10]} {old_stamp[11:16]} 收到" not in payload["line"]
    text = " ".join(payload["explains"])
    assert f"快照 {TARGET_DATE} 的当前数据版本没有对应的到达记录" in text
    assert "本地到达时间未知" in text
    assert old_stamp[11:16] not in text, "A 版的时间不能出现在当前数据的说明里"


def test_t17_receipt_without_version_evidence_is_unknown(tmp_path: Path) -> None:
    """S1 反例：旧记录没有数据版本凭证时如实标未知，不改写历史也不冒认。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_pull_log(tmp_path, [{
        "date": TARGET_DATE, "ok": True, "published": True,
        "pulled_at": "2026-09-19T10:12:02+08:00",
    }])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    assert "收到）" not in payload["line"]
    text = " ".join(payload["explains"])
    assert f"快照 {TARGET_DATE} 的到达记录没有数据版本凭证" in text
    assert "本地到达时间未知" in text


@pytest.mark.parametrize("field", ["collected_at", "generated_at"])
def test_t18_collection_and_generation_are_not_publication(tmp_path: Path, field: str) -> None:
    """S1 反例：清单只有采集/生成时间时不能改称上游发布时间（同一问题的两个变体）。"""

    ok_day = f"{PREV_DATE}T10:12:02+08:00"
    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_json(tmp_path / "data" / "snapshots" / TARGET_DATE / "manifest.json",
               {"collected_slot_count": 48, field: ok_day})
    write_pull_log(tmp_path, [pull_log_row(
        tmp_path, TARGET_DATE, ok=True,
        started_at="2026-09-19T10:11:15+08:00",
        download_finished_at="2026-09-19T10:12:00+08:00",
        pulled_at="2026-09-19T10:12:02+08:00",
    )])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    text = " ".join(payload["explains"])
    assert "上游发布 未知" in text
    assert f"上游发布 {ok_day}" not in text
    assert f"上游采集/生成 {ok_day}（不是发布时间）" in text


def test_t19_explicit_publication_field_keeps_its_name(tmp_path: Path) -> None:
    """S1 正常对照：清单里有明确发布字段时照实显示，采集/生成字段不冒充它。"""

    publish_stamp = f"{TARGET_DATE}T15:05:00+08:00"
    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    write_json(tmp_path / "data" / "snapshots" / TARGET_DATE / "manifest.json",
               {"collected_slot_count": 48, "upstream_published_at": publish_stamp,
                "generated_at": f"{TARGET_DATE}T15:01:00+08:00"})
    write_pull_log(tmp_path, [pull_log_row(
        tmp_path, TARGET_DATE, ok=True, pulled_at="2026-09-19T10:12:02+08:00")])

    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])

    text = " ".join(payload["explains"])
    assert f"上游发布 {publish_stamp}" in text
    assert f"上游采集/生成 {TARGET_DATE}T15:01:00+08:00（不是发布时间）" in text
    assert "（本地 09-19 10:12 收到）" in payload["line"], "同版到达记录仍要显示"


def set_benchmark_days(root: Path, days: dict[str, str | None]) -> None:
    """按文件逐个设置基准日期；``None`` 表示删掉该文件（模拟缺失）。"""
    bench_dir = root / "data" / "benchmark"
    bench_dir.mkdir(parents=True, exist_ok=True)
    for name, day in days.items():
        path = bench_dir / name
        if day is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f"date,close\n{day},100\n", encoding="utf-8")


def test_t20_benchmark_on_or_after_expected_day_is_not_stale(tmp_path: Path) -> None:
    """基准已更新到目标日（或更新）不算 stale：✓，且显示三只里最旧的日期。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    set_benchmark_days(tmp_path, {"hs300.csv": TARGET_DATE, "zz500.csv": PREV_DATE,
                                  "sh000001.csv": TARGET_DATE})

    cell, extra = load_mod().benchmark_cell(tmp_path, TARGET_DATE)
    assert cell == f"基准 ✓{PREV_DATE}", (cell, extra)
    payload = run_cli(tmp_path, ["--dry-run", "--target-date", TARGET_DATE])
    assert f"基准 ✓{PREV_DATE}" in payload["line"]
    assert "基准 ✗" not in payload["line"]


def test_t21_only_the_late_file_is_named_stale(tmp_path: Path) -> None:
    """一只早于 expected（其余已到目标日）→ ✗，且只点名那只。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    set_benchmark_days(tmp_path, {"hs300.csv": "2026-09-16", "zz500.csv": PREV_DATE,
                                  "sh000001.csv": TARGET_DATE})

    cell, extra = load_mod().benchmark_cell(tmp_path, TARGET_DATE)
    assert cell == "基准 ✗"
    assert "应有 2026-09-17" in extra and "hs300=2026-09-16" in extra, extra
    assert "zz500" not in extra and "sh000001" not in extra, extra


def test_t22_missing_benchmark_file_is_stale_and_named(tmp_path: Path) -> None:
    """文件缺失也算 stale，并点名。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    set_benchmark_days(tmp_path, {"hs300.csv": None})

    cell, extra = load_mod().benchmark_cell(tmp_path, TARGET_DATE)
    assert cell == "基准 ✗"
    assert "hs300=缺失" in extra, extra


def test_t23_unreadable_benchmark_file_is_stale_and_named(tmp_path: Path) -> None:
    """文件在、但读不出日期（表头以外没有数据）也算 stale，并点名。"""

    health_scenario(tmp_path, daily=100, benchmark=PREV_DATE, slots=48, collected=48,
                    minute=GOOD_MINUTE)
    (tmp_path / "data" / "benchmark" / "hs300.csv").write_text("date,close\n", encoding="utf-8")

    cell, extra = load_mod().benchmark_cell(tmp_path, TARGET_DATE)
    assert cell == "基准 ✗"
    assert "hs300=读不出" in extra, extra
