from __future__ import annotations

from pathlib import Path

import pytest


def _install_minimal_chain_fakes(monkeypatch, pool: list[str]):
    from quantdesk import daily_data_chain as chain

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "selected_executable": "test-python",
            "runtime_source": "test",
            "modules": {"pandas": True, "mootdx": True, "akshare": True},
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": symbol} for symbol in pool],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
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
            "require_raw_price_basis": True,
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "status": "passed",
            "formal_eligible": True,
            "target_date": target_date,
            "unknown_symbols": [],
            "legitimate_no_session_symbols": [],
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
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
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
        "_feature_coverage",
        lambda *args, **kwargs: {"status": "passed", "missing_symbols": []},
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
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "passed"},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda root, *, network: {
            "ok": True,
            "status": "completed" if network else "skipped",
            "network": bool(network),
        },
    )
    monkeypatch.setattr(
        chain,
        "_update_current_state",
        lambda root, manifest: {"ok": True, "status": "updated"},
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "written": True,
            "plan_id": "test-plan",
            "reason_codes": [],
        },
    )
    return chain


def _refresh_evidence(status: str, target_date: str = "2026-07-17") -> dict:
    return {
        "ok": status == "completed",
        "status": status,
        "formal_eligible": status == "completed",
        "stale": status != "completed",
        "preserved_previous": status != "completed",
        "provider": "eastmoney" if status == "completed" else "",
        "quote_date": target_date if status == "completed" else "",
        "baseline_count": 2,
        "observed_count": 2 if status == "completed" else 1,
        "coverage_ratio": 1.0 if status == "completed" else 0.5,
        "expected_pages": 1,
        "observed_pages": 1,
        "expected_rows": 2,
        "observed_rows": 2 if status == "completed" else 1,
        "reason_codes": [] if status == "completed" else ["coverage_below_minimum"],
        "old_sha256": {"spot": "old"},
        "new_sha256": {"spot": "new" if status == "completed" else "old"},
    }


def test_network_false_skips_universe_refresh_without_constructing_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pool = ["600000"]
    chain = _install_minimal_chain_fakes(monkeypatch, pool)
    calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: calls.append("build") or {"eastmoney": object()},
    )
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: calls.append("refresh")
        or (_ for _ in ()).throw(AssertionError("refresh must be skipped")),
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=False,
        dry_run=True,
        run_id="universe-offline",
        rebuild_features=False,
    )

    assert payload["status"] == "dry_run"
    component = payload["components"]["universe_refresh"]
    assert component["status"] == "skipped"
    assert component["reason"] in {"network_not_authorized", "dry_run"}
    assert calls == []


def test_complete_universe_refresh_precedes_resolve_and_updates_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pool = ["600000"]
    resolved_snapshots: list[list[str]] = []
    chain = _install_minimal_chain_fakes(monkeypatch, pool)
    original_resolve = chain.resolve_symbols

    def resolve(**kwargs):
        resolved_snapshots.append(list(pool))
        return original_resolve(**kwargs)

    monkeypatch.setattr(chain, "resolve_symbols", resolve)
    fetcher_marker = object()
    calls: list[object] = []
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: calls.append("build") or {"eastmoney": fetcher_marker},
    )

    def refresh(root, target_date, network, fetchers, **kwargs):
        calls.extend(["refresh", fetchers])
        pool[:] = ["600000", "600001"]
        return _refresh_evidence("completed", target_date)

    monkeypatch.setattr(chain, "refresh_universe_snapshot", refresh)
    feature_gate_calls: list[str] = []
    plan_freeze_calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "_feature_coverage",
        lambda *args, **kwargs: feature_gate_calls.append("called")
        or {"status": "passed", "missing_symbols": []},
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: plan_freeze_calls.append("called")
        or {"ok": True, "status": "completed", "written": True},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=True,
        dry_run=False,
        run_id="universe-complete",
        rebuild_features=False,
    )

    assert payload["status"] == "completed"
    assert resolved_snapshots == []
    assert calls[0] == "build"
    assert calls[1] == "refresh"
    assert calls[2] == {"eastmoney": fetcher_marker}
    component = payload["components"]["universe_refresh"]
    assert component["status"] == "completed"
    assert component["provider"] == "eastmoney"
    assert payload["universe"]["current_analysis_pool"]["symbols"] == []
    assert payload["scope"]["filters"][0]["used"] is False
    assert payload["coverage"]["features"]["status"] == "skipped"
    assert payload["components"]["conditional_plan_freeze"]["status"] == "not_evaluated"
    assert feature_gate_calls == []
    assert plan_freeze_calls == []


def test_blocked_universe_refresh_keeps_old_pool_and_blocks_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pool = ["600000", "600001"]
    chain = _install_minimal_chain_fakes(monkeypatch, pool)
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: {"eastmoney": object()},
    )
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: _refresh_evidence("blocked", kwargs.get("target_date", "2026-07-17")),
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=True,
        dry_run=False,
        run_id="universe-blocked",
        rebuild_features=False,
    )

    assert payload["status"] == "blocked"
    assert payload["components"]["universe_refresh"]["status"] == "blocked"
    assert payload["universe"]["current_analysis_pool"]["symbols"] == []
    assert "universe_refresh_blocked" in payload["errors"]


def test_cli_refresh_universe_defaults_true_and_can_disable(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import scripts.run_daily_data_chain as runner

    calls: list[dict] = []
    monkeypatch.setattr(
        runner,
        "run_daily_data_chain",
        lambda **kwargs: calls.append(kwargs)
        or {"status": "dry_run", "target_date": "2026-07-17", "run_id": "cli"},
    )
    assert runner.main(["--json", "false"]) == 0
    assert calls[-1]["refresh_universe"] is True
    assert runner.main(["--json", "false", "--refresh-universe", "false"]) == 0
    assert calls[-1]["refresh_universe"] is False
    capsys.readouterr()


def test_formal_subset_block_precedes_universe_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pool = ["600000", "600001"]
    chain = _install_minimal_chain_fakes(monkeypatch, pool)
    calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: calls.append("build") or {},
    )
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: calls.append("refresh") or {},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        symbols=["600000"],
        network=True,
        dry_run=False,
        run_id="subset-no-refresh",
    )

    assert "formal_scope_not_full_current_analysis_pool" not in (payload.get("errors") or [])
    assert (payload.get("scope") or {}).get("research_scope") == "explicit_symbols"
    assert (payload.get("scope") or {}).get("formal_authoritative") is False
    assert payload.get("published_latest") is False


def test_latest_chain_rejects_explicit_v5_contract_after_v10(tmp_path: Path):
    import json

    from quantdesk import daily_data_chain as chain

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "task_type": "daily_data_chain",
                "run_id": "v5-run",
                "target_date": "2026-07-17",
                "status": "completed",
                "published_latest": True,
                "daily_data_chain_contract_version": 5,
            }
        ),
        encoding="utf-8",
    )
    payload = chain.latest_daily_data_chain(tmp_path)
    assert chain.DAILY_DATA_CHAIN_CONTRACT_VERSION == 10
    assert payload["status"] == "superseded_contract"
    assert payload["authoritative"] is False


def test_refresh_universe_false_skips_network_refresh(tmp_path: Path, monkeypatch):
    pool = ["600000"]
    chain = _install_minimal_chain_fakes(monkeypatch, pool)
    calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: calls.append("build") or {},
    )
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: calls.append("refresh") or {},
    )
    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-17",
        network=True,
        dry_run=False,
        refresh_universe=False,
        run_id="universe-disabled",
        rebuild_features=False,
    )
    assert payload["components"]["universe_refresh"]["status"] == "skipped"
    assert payload["components"]["universe_refresh"]["reason"] == "disabled_by_config"
    assert calls == []
