from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantdesk.data_truth.sector_history_archive import (
    _canonical_bytes, load_forward_sector_snapshot, read_sector_membership_as_of,
    verify_snapshot_self_hash,
)
from quantdesk.data_truth.candidate_lifecycle_evidence import _load_membership


def _snapshot(root: Path, day: str, **changes: object) -> Path:
    payload = {"status": "passed", "evidence_eligible": True, "pit_eligible": True,
        "target_date": day, "usable_from": day, "captured_at": f"{day}T15:30:00+08:00",
        "sector_rankings": [{"sector": "银行", "rank": 1}], "sector_membership": [
            {"symbol": "600000", "sector": "银行", "effective_from": day, "observed_at": f"{day}T15:30:00+08:00"}], **changes}
    payload["snapshot_hash"] = "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    path = root / day / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_prior_capture_keeps_original_date_and_is_not_formal_for_today(tmp_path: Path) -> None:
    path = _snapshot(tmp_path, "2026-10-09")
    before = path.read_bytes()
    result = read_sector_membership_as_of(tmp_path, as_of="2026-10-12")
    assert result["snapshot"]["target_date"] == "2026-10-09"
    assert verify_snapshot_self_hash(result["snapshot"])
    observed = result["observation"]
    assert observed["age_calendar_days"] == 3
    assert observed["freshness"] == "prior_snapshot"
    assert observed["eligible_for_requested_day"] is False
    assert observed["evidence_kind"] == "last_known_membership_proxy"
    view = load_forward_sector_snapshot(tmp_path, as_of="2026-10-12")
    assert view["status"] == "partial" and view["evidence_eligible"] is False
    assert view["sector_rankings"] == []
    membership, source = _load_membership("2026-10-12", history_dir=tmp_path)
    assert source
    assert membership["600000"]["_as_of_observation"]["eligible_for_requested_day"] is False
    assert path.read_bytes() == before


def test_future_snapshot_cannot_change_historical_read(tmp_path: Path) -> None:
    _snapshot(tmp_path, "2026-10-09")
    first = read_sector_membership_as_of(tmp_path, as_of="2026-10-09")
    _snapshot(tmp_path, "2026-10-12")
    second = read_sector_membership_as_of(tmp_path, as_of="2026-10-09")
    assert first == second
    assert second["observation"]["eligible_for_requested_day"] is True


@pytest.mark.parametrize("field,value", [
    ("target_date", "2026-10-12"), ("usable_from", "2026-10-12"),
    ("captured_at", "2026-10-12T15:30:00+08:00"),
    ("sector_membership", [{"symbol": "600000", "sector": "银行", "effective_from": "2026-10-09", "observed_at": "2026-10-12"}]),
])
def test_signed_but_conflicting_dates_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    _snapshot(tmp_path, "2026-10-09", **{field: value})
    result = read_sector_membership_as_of(tmp_path, as_of="2026-10-12")
    assert result["snapshot"] == {}
    assert result["observation"]["freshness"] == "blocked"


def test_revocation_and_hash_change_are_not_bypassed(tmp_path: Path) -> None:
    path = _snapshot(tmp_path, "2026-10-09")
    original = path.read_bytes()
    path.write_bytes(original.replace(b"600000", b"600001"))
    assert not read_sector_membership_as_of(tmp_path, as_of="2026-10-12")["snapshot"]
    path.write_bytes(original)
    (path.parent / "revocation.json").write_text("{}", encoding="utf-8")
    assert not read_sector_membership_as_of(tmp_path, as_of="2026-10-12")["snapshot"]
