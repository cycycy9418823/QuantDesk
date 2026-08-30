from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest


def _snapshot(day: str, *, target_date: str | None = None) -> dict[str, object]:
    return {
        "status": "passed",
        "target_date": target_date or day,
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


def _write_snapshot(root: Path, day: str, *, target_date: str | None = None) -> Path:
    path = root / day / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_snapshot(day, target_date=target_date)), encoding="utf-8")
    return path


def _write_daily(path: Path, *, include_basis: bool = True, end: str = "2026-08-06") -> None:
    days = pd.bdate_range(end=pd.Timestamp(end), periods=30)
    rows = []
    for index, day in enumerate(days):
        row = {
            "date": day.date().isoformat(),
            "symbol": "600000",
            "open": 10.0 + index / 100,
            "high": 10.2 + index / 100,
            "low": 9.8 + index / 100,
            "close": 10.1 + index / 100,
            "volume": 1000 + index,
            "amount": 1_000_000 + index,
            "source": "fixture",
        }
        if include_basis:
            row["adjust"] = "raw"
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _write_local_sources(
    tmp_path: Path,
    *,
    day: str = "2026-08-06",
    price: float = 10.0,
    ledger: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    candidate_path = tmp_path / "candidate.json"
    ledger_path = tmp_path / "ledger.json"
    candidate_path.write_text(
        json.dumps(
            {
                "standard_candidates": [
                    {
                        "symbol": "600000",
                        "data_latest_date": day,
                        "close": price,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ledger_path.write_text(
        json.dumps(ledger if ledger is not None else {"positions": []}),
        encoding="utf-8",
    )
    return candidate_path, ledger_path


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_pit_sector_status_rejects_snapshot_payload_dated_after_as_of(tmp_path: Path) -> None:
    from quantdesk.data_truth.pit_sector_status import build_pit_sector_status

    start = date(2026, 8, 1)
    for offset in range(6):
        day = (start + timedelta(days=offset)).isoformat()
        _write_snapshot(
            tmp_path,
            day,
            target_date="2026-08-07" if offset == 5 else day,
        )

    payload = build_pit_sector_status(
        as_of="2026-08-06",
        history_dir=tmp_path,
        windows=(5,),
    )

    assert payload["status"] == "blocked"
    assert "pit_sector_latest_snapshot_date_mismatch" in payload["reason_codes"]


def test_sector_membership_rejects_future_payload_hidden_in_asof_directory(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import _sector_membership

    snapshot = _write_snapshot(
        tmp_path / "data" / "sector_history",
        "2026-08-06",
        target_date="2026-08-07",
    )
    status_path = tmp_path / "pit_status.json"
    status_path.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "passed",
                "target_date": "2026-08-06",
                "evidence_eligible": True,
                "pit_eligible": True,
                "reason_codes": [],
                "degraded_reasons": [],
                "source_paths": [str(snapshot)],
            }
        ),
        encoding="utf-8",
    )

    assert _sector_membership(status_path, as_of="2026-08-06") == {}


@pytest.mark.parametrize("pit_eligible", [None, False])
def test_sector_membership_source_snapshot_requires_pit_eligible(
    tmp_path: Path,
    pit_eligible: bool | None,
) -> None:
    from quantdesk.risk.risk_gate_shadow import _sector_membership

    snapshot = _write_snapshot(
        tmp_path / "data" / "sector_history",
        "2026-08-06",
    )
    snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if pit_eligible is None:
        snapshot_payload.pop("pit_eligible")
    else:
        snapshot_payload["pit_eligible"] = pit_eligible
    snapshot.write_text(json.dumps(snapshot_payload), encoding="utf-8")
    status_path = tmp_path / "pit_status.json"
    status_path.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "passed",
                "target_date": "2026-08-06",
                "evidence_eligible": True,
                "pit_eligible": True,
                "reason_codes": [],
                "degraded_reasons": [],
                "source_paths": [str(snapshot)],
            }
        ),
        encoding="utf-8",
    )

    assert _sector_membership(status_path, as_of="2026-08-06") == {}


@pytest.mark.parametrize("pit_eligible", [None, False])
def test_sector_membership_inline_rows_require_pit_eligible(
    tmp_path: Path,
    pit_eligible: bool | None,
) -> None:
    from quantdesk.risk.risk_gate_shadow import _sector_membership

    status_path = tmp_path / "pit_status.json"
    payload = _snapshot("2026-08-06")
    if pit_eligible is None:
        payload.pop("pit_eligible")
    else:
        payload["pit_eligible"] = pit_eligible
    payload["ok"] = True
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _sector_membership(status_path, as_of="2026-08-06") == {}


def test_amount_ma20_rejects_daily_frame_without_explicit_raw_basis(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import _derive_amount_ma20

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv", include_basis=False)

    assert _derive_amount_ma20(daily_dir, "600000", as_of="2026-08-06") == (
        None,
        None,
        0,
    )


def test_amount_ma20_rejects_invalid_value_inside_raw_last_window(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import _derive_amount_ma20_with_cutoff

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    frame = pd.read_csv(daily_path, encoding="utf-8-sig")
    frame["amount"] = frame["amount"].astype(object)
    frame.loc[len(frame) - 5, "amount"] = "not-a-number"
    frame.to_csv(daily_path, index=False, encoding="utf-8-sig")

    assert _derive_amount_ma20_with_cutoff(
        daily_path.parent,
        "600000",
        as_of="2026-08-06",
    ) == (None, None, 0, None)


def test_amount_ma20_requires_a_complete_raw_window(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import _derive_amount_ma20_with_cutoff

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    frame = pd.read_csv(daily_path, encoding="utf-8-sig").tail(19)
    frame.to_csv(daily_path, index=False, encoding="utf-8-sig")

    assert _derive_amount_ma20_with_cutoff(
        daily_path.parent,
        "600000",
        as_of="2026-08-06",
    ) == (None, None, 0, None)


def test_amount_ma20_accepts_zero_in_complete_raw_window_and_uses_last_date(
    tmp_path: Path,
) -> None:
    from quantdesk.risk.risk_gate_shadow import _derive_amount_ma20_with_cutoff

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    frame = pd.read_csv(daily_path, encoding="utf-8-sig")
    frame.loc[len(frame) - 5, "amount"] = 0
    frame.to_csv(daily_path, index=False, encoding="utf-8-sig")

    amount, source, rows, cutoff = _derive_amount_ma20_with_cutoff(
        daily_path.parent,
        "600000",
        as_of="2026-08-06",
    )

    assert amount is not None
    assert source == str(daily_path)
    assert rows == 20
    assert cutoff == "2026-08-06"


def test_local_evidence_requires_readable_candidate_and_ledger_sources(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv")
    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "amount_ma20": 1_000_000,
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_dir,
        candidate_source_path=tmp_path / "missing-candidate.json",
        ledger_source_path=tmp_path / "missing-ledger.json",
    )

    assert "price" not in evidence
    assert "sector" not in evidence
    assert "liquidity" not in evidence
    assert set(missing) >= {"price", "sector", "liquidity"}


def test_local_evidence_binds_readable_source_file_hashes(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv")
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "amount_ma20": 1_000_000,
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_dir,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert "price" not in missing
    assert "sector" not in missing
    assert "liquidity" not in missing
    assert evidence["price"]["source_bindings"] == [
        {
            "role": "candidate",
            "path": str(candidate_path),
            "file_sha256": _file_sha256(candidate_path),
        },
        {
            "role": "price",
            "path": str(candidate_path),
            "file_sha256": _file_sha256(candidate_path),
        },
    ]
    assert evidence["sector"]["source_bindings"] == [
        {
            "role": "candidate",
            "path": str(candidate_path),
            "file_sha256": _file_sha256(candidate_path),
        },
        {
            "role": "ledger",
            "path": str(ledger_path),
            "file_sha256": _file_sha256(ledger_path),
        },
    ]
    assert evidence["liquidity"]["source_bindings"] == [
        {
            "role": "candidate",
            "path": str(candidate_path),
            "file_sha256": _file_sha256(candidate_path),
        },
        {
            "role": "daily",
            "path": str(daily_dir / "600000.csv"),
            "file_sha256": _file_sha256(daily_dir / "600000.csv"),
        },
    ]
    candidate_hash = _canonical_sha256(
        {
            "symbol": "600000",
            "sector": "Bank",
            "amount_ma20": 1_000_000,
            "data_latest_date": "2026-08-06",
        }
    )
    assert evidence["price"]["payload_sha256"]["candidate"] == candidate_hash
    assert evidence["sector"]["payload_sha256"]["candidate"] == candidate_hash
    assert evidence["liquidity"]["payload_sha256"]["candidate"] == candidate_hash
    assert evidence["sector"]["payload_sha256"]["ledger"] == _canonical_sha256(
        {"positions": []}
    )
    assert evidence["price"]["payload_sha256"]["reference_price"] == _canonical_sha256(
        {
            "symbol": "600000",
            "as_of": "2026-08-06",
            "data_cutoff": "2026-08-06",
            "value": 10.0,
        }
    )


@pytest.mark.parametrize(
    ("source_symbol", "source_day"),
    [("600001", "2026-08-06"), ("600000", "2026-08-07")],
)
def test_candidate_source_must_prove_target_symbol_and_date(
    tmp_path: Path,
    source_symbol: str,
    source_day: str,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    candidate_path.write_text(
        json.dumps(
            {
                "standard_candidates": [
                    {
                        "symbol": source_symbol,
                        "data_latest_date": source_day,
                        "close": 10.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert "price" not in evidence
    assert "sector" not in evidence
    assert "liquidity" not in evidence
    assert set(missing) >= {"price", "sector", "liquidity"}


@pytest.mark.parametrize(
    ("source_symbol", "source_day", "source_price"),
    [
        ("600001", "2026-08-06", 10.0),
        ("600000", "2026-08-07", 10.0),
        ("600000", "2026-08-06", 10.1),
    ],
)
def test_price_source_must_prove_target_symbol_date_and_value(
    tmp_path: Path,
    source_symbol: str,
    source_day: str,
    source_price: float,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    price_path = tmp_path / "minute.csv"
    pd.DataFrame(
        [
            {
                "symbol": source_symbol,
                "datetime": f"{source_day} 09:30:00",
                "close": source_price,
            }
        ]
    ).to_csv(price_path, index=False, encoding="utf-8-sig")

    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
        price_source_path=price_path,
        price_data_cutoff="2026-08-06",
    )

    assert "price" not in evidence
    assert missing["price"] == ["price_local_evidence_missing"]


def test_ledger_file_must_match_funded_ledger_core_and_position_symbols(
    tmp_path: Path,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    ledger_path.write_text(
        json.dumps({"cash": 1.0, "positions": [{"symbol": "600001"}]}),
        encoding="utf-8",
    )
    funded_ledger = {"cash": 100.0, "positions": []}

    evidence, missing = collect_local_risk_evidence(
        funded_ledger=funded_ledger,
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert "sector" not in evidence
    assert missing["sector"] == ["sector_asset_local_evidence_missing"]


def test_stage4_memory_ledger_binds_actual_payload_without_file_impersonation(
    tmp_path: Path,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, _ledger_path = _write_local_sources(tmp_path)
    funded_ledger = {"cash": 100.0, "positions": []}

    evidence, missing = collect_local_risk_evidence(
        funded_ledger=funded_ledger,
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path="memory://stage4-replay/A",
    )

    assert "sector" not in missing
    assert evidence["sector"]["source_bindings"][1] == {
        "role": "ledger",
        "uri": "memory://stage4-replay/A",
        "payload_sha256": _canonical_sha256(funded_ledger),
    }
    assert evidence["sector"]["payload_sha256"]["ledger"] == _canonical_sha256(
        funded_ledger
    )


def test_shared_candidate_and_price_file_is_opened_once_per_collect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    real_open = Path.open
    read_count = 0

    def counted_open(path: Path, *args: object, **kwargs: object):
        nonlocal read_count
        if path.resolve() == candidate_path.resolve():
            read_count += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
        price_source_path=candidate_path,
        price_data_cutoff="2026-08-06",
    )

    assert read_count == 1


@pytest.mark.parametrize("daily_fault", ["insufficient", "invalid_tail"])
def test_candidate_amount_ma20_cannot_bypass_canonical_daily_window_gate(
    tmp_path: Path,
    daily_fault: str,
) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_path = tmp_path / "daily" / "600000.csv"
    _write_daily(daily_path)
    frame = pd.read_csv(daily_path, encoding="utf-8-sig")
    if daily_fault == "insufficient":
        frame = frame.tail(19)
    else:
        frame["amount"] = frame["amount"].astype(object)
        frame.loc[len(frame) - 5, "amount"] = "not-a-number"
    frame.to_csv(daily_path, index=False, encoding="utf-8-sig")
    candidate_path, ledger_path = _write_local_sources(tmp_path)

    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "amount_ma20": 999_999_999,
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_path.parent,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert "liquidity" not in evidence
    assert missing["liquidity"] == ["liquidity_local_evidence_missing"]


def test_explicit_price_source_must_be_a_readable_hashed_file(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv")
    candidate_path, ledger_path = _write_local_sources(tmp_path)
    evidence, missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={
            "symbol": "600000",
            "sector": "Bank",
            "amount_ma20": 1_000_000,
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_dir,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
        price_source_path=tmp_path / "missing-minute.csv",
    )

    assert "price" not in evidence
    assert missing["price"] == ["price_local_evidence_missing"]


def test_derived_liquidity_uses_actual_daily_cutoff_not_candidate_date(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv", include_basis=True, end="2026-08-06")
    candidate_path, ledger_path = _write_local_sources(tmp_path, day="2026-08-07")
    evidence, _missing = collect_local_risk_evidence(
        funded_ledger={"positions": []},
        candidate={"symbol": "600000", "data_latest_date": "2026-08-07"},
        side="buy",
        reference_price=10.0,
        as_of="2026-08-07",
        daily_dir=daily_dir,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
    )

    assert evidence["liquidity"]["data_cutoff"] == "2026-08-06"
    assert "2026-08-06" in evidence["liquidity"]["derivation"]


def test_sector_taxonomies_stay_separate_for_ledger_binding_and_pit_status(tmp_path: Path) -> None:
    from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence

    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "600000.csv", include_basis=True)
    snapshot = _write_snapshot(tmp_path / "data" / "sector_history", "2026-08-06")
    status_path = tmp_path / "pit_status.json"
    payload = _snapshot("2026-08-06")
    payload.update({"ok": True, "source_paths": [str(snapshot)]})
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    ledger = {
        "positions": [
            {
                "symbol": "600000",
                "sector": "Ledger Banking Taxonomy",
                "price_basis": "raw",
            }
        ]
    }
    candidate_path, ledger_path = _write_local_sources(tmp_path, ledger=ledger)

    evidence, _missing = collect_local_risk_evidence(
        funded_ledger=ledger,
        candidate={
            "symbol": "600000",
            "sector": "Ledger Banking Taxonomy",
            "amount_ma20": 1_000_000,
            "data_latest_date": "2026-08-06",
        },
        side="buy",
        reference_price=10.0,
        as_of="2026-08-06",
        daily_dir=daily_dir,
        candidate_source_path=candidate_path,
        ledger_source_path=ledger_path,
        sector_rotation_path=status_path,
    )

    assert evidence["sector"]["by_symbol"]["600000"] == "Ledger Banking Taxonomy"
    assert evidence["smart_stop"]["sector_status_by_symbol"]["600000"]["sector_status"] == "leading"


def test_candidate_projection_omits_invalid_pass_through_values() -> None:
    from quantdesk.daily_candidate_refresh import _candidate_projection

    projected = _candidate_projection(
        {
            "symbol": "600000",
            "close": float("nan"),
            "amount_ma20": -1,
        }
    )

    assert "close" not in projected
    assert "amount_ma20" not in projected
