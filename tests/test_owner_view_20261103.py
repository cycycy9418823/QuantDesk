"""Owner-facing brief and health line (2026-11-03 iteration).

Covers the plain-language morning push, its fallback to the technical text,
the weekday-holiday skip, the owner's snapshot rule (CHARTER §12 #18), the
backup cell that only judges the backup itself, and the STATUS rewrite that
keeps the health line on top without piling up old copies.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk import owner_view
from quantdesk.morning_brief import PROHIBITED_MESSAGE_TERMS, run_morning_brief

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


BRIEF_TESTS = _load("brief_fixture_20261103", ROOT / "tests" / "test_morning_brief.py")
HEALTH = _load("health_owner_20261103", ROOT / "scripts" / "ops" / "write_health_line.py")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _snapshot_day(root: Path, day: str, *, sheets: int, bad_sheets: int = 0) -> None:
    fixtures = _load("snapshot_evidence_fixtures", ROOT / "tests/test_snapshot_health_evidence_20261104.py")
    for index, slot in enumerate(fixtures.SLOTS[:sheets]):
        fixtures.write_archive(root, day, slot, bad=2 if index < bad_sheets else 1)


def _backup_log(root: Path, rows: list[dict]) -> None:
    log = root / "logs" / "ops" / "backup_push.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _strategy_latest(root: Path, day: str) -> None:
    _write_json(root / "results" / "auto_paper_portfolio_latest.json", {
        "valuation_date": day,
        "summary": {"total_equity": 77986.5, "total_return_pct": -22.01, "daily_pnl": 656.79,
                    "daily_return_pct": 0.8493, "pnl_identity": {"status": "consistent", "difference": 0.0}},
        "daily_actions": [
            {"action": "buy", "symbol": "300251", "name": "光线传媒", "price": 12.2, "quantity": 100,
             "decision_date": day, "reason": "长线规则满足：收盘价高于20日均线且偏离不超过20%。"},
            {"action": "buy", "symbol": "002009", "name": "天奇股份", "price": 16.88, "quantity": 100,
             "decision_date": day, "reason": "长线规则满足：收盘价高于20日均线且偏离不超过20%。"},
            {"action": "stop_loss", "symbol": "301513", "name": "尚水智能", "price": 44.38, "quantity": 100,
             "decision_date": day, "pnl": -721.01,
             "reason": "触发自动虚拟组合风险复核：最新价较模拟买入价回撤超过 8%，执行纸面退出。"},
        ],
        "positions": [
            {"symbol": "300413", "name": "芒果超媒", "unrealized_return_pct": 40.26, "holding_days": 42},
            {"symbol": "600825", "name": "新华传媒", "unrealized_return_pct": -7.06, "holding_days": 3},
        ],
        "closed_positions": [{"realized_pnl": -100.0} for _ in range(12)],
    })
    _write_json(root / "results" / "auto_paper_portfolio_history.json", {"items": [
        {"valuation_date": "2026-10-13", "total_equity": 92618.61},
        {"generated_at": "2026-09-01T20:06:00", "total_equity": 100181.34},
    ]})
    bench = root / "data" / "benchmark" / "hs300.csv"
    bench.parent.mkdir(parents=True, exist_ok=True)
    bench.write_text("date,open,high,low,close,volume,pct_chg,amount\n"
                     f"2026-10-13,1,1,1,4275.68,1,0.1,1\n{day},1,1,1,4016.918,1,0.7883,1\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Owner rules
# ---------------------------------------------------------------------------

def test_snapshot_rule_counts_sheets_at_99_percent(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=48, bad_sheets=2)
    result = owner_view.snapshot_sheet_quality(tmp_path, "2026-11-02")
    assert (result["sheets"], result["good"], result["ok"]) == (48, 46, True)
    _snapshot_day(tmp_path, "2026-11-03", sheets=48, bad_sheets=3)
    assert owner_view.snapshot_sheet_quality(tmp_path, "2026-11-03")["ok"] is False


def test_snapshot_sheet_without_csv_does_not_count(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=46)
    (tmp_path / "data/snapshots/2026-11-02/0930.csv").unlink()
    result = owner_view.snapshot_sheet_quality(tmp_path, "2026-11-02")
    assert (result["good"], result["ok"]) == (45, False)


def test_missing_snapshot_day_is_not_green(tmp_path: Path) -> None:
    assert owner_view.snapshot_sheet_quality(tmp_path, "2026-11-02")["ok"] is False


def test_backup_copy_status_reads_only_the_push_result(tmp_path: Path) -> None:
    _backup_log(tmp_path, [
        {"stamp": "snapshot_20261030T140017Z", "exit_code": 0, "all_hashes_match": True, "file_count": 80},
        {"stamp": "snapshot_20261102T140017Z", "stage": "verify", "event": "stage_enter", "exit_code": None},
        {"stamp": "snapshot_20261102T140017Z", "exit_code": 0, "all_hashes_match": True, "file_count": 82},
    ])
    ok = owner_view.backup_copy_status(tmp_path, "2026-11-02")
    assert ok["ok"] is True and ok["file_count"] == 82
    _backup_log(tmp_path, [
        {"stamp": "snapshot_20261030T140017Z", "exit_code": 0, "all_hashes_match": True},
        {"stamp": "snapshot_20261102T140017Z", "exit_code": 0, "all_hashes_match": False},
    ])
    failed = owner_view.backup_copy_status(tmp_path, "2026-11-02")
    assert failed["ok"] is False and failed["last_success"] == "2026-10-30"


# ---------------------------------------------------------------------------
# Morning brief text
# ---------------------------------------------------------------------------

def test_owner_brief_is_plain_short_and_has_no_order_words(tmp_path: Path) -> None:
    _strategy_latest(tmp_path, "2026-11-02")
    digest = owner_view.build_owner_digest(tmp_path, "2026-11-02")
    title, body = owner_view.render_owner_brief(digest, today=date(2026, 11, 3))
    assert title == "早报 11/3｜策略 +0.85% 沪深300 +0.79%"
    for section in ("两本账", "策略账昨天做了什么", "今天值得看", "想一想"):
        assert section in body
    assert "新开 2 只：光线传媒 12.20、天奇股份 16.88" in body
    assert "平掉 1 只：尚水智能（跌破 -8% 止损线" in body
    assert "10/13 以来：策略账 -15.8%，同期沪深300 -6.1%" in body
    assert "新华传媒（-7.1%）离 -8% 止损线不远" in body
    assert not any(term in title + body for term in PROHIBITED_MESSAGE_TERMS)
    assert "daily-data-chain" not in body and "127.0.0.1" not in body and "Research Only" not in body
    assert len(body) < 900


def test_owner_brief_says_when_the_book_did_not_update(tmp_path: Path) -> None:
    _strategy_latest(tmp_path, "2026-10-30")
    digest = owner_view.build_owner_digest(tmp_path, "2026-11-02")
    title, body = owner_view.render_owner_brief(digest, today=date(2026, 11, 3))
    assert title == "早报 11/3｜策略账没更新"
    assert "没有更新到 11/2，估值还停在 10/30" in body
    assert "新开" not in body


def test_owner_brief_never_invents_numbers_when_inputs_are_missing(tmp_path: Path) -> None:
    digest = owner_view.build_owner_digest(tmp_path, "2026-11-02")
    title, body = owner_view.render_owner_brief(digest, today=date(2026, 11, 3))
    assert "读不到" in body
    assert "+0.00%" not in body and "0 元" not in body


# ---------------------------------------------------------------------------
# Delivery path
# ---------------------------------------------------------------------------

def _capture_sender(sent: list[dict]):
    def sender(**kwargs: object) -> dict:
        sent.append(dict(kwargs))
        return {"status": "sent", "reason_code": "provider_accepted", "http_status": 200, "provider_code": 0}
    return sender


def test_push_uses_owner_text_and_keeps_technical_preview(tmp_path: Path) -> None:
    BRIEF_TESTS._seed_root(tmp_path)
    _strategy_latest(tmp_path, "2026-07-31")
    sent: list[dict] = []
    result = run_morning_brief(tmp_path, network=True, push=True, dry_run=False,
                               now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
                               _sendkey_loader=lambda: "SCT_FAKE", sender=_capture_sender(sent))
    assert result["status"] == "sent"
    assert sent[0]["title"].startswith("早报 8/3｜")
    assert "两本账" in sent[0]["body"] and "daily-data-chain" not in sent[0]["body"]
    preview = (tmp_path / "reports/morning_brief/morning_brief_latest.md").read_text(encoding="utf-8")
    assert "## 技术细节" in preview and "daily-data-chain-20260731T103005Z-fixture" in preview
    payload = json.loads((tmp_path / "results/morning_brief/morning_brief_latest.json").read_text(encoding="utf-8"))
    assert payload["owner_brief"]["version"].startswith("owner-brief-v1")


def test_owner_text_failure_falls_back_to_the_technical_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    BRIEF_TESTS._seed_root(tmp_path)

    def broken(*args: object, **kwargs: object):
        raise RuntimeError("simulated renderer bug")

    monkeypatch.setattr(owner_view, "render_owner_brief", broken)
    sent: list[dict] = []
    result = run_morning_brief(tmp_path, network=True, push=True, dry_run=False,
                               now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
                               _sendkey_loader=lambda: "SCT_FAKE", sender=_capture_sender(sent))
    assert result["status"] == "sent"
    assert sent[0]["title"] == "QuantDesk 研究早报 2026-07-31"
    payload = json.loads((tmp_path / "results/morning_brief/morning_brief_latest.json").read_text(encoding="utf-8"))
    assert payload["owner_brief"] == {"error": "RuntimeError"}


def test_weekday_market_holiday_is_skipped_before_sending(tmp_path: Path) -> None:
    BRIEF_TESTS._seed_root(tmp_path)
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.write_text(calendar.read_text(encoding="utf-8").replace("2026-08-04,1,fixture", "2026-08-04,0,fixture"),
                        encoding="utf-8")
    from quantdesk.trading_calendar import clear_calendar_cache
    clear_calendar_cache()
    sent: list[dict] = []
    result = run_morning_brief(tmp_path, network=True, push=True, dry_run=False,
                               now=datetime(2026, 8, 4, 8, 35, tzinfo=SHANGHAI),
                               _sendkey_loader=lambda: "SCT_FAKE", sender=_capture_sender(sent))
    assert result["status"] == "skipped_non_trading_day"
    assert sent == []
    runner = _load("run_brief_20261103", ROOT / "scripts" / "run_morning_brief.py")
    assert runner._exit_code(result) == 0


# ---------------------------------------------------------------------------
# Health line owner layer
# ---------------------------------------------------------------------------

def _owner_cells(**overrides: tuple[str, str]) -> dict:
    cells = {
        "daily": ("日线 ✓5570/5576(缺6)（研究资料✗）", ""),
        "benchmark": ("基准 ✓2026-11-02", ""),
        "snapshot": ("快照 ✗48张/覆盖完成 0/字段核实 0", "部分覆盖 48 档"),
        "minute": ("分钟 ✗81/87(缺6)", "分钟数据日 2026-11-02，缺6"),
        "strategy": ("策略账 ✓", ""),
        "ai": ("AI账 △0笔", "默认 规则回退（模型未参与）"),
        "review": ("核对 ✓", ""),
        "backup": ("备份 ✗", "文件复制已核对；当日研究资料尚未证明能完整恢复，见研究资料清单。"),
    }
    cells.update(overrides)
    return {"trading": True, "cells": cells, "line": "", "explains": []}


def test_owner_line_follows_charter_six_cells_and_owner_rules(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=48)
    _backup_log(tmp_path, [{"stamp": "snapshot_20261102T140017Z", "exit_code": 0, "all_hashes_match": True}])
    owner = HEALTH.owner_health(tmp_path, "2026-11-02", _owner_cells())
    assert owner["line"] == "日线 ✓5570/5576 · 快照 ✓48/48 · 策略账 ✓ · AI账 ✗0笔（模型未核实） · 核对 ✓ · 备份 ✓"
    assert "全部正常" not in owner["plain"]
    for warning in ("可用报价", "完整OHLCV", "分钟 ✗81/87(缺6)", "模型未参与", "不等于进程已暂停", "研究资料仍有缺口"):
        assert warning in owner["plain"]
    assert owner["explain_line"].startswith("（") and "健康业务日期 2026-11-02" in owner["explain_line"]


def test_owner_red_cells_get_one_plain_sentence_each(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=11)
    _backup_log(tmp_path, [{"stamp": "snapshot_20261030T140017Z", "exit_code": 0, "all_hashes_match": True}])
    _write_json(tmp_path / "results/auto_paper_portfolio_latest.json", {"valuation_date": "2026-10-30"})
    owner = HEALTH.owner_health(tmp_path, "2026-11-02", _owner_cells(
        strategy=("策略账 ✗", "策略账估值日 2026-10-30"), review=("核对 ✗1条陈述不符", "默认 1 · glm 0")))
    assert owner["line"] == "日线 ✓5570/5576 · 快照 ✗11/48 · 策略账 ✗ · AI账 ✗0笔（模型未核实） · 核对 ✗1条陈述不符 · 备份 ✗"
    assert len(owner["red_sentences"]) == 5
    assert "快照：盘中快照只有 11 张合格" in owner["plain"]
    assert "策略账：没有更新到 2026-11-02（估值停在 2026-10-30）" in owner["plain"]
    assert "AI 有 1 条说法和数据对不上" in owner["plain"]
    assert "上次成功 2026-10-30" in owner["plain"]
    assert "strategy_pnl_identity_mismatch" not in owner["plain"]


def test_owner_green_acquisition_never_claims_complete_minute_session(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=48)
    _backup_log(tmp_path, [{"stamp": "snapshot_20261102T140017Z", "exit_code": 0, "all_hashes_match": True}])
    owner = HEALTH.owner_health(tmp_path, "2026-11-02", _owner_cells(
        daily=("日线 ✓5570/5576", ""), snapshot=("快照 ✓48张/覆盖完成 48/字段核实 48", ""),
        minute=("分钟 ✓87/87", ""), ai=("AI账 ✓0笔", "默认 2026-11-02 0笔")))
    assert "AI账 ✓0笔" in owner["line"]
    assert "模型未核实" not in owner["line"]
    assert "不等于完整交易时段已核实" in owner["plain"]
    assert "全部正常" not in owner["plain"]


def test_strategy_plain_reads_current_top_level_identity(tmp_path: Path) -> None:
    _write_json(tmp_path / "results/auto_paper_portfolio_latest.json", {
        "valuation_date": "2026-11-02", "summary": {},
        "pnl_identity": {"status": "mismatch", "difference": 12.34}})
    assert HEALTH._strategy_plain(tmp_path, "2026-11-02") == "账面对不上，差 +12.34 元"


def test_research_gap_in_technical_assessment_is_visible_to_owner(tmp_path: Path) -> None:
    parts = _owner_cells(daily=("日线 ✓5570/5576", ""))
    parts["explains"] = ["研究资料 ✗ 2026-11-02：因子缺1组"]
    owner = HEALTH.owner_health(tmp_path, "2026-11-02", parts)
    assert "研究资料仍有缺口" in owner["plain"]
    assert "程序会重试" not in owner["plain"]
    assert "不影响收盘后的账本" not in owner["plain"]


def test_corrupt_backup_archive_stays_red(tmp_path: Path) -> None:
    _snapshot_day(tmp_path, "2026-11-02", sheets=48)
    _backup_log(tmp_path, [{"stamp": "snapshot_20261102T140017Z", "exit_code": 0, "all_hashes_match": True}])
    owner = HEALTH.owner_health(tmp_path, "2026-11-02", _owner_cells(
        backup=("备份 ✗", "备份文件校验失败；旧复制成功记录不能证明当前备份可用。")))
    assert owner["line"].endswith("备份 ✗")
    assert "备份文件校验失败" in owner["plain"]


def test_status_keeps_health_on_top_and_moves_old_copies_out(tmp_path: Path) -> None:
    status = tmp_path / "STATUS.md"
    other = "<!-- X_BEGIN -->\n## 2026-11-03 · 施工记录\n正文\n<!-- X_END -->\n\n"
    old = ("# STATUS · 更新于 2026-11-02 22:30 · 更新者：health_line\n\n## 今日健康行\n\n"
           "日线 ✓1/1 · 备份 ✗\n健康业务日期 2026-11-02 · 解释\n\n")
    manual = "# STATUS · 更新于 2026-09-17 · Codex（手写）\n\n## 今日健康行\n\n日线 旧\n\n## 尾巴\nKEEP\n"
    status.write_bytes((other + old + manual).encode("utf-8"))
    removed = HEALTH.rewrite_status(tmp_path, "# STATUS · 更新于 2026-11-03 22:30 · 更新者：health_line",
                                    "日线 ✓1/1 · 备份 ✓", "（全部正常，不用管。健康业务日期 2026-11-03）",
                                    details="技术细节：日线 ✓1/1 · 研究资料 ✗")
    text = status.read_bytes().decode("utf-8")
    assert text.startswith("# STATUS · 更新于 2026-11-03 22:30 · 更新者：health_line\n\n## 今日健康行\n\n日线 ✓1/1 · 备份 ✓\n")
    assert text.count("更新者：health_line") == 1
    assert other in text and manual in text
    assert b"".join(removed).decode("utf-8") == old
    # A second night replaces the top block in place instead of stacking another one.
    HEALTH.rewrite_status(tmp_path, "# STATUS · 更新于 2026-11-04 22:30 · 更新者：health_line",
                          "日线 ✓1/1 · 备份 ✓", "（全部正常，不用管。健康业务日期 2026-11-04）",
                          details="技术细节：x")
    again = status.read_bytes().decode("utf-8")
    assert again.count("## 今日健康行") == 2  # ours + the hand-written 09-17 block
    assert "技术细节：日线 ✓1/1" not in again and again.endswith(manual)


def test_main_writes_owner_line_and_machine_readable_history(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    day = "2026-11-02"
    for label in ("ai_trader", "ai_trader_glm"):
        _write_json(tmp_path / "state" / label / "ledger.json", {"as_of": day, "trades": []})
        _write_json(tmp_path / "state" / label / "journal_latest.json", {"as_of": day, "llm": {"used": False}})
    (tmp_path / "STATUS.md").write_text("# STATUS\n原文\n", encoding="utf-8")
    assert HEALTH.main(["--root", str(tmp_path), "--target-date", day]) == 0
    payload = json.loads(capsys.readouterr().out)
    text = (tmp_path / "STATUS.md").read_text(encoding="utf-8")
    assert text.splitlines()[4] == payload["owner_line"]
    assert text.splitlines()[5].startswith("（")
    assert " · ".join(payload["explains"]) in text
    stored = json.loads((tmp_path / "results/health_line/history" / f"{day}.json").read_text(encoding="utf-8"))
    assert stored["owner_line"] == payload["owner_line"] and stored["technical_line"] == payload["line"]
    assert owner_view.health_digest(tmp_path, day)["owner_line"] == payload["owner_line"]


def test_owner_layer_failure_still_writes_the_technical_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = "2026-11-02"
    (tmp_path / "STATUS.md").write_text("# STATUS\n原文\n", encoding="utf-8")

    def broken(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("simulated owner layer bug")

    monkeypatch.setattr(HEALTH, "owner_health", broken)
    assert HEALTH.main(["--root", str(tmp_path), "--target-date", day]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner_error"] == "owner_layer:RuntimeError"
    text = (tmp_path / "STATUS.md").read_text(encoding="utf-8")
    assert text.splitlines()[4] == payload["line"]
    assert not (tmp_path / "results/health_line/history" / f"{day}.json").exists()
