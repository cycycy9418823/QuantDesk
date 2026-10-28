"""AU-05 request conservation through the actual Flask route."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from scripts.server.routes import backtest as route


def write(root: Path, symbol: str, count: int = 160, basis: str = "raw") -> Path:
    path = root / "data/daily" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.bdate_range("2025-01-02", periods=count), "symbol": symbol,
                  "open": 10., "high": 10.1, "low": 9.9, "close": 10.,
                  "volume": 1000, "adjust": basis}).to_csv(path, index=False)
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(route, "project_root", tmp_path)
    app = Flask(__name__)
    route.register_backtest_routes(app)
    return app.test_client()


@pytest.mark.parametrize("short_kind,expected", [("short", "short_history"), ("parse", "csv_parse_failed"), ("future", "no_asof_data")])
def test_request_is_not_silently_shrunk(tmp_path: Path, client, short_kind: str, expected: str) -> None:
    write(tmp_path, "600000")
    path = write(tmp_path, "000001", count=30)
    if short_kind == "parse":
        path.write_text('date,close,adjust\n"unterminated', encoding="utf-8")
    elif short_kind == "future":
        frame = pd.read_csv(path)
        frame["date"] = pd.bdate_range("2027-01-04", periods=30)
        frame.to_csv(path, index=False)
    response = client.post("/api/backtest/run", json={"symbols": ["600000", "000001", "000002", "600000"], "end_date": "2026-10-19"})
    result = response.get_json()
    assert response.status_code == 200
    assert result["status"] == "partial" and result["ok"] is False
    evidence = result["input_evidence"]
    assert evidence["requested_symbols"] == ["600000", "000001", "000002"]
    assert evidence["loaded_symbols"] == ["600000"]
    assert {item["symbol"]: item["reason_code"] for item in evidence["excluded_symbols"]} == {"000001": expected, "000002": "daily_file_missing"}
    assert evidence["requested_count"] == evidence["loaded_count"] + evidence["excluded_count"]
    assert result["metrics_scope"] == "loaded_subset_only"
    assert "2" in result["message_for_ui"]


def test_short_cross_basis_frame_still_blocks_complete_run(tmp_path: Path, client, monkeypatch) -> None:
    write(tmp_path, "600000")
    write(tmp_path, "000001", count=2, basis="qfq")
    monkeypatch.setattr("quantdesk.backtest.simulate_portfolio_backtest", lambda *_a, **_k: pytest.fail("simulation before source-set gate"))
    response = client.post("/api/backtest/run", json={"symbols": ["600000", "000001"], "end_date": "2026-10-19"})
    result = response.get_json()
    assert response.status_code == 422 and result["status"] == "blocked"
    assert result["input_evidence"]["requested_symbols"] == ["600000", "000001"]
    assert result["price_policy_validation"]["reason_codes"] == ["cross_symbol_price_basis_mismatch"]
    assert not (tmp_path / "results/backtest/latest.json").exists()


def test_sample_limit_is_recorded_as_exclusion_not_hidden(tmp_path: Path, client) -> None:
    write(tmp_path, "600000")
    response = client.post("/api/backtest/run", json={"symbols": ["600000", "000001"], "max_symbols": 1, "end_date": "2026-10-19"})
    evidence = response.get_json()["input_evidence"]
    assert evidence["requested_symbols"] == ["600000", "000001"]
    assert evidence["excluded_symbols"] == [{"symbol": "000001", "reason_code": "sample_limit"}]

