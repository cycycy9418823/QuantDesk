from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _install_chain_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantdesk import daily_data_chain as chain

    monkeypatch.setattr(
        chain,
        "infer_target_date",
        lambda target_date=None: (str(target_date or "2026-07-17")[:10], ""),
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "selected_executable": "test-python",
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda root, *, network: {"ok": True, "status": "skipped"},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "skipped"},
    )
    monkeypatch.setattr(
        chain,
        "_update_current_state",
        lambda root, manifest: {"ok": True, "status": "skipped"},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_forward_pit_readiness",
        lambda root: {"status": "skipped"},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_t0_birth_evidence",
        lambda root: {"status": "skipped"},
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "target_date": kwargs["target_date"],
            "total_symbols": len(kwargs["symbols"]),
            "symbols_checked": len(kwargs["symbols"]),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "stale_symbol_list_after": [],
            "missing_symbol_list_after": [],
            "failed_symbol_list": [],
            "failed_symbols": 0,
            "invalid_price_basis_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "status": "passed",
            "formal_eligible": True,
            "target_date": target_date,
            "symbols": list(symbols),
            "unknown_symbols": [],
            "legitimate_no_session_symbols": [],
            "classification_counts": {"expected_trade": len(symbols), "unknown": 0},
            "counts": {"expected_trade": len(symbols), "unknown": 0},
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {"symbol": symbol, "complete_session": True}
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda symbols, target_date, **kwargs: {
            "status": "completed",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "failed_symbol_list": [],
            "updated_symbol_list": list(symbols),
            "unchanged_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "written": False,
            "reason_codes": [],
        },
    )


def test_public_chain_uses_full_a_baseline_as_default_research_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text(
        "symbol,name\n600000,Alpha\n600001,Beta\n",
        encoding="utf-8",
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=False,
        dry_run=True,
        refresh_universe=False,
        rebuild_features=False,
        run_id="full-a-default-red",
    )

    pool = payload["universe"]["current_analysis_pool"]
    assert pool["symbols"] == ["600000", "600001"]
    assert pool["symbol_count"] == 2
    assert payload["scope"]["research_scope"] == "all_a"
    assert payload["scope"]["filters"][0]["used"] is False
    assert payload["scope"]["filters"][0]["symbol_count"] == 0


def test_default_full_a_research_does_not_enable_deep_per_symbol_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text("symbol,name\n600000,Alpha\n600001,Beta\n", encoding="utf-8")
    seen: list[bool] = []

    def capture_update(**kwargs):
        seen.append(bool(kwargs["network"]))
        symbols = list(kwargs["symbols"])
        return {
            "status": "completed",
            "target_date": kwargs["target_date"],
            "total_symbols": len(symbols),
            "symbols_checked": len(symbols),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "stale_symbol_list_after": [],
            "missing_symbol_list_after": [],
            "failed_symbol_list": [],
            "failed_symbols": 0,
            "invalid_price_basis_symbols": [],
        }

    monkeypatch.setattr(chain, "update_daily_prices", capture_update)
    minute_network_calls: list[bool] = []

    def unexpected_market_minute_fetch(symbols, target_date, **kwargs):
        minute_network_calls.append(bool(kwargs["network"]))
        return {"status": "failed", "failed_symbols": len(symbols)}

    monkeypatch.setattr(
        chain, "update_intraday_1m_for_symbols", unexpected_market_minute_fetch
    )
    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=True,
        dry_run=False,
        refresh_universe=False,
        rebuild_features=False,
        run_id="full-a-local-research",
    )

    assert payload["status"] == "completed"
    assert seen == [False]
    assert payload["components"]["daily_update"]["network_requested"] is True
    assert payload["components"]["daily_update"]["network_effective"] is False
    assert payload["components"]["daily_update"]["network_scope"] == "local_full_a_research"
    assert not any(minute_network_calls)


def test_explicit_subset_feature_outputs_stay_in_its_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    seen: list[dict[str, object]] = []

    def capture_features(**kwargs):
        seen.append(kwargs)
        return {"status": "skipped", "ok": True}

    monkeypatch.setattr(chain, "build_daily_features", capture_features)
    result = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        symbols=["600000"],
        network=False,
        refresh_universe=False,
        run_id="subset-features",
    )
    run_dir = tmp_path / "results" / "data_chain" / "runs" / "subset-features"
    assert seen[0]["output_path"] == run_dir / "daily_features.csv"
    assert seen[0]["summary_path"] == run_dir / "daily_features_summary.json"
    assert result["authoritative"] is False
    assert result["published_latest"] is False


def test_low_price_filter_is_explicit_and_manifested_as_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True, exist_ok=True)
    (universe / "a_share_symbols.csv").write_text(
        "symbol,name\n600000,Alpha\n600001,Beta\n", encoding="utf-8"
    )
    (universe / "low_price_learning_symbols.csv").write_text(
        "symbol,name,included\n600001,Beta,true\n", encoding="utf-8"
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=False,
        dry_run=True,
        refresh_universe=False,
        rebuild_features=False,
        use_low_price_filter=True,
        run_id="explicit-low-filter",
    )

    assert payload["universe"]["current_analysis_pool"]["symbols"] == ["600001"]
    assert payload["universe"]["research_universe"]["filter_used"] is True
    assert payload["scope"]["filters"][0]["used"] is True
    assert payload["scope"]["filters"][0]["symbol_count"] == 1


def test_missing_full_a_baseline_is_visible_and_never_uses_low_price_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_data_chain as chain

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True, exist_ok=True)
    (universe / "low_price_learning_symbols.csv").write_text(
        "symbol,name,included\n600001,Beta,true\n", encoding="utf-8"
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=False,
        dry_run=True,
        refresh_universe=False,
        rebuild_features=False,
        run_id="missing-full-a",
    )

    research = payload["universe"]["research_universe"]
    assert research["symbols"] == []
    assert research["source"] == "all_a_baseline_missing"
    assert "all_a_universe_source_missing" in research["reason_codes"]
    assert payload["scope"]["filters"][0]["used"] is False


def _partial_manifest() -> dict[str, object]:
    target = "2026-07-17"
    symbols = ["600000", "600001"]
    digest = hashlib.sha256(
        json.dumps(
            {"name": "current_analysis_pool", "target_date": target, "symbols": symbols},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "completed",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "protected_state_unchanged": True,
        "run_id": "daily-data-chain-partial-red",
        "target_date": target,
        "daily_data_chain_contract_version": 10,
        "universe": {
            "current_analysis_pool": {
                "name": "current_analysis_pool",
                "target_date": target,
                "symbol_count": 2,
                "symbols": symbols,
                "sha256": digest,
            }
        },
        "coverage": {
            "daily": {
                "status": "blocked",
                "requested_count": 2,
                "reported_count": 2,
                "stale_count": 0,
                "stale_symbols": [],
                "missing_count": 1,
                "missing_symbols": ["600001"],
                "failed_count": 0,
                "failed_symbols": [],
                "invalid_price_basis_count": 0,
                "invalid_price_basis_symbols": [],
                "reason_codes": ["daily_symbols_missing"],
            },
            "current_analysis_pool": {
                "status": "blocked",
                "requested_count": 2,
                "failed_count": 1,
                "failed_symbols": ["600001"],
                "reason_codes": ["minute_symbols_missing"],
            },
            "features": {
                "status": "blocked",
                "output_sha256": "b" * 64,
                "missing_symbols": ["600001"],
                "invalid_symbols": [],
                "stale_symbols": [],
                "reason_codes": ["feature_symbol_set_mismatch"],
            },
            "session_expectation": {
                "status": "blocked",
                "unknown_count": 1,
                "unknown_symbols": ["600001"],
            },
        },
        "components": {
            "features": {
                "status": "partial",
                "latest_date": target,
                "accepted_price_basis": "raw",
                "output_sha256": "b" * 64,
                "per_symbol": {
                    "600000": {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": target,
                    }
                },
            },
            "session_expectation": {
                "target_date": target,
                "status": "blocked",
                "formal_eligible": False,
                "symbols": symbols,
                "classification_counts": {"expected_trade": 1, "unknown": 1},
                "counts": {"expected_trade": 1, "unknown": 1},
                "unknown_symbols": ["600001"],
            },
            "critical_sources": {
                "status": "passed",
                "formal_eligible": True,
                "failed_sources": [],
            },
        },
    }


def test_partial_symbol_evidence_keeps_candidate_gate_eligible() -> None:
    from quantdesk.daily_candidate_refresh import candidate_refresh_gate

    result = candidate_refresh_gate(_partial_manifest())

    assert result["formal_eligible"] is True
    assert result["eligible_symbols"] == ["600000"]
    assert result["excluded_symbols"] == ["600001"]


def test_candidate_refresh_default_scope_uses_full_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk.daily_candidate_refresh import run_formal_candidate_refresh

    manifest = _partial_manifest()
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True, exist_ok=True)
    (universe / "a_share_symbols.csv").write_text(
        "symbol,name\n600000,Alpha\n600001,Beta\n", encoding="utf-8"
    )
    (universe / "low_price_learning_symbols.csv").write_text(
        "symbol,name,included\n600000,Alpha,true\n", encoding="utf-8"
    )
    seen: list[Path] = []

    def strategy_runner(**kwargs):
        seen.append(Path(kwargs["universe_path"]))
        return {
            "ok": True,
            "summary": {"latest_daily_date": "2026-07-17"},
            "daily_feature_status": {"latest_date": "2026-07-17"},
            "standard_candidates": [],
        }

    monkeypatch.setattr(
        "quantdesk.daily_candidate_refresh.PROJECT_ROOT",
        tmp_path,
    )
    result = run_formal_candidate_refresh(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        strategy_runner=strategy_runner,
        captured_at="2026-07-17T18:00:00+08:00",
        apply=False,
    )

    assert result["status"] == "staged"
    assert seen and seen[0].name == "candidate_eligible_universe.csv"
    scoped = seen[0]
    assert scoped.exists()
    assert "600000" in scoped.read_text(encoding="utf-8")
    assert "600001" not in scoped.read_text(encoding="utf-8")
    assert "low_price_learning_symbols.csv" not in str(scoped)


def test_feature_coverage_marks_missing_symbol_as_partial_not_global_failure() -> None:
    from quantdesk.daily_data_chain import _feature_coverage

    payload = {
        "ok": True,
        "status": "partial",
        "requested_count": 2,
        "feature_count": 1,
        "skipped_count": 1,
        "accepted_price_basis": "raw",
        "feature_input_snapshot_stable": True,
        "output_sha256": "b" * 64,
        "output_row_count": 1,
        "price_policy_validation": {
            "status": "passed",
            "run_status": "partial",
            "eligible": True,
            "accepted_price_basis": "raw",
            "blocked_symbols": [],
            "reason_codes": [],
        },
        "per_symbol": {
            "600000": {"eligible": True, "price_basis": "raw", "latest_date": "2026-07-17"}
        },
    }

    result = _feature_coverage(payload, ["600000", "600001"], "2026-07-17")

    assert result["status"] == "partial"
    assert result["missing_symbols"] == ["600001"]


def test_freeze_accepts_symbol_degraded_coverage_and_keeps_unaffected_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk import daily_plan_freeze as freeze

    manifest = _partial_manifest()
    batch = {
        "formal_eligible": True,
        "candidates": [],
        "provenance": {
            "source_run_id": manifest["run_id"],
            "source_universe_hash": manifest["universe"]["current_analysis_pool"]["sha256"],
            "source_feature_hash": "b" * 64,
            "hash": "c" * 64,
            "captured_at": "2026-07-17T18:00:00+08:00",
        },
    }
    monkeypatch.setattr(
        freeze,
        "resolve_chain_bound_formal_candidate_batch",
        lambda *args, **kwargs: {
            "status": "passed",
            "path": tmp_path / "formal_candidate_batch.json",
            "batch": batch,
            "source_mode": "dated",
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(freeze, "verify_formal_candidate_batch", lambda payload: [])
    monkeypatch.setattr(
        freeze,
        "build_candidate_envelope",
        lambda *args, **kwargs: {},
    )
    calendar = tmp_path / "calendar.csv"
    calendar.write_text(
        "date,is_open,source,note\n2026-07-17,true,test,\n2026-07-20,true,test,\n",
        encoding="utf-8",
    )

    def plan_builder(**kwargs):
        return {
            "ok": True,
            "plan_id": "partial-plan",
            "plan_date": "2026-07-17",
            "trade_date": "2026-07-20",
            "source_data_cutoff": "2026-07-17",
            "symbols": [],
            "formal_execution_eligible": True,
            "price_data_validation": {
                "status": "passed",
                "formal_execution_eligible": True,
                "blocked_symbols": [],
                "target_date": "2026-07-17",
            },
        }

    result = freeze.freeze_t_plus_one_plan(
        project_root=tmp_path,
        data_chain_manifest=manifest,
        now=datetime(2026, 7, 17, 18, 30, tzinfo=SHANGHAI),
        calendar_path=calendar,
        plan_builder=plan_builder,
        write=False,
    )

    assert result["status"] == "prepared"
    assert result["ok"] is True
