"""S1 daytime screen: rendering, sorting, fallback and read-only guarantees.

Offline only: every fixture lives under tmp_path and every path is injected, so the
real state/ and data/ are never touched.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from ai_trader import daytime_screen
from ai_trader.paths import CyclePaths

DAY = "2026-09-22"
SLOT = "0940"
NOW = datetime(2026, 9, 22, 9, 42, 30)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ("snapshot_date", "snapshot_time", "symbol", "name", "price", "pct_chg", "change",
               "volume", "amount", "open", "pre_close", "high", "low", "quote_date", "quote_time",
               "source", "fetched_at")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({**{key: "" for key in columns}, **row})


def _rows(count: int, *, start: float = 10.0, volume: float = 1000.0) -> list[dict]:
    out = []
    for index in range(count):
        symbol = f"{600000 + index:06d}"
        out.append({"snapshot_date": DAY, "snapshot_time": SLOT, "symbol": symbol,
                    "name": f"样本{index}", "price": start + index, "pct_chg": float(index - 15),
                    "volume": volume + index, "amount": 10_000.0 + index * 100,
                    "quote_date": DAY, "quote_time": "09:40:05", "source": "fixture.sina",
                    "fetched_at": f"{DAY}T09:40:32+08:00"})
    return out


def _root(tmp_path: Path, rows: list[dict], *, slot: str = SLOT, spot: bool = True,
          previous: list[dict] | None = None) -> Path:
    _write_rows(tmp_path / "data" / "snapshots" / DAY / f"{slot}.csv", rows)
    (tmp_path / "data" / "snapshots" / DAY / f"{slot}.json").write_text(
        json.dumps({"fetched_at": f"{DAY}T09:40:32+08:00", "status": "written"}), encoding="utf-8")
    if previous is not None:
        _write_rows(tmp_path / "data" / "snapshots" / DAY / "0935.csv", previous)
    if spot:
        _write_rows(tmp_path / "data" / "cache" / "spot_snapshot_latest.csv", rows)
    (tmp_path / "data" / "benchmark").mkdir(parents=True, exist_ok=True)
    for name in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        (tmp_path / "data" / "benchmark" / name).write_text("date,close\n2026-09-21,100\n",
                                                            encoding="utf-8")
    return tmp_path


def _paths(tmp_path: Path) -> CyclePaths:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    (state / "ledger.json").write_text(json.dumps({
        "ledger_id": "ai_trader_100k", "cash": 50_000.0, "as_of": DAY,
        "positions": [{"symbol": "600000", "quantity": 100, "avg_cost": 9.5,
                       "acquired_on": "2026-09-18", "thesis": "低估值修复", "planned_hold_days": 10}],
        "trades": [{"date": DAY, "symbol": "600003", "side": "buy", "quantity": 100, "price": 13.0},
                   {"date": "2026-09-21", "symbol": "600004", "side": "sell", "quantity": 100,
                    "price": 12.0}],
    }), encoding="utf-8")
    (state / "plan_latest.json").write_text(json.dumps({
        "decision_date": "2026-09-21", "trade_date": DAY,
        "intents": [{"symbol": "600002", "side": "buy", "precondition": "preset_touch_price"}],
    }), encoding="utf-8")
    (state / "watchlist.json").write_text(json.dumps({
        "items": [{"symbol": "600005", "added_on": DAY, "reason": "放量"}],
    }), encoding="utf-8")
    (state / "daytime_intents.jsonl").write_text(
        json.dumps({"as_of": DAY, "slot": SLOT, "symbol": "600006", "side": "buy", "quantity": 100,
                    "status": "pending"}, ensure_ascii=False) + "\n"
        + json.dumps({"as_of": DAY, "slot": "0935", "symbol": "600007", "side": "sell",
                      "status": "filled"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (state / "daytime_notes.jsonl").write_text("", encoding="utf-8")
    (state / "daytime_journal.jsonl").write_text(
        json.dumps({"as_of": DAY, "slot": SLOT, "symbol": "600008", "side": "buy", "qty": 100,
                    "blocked_by": "max_opens_per_day", "ai_reason": "想追涨"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    return CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                      watchlist=state / "watchlist.json",
                      daytime_intents=state / "daytime_intents.jsonl",
                      daytime_notes=state / "daytime_notes.jsonl")


def test_screen_renders_a_thirty_row_snapshot_within_budget(tmp_path: Path) -> None:
    root = _root(tmp_path, _rows(30))
    payload = daytime_screen.build_and_render(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)
    screen, text = payload["screen"], payload["text"]

    assert payload["within_budget"] is True
    assert len(text) <= daytime_screen.MAX_TEXT_CHARS
    assert screen["slot"] == SLOT
    assert screen["source"] == "cloud_slot"
    assert screen["snapshot_time"] == f"{DAY}T09:40:32+08:00"
    assert screen["coverage"] == {"rows": 30, "valid_quotes": 30, "ratio": 1.0}
    assert "有效报价 30/30" in text
    for marker in ("持仓", "昨晚计划意图", "自选", "涨幅前", "跌幅前", "成交额前", "量比(代理",
                   "5 分钟涨幅前", "今日已成交", "被家规拦下", "只读"):
        assert marker in text, marker


def test_positions_show_cost_price_move_hold_days_and_thesis(tmp_path: Path) -> None:
    root = _root(tmp_path, _rows(30))
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)
    position = screen["positions"][0]
    assert position["symbol"] == "600000"
    assert position["avg_cost"] == 9.5
    assert position["price"] == 10.0
    assert position["held_days"] == 4
    assert position["thesis"] == "低估值修复"


def test_top_lists_sort_and_ignore_unusable_quotes(tmp_path: Path) -> None:
    rows = _rows(30)
    rows[5]["price"] = 0
    root = _root(tmp_path, rows)
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)
    gainers = screen["top"]["gainers"]
    losers = screen["top"]["losers"]
    assert [item["value"] for item in gainers] == sorted((item["value"] for item in gainers),
                                                         reverse=True)
    assert [item["value"] for item in losers] == sorted(item["value"] for item in losers)
    assert all(item["symbol"] != "600005" for item in gainers)
    assert len(gainers) <= daytime_screen.TOP_N


def test_slot_movers_and_volume_proxy_use_the_previous_slot(tmp_path: Path) -> None:
    current = _rows(3, volume=2000.0)
    current[0]["price"] = 11.0
    previous = _rows(3, volume=1000.0)
    previous[0]["price"] = 10.0
    root = _root(tmp_path, current, previous=previous)
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)

    mover = next(item for item in screen["slot_movers"] if item["symbol"] == "600000")
    assert mover["from"] == 10.0 and mover["to"] == 11.0
    assert mover["move_pct"] == pytest.approx(10.0)
    proxy = next(item for item in screen["top"]["volume_ratio_proxy"] if item["symbol"] == "600000")
    assert proxy["value"] == pytest.approx(2.0)
    assert screen["previous_slot"] == "0935"


def test_falls_back_to_local_spot_and_labels_it(tmp_path: Path) -> None:
    root = _root(tmp_path, _rows(30), spot=True)
    (root / "data" / "snapshots" / DAY / f"{SLOT}.csv").unlink()
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW)
    assert screen["source"] == "local_spot"
    assert "来源 local_spot" in daytime_screen.render_text(screen)


def test_missing_everything_warns_instead_of_inventing(tmp_path: Path) -> None:
    (tmp_path / "state" / "ai_trader").mkdir(parents=True, exist_ok=True)
    screen = daytime_screen.build_screen(tmp_path, paths=_paths(tmp_path), now=NOW)
    assert screen["coverage"]["rows"] == 0
    assert "no_slot_and_no_local_spot" in screen["warnings"]
    assert "缺失" in daytime_screen.render_text(screen)


def test_today_sections_show_pending_fills_and_mandate_blocks(tmp_path: Path) -> None:
    root = _root(tmp_path, _rows(30))
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)
    today = screen["today"]
    assert [item["symbol"] for item in today["fills"]] == ["600003"]
    assert [item["symbol"] for item in today["pending_intents"]] == ["600006"]
    assert today["blocked"][0]["blocked_by"] == "max_opens_per_day"
    text = daytime_screen.render_text(screen)
    assert "今日已成交 1 笔" in text and "待成交意图 1 条" in text and "被家规拦下 1 笔" in text
    assert "家规:max_opens_per_day" in text and "AI理由:想追涨" in text


def test_screen_is_read_only(tmp_path: Path) -> None:
    root = _root(tmp_path, _rows(30))
    paths = _paths(tmp_path)
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    daytime_screen.build_and_render(root, paths=paths, now=NOW, slot=SLOT)
    after = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    assert before == after


def test_index_rows_are_taken_from_the_slot_when_present(tmp_path: Path) -> None:
    rows = _rows(30)
    rows.append({"snapshot_date": DAY, "snapshot_time": SLOT, "symbol": "sh000001",
                 "name": "上证指数", "price": 3200.0, "pct_chg": 0.5})
    root = _root(tmp_path, rows)
    screen = daytime_screen.build_screen(root, paths=_paths(tmp_path), now=NOW, slot=SLOT)
    assert screen["index_basis"] == "slot"
    assert any("上证指数" in item["name"] for item in screen["indices"])
