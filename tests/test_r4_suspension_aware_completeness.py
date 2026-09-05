"""Three-way completeness: complete / verified-suspended / gap.

Every case below is built from a temp tree. Nothing reads the real data/, state/
or results/ trees, and nothing reaches the network.

The properties that matter, and why:
  - a suspended symbol must NOT count as a gap, or the reconciliation reports a
    permanent false "incomplete"
  - a symbol absent from the evidence must STILL count as a gap, or a broken
    fetch side silently looks like a clean day
  - a bar written for a verified-suspended day is a contradiction and must block
    `complete`, because that bar is a fabricated price
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk.suspension_aware_completeness import (
    KNOWN_SIGNATURES,
    classify_universe_completeness,
    evidence_path,
    load_suspension_evidence,
)

TARGET = "2026-08-31"


def _tree(tmp_path: Path, bars: dict[str, str], evidence: dict | None) -> Path:
    """Build a minimal project tree: universe, daily bars, optional evidence."""
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "daily").mkdir(parents=True, exist_ok=True)

    universe = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    lines = ["symbol,name,source,updated_at"]
    for sym in bars:
        lines.append(f"{sym},Name{sym},test,2026-08-31")
    universe.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for sym, last_date in bars.items():
        if last_date == "":
            continue  # symbol with no csv at all
        p = tmp_path / "data" / "daily" / f"{sym}.csv"
        rows = ["date,symbol,open,high,low,close,volume,amount"]
        rows.append(f"2026-08-27,{sym},1.0,1.1,0.9,1.0,1000,1000")
        rows.append(f"{last_date},{sym},1.0,1.1,0.9,1.0,1000,1000")
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")

    if evidence is not None:
        ep = evidence_path(TARGET, project_root=tmp_path)
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _evidence(verified: dict[str, str], flagged: dict[str, str] | None = None) -> dict:
    return {
        "target_date": TARGET,
        "source": "test",
        "generated_at": "2026-09-01T00:00:00",
        "contract": "suspended day = no bar",
        "verified_exclusions": {s: {"signature": sig, "close": "0.000", "volume": ""} for s, sig in verified.items()},
        "flagged_for_review": {s: {"signature": sig} for s, sig in (flagged or {}).items()},
    }


# --------------------------------------------------------------- evidence load


def test_missing_evidence_file_is_reported_not_assumed_empty(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET}, None)
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert ev.present is False
    assert ev.excluded_symbols == frozenset()
    assert any("missing" in w for w in ev.warnings)


def test_evidence_for_a_different_date_is_refused(tmp_path: Path) -> None:
    """Consuming yesterday's evidence would exclude the wrong symbols."""
    bad = _evidence({"002274": "zero_trade_with_estimates"})
    bad["target_date"] = "2026-08-28"
    root = _tree(tmp_path, {"002274": "2026-08-28"}, bad)
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert ev.present is False
    assert any("date_mismatch" in w for w in ev.warnings)


def test_unreadable_evidence_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET}, None)
    ep = evidence_path(TARGET, project_root=root)
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text("{not json", encoding="utf-8")
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert ev.present is False
    assert any("unreadable" in w for w in ev.warnings)


def test_symbol_in_both_lists_is_demoted_to_gap(tmp_path: Path) -> None:
    """Ambiguous status must never resolve to complete."""
    ev_payload = _evidence(
        {"002274": "zero_trade_with_estimates"},
        {"002274": "zero_trade_with_estimates"},
    )
    root = _tree(tmp_path, {"002274": "2026-08-28"}, ev_payload)
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert "002274" not in ev.excluded_symbols
    assert any("overlap" in w for w in ev.warnings)


def test_unknown_signature_is_demoted_to_gap(tmp_path: Path) -> None:
    ev_payload = _evidence({"002274": "some_new_upstream_shape"})
    root = _tree(tmp_path, {"002274": "2026-08-28"}, ev_payload)
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert "002274" not in ev.excluded_symbols
    assert "002274" in ev.review_symbols
    assert any("signature_unknown" in w for w in ev.warnings)

    verdict = classify_universe_completeness(TARGET, project_root=root)
    assert verdict["complete"] is False
    assert verdict["gap_symbols"] == ["002274"]


def test_list_shaped_evidence_is_accepted(tmp_path: Path) -> None:
    """A future format change must not silently empty the exclusion set."""
    payload = {
        "target_date": TARGET,
        "verified_exclusions": [{"symbol": "002274", "signature": "zero_trade_with_estimates"}],
        "flagged_for_review": [],
    }
    root = _tree(tmp_path, {"002274": "2026-08-28"}, payload)
    ev = load_suspension_evidence(TARGET, project_root=root)
    assert ev.excluded_symbols == frozenset({"002274"})


# ------------------------------------------------------------ classification


def test_all_symbols_with_bars_is_complete(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "000002": TARGET}, _evidence({}))
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is True
    assert r["with_bar_count"] == 2
    assert r["gap_count"] == 0


def test_suspended_symbol_counts_as_verified_exclusion(tmp_path: Path) -> None:
    """The whole point: a suspended day is not a gap."""
    root = _tree(
        tmp_path,
        {"000001": TARGET, "002274": "2026-08-28"},
        _evidence({"002274": "zero_trade_with_estimates"}),
    )
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is True, r["warnings"]
    assert r["verified_excluded_symbols"] == ["002274"]
    assert r["gap_count"] == 0


def test_symbol_absent_from_evidence_stays_a_gap(tmp_path: Path) -> None:
    """A fetch failure must not be laundered into an exclusion."""
    root = _tree(
        tmp_path,
        {"000001": TARGET, "600000": "2026-08-28"},
        _evidence({"002274": "zero_trade_with_estimates"}),
    )
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is False
    assert r["gap_symbols"] == ["600000"]


def test_missing_evidence_keeps_suspended_symbols_as_gaps(tmp_path: Path) -> None:
    """A broken fetch side must never look like a clean day."""
    root = _tree(tmp_path, {"000001": TARGET, "002274": "2026-08-28"}, None)
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is False
    assert r["evidence_present"] is False
    assert "002274" in r["gap_symbols"]


def test_bar_written_for_suspended_symbol_is_a_contradiction(tmp_path: Path) -> None:
    """A bar on a no-trading day is a fabricated price; it must block complete."""
    root = _tree(
        tmp_path,
        {"000001": TARGET, "002274": TARGET},
        _evidence({"002274": "zero_trade_with_estimates"}),
    )
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is False
    assert r["contradiction_symbols"] == ["002274"]
    assert any("bar_written_for_verified_suspended" in w for w in r["warnings"])


def test_symbol_with_no_csv_is_a_gap_and_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"000001": TARGET, "999999": ""}, _evidence({}))
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is False
    assert r["missing_file_symbols"] == ["999999"]


def test_flagged_for_review_is_surfaced_not_hidden(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"000001": TARGET, "600929": "2026-08-28"},
        _evidence({}, {"600929": "zero_volume_with_last_close"}),
    )
    r = classify_universe_completeness(TARGET, project_root=root)
    assert r["complete"] is False
    assert r["flagged_for_review_symbols"] == ["600929"]
    assert "600929" in r["gap_symbols"]


def test_known_signatures_cover_both_observed_shapes() -> None:
    """Both shapes GLM observed tonight must be recognised."""
    assert "zero_trade_with_estimates" in KNOWN_SIGNATURES
    assert "zero_volume_with_last_close" in KNOWN_SIGNATURES


# ------------------------------------------------------------- live evidence


def test_live_evidence_file_matches_the_consumed_contract() -> None:
    """End to end against tonight's real evidence file, if it exists."""
    ev = load_suspension_evidence(TARGET)
    if not ev.present:
        pytest.skip("no live suspension evidence for this date")
    assert ev.excluded_symbols, "live evidence declares no exclusions"
    for sym, meta in ev.verified_exclusions.items():
        assert str(meta.get("signature") or "") in KNOWN_SIGNATURES, (
            f"{sym} carries an unrecognised signature {meta.get('signature')!r}"
        )
