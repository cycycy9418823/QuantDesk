"""A3 contract: proactive, de-duplicated data alerts with recovery semantics.

Workflow A3 requires a pure, testable local alert contract covering:

- scheduled-task failure / timeout / missed run / disabled task
- research-pool daily bars lagging or missing
- AI-relevant symbols whose daily or minute data is unavailable
- minute data that is ``partial`` / ``provisional`` / ``missing``
- recovery events, when a previously firing condition clears

Every alert must carry a stable ``alert_key``, ``severity``, ``reason_code``,
``scope``, ``as_of``, an affected-scope summary, plus dedup and recovery
semantics.

Two honesty rules from the task book are pinned here as tests:

1. Writing a local runtime file is **not** the same as notifying the user. The
   payload must not claim delivery it did not perform.
2. A stale *full universe* alone must not raise an AI-blocking alert, because
   the AI scope can be complete while the whole tree lags.

These tests are pure: alerts are derived from an in-memory scope-health payload.
No file, network, ledger or LLM access happens here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_alerts import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    build_data_alerts,
    diff_alert_states,
)
from quantdesk.data_scope_health import (  # noqa: E402
    SCOPE_AI_TRADER,
    SCOPE_FULL_UNIVERSE,
    SCOPE_MINUTE_SCHEDULER,
    SCOPE_RESEARCH_POOL,
    ScopeObservation,
    build_scope_health,
)

TARGET = "2026-08-28"


def _health(*observations: ScopeObservation, scheduler: dict | None = None) -> dict:
    return build_scope_health(list(observations), target_date=TARGET, scheduler=scheduler)


def _obs(scope: str, **kwargs) -> ScopeObservation:
    kwargs.setdefault("target_date", TARGET)
    kwargs.setdefault("latest_date", TARGET)
    return ScopeObservation(scope=scope, **kwargs)


def test_every_alert_carries_the_required_contract_fields() -> None:
    """Would catch an alert that cannot be de-duplicated or audited."""

    health = _health(
        _obs(SCOPE_RESEARCH_POOL, total_symbols=914, covered_symbols=910, missing_symbols=4),
        scheduler={"available": True, "state": "last_run_failed", "last_exit_code": 4},
    )
    alerts = build_data_alerts(health)

    assert alerts, "a blocked pool and a failed task must raise alerts"
    for alert in alerts:
        for field in ("alert_key", "severity", "reason_code", "scope", "as_of", "dedup_key"):
            assert alert.get(field) not in (None, ""), f"{field} missing from {alert}"
        assert alert["severity"] in {SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL}
        assert isinstance(alert["affected"], dict)
        assert "resolved" in alert


def test_alert_key_is_stable_across_repeated_evaluations() -> None:
    """Would catch duplicate alerts piling up for one unchanged condition."""

    health = _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=5, missing_symbols=5))

    first = build_data_alerts(health)
    second = build_data_alerts(health)

    assert [a["alert_key"] for a in first] == [a["alert_key"] for a in second]
    assert len({a["alert_key"] for a in first}) == len(first), "alert keys must be unique"


def test_missing_data_is_critical_while_lag_is_only_a_warning() -> None:
    """Would catch missing bars being reported at the same level as staleness."""

    missing = build_data_alerts(
        _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=5, missing_symbols=5))
    )
    lagging = build_data_alerts(
        _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=8, stale_symbols=2))
    )

    assert any(a["severity"] == SEVERITY_CRITICAL for a in missing)
    assert lagging and all(a["severity"] != SEVERITY_CRITICAL for a in lagging)
    assert {a["reason_code"] for a in missing} != {a["reason_code"] for a in lagging}


def test_scheduler_failure_timeout_and_disabled_each_raise_a_distinct_alert() -> None:
    """Would catch a task failure, a timeout and a disabled task being conflated."""

    reasons = set()
    for probe in (
        {"available": True, "state": "last_run_failed", "last_exit_code": 4},
        {"available": True, "state": "timeout"},
        {"available": True, "state": "disabled"},
        {"available": True, "state": "missed"},
    ):
        alerts = build_data_alerts(_health(scheduler=probe))
        scheduler_alerts = [a for a in alerts if a["scope"] == SCOPE_MINUTE_SCHEDULER]
        assert scheduler_alerts, f"probe {probe} raised no scheduler alert"
        reasons.update(a["reason_code"] for a in scheduler_alerts)

    assert len(reasons) >= 3, f"scheduler failure modes collapsed: {reasons}"


def test_scheduler_failure_is_reported_once_with_its_specific_reason() -> None:
    """Would catch a generic scope alert duplicating the scheduler failure alert."""

    alerts = build_data_alerts(
        _health(
            scheduler={
                "available": True,
                "state": "last_run_failed",
                "last_exit_code": 4,
            }
        )
    )
    scheduler_alerts = [alert for alert in alerts if alert["scope"] == SCOPE_MINUTE_SCHEDULER]

    assert [alert["reason_code"] for alert in scheduler_alerts] == ["scheduler_task_failed"]


def test_stale_full_universe_alone_does_not_raise_an_ai_blocking_alert() -> None:
    """Would catch a lagging full tree needlessly blocking the AI paper trader."""

    health = _health(
        _obs(
            SCOPE_FULL_UNIVERSE,
            total_symbols=5886,
            covered_symbols=1261,
            stale_symbols=4625,
            latest_date="2026-08-21",
        ),
        _obs(SCOPE_AI_TRADER, total_symbols=5, covered_symbols=5),
    )
    alerts = build_data_alerts(health)

    assert any(a["scope"] == SCOPE_FULL_UNIVERSE for a in alerts), "the lag must still be visible"
    ai_alerts = [a for a in alerts if a["scope"] == SCOPE_AI_TRADER]
    assert all(a["severity"] != SEVERITY_CRITICAL for a in ai_alerts)
    assert all(not a.get("blocks_ai_trader") for a in alerts if a["scope"] == SCOPE_FULL_UNIVERSE)


def test_unavailable_ai_scope_raises_an_explicitly_ai_blocking_alert() -> None:
    """Would catch broken AI-relevant data failing to block new commitments."""

    health = _health(_obs(SCOPE_AI_TRADER, total_symbols=5, covered_symbols=2, missing_symbols=3))
    alerts = [a for a in build_data_alerts(health) if a["scope"] == SCOPE_AI_TRADER]

    assert alerts
    assert any(a["blocks_ai_trader"] for a in alerts)
    assert any(a["severity"] == SEVERITY_CRITICAL for a in alerts)


def test_provisional_minute_data_is_flagged_as_non_formal() -> None:
    """Would catch provisional minute bars being accepted for formal use."""

    health = _health(
        _obs(
            SCOPE_MINUTE_SCHEDULER,
            total_symbols=10,
            covered_symbols=4,
            coverage_state="provisional",
        )
    )
    alerts = [a for a in build_data_alerts(health) if a["scope"] == SCOPE_MINUTE_SCHEDULER]

    assert alerts
    assert any("provisional" in a["reason_code"] for a in alerts)


def test_recovery_emits_a_resolved_event_for_the_same_alert_key() -> None:
    """Would catch a cleared condition silently vanishing with no recovery event."""

    broken = build_data_alerts(
        _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=5, missing_symbols=5))
    )
    healthy = build_data_alerts(_health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=10)))

    diff = diff_alert_states(previous=broken, current=healthy)

    assert diff["resolved"], "clearing a condition must emit a recovery event"
    recovered_keys = {a["alert_key"] for a in diff["resolved"]}
    assert recovered_keys & {a["alert_key"] for a in broken}
    for alert in diff["resolved"]:
        assert alert["resolved"] is True
        assert alert["severity"] == SEVERITY_INFO


def test_unchanged_condition_is_reported_as_ongoing_not_as_new() -> None:
    """Would catch a re-notification storm for one unchanged problem."""

    alerts = build_data_alerts(
        _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=5, missing_symbols=5))
    )

    diff = diff_alert_states(previous=alerts, current=alerts)

    assert diff["new"] == []
    assert diff["ongoing"], "an unchanged condition must be visible as ongoing"
    assert diff["resolved"] == []


def test_alert_payload_does_not_claim_delivery_it_did_not_perform() -> None:
    """Would catch a local file write being described as notifying the user."""

    health = _health(_obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=5, missing_symbols=5))
    diff = diff_alert_states(previous=[], current=build_data_alerts(health))

    assert diff["delivery"]["external_push"] is False
    assert diff["delivery"]["user_notified"] is False
    assert diff["delivery"]["channel"] == "local_runtime_only"


def test_healthy_scopes_raise_no_alerts_at_all() -> None:
    """Would catch alert noise on a genuinely healthy day."""

    health = _health(
        _obs(SCOPE_FULL_UNIVERSE, total_symbols=10, covered_symbols=10),
        _obs(SCOPE_RESEARCH_POOL, total_symbols=10, covered_symbols=10),
        _obs(SCOPE_AI_TRADER, total_symbols=3, covered_symbols=3),
        scheduler={"available": True, "state": "ok", "as_of": TARGET},
    )

    assert build_data_alerts(health) == []


def test_8000_readonly_dashboard_exposes_alerts_without_mutating_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Would catch A3 existing only as a pure helper or GET writing its state."""

    import json

    from quantdesk import data_health_dashboard as dhd

    snapshot = tmp_path / "data_health.json"
    snapshot.write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": "2026-08-28T20:16:31+08:00",
                "health_level": "yellow",
                "scope_health": {
                    "schema_version": "a2_scope_health_v2",
                    "target_date": TARGET,
                    "scopes": {
                        SCOPE_FULL_UNIVERSE: {
                            "scope": SCOPE_FULL_UNIVERSE,
                            "label": "全量当前名册日线",
                            "status": "degraded",
                            "reason_code": "behind_target_date",
                            "as_of": "2026-08-21",
                            "target_date": TARGET,
                            "total_symbols": 5549,
                            "covered_symbols": 5544,
                            "stale_symbols": 4634,
                            "missing_symbols": 5,
                            "coverage_state": "complete",
                            "formal_use_allowed": False,
                            "symbols_sample": [],
                            "detail": "persisted full-A state",
                        }
                    },
                },
                "data_alerts": {"current": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = snapshot.read_bytes()
    monkeypatch.setattr(
        dhd,
        "daily_data_scheduler_status",
        lambda **_kwargs: {
            "status": "last_run_failed",
            "last_task_result": 4,
            "generated_at": f"{TARGET}T20:20:00+08:00",
            "task_name": "QuantDesk Daily Data Chain",
        },
    )
    monkeypatch.setattr(
        dhd,
        "_ai_trader_scope_observation",
        lambda _target: _obs(SCOPE_AI_TRADER, total_symbols=1, covered_symbols=1),
    )
    payload = dhd.build_cached_data_health_dashboard(
        snapshot_path=snapshot,
        daily_data_chain_payload={
            "target_date": TARGET,
            "status": "blocked",
            "coverage": {
                "daily": {"requested_count": 1, "fresh_count": 1},
                "critical": {
                    "requested_count": 1,
                    "complete_session_count": 1,
                    "status": "passed",
                },
            },
        },
        evidence_freshness_payload={"status": "passed", "metrics_trustworthy": True},
    )

    alerts = payload["data_alerts"]
    assert any(alert["reason_code"] == "scheduler_task_failed" for alert in alerts["current"])
    assert alerts["delivery"]["external_push"] is False
    assert alerts["delivery"]["user_notified"] is False
    assert alerts["read_only"] is True
    assert snapshot.read_bytes() == before


def test_data_health_ui_has_a_readonly_alert_summary_contract() -> None:
    """Would catch the API exposing A3 alerts while 8000 never renders them."""

    script = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
        encoding="utf-8"
    )

    assert "data_alerts" in script
    assert "数据告警" in script
    assert "仅本地告警" in script


def test_snapshot_writer_persists_alert_baseline_and_emits_recovery_on_next_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Would catch de-dup/recovery existing only within one in-memory request."""

    import json

    from quantdesk import data_health_dashboard as dhd

    full_scope = _obs(SCOPE_FULL_UNIVERSE, total_symbols=1, covered_symbols=1)
    ai_scope = _obs(SCOPE_AI_TRADER, total_symbols=1, covered_symbols=1)
    monkeypatch.setattr(dhd, "_ai_trader_scope_observation", lambda _target: ai_scope)
    output = tmp_path / "data_health.json"
    base_kwargs = {
        "daily_price_payload": {
            "total_symbols": 1,
            "stale_count": 0,
            "missing_count": 0,
            "target_date": TARGET,
            "latest_local_date": TARGET,
            "max_local_date": TARGET,
        },
        "freshness_payload": {
            "target_complete_trade_date": TARGET,
            "local_daily_latest_date": TARGET,
        },
        "intraday_payload": {"total_symbols": 0, "missing_symbols": []},
        "theme_payload": {},
        "current_candidate_theme_payload": {},
        "provider_capability_payload": {},
        "portfolio_payload": {},
        "daily_workflow_payload": {},
        "real_sample_payload": {},
        "self_validation_payload": {},
        "strategy_payload": {},
        "extended_gates_payload": {},
        "daily_scheduler_payload": {
            "status": "healthy",
            "last_task_result": 0,
            "generated_at": f"{TARGET}T20:20:00+08:00",
        },
        "evidence_freshness_payload": {"status": "passed", "metrics_trustworthy": True},
        "full_universe_observation": full_scope,
        "output_path": output,
        "write": True,
    }
    broken_chain = {
        "target_date": TARGET,
        "coverage": {
            "daily": {
                "requested_count": 1,
                "fresh_count": 0,
                "failed_count": 1,
            },
            "critical": {
                "requested_count": 1,
                "complete_session_count": 1,
                "status": "passed",
            },
        },
    }
    first = dhd.build_data_health_dashboard(
        **base_kwargs,
        daily_data_chain_payload=broken_chain,
    )
    assert any(alert["scope"] == SCOPE_RESEARCH_POOL for alert in first["data_alerts"]["new"])

    healthy_chain = {
        "target_date": TARGET,
        "coverage": {
            "daily": {"requested_count": 1, "fresh_count": 1},
            "critical": {
                "requested_count": 1,
                "complete_session_count": 1,
                "status": "passed",
            },
        },
    }
    second = dhd.build_data_health_dashboard(
        **base_kwargs,
        daily_data_chain_payload=healthy_chain,
    )

    assert any(alert["scope"] == SCOPE_RESEARCH_POOL for alert in second["data_alerts"]["resolved"])
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["data_alerts"]["delivery"]["external_push"] is False
