"""C-line viewer reads evidence without refreshing, scoring or writing it."""
import json

import pytest
from flask import Flask

from scripts.server.routes import ai_trader as routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "decision_latest.json"
    monkeypatch.setattr(routes, "DECISION_LATEST_PATH", path)
    app = Flask(__name__)
    app.register_blueprint(routes.create_ai_trader_blueprint())
    return app.test_client(), path


def artifact(records):
    return {"schema": "ai_trader_decision_v1", "as_of": "2026-09-07", "count": len(records), "records": records}


def test_complete_record_and_source_dates_survive_without_any_writes(client):
    http, path = client
    row = {"decision_id": "d1", "recorded_at": "2026-09-01T20:20:00+08:00", "decision_date": "2026-09-01",
           "judgment": "test claim", "result": "错", "result_as_of": "2026-09-04", "seen": {"close": None},
           "verification": {"level": 10, "source": "derived_from_declared_horizon"},
           "result_evidence": [{"kind": "window_miss", "observed": [["2026-09-04", 9]]}], "committed": False}
    path.write_text(json.dumps(artifact([row])), encoding="utf-8")
    before = path.read_bytes()
    response = http.get("/api/ai-trader/decisions")
    result = response.get_json()
    assert result["available"] and not result["degraded"]
    assert result["as_of"] == "2026-09-07" and result["records"] == [row]
    assert result["records"][0]["seen"]["close"] is None
    assert response.headers["Cache-Control"] == "no-store"
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    assert http.post("/api/ai-trader/decisions").status_code == 405


@pytest.mark.parametrize("content", [None, "{broken", "[]", '{"schema":"future","records":[]}',
                                     '{"schema":"ai_trader_decision_v1","records":{}}',
                                     '{"schema":"ai_trader_decision_v1","records":[],"count":NaN}'])
def test_unavailable_is_not_zero_records(client, content):
    http, path = client
    if content is not None:
        path.write_text(content)
    result = http.get("/api/ai-trader/decisions").get_json()
    assert result["available"] is False and result["count"] is None
    assert result["warnings"]
    assert path.exists() is (content is not None)


def test_real_empty_file_is_distinct_from_missing(client):
    http, path = client
    path.write_text(json.dumps(artifact([])))
    result = http.get("/api/ai-trader/decisions").get_json()
    assert result["available"] is True and result["count"] == 0
    assert result["degraded"] is False


def test_partial_corruption_and_unknown_outcomes_remain_visible(client):
    http, path = client
    path.write_text(json.dumps(artifact([None, {"decision_id": "valid", "result": "unknown_future_result"}])))
    result = http.get("/api/ai-trader/decisions").get_json()
    assert result["available"] and result["degraded"] and result["count"] == 1
    assert result["warnings"] and result["records"][0]["result"] == "unknown_future_result"
