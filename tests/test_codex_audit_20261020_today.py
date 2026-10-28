"""Current dated candidates and market dates through the actual read endpoints."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from quantdesk import daily_candidate_refresh as refresh
from scripts import quant_api_server as server
from scripts.server.routes import market_brain as brain
from tests.test_b5_formal_candidate_intake import make_formal_batch

TARGET = "2026-10-19"


def dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def today_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "DAILY_CANDIDATE_GENERATION_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(server, "DAILY_CANDIDATE_LATEST_CSV", tmp_path / "legacy.csv")
    monkeypatch.setattr(server, "DAILY_DIR", tmp_path / "data/daily")
    monkeypatch.setattr(server, "UNIVERSE_DIR", tmp_path / "data/universe")
    monkeypatch.setattr(server, "TRACKING_DIR", tmp_path / "data/tracking")
    monkeypatch.setattr(server, "load_candidate_ledger", lambda: pd.DataFrame())
    monkeypatch.setattr(server, "load_candidate_review_overrides", lambda: {})
    monkeypatch.setattr(server, "load_stock_data", lambda _s: pd.DataFrame())
    monkeypatch.setattr(server, "attach_reference_confidence", lambda row: row)
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date.fromisoformat(TARGET))
    return tmp_path


def test_today_reads_verified_dated_batch_without_legacy_pointer(today_root: Path) -> None:
    batch = make_formal_batch(TARGET)
    p = today_root / f"results/candidate_refresh/daily/{TARGET}.json"
    dump(p, batch)
    generation = server.latest_generation_status()
    rows = server.candidate_rows(limit=18)
    assert generation["signal_date"] == TARGET
    assert len([r for r in rows if r["is_today_recommendation"]]) == len(batch["candidates"])
    assert rows[0]["detail_lane"] in {"long", "board"}
    assert rows[0]["batch_identity"]["source_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert rows[0]["batch_identity"]["source_run_id"] == batch["provenance"]["source_run_id"]
    assert not (today_root / "legacy.json").exists()


@pytest.mark.parametrize("bad", ["hash", "date", "synthetic"])
def test_invalid_formal_file_never_borrows_legacy_today(today_root: Path, bad: str) -> None:
    batch = make_formal_batch(TARGET)
    if bad == "hash":
        batch["provenance"]["hash"] = "0" * 64
    elif bad == "date":
        batch = make_formal_batch("2026-10-20")
    else:
        batch["synthetic_test_data"] = True
    dump(today_root / f"results/candidate_refresh/daily/{TARGET}.json", batch)
    dump(today_root / "legacy.json", {"signal_date": TARGET, "candidate_count": 9})
    result = server.latest_generation_status()
    assert result["status"] == "blocked" and result["candidate_count"] == 0
    assert not any(r["is_today_recommendation"] for r in server.candidate_rows(limit=18))


def test_future_legacy_generation_is_not_current(today_root: Path) -> None:
    dump(today_root / "legacy.json", {"signal_date": "2026-10-20", "candidate_count": 1})
    assert server.generation_status_for_ui()["freshness_status"] == "future"


def test_same_day_wrong_run_tracking_does_not_override_formal_batch(today_root: Path, monkeypatch) -> None:
    batch = make_formal_batch(TARGET)
    dump(today_root / f"results/candidate_refresh/daily/{TARGET}.json", batch)
    symbol = batch["candidates"][0]["symbol"]
    monkeypatch.setattr(server, "load_candidate_ledger", lambda: pd.DataFrame([{
        "ts_code": symbol, "stock_name": "Wrong run", "strategy_type": "board", "candidate_role": "daily_recommendation",
        "signal_date": TARGET, "entry_date": TARGET, "source_run_id": "other", "source_hash": "0" * 64}]))
    rows = server.candidate_rows(limit=18)
    same = next(row for row in rows if row["symbol"] == symbol)
    assert same["name"] == batch["candidates"][0]["name"]
    assert same["batch_identity"]["source_run_id"] == batch["provenance"]["source_run_id"]


@pytest.mark.parametrize("source_day,status", [("2026-09-08", "stale"), (TARGET, "current"),
                                               ("2026-10-20", "future"), ("", "unknown")])
def test_market_brain_endpoint_surfaces_target_and_source_dates(tmp_path: Path, monkeypatch, source_day: str, status: str) -> None:
    monkeypatch.setattr(brain, "resolve_target_complete_trade_date", lambda **_k: {"target_date": TARGET})
    dump(tmp_path / "market_brain_snapshot_latest.json", {"snapshot": {
        "as_of": source_day, "target_date": source_day, "data_cutoff": source_day,
        "formal_regime": "RANGE", "confidence": .6, "evidence_eligible": True}})
    app = Flask(__name__)
    app.register_blueprint(brain.create_market_brain_blueprint(brain.MarketBrainRouteDeps(tmp_path)))
    payload = app.test_client().get("/api/market-brain/latest").get_json()
    assert payload["freshness"]["status"] == status
    assert payload["freshness"]["target_date"] == TARGET
    assert payload["formal_current_evidence"] is (status == "current")
    assert payload["artifact_sha256"]
