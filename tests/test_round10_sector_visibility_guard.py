"""Round 10 Part A③ — sector_history_archive visibility regression guards.

Context (acceptor forensics, locked as premise):
    §27.4's hypothesis — "Round 9 readiness/T0 integration made the sector
    detail section vanish, leaving only a warning" — was disproved.  The
    ``components.sector_history_archive`` detail node exists intact across every
    report face; Round 9 only *added* sibling nodes.  "Only a warning" was a
    read-location error (scanning top-level ``warnings[]`` without expanding the
    component node).

    Part A was therefore re-scoped to *visibility hardening*: every run now
    mirrors the sector node to an independent ``sector_history_archive_attempt.json``
    in the run directory, so any future "manifest node truncated/lost" regression
    has a redundant fallback and a guard test.

These tests pin the invariants that make that hardening real:
    1. manifest node exists with the full detail field set (both shapes),
    2. the independent mirror file exists and is field-for-field identical to
       the manifest node,
    3. the readiness/T0 sibling nodes never swallow the sector node,
    4. degraded runs keep BOTH the warning AND the detail node,
    5. everything runs in a temp root with no canonical/state writes.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


# --- canned sector payloads (decouple the guard from archive internals) ------

def _degraded_sector_payload() -> dict[str, object]:
    """The current real-world shape: degraded, nothing written."""

    return {
        "ok": True,
        "status": "degraded",
        "evidence_eligible": False,
        "written": False,
        "pit_eligible": False,
        "reason_codes": ["sector_membership_source_missing"],
        "degraded_reasons": ["sector_membership_source_missing"],
        "membership_count": 0,
        "membership_unknown_count": 0,
        "membership_non_target_observation_timestamp_count": 0,
        "aligned_membership_count": 0,
        "provenance": {"captured_at": "2026-07-22T18:00:00+08:00", "sources": []},
        "captured_at": "2026-07-22T18:00:00+08:00",
        "snapshot_hash": "",
        "snapshot_path": "",
        "canonical_write": False,
        "append_only": True,
    }


def _written_sector_payload() -> dict[str, object]:
    """The T0-birth shape: a real forward snapshot was written."""

    return {
        "ok": True,
        "status": "passed",
        "evidence_eligible": True,
        "written": True,
        "pit_eligible": True,
        "reason_codes": [],
        "degraded_reasons": [],
        "membership_count": 2,
        "membership_unknown_count": 0,
        "membership_non_target_observation_timestamp_count": 0,
        "aligned_membership_count": 2,
        "provenance": {
            "captured_at": "2026-07-22T15:30:00+08:00",
            "hash": "sha256:deadbeef",
            "cutoff": "2026-07-22",
            "sources": [{"cutoff": "2026-07-22T15:29:00"}],
        },
        "captured_at": "2026-07-22T15:30:00+08:00",
        "snapshot_hash": "sha256:deadbeef",
        "snapshot_path": "data/sector_history/2026-07-22/snapshot.json",
        "content_identity": "sha256:deadbeef",
        "canonical_write": False,
        "append_only": True,
    }


# --- full-chain scaffold (isolated temp root, no network, no canonical write) --

def _run_chain_with_sector_payload(
    tmp_path: Path,
    monkeypatch,
    sector_payload: dict[str, object],
    *,
    run_id: str,
) -> tuple[dict, Path]:
    from quantdesk import daily_data_chain as chain

    root = tmp_path / run_id
    universe_dir = root / "data" / "universe"
    universe_dir.mkdir(parents=True)
    sentinel = universe_dir / "canonical_sentinel.csv"
    sentinel.write_bytes(b"symbol,name\n600000,untouched\n")
    universe_before = {
        path.relative_to(universe_dir).as_posix(): path.read_bytes()
        for path in universe_dir.rglob("*")
        if path.is_file()
    }
    captured_at = "2026-07-22T18:00:00+08:00"

    monkeypatch.setattr(chain, "PROJECT_ROOT", root)
    monkeypatch.setattr(chain, "now_text", lambda: captured_at)
    # Canned sector payload: this guard exercises the chain's mirror-write and
    # integration ordering, not the archive internals (covered by round6 tests).
    monkeypatch.setattr(
        chain,
        "_archive_forward_sector_snapshot",
        lambda *args, **kwargs: dict(sector_payload),
    )
    monkeypatch.setattr(chain, "build_default_fetchers", lambda: {"provider": object()})
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "completed",
            "formal_eligible": True,
            "provider": "fixture",
            "quote_date": "2026-07-22",
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "skipped_test"},
    )
    monkeypatch.setattr(
        chain,
        "_update_current_state",
        lambda root, manifest: {"ok": True, "status": "skipped_test"},
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(chain, "resolve_symbols", lambda **kwargs: [{"symbol": "600000"}])
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
            "symbols_checked": 1,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "invalid_price_basis_symbols": [],
            "fresh_symbol_list_after": ["600000"],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
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
            "updated_symbol_list": list(symbols),
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
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
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "apply_audited_intraday_batch",
        lambda **kwargs: {"status": "completed", "applied": False, "reason_codes": []},
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
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "written": True,
            "plan_id": "sector-visibility-test-plan",
            "reason_codes": [],
        },
    )

    result = chain.run_daily_data_chain(
        project_root=root,
        target_date="2026-07-22",
        network=True,
        dry_run=False,
        run_id=run_id,
        batch_size=1,
        batch_workers=1,
        rebuild_features=False,
    )

    run_dir = root / "results" / "data_chain" / "runs" / run_id
    universe_after = {
        path.relative_to(universe_dir).as_posix(): path.read_bytes()
        for path in universe_dir.rglob("*")
        if path.is_file()
    }
    # Invariant: the sector visibility path never touches canonical universe bytes.
    assert universe_after == universe_before
    return result, run_dir


_DETAIL_CORE_FIELDS = {
    "status",
    "written",
    "pit_eligible",
    "evidence_eligible",
    "reason_codes",
    "degraded_reasons",
    "membership_count",
    "membership_unknown_count",
    "membership_non_target_observation_timestamp_count",
    "aligned_membership_count",
    "provenance",
    "captured_at",
    "snapshot_hash",
}


def _read_manifest_and_mirror(run_dir: Path) -> tuple[dict, dict]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    mirror_path = run_dir / "sector_history_archive_attempt.json"
    assert mirror_path.exists(), "independent sector mirror file must be written"
    mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    return manifest, mirror


# --- guards ------------------------------------------------------------------

def test_sector_refresh_custom_root_guard_survives_project_root_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An isolated chain root must never fall through to the production cache.

    The full-chain fixture replaces ``daily_data_chain.PROJECT_ROOT`` so every
    ordinary chain artifact lands below ``tmp_path``.  That replacement must
    not also redefine what counts as the *canonical* root for the default
    industry-board refresher: doing so would call the helper whose destination
    is the real ``data/cache/industry_board_latest.csv``.
    """

    from quantdesk import daily_data_chain as chain
    from scripts.server.helpers import market as market_helpers

    isolated_root = tmp_path / "isolated-root"
    provider_calls: list[bool] = []

    def forbidden_default_refresher() -> dict[str, object]:
        provider_calls.append(True)
        return {"ok": False}

    monkeypatch.setattr(chain, "PROJECT_ROOT", isolated_root)
    monkeypatch.setattr(
        market_helpers,
        "refresh_industry_board_cache",
        forbidden_default_refresher,
    )

    payload = chain._refresh_sector_snapshot(isolated_root, network=True)

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["sector_refresh_custom_root_not_supported"]
    assert provider_calls == []


def test_degraded_run_keeps_detail_node_warning_and_independent_mirror(
    tmp_path: Path, monkeypatch
) -> None:
    result, run_dir = _run_chain_with_sector_payload(
        tmp_path, monkeypatch, _degraded_sector_payload(), run_id="round10-degraded"
    )

    manifest, mirror = _read_manifest_and_mirror(run_dir)
    sector_node = manifest["components"]["sector_history_archive"]

    # (1) manifest detail node exists with the full detail field set.
    assert _DETAIL_CORE_FIELDS <= set(sector_node), (
        "degraded manifest sector node missing detail fields: "
        f"{_DETAIL_CORE_FIELDS - set(sector_node)}"
    )
    assert sector_node["status"] == "degraded"
    assert sector_node["written"] is False
    assert sector_node["reason_codes"] == ["sector_membership_source_missing"]

    # (2) independent mirror is field-for-field identical to the manifest node.
    assert mirror == sector_node

    # (4) degraded → BOTH the warning AND the detail node survive (warning does
    #     NOT replace the detail — the §27.4 read-location trap, pinned down).
    assert "sector_history_archive_degraded" in manifest["warnings"]
    assert "sector_history_archive_degraded" not in manifest["errors"]
    assert manifest["components"]["sector_history_archive"]  # node still present


def test_written_run_mirrors_full_detail_to_independent_file(
    tmp_path: Path, monkeypatch
) -> None:
    result, run_dir = _run_chain_with_sector_payload(
        tmp_path, monkeypatch, _written_sector_payload(), run_id="round10-written"
    )

    manifest, mirror = _read_manifest_and_mirror(run_dir)
    sector_node = manifest["components"]["sector_history_archive"]

    # (1) written shape detail fields present.
    assert _DETAIL_CORE_FIELDS <= set(sector_node)
    assert sector_node["status"] == "passed"
    assert sector_node["written"] is True
    assert sector_node["pit_eligible"] is True
    assert sector_node["snapshot_hash"] == "sha256:deadbeef"

    # (2) mirror identical to manifest node.
    assert mirror == sector_node

    # written (non-degraded) → no degraded warning added.
    assert "sector_history_archive_degraded" not in manifest["warnings"]


def test_readiness_and_t0_siblings_do_not_swallow_sector_node(
    tmp_path: Path, monkeypatch
) -> None:
    """Round 9 integration added forward_pit_readiness / t0_birth_evidence as
    sibling nodes.  Guard that they coexist with — never overwrite/truncate —
    the sector_history_archive detail node (the exact regression §27.4 feared)."""

    result, run_dir = _run_chain_with_sector_payload(
        tmp_path, monkeypatch, _degraded_sector_payload(), run_id="round10-siblings"
    )

    manifest, mirror = _read_manifest_and_mirror(run_dir)
    components = manifest["components"]

    # sector node is intact...
    sector_node = components["sector_history_archive"]
    assert _DETAIL_CORE_FIELDS <= set(sector_node)
    assert mirror == sector_node

    # ...alongside the Round 9 siblings (added, not substituted).
    for sibling in ("forward_pit_readiness", "t0_birth_evidence"):
        assert sibling in components, f"expected Round 9 sibling node {sibling!r}"
    # Distinct objects: a sibling never aliases/overwrites the sector node.
    assert components["forward_pit_readiness"] is not sector_node
    assert components["t0_birth_evidence"] is not sector_node
