"""A2 contract: data health must report independent scopes, never one date.

Workflow A2 of the 8000+8001 product closure requires four *independent*
freshness scopes:

1. ``full_universe``          — every local daily bar under ``data/daily``
2. ``research_pool``          — the symbols the 8000 desk actually studies
3. ``ai_trader``              — AI paper positions, committed intents, candidates
4. ``minute_and_scheduler``   — minute coverage plus real scheduler state

The defect these tests pin down is real: ``quantdesk.data_health_dashboard``
``_daily_card()`` is labelled 低价观察池 but derives its verdict from a single
``latest`` / ``max_local`` date, so a stale full universe and a fresh research
pool cannot be told apart. On 2026-08-28 that is exactly the live situation —
4,625 full-universe files stopped at 08-21 while 910 of 914 pool symbols had
reached 08-28 — and a single ``latest_date`` cannot state both truths at once.

These tests are pure: they pass explicit observations in and assert on the
returned contract. No file, network, ledger, plan or LLM access happens here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_scope_health import (  # noqa: E402
    SCOPE_AI_TRADER,
    SCOPE_FULL_UNIVERSE,
    SCOPE_MINUTE_SCHEDULER,
    SCOPE_RESEARCH_POOL,
    ScopeObservation,
    build_scope_health,
    scheduler_scope_status,
)


TARGET = "2026-08-28"


def _observation(
    scope: str,
    *,
    total: int,
    covered: int,
    stale: int = 0,
    missing: int = 0,
    latest: str = TARGET,
) -> ScopeObservation:
    return ScopeObservation(
        scope=scope,
        total_symbols=total,
        covered_symbols=covered,
        stale_symbols=stale,
        missing_symbols=missing,
        latest_date=latest,
        target_date=TARGET,
    )


def _live_20260828_observations() -> list[ScopeObservation]:
    """The real 2026-08-28 situation: stale full universe, fresh pool and AI."""

    return [
        # 4,625 of 5,886 full-universe files still stopped at 2026-08-21.
        _observation(
            SCOPE_FULL_UNIVERSE,
            total=5886,
            covered=1261,
            stale=4625,
            latest="2026-08-21",
        ),
        # 910 of 914 research-pool symbols reached the target day; 4 failed.
        _observation(
            SCOPE_RESEARCH_POOL,
            total=914,
            covered=910,
            missing=4,
            latest=TARGET,
        ),
        # AI positions and its committed intent are all covered.
        _observation(SCOPE_AI_TRADER, total=5, covered=5, latest=TARGET),
    ]


def test_stale_full_universe_and_fresh_pool_are_reported_side_by_side() -> None:
    """Would catch a single latest_date hiding either the stale or the fresh truth."""

    health = build_scope_health(_live_20260828_observations(), target_date=TARGET)
    scopes = health["scopes"]

    # Both truths must survive in the same payload.
    assert scopes[SCOPE_FULL_UNIVERSE]["as_of"] == "2026-08-21"
    assert scopes[SCOPE_RESEARCH_POOL]["as_of"] == TARGET
    assert scopes[SCOPE_AI_TRADER]["as_of"] == TARGET

    # The stale scope must not be presented as healthy...
    assert scopes[SCOPE_FULL_UNIVERSE]["status"] != "ok"
    # ...and must not drag the fresh AI scope down with it.
    assert scopes[SCOPE_AI_TRADER]["status"] == "ok"


def test_no_single_latest_date_field_can_stand_in_for_every_scope() -> None:
    """Would catch a regression that reintroduces one global freshness value."""

    health = build_scope_health(_live_20260828_observations(), target_date=TARGET)

    # A flat top-level latest_date would let a caller show one date for all
    # scopes again; the contract forbids it.
    assert "latest_date" not in health
    assert "local_daily_latest_date" not in health

    observed = {scope["as_of"] for scope in health["scopes"].values()}
    assert len(observed) > 1, "distinct scope dates collapsed into one value"


def test_every_scope_reports_its_own_counts_and_reason_code() -> None:
    """Would catch a scope shipping a verdict with no auditable evidence."""

    health = build_scope_health(_live_20260828_observations(), target_date=TARGET)

    for name, scope in health["scopes"].items():
        assert scope["scope"] == name
        assert scope["status"] in {"ok", "warning", "degraded", "blocked", "unknown"}
        assert scope["reason_code"], f"{name} has no reason_code"
        assert scope["target_date"] == TARGET
        for field in ("total_symbols", "covered_symbols", "stale_symbols", "missing_symbols"):
            assert isinstance(scope[field], int)


def test_missing_symbols_block_a_scope_and_stale_only_degrades_it() -> None:
    """Would catch missing data being softened to the same level as lag."""

    blocked = build_scope_health(
        [_observation(SCOPE_RESEARCH_POOL, total=10, covered=5, missing=5)],
        target_date=TARGET,
    )["scopes"][SCOPE_RESEARCH_POOL]
    degraded = build_scope_health(
        [_observation(SCOPE_RESEARCH_POOL, total=10, covered=8, stale=2)],
        target_date=TARGET,
    )["scopes"][SCOPE_RESEARCH_POOL]

    assert blocked["status"] == "blocked"
    assert degraded["status"] in {"warning", "degraded"}
    assert blocked["reason_code"] != degraded["reason_code"]


def test_absent_scope_is_unknown_rather_than_silently_ok() -> None:
    """Would catch an unobserved scope defaulting to a healthy verdict."""

    health = build_scope_health([], target_date=TARGET)

    for name in (SCOPE_FULL_UNIVERSE, SCOPE_RESEARCH_POOL, SCOPE_AI_TRADER):
        scope = health["scopes"][name]
        assert scope["status"] == "unknown"
        assert scope["reason_code"] == "not_observed"
        assert scope["as_of"] == ""


def test_overall_status_takes_the_worst_scope_without_erasing_the_others() -> None:
    """Would catch an aggregate that reports green while a scope is blocked."""

    health = build_scope_health(
        [
            _observation(SCOPE_FULL_UNIVERSE, total=10, covered=10),
            _observation(SCOPE_RESEARCH_POOL, total=10, covered=5, missing=5),
            _observation(SCOPE_AI_TRADER, total=3, covered=3),
        ],
        target_date=TARGET,
    )

    assert health["overall_status"] == "blocked"
    assert health["scopes"][SCOPE_FULL_UNIVERSE]["status"] == "ok"
    assert health["scopes"][SCOPE_AI_TRADER]["status"] == "ok"


def test_scheduler_failure_is_never_rendered_as_running() -> None:
    """Would catch a real scheduled-task failure displayed as a healthy run."""

    failed = scheduler_scope_status(
        probe={"available": True, "state": "last_run_failed", "last_exit_code": 4},
        snapshot={"status": "running", "as_of": "2026-08-27"},
        target_date=TARGET,
    )

    assert failed["status"] in {"degraded", "blocked"}
    assert failed["reason_code"] != "running"
    assert "running" not in str(failed.get("detail", "")).lower()
    assert failed["evidence_source"] == "live_probe"


def test_stale_snapshot_cannot_impersonate_a_live_scheduler_probe() -> None:
    """Would catch an old snapshot being presented as current scheduler truth."""

    probe_unavailable = scheduler_scope_status(
        probe={"available": False},
        snapshot={"status": "ok", "as_of": "2026-08-20"},
        target_date=TARGET,
    )

    assert probe_unavailable["status"] in {"unknown", "degraded"}
    assert probe_unavailable["evidence_source"] == "stale_snapshot"
    assert probe_unavailable["as_of"] == "2026-08-20"


def test_minute_scope_distinguishes_partial_provisional_and_missing() -> None:
    """Would catch provisional minute data being treated as complete."""

    statuses = {}
    for coverage_state in ("complete", "partial", "provisional", "missing"):
        health = build_scope_health(
            [
                ScopeObservation(
                    scope=SCOPE_MINUTE_SCHEDULER,
                    total_symbols=10,
                    covered_symbols=10 if coverage_state == "complete" else 4,
                    stale_symbols=0,
                    missing_symbols=0 if coverage_state != "missing" else 10,
                    latest_date=TARGET,
                    target_date=TARGET,
                    coverage_state=coverage_state,
                )
            ],
            target_date=TARGET,
        )
        statuses[coverage_state] = health["scopes"][SCOPE_MINUTE_SCHEDULER]

    assert statuses["complete"]["status"] == "ok"
    assert statuses["partial"]["status"] in {"warning", "degraded"}
    assert statuses["provisional"]["status"] in {"warning", "degraded"}
    assert statuses["missing"]["status"] == "blocked"

    # Provisional must be explicitly marked so no consumer treats it as formal.
    assert statuses["provisional"]["coverage_state"] == "provisional"
    assert statuses["provisional"]["formal_use_allowed"] is False
    assert statuses["complete"]["formal_use_allowed"] is True


def test_scheduler_failure_does_not_erase_provisional_minute_evidence() -> None:
    """Would catch scheduler overlay replacing, rather than combining, minute facts."""

    health = build_scope_health(
        [
            ScopeObservation(
                scope=SCOPE_MINUTE_SCHEDULER,
                total_symbols=10,
                covered_symbols=4,
                latest_date=TARGET,
                target_date=TARGET,
                coverage_state="provisional",
            )
        ],
        target_date=TARGET,
        scheduler={
            "available": True,
            "state": "last_run_failed",
            "last_exit_code": 4,
            "as_of": TARGET,
        },
    )
    scope = health["scopes"][SCOPE_MINUTE_SCHEDULER]

    assert scope["coverage_state"] == "provisional"
    assert scope["total_symbols"] == 10
    assert scope["covered_symbols"] == 4
    assert scope["scheduler_state"] == "last_run_failed"
    assert scope["formal_use_allowed"] is False


# ---------------------------------------------------------------------------
# Integration: the 8000 dashboard must actually expose the four scopes.
# ---------------------------------------------------------------------------


def test_dashboard_payload_exposes_independent_scope_health() -> None:
    """Would catch the four scopes never reaching the real 8000 payload."""

    from quantdesk import data_health_dashboard as dhd

    chain = {
        "target_date": TARGET,
        "status": "blocked",
        "coverage": {
            "daily": {"requested_count": 5886, "complete_session_count": 1261, "stale_count": 4625},
            "current_analysis_pool": {
                "requested_count": 914,
                "complete_session_count": 910,
                "missing_count": 4,
                "status": "passed",
            },
            "critical": {"requested_count": 20, "complete_session_count": 20, "status": "passed"},
        },
    }

    block = dhd._build_scope_health_block(
        chain,
        formal_chain_status={"pool_status": "full_pool"},
        scheduler={"status": "last_run_failed"},
    )

    scopes = block["scopes"]
    assert SCOPE_FULL_UNIVERSE in scopes
    assert SCOPE_RESEARCH_POOL in scopes
    assert SCOPE_AI_TRADER in scopes
    assert SCOPE_MINUTE_SCHEDULER in scopes

    # The stale full tree must not be reported as healthy...
    assert scopes[SCOPE_FULL_UNIVERSE]["status"] != "ok"
    # ...while the pool that did reach the target day says so on its own.
    assert scopes[SCOPE_RESEARCH_POOL]["as_of"] == TARGET
    # And no flat field may re-collapse them.
    assert "latest_date" not in block


def test_dashboard_maps_full_universe_and_research_pool_from_distinct_evidence() -> None:
    """Would catch the 914-pool daily block being mislabeled as full universe."""

    from quantdesk import data_health_dashboard as dhd

    chain = {
        "target_date": TARGET,
        "coverage": {
            # This is the *current research pool* daily result, not a full-A audit.
            "daily": {
                "requested_count": 914,
                "fresh_count": 910,
                "stale_count": 4,
                "failed_count": 4,
            },
            "critical": {
                "requested_count": 20,
                "complete_session_count": 20,
                "status": "passed",
            },
        },
    }
    full_universe = _observation(
        SCOPE_FULL_UNIVERSE,
        total=5549,
        covered=5544,
        stale=4634,
        missing=5,
        latest="2026-08-17",
    )

    block = dhd._build_scope_health_block(
        chain,
        full_universe_observation=full_universe,
        scheduler={
            "available": True,
            "state": "last_run_failed",
            "last_exit_code": 4,
            "as_of": TARGET,
        },
    )
    scopes = block["scopes"]

    assert scopes[SCOPE_FULL_UNIVERSE]["total_symbols"] == 5549
    assert scopes[SCOPE_FULL_UNIVERSE]["as_of"] == "2026-08-17"
    assert scopes[SCOPE_RESEARCH_POOL]["total_symbols"] == 914
    assert scopes[SCOPE_RESEARCH_POOL]["covered_symbols"] == 910
    assert scopes[SCOPE_RESEARCH_POOL]["missing_symbols"] == 4
    assert scopes[SCOPE_RESEARCH_POOL]["as_of"] == TARGET
    assert scopes[SCOPE_MINUTE_SCHEDULER]["evidence_source"] == "live_probe"
    assert scopes[SCOPE_MINUTE_SCHEDULER]["scheduler_state"] == "last_run_failed"


def test_cached_dashboard_reuses_v2_full_scope_and_overlays_live_scheduler_without_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Would catch GET relabeling a persisted full-A audit or mutating its snapshot."""

    import json

    from quantdesk import data_health_dashboard as dhd

    cached_full = {
        "scope": SCOPE_FULL_UNIVERSE,
        "label": "全量当前名册日线",
        "status": "blocked",
        "reason_code": "coverage_incomplete_missing",
        "as_of": "2026-08-21",
        "target_date": TARGET,
        "total_symbols": 5549,
        "covered_symbols": 5544,
        "stale_symbols": 4634,
        "missing_symbols": 5,
        "coverage_state": "complete",
        "formal_use_allowed": False,
        "symbols_sample": ["301655"],
        "detail": "persisted full-A audit",
    }
    snapshot = tmp_path / "data_health.json"
    snapshot.write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": "2026-08-28T20:16:31+08:00",
                "health_level": "yellow",
                "daily_data_scheduler": {"status": "healthy"},
                "scope_health": {
                    "schema_version": "a2_scope_health_v2",
                    "target_date": TARGET,
                    "scopes": {SCOPE_FULL_UNIVERSE: cached_full},
                },
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
        lambda _target: _observation(SCOPE_AI_TRADER, total=1, covered=1),
    )
    chain = {
        "target_date": TARGET,
        "status": "blocked",
        "coverage": {
            "daily": {
                "requested_count": 914,
                "fresh_count": 910,
                "stale_count": 4,
                "failed_count": 4,
            },
            "critical": {
                "requested_count": 1,
                "complete_session_count": 1,
                "status": "passed",
            },
        },
    }

    payload = dhd.build_cached_data_health_dashboard(
        snapshot_path=snapshot,
        daily_data_chain_payload=chain,
        evidence_freshness_payload={"status": "passed", "metrics_trustworthy": True},
    )

    scopes = payload["scope_health"]["scopes"]
    assert scopes[SCOPE_FULL_UNIVERSE]["total_symbols"] == 5549
    assert scopes[SCOPE_FULL_UNIVERSE]["as_of"] == "2026-08-21"
    assert scopes[SCOPE_RESEARCH_POOL]["covered_symbols"] == 910
    assert scopes[SCOPE_MINUTE_SCHEDULER]["evidence_source"] == "live_probe"
    assert scopes[SCOPE_MINUTE_SCHEDULER]["scheduler_state"] == "last_run_failed"
    assert snapshot.read_bytes() == before


def test_8000_data_health_ui_renders_each_scope_without_a_single_latest_date() -> None:
    """Would catch A2 being present in JSON but invisible to the user."""

    script = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
        encoding="utf-8"
    )

    assert "scope_health" in script
    assert "范围化数据状态" in script
    assert "scope.as_of" in script
    assert "scope.target_date" in script


def test_scope_block_degrades_instead_of_breaking_the_readonly_route() -> None:
    """Would catch a scope-health error turning the dashboard GET into a 500."""

    from quantdesk import data_health_dashboard as dhd

    block = dhd._build_scope_health_block(None)

    assert isinstance(block, dict)
    assert block["scopes"][SCOPE_FULL_UNIVERSE]["status"] == "unknown"
    assert block["scopes"][SCOPE_FULL_UNIVERSE]["reason_code"] == "not_observed"
