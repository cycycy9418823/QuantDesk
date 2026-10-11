from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import quantdesk.daily_candidate_refresh as candidate_refresh
import quantdesk.daily_workflow_orchestrator as workflow
import quantdesk.paper.auto_paper_portfolio as auto_portfolio
import quantdesk.paper.position_management_engine as position
from quantdesk.paper.conditional_trade_plan import build_conditional_trade_plan
from quantdesk.paper.position_management_engine import simulate_conditional_plan


TARGET_DATE = "2026-08-10"
TRADE_DATE = "2026-08-11"


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _formal_candidate(symbol: str = "600143") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": "formal candidate",
        "strategy_type": "long",
        "strategy_label": "long",
        "rank": 1,
        "score": 107.85,
        "reason": "fixture",
        "candidate_source": "auto_strategy_sandbox",
        "data_latest_date": TARGET_DATE,
        "is_target_date_covered": True,
        "daily_feature_available": True,
        "daily_feature_latest_date": TARGET_DATE,
        "data_health_status": "ok",
        "liquidity_status": "ok",
        "sector": "basic_chemicals",
        "industry": "basic_chemicals",
        "primary_theme": "fixture",
        "close": 15.71,
        "amount_ma20": 698_473_307.2,
        "source_qualification": {
            "universe_member": True,
            "feature_eligible": True,
            "feature_latest_date": TARGET_DATE,
            "feature_price_basis": "raw",
            "excluded_by_daily_gate": False,
            "excluded_by_feature_gate": False,
        },
    }


def _formal_batch(symbol: str = "600143") -> dict[str, Any]:
    candidate = _formal_candidate(symbol)
    return {
        "task_type": "formal_candidate_daily_batch",
        "schema_version": "round13_formal_candidate_refresh_v1",
        "status": "passed",
        "formal_eligible": True,
        "target_date": TARGET_DATE,
        "candidate_count": 1,
        "candidates": [candidate],
        "provenance": {
            "hash": "a" * 64,
            "source": "daily_data_chain:fixture-run",
            "source_run_id": "fixture-run",
        },
    }


def _candidate_envelope(
    root: Path,
    symbol: str = "600143",
) -> dict[str, Any]:
    candidate = _formal_candidate(symbol)
    batch_path = (
        root
        / "results"
        / "candidate_refresh"
        / "daily"
        / f"{TARGET_DATE}.json"
    )
    return {
        "schema_version": "formal_candidate_envelope_v1",
        "target_date": TARGET_DATE,
        "batch_path": str(batch_path),
        "batch_provenance_hash": "a" * 64,
        "candidate_sha256": _json_sha256(candidate),
        "candidate": candidate,
    }


def _patch_daily_workflow(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    formal_load_result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results = root / "results"
    reports = root / "reports"
    daily = root / "data" / "daily"
    state = root / "state" / "auto_paper_portfolio.json"
    for path in (results, reports, daily, state.parent):
        path.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": "3.7.0.6",
                "initial_cash": 10_000.0,
                "cash": 10_000.0,
                "positions": [],
                "closed_positions": [],
                "applied_execution_ids": [],
            }
        ),
        encoding="utf-8",
    )

    poison = {
        "symbol": "999999",
        "name": "must never reach paper",
        "strategy_type": "long",
        "close": 1.0,
    }
    formal_calls: list[dict[str, Any]] = []
    portfolio_calls: list[dict[str, Any]] = []

    def load_formal(*args: Any, **kwargs: Any) -> dict[str, Any]:
        formal_calls.append({"args": args, "kwargs": kwargs})
        return copy.deepcopy(formal_load_result)

    def run_portfolio(**kwargs: Any) -> dict[str, Any]:
        portfolio_calls.append(copy.deepcopy(kwargs))
        return {
            "ok": True,
            "summary": {"position_count": 0},
            "conditional_plan": {
                "plan_id": "fixture-next-plan",
                "trade_date": "2026-08-11",
            },
            "conditional_plan_summary": {"symbol_count": 1},
            "conditional_execution": {
                "trade_date": TARGET_DATE,
                "events": [],
            },
            "conditional_execution_summary": {
                "event_count": 0,
                "no_fill_count": 0,
                "missing_intraday_count": 0,
            },
        }

    monkeypatch.setattr(workflow, "PROJECT_ROOT", root)
    monkeypatch.setattr(workflow, "RESULTS_DIR", results)
    monkeypatch.setattr(workflow, "REPORTS_DIR", reports)
    monkeypatch.setattr(workflow, "DAILY_DIR", daily)
    monkeypatch.setattr(
        workflow,
        "DAILY_WORKFLOW_LATEST_JSON",
        results / "daily_workflow_latest.json",
    )
    monkeypatch.setattr(workflow, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state)
    monkeypatch.setattr(workflow, "THEME_MAP_PATH", root / "theme_map.csv")
    monkeypatch.setattr(
        workflow,
        "load_formal_candidate_batch",
        load_formal,
        raising=False,
    )
    monkeypatch.setattr(
        workflow,
        "_low_price_status",
        lambda: {"ok": True, "included_count": 1, "message": "ok"},
    )
    monkeypatch.setattr(workflow, "_latest_daily_date", lambda: TARGET_DATE)
    monkeypatch.setattr(workflow, "infer_target_date", lambda target_date=None: (target_date or TARGET_DATE, "fixture"))
    monkeypatch.setattr(
        workflow,
        "daily_price_status",
        lambda **kwargs: {
            "ok": True,
            "target_date": TARGET_DATE,
            "latest_local_date": TARGET_DATE,
            "max_local_date": TARGET_DATE,
            "is_stale": False,
            "fresh_count": 1,
            "stale_count": 0,
            "missing_count": 0,
        },
    )
    monkeypatch.setattr(
        workflow,
        "_run_low_price_report",
        lambda: {
            "ok": True,
            "summary": {
                "latest_complete_daily_date": TARGET_DATE,
                "analyzed_count": 1,
            },
        },
    )
    monkeypatch.setattr(
        workflow,
        "build_daily_features",
        lambda **kwargs: {"ok": True, "latest_date": TARGET_DATE},
    )
    monkeypatch.setattr(
        workflow,
        "daily_feature_status",
        lambda: {"ok": True, "latest_date": TARGET_DATE, "feature_count": 1},
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_data_freshness",
        lambda **kwargs: {
            "ok": True,
            "target_complete_trade_date": TARGET_DATE,
            "local_daily_latest_date": TARGET_DATE,
            "can_review_yesterday_positions": True,
            "portfolio_review_valid": True,
            "intraday_review_valid": True,
            "review_mode": "current_review",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        workflow,
        "theme_map_status",
        lambda: {"ok": True, "mapped_count": 1, "unknown_count": 0},
    )
    monkeypatch.setattr(
        workflow,
        "run_auto_strategy_cycle",
        lambda **kwargs: {
            "ok": True,
            "generated_at": f"{TARGET_DATE}T20:00:00+08:00",
            "long_candidates": [poison],
            "board_candidates": [],
            "standard_candidates": [poison],
            "summary": {
                "latest_daily_date": TARGET_DATE,
                "long_candidate_count": 1,
                "board_candidate_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        workflow,
        "attach_theme_map_to_candidates",
        lambda rows, theme_map: list(rows),
    )
    monkeypatch.setattr(workflow, "load_theme_map", lambda path: {})
    monkeypatch.setattr(
        workflow,
        "_attach_reference_confidence_to_candidates",
        lambda rows, **kwargs: list(rows),
    )
    monkeypatch.setattr(
        workflow,
        "validate_theme_map_for_current_candidates",
        lambda: {"unknown_current_candidates": 0, "message": "ok"},
    )
    monkeypatch.setattr(
        workflow,
        "build_daily_watchpool_step",
        lambda rows: {
            "ok": True,
            "candidate_count": len(rows),
            "selected_count": len(rows),
            "added_count": 0,
            "watchlist_count": 0,
            "added_symbols": [],
        },
    )
    monkeypatch.setattr(
        workflow,
        "load_archived_conditional_plan",
        lambda *args, **kwargs: {
            "status": "missing",
            "reason_codes": ["conditional_plan_archive_missing"],
        },
    )
    monkeypatch.setattr(
        workflow,
        "scan_unreplayed_archived_plans",
        lambda *args, **kwargs: {
            "status": "passed",
            "unreplayed_count": 0,
            "unreplayed": [],
            "archive_unreadable": [],
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(workflow, "run_auto_paper_portfolio", run_portfolio)
    monkeypatch.setattr(
        workflow,
        "intraday_gap_status",
        lambda **kwargs: {
            "ok": True,
            "target_trade_date": TARGET_DATE,
            "total_symbols": 0,
            "available_count": 0,
            "missing_count": 0,
            "invalid_count": 0,
            "missing_symbols": [],
        },
    )
    monkeypatch.setattr(
        workflow,
        "_run_daily_paper_consistency_check",
        lambda **kwargs: {
            "ok": True,
            "status": "passed",
            "issue_count": 0,
            "warning_count": 0,
            "warnings": [],
        },
    )

    import quantdesk.market_brain_daily as market_brain

    monkeypatch.setattr(
        market_brain,
        "run_daily_brain_snapshots",
        lambda **kwargs: {
            "ok": True,
            "market_brain": {
                "snapshot": {"formal_regime": "UNKNOWN", "evidence_eligible": False}
            },
            "strategy_brain_shadow": {"snapshot": {"candidate_count": 0}},
            "assembly_warnings": [],
        },
    )
    return formal_calls, portfolio_calls


def test_daily_review_new_candidates_use_only_dated_formal_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / f"{TARGET_DATE}.json"
    )
    formal_calls, portfolio_calls = _patch_daily_workflow(
        monkeypatch,
        tmp_path,
        formal_load_result={
            "status": "passed",
            "batch": _formal_batch(),
            "path": str(batch_path),
            "reason_codes": [],
        },
    )

    workflow.run_daily_workflow(with_guardrails=False)

    assert len(formal_calls) == 1, "Daily Review never loaded the formal batch"
    assert len(portfolio_calls) == 1
    call = portfolio_calls[0]
    assert call["formal_candidate_batch"]["candidates"][0]["symbol"] == "600143"
    assert Path(call["formal_candidate_batch_path"]) == batch_path
    assert call["publish_next_plan"] is False
    assert "auto_strategy_payload" not in call
    assert "999999" not in json.dumps(call, sort_keys=True)


def test_daily_review_prefers_exact_completed_chain_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = "daily-run-20260810-retry"
    manifest = {
        "status": "completed",
        "authoritative": True,
        "published_latest": True,
        "run_id": run_id,
        "target_date": TARGET_DATE,
    }
    latest_path = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    run_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / run_id
        / "manifest.json"
    )
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(manifest), encoding="utf-8")
    run_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls: list[dict[str, Any]] = []

    def resolve(root: Path, *, data_chain_manifest: dict[str, Any]):
        calls.append({"root": root, "manifest": copy.deepcopy(data_chain_manifest)})
        return {
            "status": "passed",
            "batch": _formal_batch(),
            "path": str(
                tmp_path
                / "results"
                / "candidate_refresh"
                / "runs"
                / run_id
                / "formal_candidate_batch.json"
            ),
            "source_mode": "run_scoped_conflict_recovery",
            "reason_codes": [],
        }

    monkeypatch.setattr(workflow, "resolve_chain_bound_formal_candidate_batch", resolve)
    monkeypatch.setattr(
        workflow,
        "load_formal_candidate_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed chain must not fall back to dated observation")
        ),
    )

    result = workflow._load_formal_candidate_batch_for_review(
        tmp_path,
        target_date=TARGET_DATE,
    )

    assert result["status"] == "passed"
    assert result["source_mode"] == "run_scoped_conflict_recovery"
    assert calls == [{"root": tmp_path.resolve(), "manifest": manifest}]


def test_daily_review_blocks_when_completed_latest_and_run_manifest_disagree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = "daily-run-20260810-retry"
    latest = {
        "status": "completed",
        "authoritative": True,
        "published_latest": True,
        "run_id": run_id,
        "target_date": TARGET_DATE,
    }
    terminal = {**latest, "published_latest": False}
    latest_path = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    run_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / run_id
        / "manifest.json"
    )
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(latest), encoding="utf-8")
    run_path.write_text(json.dumps(terminal), encoding="utf-8")
    monkeypatch.setattr(
        workflow,
        "resolve_chain_bound_formal_candidate_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched terminal manifests must block before resolve")
        ),
    )
    monkeypatch.setattr(
        workflow,
        "load_formal_candidate_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed mismatch must not fall back to dated observation")
        ),
    )

    result = workflow._load_formal_candidate_batch_for_review(
        tmp_path,
        target_date=TARGET_DATE,
    )

    assert result["status"] == "blocked"
    assert result["batch"] is None
    assert result["reason_codes"] == [
        "formal_candidate_data_chain_terminal_mismatch"
    ]


@pytest.mark.parametrize("formal_status", ["missing", "blocked"])
def test_daily_review_invalid_formal_batch_never_falls_back_to_strategy_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    formal_status: str,
) -> None:
    formal_calls, portfolio_calls = _patch_daily_workflow(
        monkeypatch,
        tmp_path,
        formal_load_result={
            "status": formal_status,
            "batch": None,
            "path": str(
                tmp_path
                / "results"
                / "candidate_refresh"
                / "daily"
                / f"{TARGET_DATE}.json"
            ),
            "reason_codes": [f"formal_candidate_batch_{formal_status}"],
        },
    )

    workflow.run_daily_workflow(with_guardrails=False)

    assert len(formal_calls) == 1, "Daily Review skipped the fail-closed formal loader"
    assert len(portfolio_calls) == 1, "due-plan replay must remain callable"
    call = portfolio_calls[0]
    assert call.get("formal_candidate_batch") is None
    assert call["formal_candidate_status"] == formal_status
    assert "auto_strategy_payload" not in call
    assert "999999" not in json.dumps(call, sort_keys=True)


def test_daily_review_refreshes_pit_sector_status_for_target_date_before_paper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / f"{TARGET_DATE}.json"
    )
    pit_calls: list[dict[str, Any]] = []

    def build_pit(**kwargs: Any) -> dict[str, Any]:
        pit_calls.append({"build": copy.deepcopy(kwargs)})
        return {
            "ok": True,
            "status": "passed",
            "target_date": TARGET_DATE,
            "evidence_eligible": True,
            "pit_eligible": True,
            "reason_codes": [],
            "sector_count": 1,
        }

    def write_pit(payload: dict[str, Any], *, output_path: Path) -> Path:
        pit_calls.append(
            {"write": copy.deepcopy(payload), "output_path": Path(output_path)}
        )
        return Path(output_path)

    monkeypatch.setattr(workflow, "build_pit_sector_status", build_pit, raising=False)
    monkeypatch.setattr(workflow, "write_pit_sector_status", write_pit, raising=False)
    _patch_daily_workflow(
        monkeypatch,
        tmp_path,
        formal_load_result={
            "status": "passed",
            "batch": _formal_batch(),
            "path": str(batch_path),
            "reason_codes": [],
        },
    )

    workflow.run_daily_workflow(with_guardrails=False)

    assert pit_calls[0]["build"]["as_of"] == TARGET_DATE
    assert pit_calls[0]["build"]["history_dir"] == tmp_path / "data" / "sector_history"
    assert pit_calls[1]["write"]["pit_eligible"] is True
    assert pit_calls[1]["output_path"] == tmp_path / "results" / "pit_sector_status_latest.json"


def _write_plan_inputs(root: Path, symbol: str) -> tuple[Path, Path, Path]:
    daily = root / "data" / "daily"
    features = root / "data" / "features" / "daily_features_latest.csv"
    universe = root / "data" / "universe" / "low_price_learning_symbols.csv"
    daily.mkdir(parents=True, exist_ok=True)
    features.parent.mkdir(parents=True, exist_ok=True)
    universe.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(end=TARGET_DATE, periods=30)
    pd.DataFrame(
        {
            "date": [day.date().isoformat() for day in dates],
            "open": [15.5] * len(dates),
            "high": [16.0] * len(dates),
            "low": [15.0] * len(dates),
            "close": [15.71] * len(dates),
            "volume": [1_000_000] * len(dates),
            "amount": [15_710_000.0] * len(dates),
            "adjust": ["raw"] * len(dates),
        }
    ).to_csv(daily / f"{symbol}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [
            {
                "symbol": symbol,
                "latest_date": TARGET_DATE,
                "ma5": 15.5,
                "ma10": 15.4,
                "ma20": 15.3,
            }
        ]
    ).to_csv(features, index=False, encoding="utf-8-sig")
    universe.write_text(
        f"symbol,name,included\n{symbol},formal candidate,true\n",
        encoding="utf-8-sig",
    )
    return daily, features, universe


def test_conditional_plan_preserves_bound_candidate_envelope(tmp_path: Path) -> None:
    from tests.test_b5_formal_candidate_intake import make_formal_batch

    symbol = "600143"
    batch = make_formal_batch(TARGET_DATE, candidate=_formal_candidate(symbol))
    batch_path = tmp_path / "results/candidate_refresh/daily" / f"{TARGET_DATE}.json"
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    envelope = candidate_refresh.build_candidate_envelope(batch, batch_path=batch_path, candidate=batch["candidates"][0])
    daily, features, universe = _write_plan_inputs(tmp_path, symbol)
    candidate = {
        **copy.deepcopy(envelope["candidate"]),
        "candidate_envelope": copy.deepcopy(envelope),
    }

    plan = build_conditional_trade_plan(
        auto_strategy_payload={
            "ok": True,
            "generated_at": f"{TARGET_DATE}T18:50:00+08:00",
            "standard_candidates": [candidate],
            "long_candidates": [candidate],
            "board_candidates": [],
            "formal_candidate_batch": batch,
            "formal_candidate_batch_path": str(batch_path),
        },
        state={
            "initial_cash": 10_000.0,
            "cash": 10_000.0,
            "positions": [],
        },
        plan_date=TARGET_DATE,
        trade_date="2026-08-11",
        write=False,
        daily_dir=daily,
        daily_feature_path=features,
        low_price_universe_path=universe,
    )

    row = next(item for item in plan["symbols"] if item["symbol"] == symbol)
    assert row["candidate_envelope"] == envelope
    assert row["candidate_envelope"]["batch_path"].endswith(
        f"candidate_refresh{Path('/')}daily{Path('/')}{TARGET_DATE}.json"
    )
    assert row["candidate_envelope"]["candidate_sha256"] == _json_sha256(
        envelope["candidate"]
    )


def _triggerable_plan_item(root: Path) -> dict[str, Any]:
    envelope = _candidate_envelope(root)
    return {
        "plan_id": "plan_20260810_for_20260811",
        "symbol": "600143",
        "name": "formal candidate",
        "strategy_type": "long",
        "position_state": "new_candidate",
        "plan_date": TARGET_DATE,
        "trade_date": "2026-08-11",
        "reference_close": 15.71,
        "no_chase_above": 20.0,
        "lot_size": 100,
        "candidate_envelope": envelope,
        "rules": {
            "entry_rules": [
                {
                    "action": "buy",
                    "trigger_type": "pullback_buy",
                    "price_low": 15.0,
                    "price_high": 16.0,
                    "quantity": 100,
                    "reason": "fixture trigger",
                }
            ],
            "add_rules": [],
            "exit_rules": [],
            "risk_guards": [],
        },
    }


def test_execution_event_preserves_candidate_envelope_exactly(tmp_path: Path) -> None:
    plan_item = _triggerable_plan_item(tmp_path)

    event = simulate_conditional_plan(
        plan_item,
        pd.DataFrame(
            {
                "datetime": ["2026-08-11 09:31:00"],
                "open": [15.5],
                "high": [15.6],
                "low": [15.4],
                "close": [15.5],
                "volume": [1_000],
                "amount": [15_500.0],
                # T08：合成分钟帧是未复权原始报价，显式声明口径。
                "adjust": ["raw"],
            }
        ),
    )

    assert event["action"] == "buy"
    assert event["quantity"] == 100
    assert event["candidate_envelope"] == plan_item["candidate_envelope"]


def test_new_position_projects_taxonomy_from_verified_candidate_envelope(tmp_path: Path) -> None:
    envelope = _candidate_envelope(tmp_path)
    event = {
        "symbol": envelope["candidate"]["symbol"],
        "name": envelope["candidate"]["name"],
        "strategy_type": "long",
        "trade_date": TRADE_DATE,
        "trigger_price": 15.71,
        "quantity": 100,
        "candidate_envelope": envelope,
    }

    created = position._new_position_from_event(event)

    assert created["sector"] == "basic_chemicals"
    assert created["industry"] == "basic_chemicals"


def test_new_position_does_not_project_taxonomy_from_invalid_envelope(tmp_path: Path) -> None:
    envelope = _candidate_envelope(tmp_path)
    envelope["candidate_sha256"] = "0" * 64
    event = {
        "symbol": envelope["candidate"]["symbol"],
        "name": envelope["candidate"]["name"],
        "strategy_type": "long",
        "trade_date": TRADE_DATE,
        "trigger_price": 15.71,
        "quantity": 100,
        "candidate_envelope": envelope,
    }

    created = position._new_position_from_event(event)

    assert "sector" not in created
    assert "industry" not in created


def test_shadow_evaluator_uses_envelope_candidate_and_dated_batch_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan_item = _triggerable_plan_item(tmp_path)
    event = simulate_conditional_plan(
        plan_item,
        pd.DataFrame(
            {
                "datetime": ["2026-08-11 09:31:00"],
                "open": [15.5],
                "high": [15.6],
                "low": [15.4],
                "close": [15.5],
                "volume": [1_000],
                "amount": [15_500.0],
                # T08：合成分钟帧是未复权原始报价，显式声明口径。
                "adjust": ["raw"],
            }
        ),
    )
    captured: list[dict[str, Any]] = []

    def evaluate(**kwargs: Any) -> dict[str, Any]:
        captured.append(copy.deepcopy(kwargs))
        return {
            "evaluated": True,
            "status": "would_pass",
            "would_pass": True,
            "would_block": False,
            "insufficient_evidence": False,
            "reason_codes": [],
            "missing_components": [],
            "missing_component_count": 0,
            "symbol": "600143",
            "side": "buy",
        }

    monkeypatch.setattr(position, "evaluate_shadow_risk_order", evaluate)
    records: list[dict[str, Any]] = []
    execution_path = tmp_path / "results" / "conditional_execution_latest.json"
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    position.apply_conditional_execution_to_state(
        {
            "schema_version": "3.7.0.6",
            "initial_cash": 10_000.0,
            "cash": 10_000.0,
            "positions": [],
            "closed_positions": [],
            "applied_execution_ids": [],
        },
        {
            "plan_id": plan_item["plan_id"],
            "trade_date": "2026-08-11",
            "events": [event],
            "no_fill": [],
            "blocked": [],
        },
        daily_dir=tmp_path / "data" / "daily",
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_records=records,
        execution_source_path=execution_path,
        ledger_source_path=ledger_path,
    )

    assert len(captured) == 1
    call = captured[0]
    assert call["candidate"] == plan_item["candidate_envelope"]["candidate"]
    assert Path(call["candidate_source_path"]) == Path(
        plan_item["candidate_envelope"]["batch_path"]
    )
    assert Path(call["price_source_path"]) == execution_path
    assert call["candidate"] != event


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing", "candidate_envelope_missing"),
        ("tampered_hash", "candidate_envelope_candidate_hash_mismatch"),
    ],
)
def test_missing_or_tampered_candidate_envelope_fails_shadow_closed_but_keeps_legacy_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    plan_item = _triggerable_plan_item(tmp_path)
    event = simulate_conditional_plan(
        plan_item,
        pd.DataFrame(
            {
                "datetime": ["2026-08-11 09:31:00"],
                "open": [15.5],
                "high": [15.6],
                "low": [15.4],
                "close": [15.5],
                "volume": [1_000],
                "amount": [15_500.0],
                # T08：合成分钟帧是未复权原始报价，显式声明口径。
                "adjust": ["raw"],
            }
        ),
    )
    if mutation == "missing":
        event.pop("candidate_envelope", None)
    else:
        event["candidate_envelope"]["candidate_sha256"] = "0" * 64

    def must_not_evaluate(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid envelope reached evaluate_shadow_risk_order")

    monkeypatch.setattr(position, "evaluate_shadow_risk_order", must_not_evaluate)
    records: list[dict[str, Any]] = []
    result = position.apply_conditional_execution_to_state(
        {
            "schema_version": "3.7.0.6",
            "initial_cash": 10_000.0,
            "cash": 10_000.0,
            "positions": [],
            "closed_positions": [],
            "applied_execution_ids": [],
        },
        {
            "plan_id": plan_item["plan_id"],
            "trade_date": "2026-08-11",
            "events": [event],
            "no_fill": [],
            "blocked": [],
        },
        daily_dir=tmp_path / "data" / "daily",
        risk_gate_shadow_enabled=True,
        risk_gate_enforced=False,
        risk_gate_shadow_records=records,
        risk_gate_shadow_write_errors=False,
        execution_source_path=tmp_path / "results" / "execution.json",
        ledger_source_path=tmp_path / "state" / "ledger.json",
    )

    assert result["actions"], "shadow-only mode must preserve the legacy paper apply"
    assert result["state"]["cash"] < 10_000.0
    assert len(records) == 1
    assert records[0]["status"] == "insufficient_evidence"
    assert records[0]["insufficient_evidence"] is True
    assert expected_reason in records[0]["reason_codes"]


def _legacy_due_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "plan_id": "plan_20260810_for_20260811",
        "plan_date": TARGET_DATE,
        "trade_date": TRADE_DATE,
        "source_data_cutoff": TARGET_DATE,
        "symbols": copy.deepcopy(rows),
    }


def _legacy_row(
    symbol: str,
    *,
    strategy_type: str = "long",
    position_state: str = "new_candidate",
    reference_close: float = 15.71,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "strategy_type": strategy_type,
        "position_state": position_state,
        "plan_date": TARGET_DATE,
        "trade_date": TRADE_DATE,
        "source_data_cutoff": TARGET_DATE,
        "reference_close": reference_close,
    }


def _modern_due_plan_with_run_scoped_envelope(
    root: Path,
    *,
    symbol: str = "600143",
) -> dict[str, Any]:
    run_id = "daily-data-chain-20260810T103003Z-fixture"
    batch = _formal_batch(symbol)
    batch["provenance"] = {
        **batch["provenance"],
        "source": f"daily_data_chain:{run_id}",
        "source_run_id": run_id,
    }
    batch_path = (
        root
        / "results"
        / "candidate_refresh"
        / "runs"
        / run_id
        / "formal_candidate_batch.json"
    )
    row = _legacy_row(symbol)
    row["candidate_envelope"] = candidate_refresh.build_candidate_envelope(
        batch,
        batch_path=batch_path,
        candidate=batch["candidates"][0],
    )
    return {
        **_legacy_due_plan([row]),
        "freeze_status": "completed",
        "data_chain_run_id": run_id,
        "formal_candidate_batch_path": str(batch_path),
        "formal_candidate_batch_provenance_hash": batch["provenance"]["hash"],
        "formal_candidate_batch_source_mode": "run_scoped_conflict_recovery",
    }


def test_modern_due_plan_preserves_exact_run_scoped_envelope_without_dated_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _modern_due_plan_with_run_scoped_envelope(tmp_path)
    before = copy.deepcopy(plan)

    def dated_loader_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("modern due plan must not be rebound from dated snapshot")

    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        dated_loader_must_not_run,
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        plan,
    )

    assert result["status"] == "completed"
    assert result["plan"] == before
    assert result["formal_batch_path"] == plan["formal_candidate_batch_path"]
    assert result["exact_bindable"] == 1
    assert result["existing_valid_envelope"] == 1
    assert result["enriched"] == 0
    assert result["rebound_existing_envelope"] == 0
    assert result["rejected_mismatch"] == 0


def test_modern_due_plan_preserves_exact_dated_snapshot_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _modern_due_plan_with_run_scoped_envelope(tmp_path)
    dated_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / f"{TARGET_DATE}.json"
    )
    plan["formal_candidate_batch_path"] = str(dated_path)
    plan["formal_candidate_batch_source_mode"] = "dated_snapshot"
    envelope = plan["symbols"][0]["candidate_envelope"]
    envelope["batch_path"] = str(dated_path)
    envelope.pop("source_run_id", None)
    before = copy.deepcopy(plan)

    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("modern dated envelope must be preserved, not rebound")
        ),
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        plan,
    )

    assert result["status"] == "completed"
    assert result["plan"] == before
    assert result["existing_valid_envelope"] == 1
    assert result["enriched"] == 0
    assert result["rejected_mismatch"] == 0


def test_historical_plan_with_only_data_chain_run_id_still_uses_legacy_dated_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch = _formal_batch("600143")
    dated_path = (
        tmp_path
        / "results"
        / "candidate_refresh"
        / "daily"
        / f"{TARGET_DATE}.json"
    )
    calls: list[str] = []

    def load_dated(_root: Path, *, target_date: str) -> dict[str, Any]:
        calls.append(target_date)
        return {
            "status": "passed",
            "batch": copy.deepcopy(batch),
            "path": str(dated_path),
            "reason_codes": [],
        }

    monkeypatch.setattr(candidate_refresh, "load_formal_candidate_batch", load_dated)
    plan = _legacy_due_plan([_legacy_row("600143")])
    plan["data_chain_run_id"] = "daily-data-chain-20260810T103007Z-historical"

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        plan,
    )

    assert calls == [TARGET_DATE]
    assert result["status"] == "completed"
    assert result["enriched"] == 1
    assert isinstance(result["plan"]["symbols"][0]["candidate_envelope"], dict)


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("path", "modern_candidate_batch_path_mismatch"),
        ("source_mode_path", "modern_candidate_batch_source_mode_path_mismatch"),
        ("hash", "modern_candidate_batch_hash_mismatch"),
        ("run", "modern_candidate_data_chain_run_mismatch"),
        ("symbol", "modern_candidate_symbol_mismatch"),
        ("strategy", "modern_candidate_strategy_mismatch"),
        ("reference_close", "modern_candidate_reference_close_mismatch"),
    ],
)
def test_modern_due_plan_binding_mismatch_fails_closed_without_dated_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    plan = _modern_due_plan_with_run_scoped_envelope(tmp_path)
    if mutation == "path":
        plan["formal_candidate_batch_path"] = str(
            tmp_path
            / "results"
            / "candidate_refresh"
            / "runs"
            / plan["data_chain_run_id"]
            / "other_candidate_batch.json"
        )
    elif mutation == "source_mode_path":
        plan["formal_candidate_batch_source_mode"] = "dated_snapshot"
    elif mutation == "hash":
        plan["formal_candidate_batch_provenance_hash"] = "b" * 64
    elif mutation == "run":
        plan["data_chain_run_id"] = "daily-data-chain-20260810T103003Z-other"
    elif mutation == "symbol":
        plan["symbols"][0]["symbol"] = "600000"
    elif mutation == "strategy":
        plan["symbols"][0]["strategy_type"] = "board"
    elif mutation == "reference_close":
        plan["symbols"][0]["reference_close"] = 15.72

    def dated_loader_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("mismatched modern plan must fail closed, not rebind dated")

    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        dated_loader_must_not_run,
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        plan,
    )

    assert result["status"] in {"blocked", "completed_with_rejections"}
    assert "candidate_envelope" not in result["plan"]["symbols"][0]
    assert result["enriched"] == 0
    assert result["rebound_existing_envelope"] == 0
    assert result["existing_valid_envelope"] == 0
    assert result["rejected_mismatch"] == 1
    assert expected_reason in result["reason_codes"]


def test_legacy_due_plan_enrichment_binds_exact_candidates_in_memory_and_leaves_watch_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch = {
        **_formal_batch("600143"),
        "candidates": [
            _formal_candidate("600143"),
            _formal_candidate("600000"),
        ],
        "candidate_count": 2,
    }
    # The production loader is deliberately bypassed only to keep this unit
    # fixture small; the helper must still enforce the same date/key/price
    # binding against the loader result.
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "passed",
            "batch": copy.deepcopy(batch),
            "path": str(
                tmp_path
                / "results"
                / "candidate_refresh"
                / "daily"
                / f"{target_date}.json"
            ),
            "reason_codes": [],
        },
    )
    rows = [
        _legacy_row("600143"),
        _legacy_row("600000", position_state="holding", reference_close=15.71),
        _legacy_row("999999", position_state="watch_exit", reference_close=1.0),
    ]
    plan = _legacy_due_plan(rows)
    archive = tmp_path / "results" / "conditional_trade_plans_daily" / f"{TRADE_DATE}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    before = archive.read_bytes()

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        plan,
    )

    assert result["status"] == "completed"
    assert result["exact_bindable"] == 2
    assert result["enriched"] == 2
    assert result["unchanged_unmatched"] == 1
    assert result["rejected_mismatch"] == 0
    enriched = result["plan"]["symbols"]
    assert all(isinstance(enriched[i].get("candidate_envelope"), dict) for i in (0, 1))
    assert "candidate_envelope" not in enriched[2]
    assert archive.read_bytes() == before


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("date", "legacy_due_plan_date_mismatch"),
        ("price", "legacy_candidate_reference_close_mismatch"),
        ("strategy", "legacy_candidate_strategy_mismatch"),
        ("duplicate", "legacy_formal_candidate_duplicate_key"),
    ],
)
def test_legacy_due_plan_enrichment_rejects_binding_mismatches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    first = _formal_candidate("600143")
    candidates = [first]
    row = _legacy_row("600143")
    plan = _legacy_due_plan([row])
    if mutation == "date":
        plan["source_data_cutoff"] = "2026-08-09"
    elif mutation == "price":
        plan["symbols"][0]["reference_close"] = 15.72
    elif mutation == "strategy":
        plan["symbols"][0]["strategy_type"] = "board"
    elif mutation == "duplicate":
        candidates.append(copy.deepcopy(first))
    batch = {
        **_formal_batch("600143"),
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "passed",
            "batch": copy.deepcopy(batch),
            "path": str(tmp_path / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"),
            "reason_codes": [],
        },
    )
    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(tmp_path, plan)
    assert result["plan"]["symbols"][0].get("candidate_envelope") is None
    assert expected_reason in result["reason_codes"] or any(
        expected_reason in (entry.get("reason_codes") or [])
        for entry in result.get("rejections", [])
    )


def test_legacy_due_plan_enrichment_missing_batch_is_blocked_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _legacy_due_plan([_legacy_row("600143")])
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "missing",
            "batch": None,
            "path": str(tmp_path / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"),
            "reason_codes": ["formal_candidate_batch_missing"],
        },
    )
    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(tmp_path, plan)
    assert result["status"] == "blocked"
    assert result["enriched"] == 0
    assert result["plan"] == plan
    assert "formal_candidate_batch_missing" in result["reason_codes"]


def test_auto_paper_enriches_due_plan_after_archive_load_before_simulation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    due_plan = _legacy_due_plan([_legacy_row("600143")])
    call_order: list[str] = []
    captured_simulation: list[dict[str, Any]] = []

    def enrich(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
        call_order.append("enrich")
        enriched = copy.deepcopy(plan)
        enriched["symbols"][0]["candidate_envelope"] = {"fixture": True}
        return {
            "status": "completed",
            "plan": enriched,
            "target_date": TARGET_DATE,
            "formal_batch_path": str(
                tmp_path
                / "results"
                / "candidate_refresh"
                / "daily"
                / f"{TARGET_DATE}.json"
            ),
            "exact_bindable": 1,
            "enriched": 1,
            "existing_valid_envelope": 0,
            "unchanged_unmatched": 0,
            "rejected_mismatch": 0,
            "rejections": [],
            "reason_codes": [],
        }

    def simulate(plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        call_order.append("simulate")
        captured_simulation.append(copy.deepcopy(plan))
        return {
            "ok": True,
            "plan_id": plan["plan_id"],
            "trade_date": TRADE_DATE,
            "summary": {"event_count": 0, "no_fill_count": 0, "blocked_count": 0},
            "events": [],
            "no_fill": [],
            "blocked": [],
        }

    monkeypatch.setattr(auto_portfolio, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(auto_portfolio, "INTRADAY_DIR", tmp_path / "data/intraday")
    monkeypatch.setattr(
        auto_portfolio,
        "AUTO_PAPER_PORTFOLIO_STATE_JSON",
        tmp_path / "state" / "auto_paper_portfolio.json",
    )
    monkeypatch.setattr(auto_portfolio, "enrich_legacy_due_plan_candidate_envelopes", enrich, raising=False)
    monkeypatch.setattr(
        auto_portfolio,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": TARGET_DATE,
            "can_review_yesterday_positions": True,
        },
    )
    base_state = {
        "schema_version": "3.7.0.6",
        "initial_cash": 10_000.0,
        "cash": 10_000.0,
        "positions": [],
        "closed_positions": [],
        "daily_actions": [],
        "actions": [],
        "applied_execution_ids": [],
    }
    monkeypatch.setattr(auto_portfolio, "_load_state", lambda _cash: copy.deepcopy(base_state))
    monkeypatch.setattr(
        auto_portfolio,
        "build_conditional_trade_plan",
        lambda **_kwargs: {
            "ok": True,
            "plan_id": "plan_20260811_for_20260812",
            "plan_date": TRADE_DATE,
            "trade_date": "2026-08-12",
            "source_data_cutoff": TRADE_DATE,
            "summary": {"symbol_count": 0, "new_candidate_plan_count": 0},
        },
    )
    monkeypatch.setattr(auto_portfolio, "simulate_conditional_plan_file", simulate)
    monkeypatch.setattr(auto_portfolio, "_load_auto_adjustment_factors", lambda _symbols: None)
    monkeypatch.setattr(
        auto_portfolio,
        "apply_conditional_execution_to_state",
        lambda state, _execution, **_kwargs: {
            "ok": True,
            "state": copy.deepcopy(state),
            "actions": [],
            "already_updated_today": False,
        },
    )
    monkeypatch.setattr(
        auto_portfolio,
        "intraday_gap_status",
        lambda **_kwargs: {"ok": True, "missing_count": 0},
    )
    monkeypatch.setattr(
        auto_portfolio,
        "build_conditional_replay_receipt",
        lambda **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr(auto_portfolio, "build_decision_review", lambda *_args: {})
    monkeypatch.setattr(auto_portfolio, "build_yesterday_holding_review", lambda *_args: {})
    monkeypatch.setattr(auto_portfolio, "_enrich_positions_with_theme_map", lambda rows: list(rows))

    result = auto_portfolio._run_conditional_auto_paper_update(
        initial_cash=10_000.0,
        quantity_per_stock=100,
        strategy="all",
        intraday_source="1m",
        allow_daily_approx=False,
        dry_run=True,
        auto_strategy_path=None,
        auto_strategy_payload={
            "ok": True,
            "generated_at": f"{TRADE_DATE}T20:00:00+08:00",
            "standard_candidates": [],
            "long_candidates": [],
            "board_candidates": [],
        },
        plan_date=TRADE_DATE,
        trade_date="2026-08-12",
        lookback_days=250,
        allow_same_day_rerun=False,
        risk_gate_shadow_enabled=False,
        risk_gate_enforced=False,
        due_plan_payload=due_plan,
        formal_candidate_batch_path=None,
        formal_candidate_status="missing",
    )

    assert call_order == ["enrich", "simulate"]
    assert captured_simulation[0]["symbols"][0]["candidate_envelope"] == {"fixture": True}
    assert result["due_plan_enrichment"]["enriched"] == 1


def test_legacy_due_plan_rebinds_structurally_valid_envelope_from_wrong_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wrong = _candidate_envelope(tmp_path, "600143")
    row = _legacy_row("600000")
    row["candidate_envelope"] = wrong
    batch = {
        **_formal_batch("600000"),
        "candidates": [_formal_candidate("600000")],
        "candidate_count": 1,
    }
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "passed",
            "batch": copy.deepcopy(batch),
            "path": str(tmp_path / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"),
            "reason_codes": [],
        },
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        _legacy_due_plan([row]),
    )

    envelope = result["plan"]["symbols"][0]["candidate_envelope"]
    assert envelope["candidate"]["symbol"] == "600000"
    assert result["existing_valid_envelope"] == 0
    assert result["rebound_existing_envelope"] == 1


def test_legacy_due_plan_rejects_row_trade_date_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    row = _legacy_row("600143")
    row["trade_date"] = "2026-08-12"
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "passed",
            "batch": _formal_batch("600143"),
            "path": str(tmp_path / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"),
            "reason_codes": [],
        },
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        _legacy_due_plan([row]),
    )

    assert result["plan"]["symbols"][0].get("candidate_envelope") is None
    assert "legacy_candidate_trade_date_mismatch" in result["reason_codes"]
    assert result["rejected_mismatch"] == 1


def test_legacy_due_plan_unknown_non_exit_candidate_is_rejected_not_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        candidate_refresh,
        "load_formal_candidate_batch",
        lambda _root, *, target_date: {
            "status": "passed",
            "batch": _formal_batch("600143"),
            "path": str(tmp_path / "results" / "candidate_refresh" / "daily" / f"{target_date}.json"),
            "reason_codes": [],
        },
    )

    result = candidate_refresh.enrich_legacy_due_plan_candidate_envelopes(
        tmp_path,
        _legacy_due_plan([_legacy_row("600999")]),
    )

    assert result["status"] == "completed_with_rejections"
    assert result["unchanged_unmatched"] == 0
    assert result["rejected_mismatch"] == 1
    assert "legacy_candidate_not_in_formal_batch" in result["reason_codes"]
