from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantdesk.data_health_dashboard import annotate_cached_card_scopes
from quantdesk.decision_board import _account, _position_rows
from quantdesk.project_status import read_runtime_observations
from scripts.run_research_viewer import new_run_directory, readonly_lock_probe, remap_runtime_inputs
from scripts.server.routes.product_review_routes import research_summary


def test_snapshot_arrival_and_quality_are_independent(tmp_path):
    path = tmp_path / "data/snapshots/2026-10-23/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"trade_date": "2026-10-23", "archived_slot_count": 48,
        "collected_slot_count": 0, "expected_slot_count": 48,
        "field_verified_complete_slot_count": 0, "quoted_row_count": 1000, "valid_quote_count": 999}), encoding="utf-8")
    snapshot = read_runtime_observations(tmp_path, report_date="2026-10-23")["snapshot"]
    assert snapshot["received_slots"] == 48
    assert snapshot["strict_qualified_slots"] == snapshot["field_complete_slots"] == 0
    assert snapshot["valid_quote_ratio"] == .999
    assert "已收到 48/48" in snapshot["message"]
    assert "字段完整 0/48" in snapshot["message"]


def test_cached_coverage_never_subtracts_different_populations():
    original = "10 / 41 只股票有分钟线，缺少 3 只。"
    payload = {"generated_at": "2026-10-23T19:12:24", "cards": {
        "intraday_data": {"detail": original},
        "daily_data": {"detail": "目标完整交易日 2026-10-23：911/914 只已覆盖到目标日，3 只滞后到更早日期（最旧 2026-10-23）。"}}}
    annotate_cached_card_scopes(payload)
    assert payload["cards"]["intraday_data"]["unclassified_count"] == 28
    assert payload["cards"]["intraday_data"]["original_detail"] == original
    assert "全天覆盖" in payload["cards"]["intraday_data"]["detail"]
    assert payload["cards"]["daily_data"]["gap_reason_status"] == "unresolved"
    assert "全部归为日期滞后" in payload["cards"]["daily_data"]["detail"]


def test_account_valuation_date_does_not_use_generation_time():
    assert _account({"valuation_date": "2026-10-23", "generated_at": "2026-10-25"})["valuation_date"] == "2026-10-23"
    assert not _account({"generated_at": "2026-10-25"})["valuation_date"]


def test_position_display_uses_the_pnl_cost_not_legacy_average():
    rows = _position_rows({"positions": [{"symbol": "600000", "quantity": 500,
        "entry_price": 9.4047, "avg_cost": 9.3186, "latest_price": 9.53,
        "unrealized_pnl": 62.65}]}, as_of="2026-10-23", calendar_days=[], warnings=[])
    assert rows[0]["entry_price"] == 9.4047
    assert rows[0]["historical_avg_cost"] == 9.3186
    assert rows[0]["unrealized_pnl"] == pytest.approx((9.53 - rows[0]["entry_price"]) * 500)


def test_synthetic_pass_does_not_prove_current_rule():
    result = research_summary({"status": "passed", "summary": {"synthetic_test_data": True},
                               "allowed_for_trade_plan": True}, source="fixture")
    assert result["synthetic"] is True
    assert result["process_status"] == "passed"
    assert result["current_rule_validation"] == "not_established"


def test_entry_can_allocate_two_runs_without_collision(tmp_path):
    first, second = new_run_directory(tmp_path), new_run_directory(tmp_path)
    assert first != second and first.is_dir() and second.is_dir()


def test_readonly_lock_probe_preserves_existing_bytes(tmp_path):
    path = tmp_path / "probe.lock"
    path.write_bytes(b"existing-lock-record")
    assert readonly_lock_probe(path) is False
    assert path.read_bytes() == b"existing-lock-record"
    assert readonly_lock_probe(tmp_path / "absent") is False
    assert not (tmp_path / "absent").exists()


def test_viewer_remaps_captured_roots_and_late_ai_readers(tmp_path, monkeypatch):
    from dataclasses import dataclass
    from types import ModuleType, SimpleNamespace
    import sys
    source, data = tmp_path / "source", tmp_path / "data-root"
    @dataclass(frozen=True)
    class Deps:
        results_dir: Path
    root, deps = source, Deps(source / "results")
    def view():
        return root, deps.results_dir
    view.__module__ = "scripts.viewer_remap_test"
    module = ModuleType("ai_trader.viewer_remap_test")
    module.STATE_DIR = source / "state/ai_trader"
    module.PACKAGE_DIR = source / "ai_trader"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    remap_runtime_inputs(source, data, app=SimpleNamespace(view_functions={"test": view}))
    assert view() == (data, data / "results")
    assert module.STATE_DIR == data / "state/ai_trader"
    assert module.PACKAGE_DIR == source / "ai_trader"


@pytest.fixture
def api(monkeypatch):
    from scripts import quant_api_server as server
    monkeypatch.setitem(server.app.config, "TESTING", True)
    monkeypatch.setitem(server.app.config, "UI_READ_CACHE_ENABLED", False)
    return server


def test_peak_to_trough_drawdown_includes_fall_after_profit(api, monkeypatch):
    dates = pd.date_range("2026-10-19", periods=3)
    frame = pd.DataFrame({"date": dates, "close": [100, 142.22, 105.8]})
    monkeypatch.setattr(api, "load_stock_data", lambda symbol: frame)
    monkeypatch.setattr(api, "_paper_simulation_smart_stop_context", lambda *args, **kwargs: {})
    row = api.refresh_paper_simulation_item_tracking({"symbol": "600667", "entry_date": "2026-10-19", "initial_reference_price": 100})
    assert row["max_drawdown_since_added"] == pytest.approx(-25.61, abs=.01)
    assert row["return_since_added_pct"] == 5.8
    assert row["min_return_since_added_pct"] == 0
    assert row["tracking_metric_version"] == "peak-to-trough-v2"


def test_read_mode_denies_mutation_before_action(api, monkeypatch):
    monkeypatch.setitem(api.app.config, "RESEARCH_READ_ONLY", True)
    monkeypatch.setitem(api.app.config, "STOCK_QUOTE_REFRESH_ALLOWED", False)
    client = api.app.test_client()
    assert client.post("/api/backtest/run", json={}).status_code == 423
    caps = client.get("/api/runtime-capabilities").get_json()
    assert caps["read_only"] is True and caps["mutations_enabled"] is False


def test_liquidity_gap_uses_same_candidate_evidence(api):
    row = {"symbol": "600000", "data_quality": {"tone": "ok"},
           "missing_evidence": [{"kind": "missing_liquidity_evidence"}],
           "data_confidence": {"status": "partial"}}
    assert api.data_gap_summary([row])["field_gap_count"] == 1
    assert api.candidate_confidence_summary([row])["top_data_gaps"][0]["count"] == 1


def test_candidate_exposure_keeps_recorded_facts_without_inventing_dates(api):
    actual = api.candidate_exposure_evidence(pd.Series({"industry": "C28", "theme": "未知题材",
        "industry_source": "local_registry", "sector": pd.NA}))
    assert actual["industry"] == "C28" and actual["sector"] == actual["theme"] == ""
    assert actual["exposure_evidence"]["industry_source"] == "local_registry"
    assert actual["exposure_evidence"]["industry_as_of"] == ""
    assert actual["exposure_evidence"]["theme_status"] == "unknown"
    assert actual["exposure_evidence"]["unknown_does_not_prove_diversification"] is True


def test_market_cache_dates_are_not_inferred_from_fetch_or_file_time(tmp_path, monkeypatch):
    from scripts.server.helpers import market
    spot, meta, sector = tmp_path / "spot.csv", tmp_path / "meta.json", tmp_path / "sector.csv"
    spot.write_text("symbol,pct_chg,amount,quote_time\n600000,1,100,15:00:02\n", encoding="utf-8")
    meta.write_text('{"fetched_at":"2026-10-23T15:05:02+08:00"}', encoding="utf-8")
    sector.write_text("name,pct_chg,amount\nfixture,1,100\n", encoding="utf-8")
    monkeypatch.setattr(market, "SPOT_SNAPSHOT_CACHE", spot)
    monkeypatch.setattr(market, "SPOT_SNAPSHOT_META", meta)
    monkeypatch.setattr(market, "INDUSTRY_BOARD_CACHE", sector)
    breadth, sectors = market.spot_market_breadth(), market.calculate_sector_stats()
    assert breadth["quote_date"] == "" and breadth["fetched_at"].startswith("2026-10-23")
    assert sectors["as_of"] == "" and sectors["cache_modified_at"]
    assert sectors["freshness_status"] == "unknown"


def test_overview_mixed_dates_never_claim_one_current_market_day(api, monkeypatch):
    monkeypatch.setattr(api, "load_benchmark_index", lambda name: {"date": "2026-10-23", "change": 1})
    monkeypatch.setattr(api, "spot_market_breadth", lambda: {"quote_date": "2026-10-22"})
    monkeypatch.setattr(api, "calculate_sector_stats", lambda: {"as_of": "", "freshness_status": "unknown"})
    result = api.app.test_client().get("/api/daily/overview").get_json()
    assert result["as_of"] in ("", "数据缺失")
    assert result["market_dates"]["hs300"] == "2026-10-23"
    assert result["market_dates"]["breadth"] == "2026-10-22"
    assert result["sector_snapshot"]["freshness_status"] == "unknown"


def test_ranking_explanation_reconciles_unchanged_score_and_weight(api):
    for quality in ("ok", "warn", "missing"):
        for tone in ("ok", "watch", "warn", "missing"):
            for pct in (-12, -1, 0, 12, None):
                row = {"is_today_recommendation": True, "data_quality": {"tone": quality},
                    "tracking": {"status_tone": tone}, "risk_level": "中", "group_key": "board", "pct_chg": pct}
                value = api.focus_score_explanation(row, .8)
                assert sum(item["points"] for item in value["components"]) == pytest.approx(api.focus_score(row))
                assert value["total"] == round(api.focus_score(row) - 2, 2)
                assert value["ranking_only"] is True


def test_pool_relationships_keep_independent_source_dates(api, monkeypatch):
    watch = {"items": [{"symbol": "600000", "latest_reference_date": "2026-07-08"}]}
    state = {"positions": [{"symbol": "600000"}], "valuation_date": "2026-10-23"}
    monkeypatch.setattr(api, "read_json_file", lambda path: watch if path == api.PAPER_WATCHLIST_JSON else state)
    rows = [{"symbol": "600000", "is_today_recommendation": True, "signal_date": "2026-10-23"}]
    result = api.candidate_pool_context(rows, rows)
    by_pool = {row["pool"]: row["as_of"] for row in result["by_symbol"]["600000"]}
    assert by_pool == {"today": "2026-10-23", "focus": "2026-10-23", "paper_watchlist": "2026-07-08", "paper_position": "2026-10-23"}
    assert result["synchronization_executed"] is False


def test_candidate_dependency_selection_matches_the_view(api, monkeypatch):
    ledger = pd.DataFrame([{"ts_code": f"600{i:03}", "strategy_type": "long",
        "entry_date": pd.Timestamp("2026-10-22"), "signal_date": "2026-10-22",
        "candidate_role": "observation"} for i in range(24)])
    monkeypatch.setattr(api, "load_candidate_ledger", lambda: ledger)
    monkeypatch.setattr(api, "latest_generation_status", lambda: {})
    monkeypatch.setattr(api, "load_candidate_review_overrides", lambda: {})
    monkeypatch.setattr(api, "build_candidate", lambda symbol, *args, **kwargs: {"symbol": symbol})
    monkeypatch.setattr(api, "attach_strategy_conflict", lambda row, *args: row)
    monkeypatch.setattr(api, "attach_reference_confidence", lambda row: row)
    monkeypatch.setattr(api, "apply_candidate_review_state", lambda row, *args: row)
    assert api._candidate_read_symbols(18) == [r["symbol"] for r in api.candidate_rows(18)]


def test_formal_candidate_dependencies_match_the_bounded_view(api, monkeypatch):
    ledger = pd.DataFrame([{"ts_code": f"600{i:03}", "strategy_type": "long",
        "entry_date": pd.Timestamp("2026-10-22"), "signal_date": "2026-10-22",
        "candidate_role": "observation"} for i in range(24)])
    generation = {"source": "formal_candidate_daily_batch", "formal_current": True,
        "signal_date": "2026-10-23", "formal_rows": [
            {"symbol": "920906", "name": "fixture", "strategy_type": "long"},
            {"symbol": "600000", "name": "fixture", "strategy_type": "board"}],
        "batch_identity": {"batch_provenance_hash": "fixture", "source_run_id": "fixture"}}
    monkeypatch.setattr(api, "load_candidate_ledger", lambda: ledger)
    monkeypatch.setattr(api, "latest_generation_status", lambda: generation)
    monkeypatch.setattr(api, "load_candidate_review_overrides", lambda: {})
    monkeypatch.setattr(api, "build_candidate", lambda symbol, *args, **kwargs: {"symbol": symbol})
    monkeypatch.setattr(api, "attach_strategy_conflict", lambda row, *args: row)
    monkeypatch.setattr(api, "attach_reference_confidence", lambda row: row)
    monkeypatch.setattr(api, "apply_candidate_review_state", lambda row, *args: row)
    actual = [row["symbol"] for row in api.candidate_rows(18)]
    assert api._candidate_read_symbols(18) == actual
    assert len(actual) == 18 and "920906" in actual


def test_generation_is_built_once_per_request_not_shared_across_requests(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_ui_generation_dependency_paths", lambda day: [])
    def cached(paths, build, **kwargs):
        calls.append(len(calls) + 1)
        return {"snapshot": calls[-1]}
    monkeypatch.setattr(api, "cached_file_value", cached)
    with api.app.test_request_context("/api/daily/candidates"):
        assert api.latest_generation_status() == api.latest_generation_status() == {"snapshot": 1}
    with api.app.test_request_context("/api/daily/candidates"):
        assert api.latest_generation_status() == {"snapshot": 2}


def test_generation_memo_cannot_hide_input_changes_during_response(api, monkeypatch, tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text('{"generation":1}', encoding="utf-8")
    monkeypatch.setitem(api.app.config, "UI_READ_CACHE_ENABLED", True)
    monkeypatch.setattr(api, "_ui_dependency_paths", lambda: [path])
    monkeypatch.setattr(api, "_ui_generation_dependency_paths", lambda day: [path])
    monkeypatch.setattr(api, "_latest_generation_status_uncached", lambda: {"generation": 1})
    def changed_view():
        api.latest_generation_status()
        path.write_text('{"generation":2}', encoding="utf-8")
        api.latest_generation_status()
        return api.jsonify(ok=True)
    with api.app.test_request_context("/api/daily/candidates"):
        endpoint = api.request.endpoint
    monkeypatch.setitem(api.app.view_functions, endpoint, changed_view)
    response = api.app.test_client().get("/api/daily/candidates")
    assert response.status_code == 409
    assert response.get_json()["status"] == "read_input_unstable"


def test_simple_backtest_exposes_its_fill_assumptions(api):
    frame = pd.DataFrame({"close": 10 + np.sin(np.arange(100) / 5), "high": 12, "low": 8})
    result = api.build_research_reference(frame)["simple_backtest"]
    assert result["signal_timing"] == result["fill_timing"] == "same_close"
    assert result["costs_included"] is False and result["capital_constrained"] is False
    assert "T+1" in result["note"]
