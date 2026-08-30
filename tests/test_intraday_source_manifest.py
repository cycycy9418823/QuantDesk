from __future__ import annotations

import json
from pathlib import Path

from quantdesk.intraday.intraday_gap_checker import collect_intraday_required_symbols


TARGET = "2026-07-16"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_plan_source_manifest_fails_closed_for_corrupt_and_stale(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    corrupt_payload = collect_intraday_required_symbols(
        source="plans",
        target_trade_date=TARGET,
        plan_path=corrupt,
    )
    corrupt_evidence = corrupt_payload["source_manifest"]["sources"][0]
    assert corrupt_payload["source_manifest_status"] == "blocked"
    assert corrupt_evidence["read_ok"] is False
    assert corrupt_evidence["formal_eligible"] is False

    stale = tmp_path / "stale.json"
    _write(
        stale,
        {
            "plan_id": "stale",
            "trade_date": "2026-07-15",
            "symbols": [{"symbol": "600000"}],
        },
    )
    stale_payload = collect_intraday_required_symbols(
        source="plans",
        target_trade_date=TARGET,
        plan_path=stale,
    )
    stale_evidence = stale_payload["source_manifest"]["sources"][0]
    assert stale_payload["source_manifest_status"] == "blocked"
    assert stale_evidence["alignment"] == "stale"
    assert stale_evidence["sha256"]


def test_current_source_manifest_keeps_position_outside_analysis_pool(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    state = tmp_path / "state.json"
    portfolio = tmp_path / "portfolio.json"
    strategy = tmp_path / "strategy.json"
    workflow = tmp_path / "workflow.json"
    _write(
        plan,
        {
            "plan_id": "current",
            "trade_date": TARGET,
            "plan_date": "2026-07-15",
            "source_data_cutoff": "2026-07-15",
            "generated_at": "2026-07-15T18:00:00+08:00",
            "symbols": [],
        },
    )
    _write(
        state,
        {
            "updated_at": "2026-07-13T00:00:00",
            "positions": [{"symbol": "999999", "name": "outside-pool"}],
        },
    )
    _write(
        portfolio,
        {
            "generated_at": "2026-07-13T00:00:00",
            "positions": [],
        },
    )
    _write(
        strategy,
        {
            "generated_at": f"{TARGET}T18:00:00",
            "long_candidates": [],
        },
    )
    _write(
        workflow,
        {
            "generated_at": f"{TARGET}T18:01:00",
            "auto_strategy": {"long_candidates": []},
        },
    )

    payload = collect_intraday_required_symbols(
        source="current",
        target_trade_date=TARGET,
        plan_path=plan,
        state_path=state,
        portfolio_latest_path=portfolio,
        strategy_path=strategy,
        workflow_path=workflow,
    )

    assert payload["source_manifest_status"] == "passed"
    assert payload["source_manifest_formal_eligible"] is True
    assert [item["symbol"] for item in payload["items"]] == ["999999"]
    assert payload["items"][0]["sources"] == ["auto_paper_state"]
    assert all(item["sha256"] for item in payload["source_manifest"]["sources"])


def test_plan_source_manifest_rejects_retroactive_generation(tmp_path: Path) -> None:
    plan = tmp_path / "retroactive.json"
    _write(
        plan,
        {
            "plan_id": "retroactive",
            "plan_date": "2026-07-15",
            "trade_date": TARGET,
            "source_data_cutoff": "2026-07-15",
            "generated_at": "2026-07-17T12:20:01+08:00",
            "symbols": [{"symbol": "600000"}],
        },
    )

    payload = collect_intraday_required_symbols(
        source="plans",
        target_trade_date=TARGET,
        plan_path=plan,
    )
    evidence = payload["source_manifest"]["sources"][0]

    assert payload["source_manifest_status"] == "blocked"
    assert evidence["formal_eligible"] is False
    assert evidence["alignment"] == "retroactive"
    assert evidence["plan_freeze_status"] == "blocked"
    assert "plan_generated_after_freeze" in evidence["reason_codes"]


def test_preplan_source_uses_funded_state_without_old_plan_cycle(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    state = tmp_path / "state.json"
    portfolio = tmp_path / "portfolio.json"
    strategy = tmp_path / "strategy.json"
    workflow = tmp_path / "workflow.json"
    _write(
        plan,
        {
            "plan_id": "retroactive-old-plan",
            "plan_date": "2026-07-15",
            "trade_date": TARGET,
            "source_data_cutoff": "2026-07-15",
            "generated_at": "2026-07-17T12:20:01+08:00",
            "symbols": [{"symbol": "600000"}],
        },
    )
    _write(
        state,
        {
            "updated_at": "2026-07-17T15:01:00+08:00",
            "positions": [{"symbol": "999999", "name": "funded-paper-position"}],
        },
    )
    portfolio.write_text("{corrupt", encoding="utf-8")
    _write(strategy, {"generated_at": "2026-07-16T18:00:00", "long_candidates": []})
    _write(workflow, {"generated_at": "2026-07-16T18:01:00", "auto_strategy": {}})

    payload = collect_intraday_required_symbols(
        source="preplan",
        target_trade_date="2026-07-17",
        plan_path=plan,
        state_path=state,
        portfolio_latest_path=portfolio,
        strategy_path=strategy,
        workflow_path=workflow,
    )

    manifest = payload["source_manifest"]
    assert payload["source_manifest_status"] == "passed"
    assert payload["source_manifest_formal_eligible"] is True
    assert [item["symbol"] for item in payload["items"]] == ["999999"]
    assert payload["items"][0]["sources"] == ["auto_paper_state"]
    assert [item["label"] for item in manifest["sources"]] == ["paper_state"]
    assert manifest["ignored_preplan_sources"] == [
        "plan",
        "paper_latest",
        "strategy",
        "workflow",
    ]
    assert manifest["reason_codes"] == ["preplan_excludes_derivative_future_plan_sources"]
