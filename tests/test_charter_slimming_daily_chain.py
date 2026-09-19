from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_research_pool_separation import _install_chain_fakes


@pytest.mark.parametrize("symbol_count", [3, 5509])
def test_default_close_builds_full_a_features_without_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol_count: int
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    symbols = [str(600000 + index) for index in range(symbol_count)]
    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n" + "\n".join(symbols), encoding="utf-8")
    monkeypatch.setattr(
        chain, "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [symbols[0]], "source_manifest": {}},
    )
    minute_calls: list[list[str]] = []
    audit_calls: list[list[str]] = []
    feature_calls: list[list[str]] = []
    original_update = chain.update_intraday_1m_for_symbols
    original_audit = chain.audit_intraday_batch

    def update(requested, target, **kwargs):
        minute_calls.append(list(requested))
        return original_update(requested, target, **kwargs)

    def audit(requested, target, **kwargs):
        audit_calls.append(list(requested))
        return original_audit(requested, target, **kwargs)

    def features(**kwargs):
        requested = list(kwargs["symbols"])
        feature_calls.append(requested)
        return {
            "ok": True, "status": "passed", "requested_count": len(requested),
            "feature_count": len(requested), "skipped_count": 0,
            "output_row_count": len(requested), "output_sha256": "a" * 64,
            "accepted_price_basis": "raw", "feature_input_snapshot_stable": True,
            "price_policy_validation": {
                "status": "passed", "run_status": "passed", "eligible": True,
                "accepted_price_basis": "raw", "blocked_symbols": [], "reason_codes": [],
            },
            "per_symbol": {
                symbol: {"eligible": True, "price_basis": "raw", "latest_date": "2026-07-17"}
                for symbol in requested
            },
        }

    monkeypatch.setattr(chain, "update_intraday_1m_for_symbols", update)
    monkeypatch.setattr(chain, "audit_intraday_batch", audit)
    monkeypatch.setattr(chain, "build_daily_features", features)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date="2026-07-17", network=False,
        refresh_universe=False, run_id="bounded-close",
    )

    assert result["status"] == "completed"
    assert feature_calls == [symbols]
    assert minute_calls == []
    assert audit_calls == []
    assert result["coverage"]["features"]["status"] == "passed"
    assert result["coverage"]["current_analysis_pool"]["status"] == "not_requested"
    assert result["coverage"]["current_analysis_pool"]["audited_count"] == 0
    assert result["fill_labels"]["minute_not_assessed"] == symbols
    assert result["coverage"]["critical"]["status"] == "not_requested"
    assert result["components"]["analysis_intraday_batches"] == []
    run_dir = tmp_path / "results" / "data_chain" / "runs" / "bounded-close"
    assert not (run_dir / "batches").exists()
    assert not (run_dir / "analysis_pool_intraday_quality.json").exists()


def test_broad_minute_research_requires_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    calls: list[list[str]] = []
    original = chain.update_intraday_1m_for_symbols

    def update(requested, target, **kwargs):
        calls.append(list(requested))
        return original(requested, target, **kwargs)

    monkeypatch.setattr(chain, "update_intraday_1m_for_symbols", update)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date="2026-07-17", symbols=["600000", "600001"],
        network=False, refresh_universe=False, rebuild_features=False,
        skip_analysis_pool_minute=False, run_id="explicit-minute-research",
    )
    assert sorted(symbol for call in calls for symbol in call) == ["600000", "600001"]
    assert len(result["components"]["analysis_intraday_batches"]) == 2
    assert result["fill_labels"]["minute_not_assessed"] == []


def test_daily_chain_cli_defaults_to_daily_research() -> None:
    from scripts.run_daily_data_chain import build_parser

    parser = build_parser()
    assert parser.parse_args([]).skip_analysis_pool_minute == "true"
    assert parser.parse_args(["--skip-analysis-pool-minute", "false"]).skip_analysis_pool_minute == "false"


@pytest.mark.parametrize("reason", ["analysis_pool_minute_on_demand", "audit_failed", ""])
def test_research_reader_accepts_only_declared_unrequested_minutes(reason: str) -> None:
    from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate
    from tests.test_daily_plan_freeze import _completed_manifest

    manifest = _completed_manifest()
    manifest["published_latest"] = True
    manifest["coverage"]["current_analysis_pool"] = {
        "status": "not_requested", "reason_codes": [reason] if reason else [],
        "requested_count": 0, "audited_count": 0,
    }
    result = evaluate_data_chain_consumer_gate(manifest)
    assert result["allowed"] is (reason == "analysis_pool_minute_on_demand")
    assert result["execution_authorized"] is False
    assert result["chain"]["coverage"]["current_analysis_pool"]["status"] == "not_requested"


def test_health_does_not_report_unrequested_minutes_as_a_full_market_gap() -> None:
    from quantdesk.data_health_dashboard import build_formal_daily_chain_status

    result = build_formal_daily_chain_status({
        "status": "completed",
        "universe": {"current_analysis_pool": {"symbol_count": 5509}},
        "coverage": {"current_analysis_pool": {"status": "not_requested", "requested_count": 0}},
    })
    assert result["analysis_pool_requested"] == 0
    assert result["analysis_pool_coverage_ratio"] is None
    assert result["pool_status"] == "not_requested"


@pytest.mark.parametrize("invalid_basis", [False, True])
def test_reader_keeps_per_symbol_gaps_visible_but_never_accepts_unknown_price_basis(invalid_basis: bool) -> None:
    from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate
    from tests.test_research_pool_separation import _partial_manifest

    manifest = _partial_manifest()
    manifest["published_latest"] = True
    manifest["coverage"]["current_analysis_pool"] = {
        "status": "not_requested", "reason_codes": ["analysis_pool_minute_on_demand"],
        "requested_count": 0, "audited_count": 0,
    }
    if invalid_basis:
        manifest["coverage"]["daily"]["invalid_price_basis_symbols"] = ["600000"]
        manifest["coverage"]["daily"]["invalid_price_basis_count"] = 1
    result = evaluate_data_chain_consumer_gate(manifest)
    assert result["allowed"] is not invalid_basis
    assert result["execution_authorized"] is False
    assert result["chain"]["coverage"]["daily"] == manifest["coverage"]["daily"]
    assert result["research_eligibility"]["eligible_symbols"] == ([] if invalid_basis else ["600000"])
