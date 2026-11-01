from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from flask import Flask

from scripts.server.routes.research import create_research_blueprint


def _client(root):
    app = Flask(__name__)
    app.register_blueprint(create_research_blueprint(SimpleNamespace(project_root=lambda: root)))
    return app.test_client()


def test_observed_route_reads_originals_and_compares_exact_dates_without_writes(tmp_path):
    for folder in ("results", "state", "data/benchmark"):
        (tmp_path / folder).mkdir(parents=True)
    history = {"items": [
        {"valuation_date": "2026-10-13", "cash": 900, "market_value": 100, "total_equity": 1000},
        {"valuation_date": "2026-10-27", "cash": 800, "market_value": 100, "total_equity": 900},
        {"generated_at": "2026-10-28T20:00:00", "total_equity": 990},
    ]}
    (tmp_path / "results/auto_paper_portfolio_history.json").write_text(json.dumps(history), encoding="utf-8")
    (tmp_path / "state/auto_paper_portfolio.json").write_text(json.dumps({
        "valuation_date": "2026-10-27", "total_equity": 900,
        "accounting_reconciliation": {"pnl_identity": {"difference": 3.03}},
    }), encoding="utf-8")
    (tmp_path / "data/benchmark/hs300.csv").write_text("date,close\n2026-10-13,100\n2026-10-27,105\n", encoding="utf-8")
    snapshot = lambda: {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in tmp_path.rglob("*") if p.is_file()}
    before = snapshot()
    response = _client(tmp_path).get("/api/paper/observed-performance")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True and data["paper_only"] is True
    report = data["report"]
    assert report["undated_count"] == 1
    assert report["portfolio"]["identity_difference"] == 3.03
    hs300 = report["comparisons"]["hs300"]
    assert (hs300["start"], hs300["end"]) == ("2026-10-13", "2026-10-27")
    assert hs300["recorded_equity_change_pct"] == -10.0
    assert hs300["index_change_pct"] == 5.0
    assert report["comparisons"]["zz500"]["available"] is False
    assert snapshot() == before


def test_missing_or_corrupt_book_is_visible_and_creates_no_files(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/paper/observed-performance")
    assert response.status_code == 200 and response.get_json()["status"] == "unavailable"
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "results").mkdir()
    (tmp_path / "results/auto_paper_portfolio_history.json").write_text("{broken", encoding="utf-8")
    assert client.get("/api/paper/observed-performance").get_json()["ok"] is False
    assert client.post("/api/paper/observed-performance").status_code == 405
