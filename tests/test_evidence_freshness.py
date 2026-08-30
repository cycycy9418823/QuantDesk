from __future__ import annotations

import csv
import json
from pathlib import Path

from quantdesk.data_health_dashboard import (
    build_cached_data_health_dashboard,
    build_data_health_dashboard,
)
from quantdesk.evidence_freshness import build_evidence_freshness

AS_OF = "2026-08-19"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_tracking_csv(path: Path, *, date_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["entry_date", "signal_date", "hold_20d_ret"])
        writer.writeheader()
        writer.writerow({"entry_date": date_text, "signal_date": date_text, "hold_20d_ret": "0.1"})


def _write_consistent_evidence(root: Path, *, date_text: str = AS_OF) -> None:
    _write_json(
        root / "results/candidate_tracking/latest.json",
        {
            "status": "completed",
            "target_date": date_text,
            "generated_at": f"{date_text}T18:00:00+08:00",
            "entries": [
                {"symbol": "600000", "horizons": {"5d": {"status": "mature", "return": 0.1}}}
            ],
        },
    )
    _write_tracking_csv(root / "data/tracking/candidate_ledger.csv", date_text=date_text)
    _write_tracking_csv(root / "data/tracking/candidate_performance.csv", date_text=date_text)
    _write_json(
        root / "data/tracking/tracking_summary.json",
        {"status": "completed", "generated_at": f"{date_text}T18:05:00+08:00"},
    )
    _write_json(
        root / "state/auto_paper_portfolio.json",
        {
            "updated_at": f"{date_text}T18:10:00+08:00",
            "cash": 100.0,
            "market_value": 20.0,
            "unrealized_pnl": 2.0,
            "realized_pnl": 3.0,
            "total_equity": 120.0,
            "positions": [{"symbol": "600000", "market_value": 20.0, "unrealized_pnl": 2.0}],
            "closed_positions": [{"symbol": "600001", "realized_pnl": 3.0}],
        },
    )
    _write_json(
        root / "results/auto_paper_portfolio_latest.json",
        {
            "generated_at": f"{date_text}T18:10:00+08:00",
            "status": "completed",
            "summary": {
                "cash": 100.0,
                "market_value": 20.0,
                "unrealized_pnl": 2.0,
                "realized_pnl": 3.0,
                "total_equity": 120.0,
            },
            "positions": [{"symbol": "600000", "market_value": 20.0, "unrealized_pnl": 2.0}],
            "closed_positions": [{"symbol": "600001", "realized_pnl": 3.0}],
        },
    )


def test_evidence_freshness_is_read_only_and_reports_fresh_required_artifacts(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["status"] == "fresh"
    assert payload["metrics_trustworthy"] is True
    assert payload["paper_account"]["status"] == "consistent"
    assert payload["artifacts"]["candidate_performance"]["status"] == "fresh"
    assert payload["artifacts"]["candidate_performance"]["observed_date"] == AS_OF
    assert not (tmp_path / "results/evidence_freshness_latest.json").exists()


def test_stale_or_missing_audit_artifacts_do_not_change_market_freshness_semantics(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path, date_text="2026-08-10")
    (tmp_path / "results/candidate_tracking/latest.json").unlink()

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["status"] == "warning"
    assert payload["metrics_trustworthy"] is False
    assert payload["artifacts"]["formal_candidate_tracking"]["status"] == "missing"
    assert payload["artifacts"]["candidate_performance"]["status"] == "stale"
    assert "audit_evidence_stale_or_missing" in payload["reason_codes"]
    assert "market_data_freshness" not in payload


def test_paper_account_drift_is_explicit_and_not_silently_repaired(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)
    state_path = tmp_path / "state/auto_paper_portfolio.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["market_value"] = 999.0
    _write_json(state_path, state)

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["status"] == "failed"
    assert payload["metrics_trustworthy"] is False
    assert payload["paper_account"]["status"] == "inconsistent"
    assert "market_value" in payload["paper_account"]["drift_fields"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["market_value"] == 999.0


def test_malformed_paper_account_state_is_invalid_and_metrics_are_untrustworthy(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)
    state_path = tmp_path / "state/auto_paper_portfolio.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["positions"][0]["market_value"] = "not-a-number"
    _write_json(state_path, state)

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["status"] == "failed"
    assert payload["metrics_trustworthy"] is False
    assert payload["paper_account"]["state_status"] == "invalid"
    assert "positions_row_0_market_value_not_numeric" in payload["paper_account"]["invalid_reason_codes"]
    assert "paper_account_invalid" in payload["reason_codes"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["positions"][0]["market_value"] == "not-a-number"


def test_malformed_paper_account_latest_is_not_trustworthy(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)
    latest_path = tmp_path / "results/auto_paper_portfolio_latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest["summary"]["market_value"] = 999.0
    latest["positions"] = ["not-a-position-row"]
    _write_json(latest_path, latest)

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["metrics_trustworthy"] is False
    assert payload["paper_account"]["latest_status"] == "invalid"
    assert "paper_account_latest_invalid" in payload["reason_codes"]


def test_paper_account_latest_summary_mismatch_is_not_trustworthy(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)
    latest_path = tmp_path / "results/auto_paper_portfolio_latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest["summary"]["market_value"] = 21.0
    latest["positions"][0]["market_value"] = 21.0
    _write_json(latest_path, latest)

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["metrics_trustworthy"] is False
    assert payload["paper_account"]["status"] == "inconsistent"
    assert "market_value" in payload["paper_account"]["state_latest_mismatch_fields"]
    assert "paper_account_latest_mismatch" in payload["reason_codes"]


def test_refresh_persisted_evidence_freshness_updates_dashboard_overlay(tmp_path: Path) -> None:
    from quantdesk.evidence_freshness import refresh_persisted_evidence_freshness

    dashboard = tmp_path / "results" / "data_health" / "data_health_dashboard_latest.json"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(
        json.dumps({"generated_at": "2026-07-01T00:00:00", "source_status": {}}),
        encoding="utf-8",
    )

    refreshed = refresh_persisted_evidence_freshness(
        tmp_path,
        as_of="2026-08-19",
        provenance={
            "trigger": "scheduled_daily_review",
            "authoritative_chain_run_id": "daily-data-chain-fixture",
        },
    )

    persisted = json.loads(dashboard.read_text(encoding="utf-8"))
    assert refreshed["status"] == "warning"
    assert persisted["evidence_freshness"] == refreshed
    assert persisted["source_status"]["audit_evidence"] == refreshed
    assert persisted["audit_evidence_provenance"]["trigger"] == "scheduled_daily_review"


def test_formal_tracking_freshness_does_not_imply_mature_metrics(tmp_path: Path) -> None:
    _write_consistent_evidence(tmp_path)
    formal_path = tmp_path / "results/candidate_tracking/latest.json"
    _write_json(
        formal_path,
        {
            "status": "completed_with_warnings",
            "target_date": AS_OF,
            "generated_at": f"{AS_OF}T18:00:00+08:00",
            "entries": [{"symbol": "600000", "horizons": {"5d": {"status": "pending"}}}],
            "horizons": ["5d", "20d"],
        },
    )

    payload = build_evidence_freshness(tmp_path, as_of=AS_OF, stale_after_days=3)

    assert payload["status"] == "fresh"
    assert payload["metrics_trustworthy"] is False
    formal = payload["artifacts"]["formal_candidate_tracking"]
    assert formal["metrics_available"] is False
    assert "formal_tracking_no_mature_samples" in formal["reason_codes"]
    assert "formal_tracking_no_mature_samples" in payload["reason_codes"]
    assert "尚无到期样本" in payload["message_for_ui"]


def test_market_health_and_audit_evidence_are_separate_surfaces() -> None:
    payload = build_data_health_dashboard(
        daily_price_payload={"target_date": AS_OF, "latest_local_date": AS_OF, "max_local_date": AS_OF, "total_symbols": 1},
        freshness_payload={"target_complete_trade_date": AS_OF, "local_daily_latest_date": AS_OF, "daily_feature_date": AS_OF},
        intraday_payload={"target_trade_date": AS_OF, "total_symbols": 0, "available_count": 0, "missing_count": 0},
        theme_payload={"exists": True, "total": 0, "mapped_count": 0, "unknown_count": 0},
        current_candidate_theme_payload={"candidate_count": 0},
        provider_capability_payload={"summary": {"ok_count": 0}},
        portfolio_payload={"exists": True, "positions": []},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={"long_candidates": []},
        theme_map={},
        daily_data_chain_payload={"status": "completed", "coverage": {"daily": {"status": "passed"}}},
        daily_scheduler_payload={"status": "healthy", "automatic_update_configured": True},
        evidence_freshness_payload={
            "status": "warning",
            "metrics_trustworthy": False,
            "reason_codes": ["audit_evidence_stale_or_missing"],
            "message_for_ui": "审计证据过期。",
        },
    )

    assert payload["cards"]["daily_data"]["status"] == "green"
    assert payload["evidence_freshness"]["status"] == "warning"
    assert payload["evidence_freshness"]["metrics_trustworthy"] is False
    assert "audit_evidence_stale_or_missing" in payload["health_degraded_reason_codes"]


def test_cached_dashboard_degrades_abandoned_chain_and_surfaces_evidence(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "ok": True,
                "health_level": "green",
                "generated_at": "2026-08-19T08:00:00",
                "summary_text": "基础摘要。",
            }
        ),
        encoding="utf-8",
    )
    payload = build_cached_data_health_dashboard(
        snapshot_path=snapshot,
        daily_data_chain_payload={"status": "abandoned", "run_id": "run-abandoned"},
        evidence_freshness_payload={
            "status": "warning",
            "metrics_trustworthy": False,
            "reason_codes": ["audit_evidence_stale_or_missing"],
        },
    )

    assert payload["health_level"] == "red"
    assert "latest_daily_chain_abandoned" in payload["health_degraded_reason_codes"]
    assert payload["evidence_freshness"]["status"] == "warning"
    assert "审计证据过期" in payload["summary_text"]


def test_read_only_dashboard_applies_evidence_overlay_without_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "quantdesk.data_health_dashboard.build_evidence_freshness",
        lambda _root: {
            "status": "failed",
            "metrics_trustworthy": False,
            "reason_codes": ["paper_account_latest_invalid"],
            "message_for_ui": "paper latest 无法校验。",
        },
    )
    payload = build_data_health_dashboard(
        daily_price_payload={"target_date": AS_OF, "latest_local_date": AS_OF, "max_local_date": AS_OF, "total_symbols": 1},
        freshness_payload={"target_complete_trade_date": AS_OF, "local_daily_latest_date": AS_OF, "daily_feature_date": AS_OF},
        intraday_payload={"target_trade_date": AS_OF, "total_symbols": 0, "available_count": 0, "missing_count": 0},
        theme_payload={"exists": True, "total": 0, "mapped_count": 0, "unknown_count": 0},
        current_candidate_theme_payload={"candidate_count": 0},
        provider_capability_payload={"summary": {"ok_count": 0}},
        portfolio_payload={"exists": True, "positions": []},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={"long_candidates": []},
        theme_map={},
        daily_data_chain_payload={"status": "completed", "coverage": {"daily": {"status": "passed"}}},
        write=False,
    )

    assert payload["health_level"] == "red"
    assert "paper_account_latest_invalid" in payload["health_degraded_reason_codes"]


def test_fresh_evidence_without_mature_metrics_degrades_only_audit_surface() -> None:
    payload = build_data_health_dashboard(
        daily_price_payload={"target_date": AS_OF, "latest_local_date": AS_OF, "max_local_date": AS_OF, "total_symbols": 1},
        freshness_payload={"target_complete_trade_date": AS_OF, "local_daily_latest_date": AS_OF, "daily_feature_date": AS_OF},
        intraday_payload={"target_trade_date": AS_OF, "total_symbols": 0, "available_count": 0, "missing_count": 0},
        theme_payload={"exists": True, "total": 0, "mapped_count": 0, "unknown_count": 0},
        current_candidate_theme_payload={"candidate_count": 0},
        provider_capability_payload={"summary": {"ok_count": 0}},
        portfolio_payload={"exists": True, "positions": []},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={"long_candidates": []},
        theme_map={},
        daily_data_chain_payload={"status": "completed", "coverage": {"daily": {"status": "passed"}}},
        daily_scheduler_payload={"status": "healthy"},
        evidence_freshness_payload={
            "status": "fresh",
            "metrics_available": False,
            "metrics_trustworthy": False,
            "reason_codes": ["formal_tracking_no_mature_samples"],
            "message_for_ui": "审计证据新鲜，但候选跟踪尚无到期样本。",
        },
    )

    assert payload["market_health_level"] == "yellow"
    assert payload["health_level"] == "yellow"
    assert "formal_tracking_no_mature_samples" in payload["health_degraded_reason_codes"]
    assert "审计证据新鲜" in payload["summary_text"]


def test_cli_reason_merge_keeps_evidence_reasons_for_failed_or_warning_status() -> None:
    from scripts.run_data_health_dashboard import _merge_evidence_reason_codes

    assert _merge_evidence_reason_codes(["component_failed"], ["audit_evidence_invalid"], "failed") == [
        "component_failed",
        "audit_evidence_invalid",
    ]
    assert _merge_evidence_reason_codes(["component_warning"], ["audit_evidence_invalid"], "warning") == [
        "component_warning",
        "audit_evidence_invalid",
    ]
