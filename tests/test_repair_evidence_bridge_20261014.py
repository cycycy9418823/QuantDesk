"""D1 bridge: this run's verified no-session evidence must reach the TOTAL
reconciliation, not only the session-expectation chain.

Before this repair ``daily_gap_recovery`` persisted its evidence as
``target_day_gap_recovery/session_<symbol>.json`` and the chain handed it to
``session_expectation``, while ``full_a_daily_reconciliation`` still called
``classify_universe_completeness`` with no run evidence at all - so the total
reconciliation fell back to the legacy ``data/metadata/suspension_evidence``
path.  One day therefore produced two incompatible verdicts: the chain knew a
symbol had no session, the reconciliation still counted it as a gap.

Every case below is a temp tree.  No real ``data/``, ``state/`` or
``results/`` tree is read, nothing reaches the network, and no provider is
called.  The strict contract under test is the existing
``quantdesk.daily_session_evidence`` validator - this repair reuses it instead
of inventing a looser suspension rule.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from quantdesk.daily_session_evidence import build_baostock_session_evidence
from quantdesk.full_a_daily_reconciliation import (
    build_reconciliation_partition,
    resolve_run_session_evidence,
)
from quantdesk.suspension_aware_completeness import (
    KNOWN_SIGNATURES,
    RUN_SESSION_SIGNATURE,
    classify_universe_completeness,
    load_run_session_evidence,
)

TARGET = "2026-10-14"
PREVIOUS = "2026-10-13"
CHAIN_RUN = "daily-data-chain-20261014T103846Z-91d929b826"


def _provider_code(symbol: str) -> str:
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz.{symbol}"
    return ""


def _session_evidence(
    symbol: str,
    day: str = TARGET,
    *,
    tradestatus: str = "0",
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """A payload produced by the real strict builder, never hand-rolled."""
    return build_baostock_session_evidence(
        symbol=symbol,
        provider_code=_provider_code(symbol),
        query_start_date=start or day,
        query_end_date=end or day,
        records=[{"trade_date": day, "tradestatus": tradestatus}],
        captured_at="2026-10-14T18:45:10",
    )


def _tree(tmp_path: Path, bars: dict[str, str]) -> Path:
    """Minimal project tree: universe roster + append-only daily bars."""
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "daily").mkdir(parents=True, exist_ok=True)
    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    with universe.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name", "source", "updated_at"])
        for symbol in bars:
            writer.writerow([symbol, f"Name{symbol}", "test", TARGET])
    for symbol, last_date in bars.items():
        if not last_date:
            continue
        path = tmp_path / "data" / "daily" / f"{symbol}.csv"
        path.write_text(
            "date,symbol,open,high,low,close,volume,amount\n"
            f"{PREVIOUS},{symbol},1.0,1.1,0.9,1.0,1000,1000\n"
            f"{last_date},{symbol},1.0,1.1,0.9,1.0,1000,1000\n",
            encoding="utf-8",
        )
    return tmp_path


def _write_run_evidence_dir(root: Path, evidence: dict[str, dict], *, run_id: str = CHAIN_RUN) -> Path:
    directory = root / "results" / "data_chain" / "runs" / run_id / "target_day_gap_recovery"
    directory.mkdir(parents=True, exist_ok=True)
    for symbol, payload in evidence.items():
        (directory / f"session_{symbol}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return directory


def _write_chain_pointer(
    root: Path,
    evidence: dict[str, dict],
    *,
    target: str = TARGET,
    run_id: str = CHAIN_RUN,
) -> None:
    """The single published pointer the reconciliation binds itself to."""
    path = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "daily_data_chain.v1",
                "run_id": run_id,
                "target_date": target,
                "authoritative": True,
                "status": "completed",
                "components": {
                    "target_day_gap_recovery": {
                        "schema_version": "target_day_gap_recovery.v1",
                        "target_date": target,
                        "status": "partial",
                        "appended_symbols": [],
                        "verified_no_session_symbols": sorted(evidence),
                        "session_evidence": evidence,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _legacy_evidence(root: Path, verified: dict[str, str], *, target: str = TARGET) -> None:
    path = root / "data" / "metadata" / "suspension_evidence" / f"{target.replace('-', '')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "target_date": target,
                "source": "sina_snapshot",
                "verified_exclusions": {
                    symbol: {"signature": signature, "close": "0.000", "volume": ""}
                    for symbol, signature in verified.items()
                },
                "flagged_for_review": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------- minimal reproduction


def test_run_evidence_reaches_the_total_reconciliation(tmp_path: Path) -> None:
    """THE repro: 000016 has no bar but a verified no-session record for today.

    Before the repair the partition kept 000016 pending forever even though the
    same run had already proved the day had no session.
    """
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")})

    partition = build_reconciliation_partition(root, target_date=TARGET)

    assert partition["verified_excluded_symbols"] == ["000016"]
    assert partition["pending_symbols"] == []
    assert partition["completeness"]["complete"] is True
    assert partition["completeness"]["gap_count"] == 0


def test_run_evidence_dir_is_also_accepted_directly(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016")})

    evidence = resolve_run_session_evidence(root, target_date=TARGET, run_evidence_dir=directory)
    assert evidence.excluded_symbols == frozenset({"000016"})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence=evidence)
    assert result["verified_excluded_symbols"] == ["000016"]
    assert result["complete"] is True


def test_verified_exclusion_is_not_counted_as_a_bar(tmp_path: Path) -> None:
    """A verified no-session day must not be presented as a recovered bar."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["with_bar_count"] == 1  # only 000001 physically carries a target bar
    assert result["verified_excluded_count"] == 1
    assert result["gap_count"] == 0
    assert "000016" not in result["gap_symbols"]
    assert result["run_evidence_present"] is True
    assert result["run_verified_excluded_count"] == 1


def test_unresolved_symbol_stays_pending_not_laundered(tmp_path: Path) -> None:
    """688496-style: attempted, no verified evidence -> still pending, still a gap."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS, "688496": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")})

    partition = build_reconciliation_partition(root, target_date=TARGET)

    assert partition["verified_excluded_symbols"] == ["000016"]
    assert partition["pending_symbols"] == ["688496"]
    assert partition["completeness"]["complete"] is False


def test_only_the_named_run_directory_is_read(tmp_path: Path) -> None:
    """No history scan: a decoy session file in another run must be ignored."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS, "000002": PREVIOUS})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016")})
    _write_run_evidence_dir(
        root,
        {"000002": _session_evidence("000002")},
        run_id="daily-data-chain-20261013T000000Z-deadbeef",
    )

    evidence = load_run_session_evidence(TARGET, run_evidence_dir=directory)

    assert evidence.excluded_symbols == frozenset({"000016"})


# ----------------------------------------------------------- rejected inputs


def test_wrong_date_evidence_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    stale = _session_evidence("000016", day=PREVIOUS, start=PREVIOUS, end=PREVIOUS)
    directory = _write_run_evidence_dir(root, {"000016": stale})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]
    assert any("run_session_evidence_rejected" in w for w in result["warnings"])


def test_future_date_evidence_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    future = _session_evidence("000016", day="2026-10-15", start="2026-10-15", end="2026-10-15")
    directory = _write_run_evidence_dir(root, {"000016": future})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]


def test_period_not_exactly_covering_the_target_is_refused(tmp_path: Path) -> None:
    """A range that reaches past the target is not proof about the target day."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    payload = _session_evidence("000016", day="2026-10-15", start=TARGET, end="2026-10-15")
    directory = _write_run_evidence_dir(root, {"000016": payload})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]


def test_symbol_mismatch_is_refused(tmp_path: Path) -> None:
    """A file named for 000016 whose payload is 000001 proves nothing."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000001")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]


def test_hash_mismatch_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    tampered = _session_evidence("000016")
    tampered["records_sha256"] = "0" * 64
    directory = _write_run_evidence_dir(root, {"000016": tampered})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]


def test_traded_target_day_is_not_a_verified_exclusion(tmp_path: Path) -> None:
    """tradestatus=1 says the symbol DID trade; it must never be excluded."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016", tradestatus="1")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["flagged_for_review_symbols"]
    assert "000016" in result["gap_symbols"]


def test_unreadable_run_file_is_surfaced_not_verified(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    directory = _write_run_evidence_dir(root, {})
    (directory / "session_000016.json").write_text("{not json", encoding="utf-8")

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]


# --------------------------------------------------- compatibility & merging


def test_legacy_metadata_evidence_still_works(tmp_path: Path) -> None:
    """The old path keeps working; the run bridge is additive, not a replacement."""
    root = _tree(tmp_path, {"000001": TARGET, "002274": PREVIOUS})
    _legacy_evidence(root, {"002274": "zero_trade_with_estimates"})

    result = classify_universe_completeness(TARGET, project_root=root)

    assert result["verified_excluded_symbols"] == ["002274"]
    assert result["complete"] is True


def test_run_and_legacy_evidence_are_merged(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS, "002274": PREVIOUS})
    _legacy_evidence(root, {"002274": "zero_trade_with_estimates"})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["verified_excluded_symbols"] == ["000016", "002274"]
    assert result["complete"] is True


def test_bar_written_for_a_verified_exclusion_is_still_a_contradiction(tmp_path: Path) -> None:
    """Stale or current evidence must never overwrite a real target-day bar."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": TARGET})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert result["complete"] is False
    assert result["contradiction_symbols"] == ["000016"]


def test_pointer_for_another_date_is_refused(tmp_path: Path) -> None:
    """A stale published pointer for another day must not be reused."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")}, target=PREVIOUS)

    partition = build_reconciliation_partition(root, target_date=TARGET)

    assert partition["verified_excluded_symbols"] == []
    assert "000016" in partition["pending_symbols"]


def test_run_signature_is_not_in_the_legacy_allowlist() -> None:
    """The bridge label must not become a legacy signature that is trusted alone."""
    assert RUN_SESSION_SIGNATURE not in KNOWN_SIGNATURES


def test_bare_run_signature_in_legacy_metadata_is_not_verified(tmp_path: Path) -> None:
    """A legacy entry carrying the bridge label, with no provider records or hash,
    must stay unverified - exactly as it did before this repair."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _legacy_evidence(root, {"000016": RUN_SESSION_SIGNATURE})

    result = classify_universe_completeness(TARGET, project_root=root)

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["flagged_for_review_symbols"]
    assert "000016" in result["gap_symbols"]
    assert result["complete"] is False
    assert any("suspension_signature_unknown" in w for w in result["warnings"])


# ------------------------------------------- current run overrides legacy (R1)


def test_current_traded_evidence_revokes_a_legacy_exclusion(tmp_path: Path) -> None:
    """The run says the symbol traded today; that revokes the old suspension."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _legacy_evidence(root, {"000016": "zero_trade_with_estimates"})
    directory = _write_run_evidence_dir(root, {"000016": _session_evidence("000016", tradestatus="1")})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert "000016" not in result["verified_excluded_symbols"]
    assert "000016" in result["gap_symbols"]
    assert result["complete"] is False


def test_current_rejected_evidence_revokes_a_legacy_exclusion(tmp_path: Path) -> None:
    """A rejected current record cannot vanish behind a legacy verified entry."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _legacy_evidence(root, {"000016": "zero_trade_with_estimates"})
    tampered = _session_evidence("000016")
    tampered["records_sha256"] = "0" * 64
    directory = _write_run_evidence_dir(root, {"000016": tampered})

    result = classify_universe_completeness(TARGET, project_root=root, run_evidence_dir=directory)

    assert "000016" not in result["verified_excluded_symbols"]
    assert "000016" in result["gap_symbols"]
    assert result["complete"] is False


# --------------------------------------------------- binding / identity (R3)


def test_run_evidence_for_another_day_is_refused(tmp_path: Path) -> None:
    """An object verified for 10/14 must not exclude 10/15 gaps."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": TARGET})
    evidence = load_run_session_evidence(TARGET, run_evidence={"000016": _session_evidence("000016")})
    assert evidence.excluded_symbols == frozenset({"000016"})

    result = classify_universe_completeness(
        "2026-10-15", project_root=root, run_evidence=evidence
    )

    assert result["verified_excluded_symbols"] == []
    assert "000016" in result["gap_symbols"]
    assert result["complete"] is False
    assert any("run_evidence_target_date_mismatch" in w for w in result["warnings"])


def test_contradictory_pointer_binding_is_rejected(tmp_path: Path) -> None:
    """Filling in a binding is not the same as proving it: contradictions refuse."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")})
    pointer = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["authoritative"] = False
    component = payload["components"]["target_day_gap_recovery"]
    component["target_date"] = PREVIOUS
    component["evidence_source"] = {
        "run_id": "different-run",
        "target_date": PREVIOUS,
        "source": "different-producer",
        "evidence_dir": str(root / "wrong-directory"),
        "schema_version": "wrong-schema",
    }
    pointer.write_text(json.dumps(payload), encoding="utf-8")

    evidence = resolve_run_session_evidence(root, target_date=TARGET)

    assert evidence.excluded_symbols == frozenset()
    assert evidence.warnings


def test_pointer_component_for_another_day_is_rejected(tmp_path: Path) -> None:
    """The component's own date must agree with the day being reconciled."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")})
    pointer = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["components"]["target_day_gap_recovery"]["target_date"] = PREVIOUS
    pointer.write_text(json.dumps(payload), encoding="utf-8")

    evidence = resolve_run_session_evidence(root, target_date=TARGET)

    assert evidence.excluded_symbols == frozenset()
    assert any("component_date_mismatch" in w for w in evidence.warnings)


def test_old_pointer_without_binding_fields_stays_compatible(tmp_path: Path) -> None:
    """Absent binding fields are compatible; only explicit contradictions refuse."""
    root = _tree(tmp_path, {"000001": TARGET, "000016": PREVIOUS})
    _write_chain_pointer(root, {"000016": _session_evidence("000016")})
    pointer = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload.pop("authoritative", None)
    pointer.write_text(json.dumps(payload), encoding="utf-8")

    evidence = resolve_run_session_evidence(root, target_date=TARGET)

    assert evidence.excluded_symbols == frozenset({"000016"})
