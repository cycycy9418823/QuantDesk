"""S4 wiring: default-off means nothing happens; one slot drives screen→decide→fill.

Offline by construction: local snapshot folders under tmp_path, a fake model, and
a tmp_path book. Nothing here opens a socket.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from ai_trader import daytime_decide, daytime_runner
from ai_trader.paths import CyclePaths

DAY = "2026-09-22"
NOW = datetime(2026, 9, 22, 9, 45, 10)
GOOD = json.dumps({
    "want_minutes": [],
    "orders": [{"symbol": "600000", "side": "buy", "qty": 200, "reason": "本槽放量上穿均价",
                "stop": 9.8, "valid_until_slot": "1000"}],
    "note": "一只",
}, ensure_ascii=False)


def _screen_rows() -> list[dict]:
    return [{"symbol": "600000", "name": "浦发银行", "price": 10.0, "pct_chg": 1.2},
            {"symbol": "600111", "name": "北方稀土", "price": 20.0, "pct_chg": -0.5}]


def _write_slot_csv(root: Path, slot: str = "0940") -> Path:
    directory = root / "data" / "snapshots" / DAY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slot}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "name", "price", "pct_chg"])
        writer.writeheader()
        for row in _screen_rows():
            writer.writerow(row)
    return path


class FakeModel:
    def __init__(self, text: str = GOOD) -> None:
        self.text = text
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, max_tokens: int, timeout: float) -> daytime_decide.ModelCall:
        self.prompts.append(prompt)
        return daytime_decide.ModelCall(text=self.text, model="fake-model", elapsed_ms=42,
                                        prompt_chars=len(prompt), ok=True)


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _paths(tmp_path: Path) -> CyclePaths:
    state = _state(tmp_path)
    return CyclePaths(ledger=state / "ledger.json", plan_latest=state / "plan_latest.json",
                      watchlist=state / "watchlist.json",
                      daytime_intents=state / "daytime_intents.jsonl",
                      daytime_notes=state / "daytime_notes.jsonl")


def test_default_off_does_no_work(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(daytime_decide.DAYTIME_ENV, raising=False)
    result = daytime_runner.run_slot(tmp_path, now=NOW)
    assert result["enabled"] is False and result["calls"] == 0 and result["fills"] == 0
    assert not (tmp_path / "state").exists()
    all_profiles = daytime_runner.run_all_profiles(tmp_path, now=NOW)
    assert all_profiles["enabled"] is False and all_profiles["profiles"] == []


def test_one_slot_runs_screen_decide_and_fill(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    paths = _paths(tmp_path)
    model = FakeModel()
    result = daytime_runner.run_slot(tmp_path, now=NOW, slot="0940", rows=_screen_rows(),
                                     quote_slot="0940", source="cloud_slot",
                                     quote_time="2026-09-22T09:40:30+08:00", llm=model,
                                     paths=paths)
    assert result["enabled"] is True
    assert result["screen_chars"] > 0 and result["intents"] == 1 and result["calls"] == 1
    assert result["model_used"] is True and result["idle_day"] is True
    assert result["ledger_written"] is False
    assert "600000" in paths.daytime_intents.read_text(encoding="utf-8")


def test_first_trading_day_idles_and_the_second_day_is_not_first(tmp_path,
                                                                monkeypatch) -> None:
    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    monkeypatch.setenv(daytime_runner.daytime_fill.DRY_ENV, "0")
    paths = _paths(tmp_path)
    assert daytime_runner.is_first_trading_day(paths) is True
    first = daytime_runner.run_slot(tmp_path, now=NOW, slot="0940", rows=_screen_rows(),
                                    quote_slot="0940", source="cloud_slot",
                                    quote_time="2026-09-22T09:40:30+08:00", llm=FakeModel(),
                                    paths=paths)
    assert first["idle_day"] is True and first["ledger_written"] is False
    assert not (paths.daytime_notes.parent / "daytime_dry_fills.jsonl").exists()
    # One trade on record ends "first day" for this book.
    paths.ledger.write_text(json.dumps({
        "schema_version": "ai_trader_ledger_v1", "ledger_id": "ai_trader_100k", "cash": 99000.0,
        "equity": 99000.0, "positions": [],
        "trades": [{"date": "2026-09-19", "symbol": "600000", "side": "buy", "quantity": 100}],
    }, ensure_ascii=False), encoding="utf-8")
    assert daytime_runner.is_first_trading_day(paths) is False


def test_slot_rows_prefers_the_slot_csv_then_reports_nothing(tmp_path) -> None:
    missing = daytime_runner.slot_rows(tmp_path, DAY, "0940")
    assert missing["rows"] == [] and missing["reason"] == "no_slot_and_no_spot"
    _write_slot_csv(tmp_path, "0940")
    from_slot = daytime_runner.slot_rows(tmp_path, DAY, "0940")
    assert from_slot["source"] == daytime_runner.SOURCE_CLOUD_SLOT and len(from_slot["rows"]) == 2
    assert from_slot["time_evidence"] == "file_mtime" and from_slot["time"]
    assert daytime_runner.slot_rows(tmp_path, DAY, "0950")["rows"] == []


def test_floor_slot_rounds_down_to_five_minutes() -> None:
    assert daytime_runner.floor_slot(datetime(2026, 9, 22, 9, 43, 59)) == "0940"
    assert daytime_runner.floor_slot(datetime(2026, 9, 22, 9, 45, 0)) == "0945"
    assert daytime_runner.floor_slot(datetime(2026, 9, 22, 14, 59, 59)) == "1455"


def test_run_all_profiles_keeps_one_failure_to_itself(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(daytime_decide.DAYTIME_ENV, "1")
    _state(tmp_path)
    seen: list[str] = []

    def factory(profile: str):
        seen.append(profile or "default")
        if profile:
            raise RuntimeError("second book exploded")
        return FakeModel()

    result = daytime_runner.run_all_profiles(
        tmp_path, now=NOW, slot="0940",
        rows_provider=lambda root, as_of, slot: _screen_rows(),
        llm_factory=factory, paths_factory=lambda profile: _paths(tmp_path), write=False)
    assert result["enabled"] is True
    labels = [run["profile"] for run in result["profiles"]]
    assert labels and "default" in labels
    assert seen == labels
    if len(labels) > 1:
        broken = [run for run in result["profiles"] if run.get("ok") is False]
        assert broken and broken[0]["fills"] == 0
