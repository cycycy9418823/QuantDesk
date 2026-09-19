"""Core update entrypoints must use one writer and retain failure provenance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import startup_data_update as startup
from scripts import run_daily_data_refresh as refresh
from scripts import run_data_close_pipeline as pipeline
from scripts import quant_api_server as server


@pytest.mark.parametrize(("network", "dry_run"), [(False, False), (True, True)])
def test_offline_startup_does_not_write_or_run_pipeline(tmp_path, monkeypatch, network, dry_run):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kw: pytest.fail("unexpected Pipeline"))
    payload = startup.run_startup_data_update(tmp_path, enable_network=network, dry_run=dry_run,
                                             target_date="2026-09-08", generate_candidates=True)
    assert payload["status"] == "planned_offline"
    assert payload["candidate_generation"]["status"] == "skipped"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", ["completed_all_executed", "completed_with_skips", "failed", "blocked"])
def test_startup_delegates_full_a_and_preserves_pipeline_outcome(tmp_path, monkeypatch, status):
    calls = []
    monkeypatch.setattr(refresh, "_complete_live_session", lambda _: True)
    def run(**kwargs):
        calls.append(kwargs)
        return {"run_id": "test-close", "status": status, "stages": {}}
    monkeypatch.setattr(pipeline, "run_pipeline", run)
    payload = startup.run_startup_data_update(tmp_path, enable_network=True, target_date="2026-09-08",
                                             max_symbols=1, generate_candidates=True)
    assert calls == [{"root": tmp_path, "network": True, "target_date": "2026-09-08",
                      "trigger": "manual", "alert_push": False, "alert_network": False}]
    assert payload["scope"] == "full_a" and payload["only_low_price"] is False
    assert payload["status"] == status
    assert payload["ok"] is status.startswith("completed_")
    assert payload["stale_count_after"] is None
    assert json.loads((tmp_path / payload["path"]).read_text(encoding="utf-8"))["status"] == status


def test_unfinished_session_does_not_start_snapshot_or_historical_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "_complete_live_session", lambda _: False)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kw: pytest.fail("not a closed session"))
    result = startup.run_startup_data_update(tmp_path, enable_network=True, target_date="2026-09-08")
    assert result["status"] == "awaiting_close"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("endpoint", ["/api/data/update-all", "/api/daily/update-candidates"])
def test_daily_update_routes_share_pipeline_without_candidate_pool(tmp_path, monkeypatch, endpoint):
    calls = []
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "candidate_rows", lambda *a, **kw: pytest.fail("candidate pool dependency"))
    monkeypatch.setattr(server, "run_startup_data_update", lambda *a, **kw: calls.append((a, kw)) or {
        "status": "failed", "scope": "full_a", "message": "fixture failure", "ok": False})
    response = server.app.test_client().post(endpoint, json={"explicit_network": True, "max_symbols": 1})
    assert response.status_code == 500
    assert response.get_json()["status"] == "failed"
    assert response.get_json()["scope"] == "full_a"
    assert calls == [((tmp_path,), {"enable_network": True})]


def test_single_stock_view_and_removed_repair_cannot_write(monkeypatch):
    monkeypatch.setattr(server, "update_daily_prices", lambda **kw: pytest.fail("bypass writer"))
    result = server.ensure_single_stock_daily_with_auth("000001", refresh_daily=True, network_allowed=True)
    assert result["attempted"] is False and result["canonical_write"] is False
    response = server.app.test_client().post("/api/data/repair-daily", json={})
    assert response.status_code == 410 and response.get_json()["canonical_write"] is False


@pytest.mark.parametrize("target", ["2026-09-08", "2026-09-05"])
def test_weekly_apply_audits_saved_data_without_a_second_writer(tmp_path, monkeypatch, capsys, target):
    from scripts import run_weekly_wide_pool_maintenance as weekly
    monkeypatch.setattr(weekly, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(weekly, "resolve_target_complete_trade_date", lambda *a, **kw: {"target_date": target})
    monkeypatch.setattr(weekly, "run_weekly_daily_wide_pool", lambda *a, **kw: pytest.fail("legacy writer"))
    monkeypatch.setattr(startup, "run_startup_data_update", lambda *a, **kw: pytest.fail("unexpected market refresh"))
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n600001\n600002\n", encoding="utf-8")
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    for symbol, day in (("600000", target), ("600001", "2026-09-04")):
        (daily / f"{symbol}.csv").write_text(f"date,close,adjust\n{day},10,raw\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in daily.glob("*.csv")}
    assert weekly.main(["--apply", "--enable-network", "--daily-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["legacy_weekly_request"] is True
    assert result["status"] == "completed_with_warnings"
    assert result["audit_only"] is True
    assert result["canonical_write"] is result["network"] is result["apply"] is False
    assert result["retry_symbols"] == ["600001", "600002"]
    assert {path.name: path.read_bytes() for path in daily.glob("*.csv")} == before
    assert (tmp_path / result["manifest_path"]).is_file()


def test_startup_status_does_not_turn_failed_update_green(tmp_path, monkeypatch):
    path = tmp_path / "startup.json"
    path.write_text(json.dumps({"status": "failed", "message": "provider failed", "updated_at": "2026-09-08T15:10:00"}))
    monkeypatch.setattr(server, "STARTUP_UPDATE_JSON", path)
    monkeypatch.setattr(server, "startup_refresh_state_payload", lambda: {"running": False})
    monkeypatch.setattr(server, "candidate_rows", lambda *a, **kw: pytest.fail("candidate freshness cannot overwrite run"))
    before = path.read_bytes()
    result = server.app.test_client().get("/api/startup/update-status").get_json()
    assert result["status"] == "failed" and result["message"] == "provider failed"
    assert result["current_view_override"] is False
    assert path.read_bytes() == before
