from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest


def _sector_snapshot(day: str) -> dict[str, object]:
    return {
        "status": "passed",
        "target_date": day,
        "evidence_eligible": True,
        "pit_eligible": True,
        "reason_codes": [],
        "degraded_reasons": [],
        "sector_rankings": [
            {
                "sector": "Bank",
                "current_rank": 1,
                "rotation_strength": 0.2,
                "sector_status": "leading",
                "status": "leading",
            }
        ],
        "sector_membership": [{"symbol": "600000", "sector": "Bank"}],
    }


def _write_sector_snapshot(root: Path, day: str) -> Path:
    path = root / day / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sector_snapshot(day)), encoding="utf-8")
    return path


def _write_daily(path: Path, *, periods: int = 30) -> None:
    rows: list[dict[str, object]] = []
    for index, day in enumerate(pd.bdate_range(end="2026-08-06", periods=periods)):
        rows.append(
            {
                "date": day.date().isoformat(),
                "symbol": "600000",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000 + index,
                "amount": 1_000_000 + index,
                "adjust": "raw",
                "source": "fixture",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _write_candidate_and_ledger(
    root: Path,
    *,
    source_sector: str = "Bank",
    source_amount_ma20: float = 1_000_000.0,
) -> tuple[Path, Path]:
    candidate_path = root / "candidate.json"
    ledger_path = root / "ledger.json"
    candidate_path.write_text(
        json.dumps(
            {
                "standard_candidates": [
                    {
                        "symbol": "600000",
                        "data_latest_date": "2026-08-06",
                        "daily_feature_latest_date": "2026-08-06",
                        "is_target_date_covered": True,
                        "close": 10.0,
                        "sector": source_sector,
                        "amount_ma20": source_amount_ma20,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ledger_path.write_text(json.dumps({"positions": []}), encoding="utf-8")
    return candidate_path, ledger_path


def _status_payload(snapshot_path: Path, *, pit_eligible: object = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "status": "passed",
        "target_date": "2026-08-06",
        "evidence_eligible": True,
        "reason_codes": [],
        "degraded_reasons": [],
        "source_paths": [str(snapshot_path)],
        "sector_rankings": [
            {
                "sector": "Bank",
                "sector_status": "leading",
                "status": "leading",
                "heat_score": 0.2,
            }
        ],
    }
    if pit_eligible != "missing":
        payload["pit_eligible"] = pit_eligible
    return payload


def test_pit_sector_status_passed_payload_explicitly_certifies_pit(tmp_path: Path) -> None:
    from quantdesk.data_truth.pit_sector_status import build_pit_sector_status

    start = date(2026, 8, 1)
    for offset in range(6):
        _write_sector_snapshot(tmp_path, (start + timedelta(days=offset)).isoformat())

    payload = build_pit_sector_status(
        as_of="2026-08-06",
        history_dir=tmp_path,
        windows=(5,),
    )

    assert payload["status"] == "passed"
    assert payload["evidence_eligible"] is True
    assert payload["pit_eligible"] is True


def test_pit_sector_status_blocked_payload_explicitly_rejects_pit(tmp_path: Path) -> None:
    from quantdesk.data_truth.pit_sector_status import build_pit_sector_status

    payload = build_pit_sector_status(
        as_of="2026-08-06",
        history_dir=tmp_path,
        windows=(5,),
    )

    assert payload["status"] == "blocked"
    assert payload["evidence_eligible"] is False
    assert payload["pit_eligible"] is False


@pytest.mark.parametrize("pit_eligible", ["missing", False])
def test_sector_statuses_require_top_level_pit_eligibility(
    tmp_path: Path,
    pit_eligible: object,
) -> None:
    from quantdesk.risk.risk_gate_shadow import _sector_statuses

    snapshot_path = _write_sector_snapshot(tmp_path / "history", "2026-08-06")
    status_path = tmp_path / "pit-sector-status.json"
    status_path.write_text(
        json.dumps(_status_payload(snapshot_path, pit_eligible=pit_eligible)),
        encoding="utf-8",
    )

    statuses, _cutoff, _source = _sector_statuses(status_path, as_of="2026-08-06")

    assert statuses == {}


def test_candidate_amount_ma20_never_falls_back_when_canonical_window_is_short(
    tmp_path: Path,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path, periods=19)
    candidate_path, ledger_path = _write_candidate_and_ledger(
        tmp_path,
        source_amount_ma20=999_999_999.0,
    )
    candidate = {
        "symbol": "600000",
        "data_latest_date": "2026-08-06",
        "daily_feature_latest_date": "2026-08-06",
        "is_target_date_covered": True,
        "close": 10.0,
        "sector": "Bank",
        "amount_ma20": 999_999_999.0,
    }

    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate=candidate,
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert "liquidity" not in evidence
    assert missing["liquidity"] == ["liquidity_local_evidence_missing"]


@pytest.mark.parametrize(
    ("source_sector", "source_amount_ma20"),
    [("Energy", 1_000_000.0), ("Bank", 9_999_999.0)],
)
def test_candidate_source_binding_requires_business_field_match(
    tmp_path: Path,
    source_sector: str,
    source_amount_ma20: float,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_candidate_and_ledger(
        tmp_path,
        source_sector=source_sector,
        source_amount_ma20=source_amount_ma20,
    )
    candidate = {
        "symbol": "600000",
        "data_latest_date": "2026-08-06",
        "daily_feature_latest_date": "2026-08-06",
        "is_target_date_covered": True,
        "close": 10.0,
        "sector": "Bank",
        "amount_ma20": 1_000_000.0,
    }

    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate=candidate,
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert not ({"price", "sector", "liquidity"} & set(evidence))
    assert set(missing) >= {"price", "sector", "liquidity"}


def test_membership_and_statuses_share_one_status_file_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_candidate_and_ledger(tmp_path)
    snapshot_path = _write_sector_snapshot(tmp_path / "history", "2026-08-06")
    status_path = tmp_path / "pit-sector-status.json"
    status_path.write_text(json.dumps(_status_payload(snapshot_path)), encoding="utf-8")
    candidate = {
        "symbol": "600000",
        "data_latest_date": "2026-08-06",
        "daily_feature_latest_date": "2026-08-06",
        "is_target_date_covered": True,
        "close": 10.0,
        "sector": "Bank",
        "amount_ma20": 1_000_000.0,
    }
    status_reads = 0
    original_open = Path.open
    resolved_status = status_path.resolve()

    def counted_open(file: Path, *args: object, **kwargs: object):
        nonlocal status_reads
        resolved = file.resolve()
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if resolved == resolved_status and "r" in mode:
            status_reads += 1
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate=candidate,
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
        sector_rotation_path=status_path,
    )

    assert "sector" in evidence
    assert "smart_stop" not in missing
    assert status_reads == 1
