from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


BASE_HEADER = [
    "symbol",
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "updated_at",
]


def _row(minute: str, *, close: str = "10.0") -> dict[str, str]:
    return {
        "symbol": "600000",
        "datetime": f"2026-07-03 {minute}",
        "date": "2026-07-03",
        "time": minute,
        "open": "10.0",
        "high": "10.1",
        "low": "9.9",
        "close": close,
        "volume": "1000",
        "amount": "10000",
        "source": "tushare_proxy_stk_mins",
        "updated_at": "2026-07-04T00:00:00",
    }


def _write_csv(path: Path, rows: list[dict[str, str]], *, staged: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [*BASE_HEADER, *( ["price_basis", "price_only_intraday"] if staged else [])]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for raw in rows:
            row = dict(raw)
            if staged:
                row.update({"price_basis": "raw", "price_only_intraday": "false"})
            writer.writerow(row)


def _fixture(tmp_path: Path, *, verdict: str = "raw_proven", conflict: bool = False) -> dict[str, Path]:
    canonical_dir = tmp_path / "data" / "intraday" / "1m"
    staging_dir = tmp_path / "results" / "round17" / "staging"
    original_dir = tmp_path / "vendor_staging"
    backup_dir = tmp_path / "backup"
    canonical_rows = [_row("09:30:00", close="10.2" if conflict else "10.0")]
    source_rows = [_row("09:30:00"), _row("09:31:00", close="10.05")]
    _write_csv(canonical_dir / "600000.csv", canonical_rows, staged=False)
    _write_csv(original_dir / "600000.csv", source_rows, staged=False)
    _write_csv(staging_dir / "600000.csv", source_rows, staged=True)
    backup_copy = backup_dir / "source" / "600000.csv"
    backup_copy.parent.mkdir(parents=True, exist_ok=True)
    backup_copy.write_bytes((original_dir / "600000.csv").read_bytes())

    verdict_record = {
        "symbol": "600000",
        "verdict": verdict,
        "reason": "all_corporate_action_sessions_match_canonical_raw_daily_gaps",
        "event_count": 1,
        "events": [
            {
                "trade_date": "2026-06-30",
                "classification": "raw_match",
                "daily_raw_gap": -0.1,
                "minute_gap": -0.1,
                "absolute_gap_difference": 0.0,
            }
        ],
    }
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text(json.dumps({"verdicts": [verdict_record]}), encoding="utf-8")
    provenance_path = tmp_path / "provenance.json"
    provenance = {
        "schema_version": "round17_tushare_raw_import_provenance_v1",
        "symbols": {
            "600000": {
                "symbol": "600000",
                "verdict": verdict,
                "verdict_path": str(verdict_path),
                "verdict_sha256": _sha(verdict_path),
                "verdict_record_sha256": _digest(verdict_record),
                "event_count": 1,
                "raw_match_count": 1,
                "original_source_path": str(original_dir / "600000.csv"),
                "original_source_sha256": _sha(original_dir / "600000.csv"),
                "backup_copy_path": str(backup_copy),
                "backup_copy_sha256": _sha(backup_copy),
                "staging_path": str(staging_dir / "600000.csv"),
                "staging_sha256": _sha(staging_dir / "600000.csv"),
            }
        },
    }
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    return {
        "canonical_dir": canonical_dir,
        "staging_dir": staging_dir,
        "backup_dir": backup_dir,
        "provenance_path": provenance_path,
    }


def _repoint_provenance(provenance_path: Path, staging_path: Path) -> None:
    """Refresh the staging SHA after a test rewrites the staging file."""

    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    payload["symbols"]["600000"]["staging_sha256"] = _sha(staging_path)
    provenance_path.write_text(json.dumps(payload), encoding="utf-8")


def test_historical_raw_plan_binds_verdict_and_prepares_append_and_label(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path)
    payload = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    assert payload["status"] == "ready"
    assert payload["quality_status"] == "passed"
    assert payload["canonical_rows_before"] == 1
    assert payload["staging_rows"] == 2
    assert payload["missing_staging_rows"] == 1
    assert payload["canonical_rows_after"] == 2
    assert payload["verdict_binding"]["event_count"] == 1
    assert payload["plan_digest"]


def test_historical_raw_plan_blocks_same_timestamp_value_conflict(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path, conflict=True)
    before = (paths["canonical_dir"] / "600000.csv").read_bytes()
    payload = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    assert payload["status"] == "blocked"
    assert "canonical_staging_timestamp_conflict" in payload["reason_codes"]
    assert (paths["canonical_dir"] / "600000.csv").read_bytes() == before


def test_historical_raw_plan_blocks_non_raw_verdict(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path, verdict="indeterminate")
    payload = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    assert payload["status"] == "blocked"
    assert "verdict_not_raw_proven" in payload["reason_codes"]


def test_historical_raw_apply_is_atomic_and_restores_only_failed_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_historical_raw_import as importer
    from quantdesk.intraday.stage6_append_only_import import (
        apply_proven_historical_intraday_import,
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path)
    canonical = paths["canonical_dir"] / "600000.csv"
    before = canonical.read_bytes()
    plan = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    original_replace = importer._replace_canonical_bytes

    def fail_after_replace(path: Path, data: bytes) -> None:
        original_replace(path, data)
        raise OSError("injected failure")

    monkeypatch.setattr(importer, "_replace_canonical_bytes", fail_after_replace)
    failed = apply_proven_historical_intraday_import(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
        expected_plan_digest=plan["plan_digest"],
        recovery_backup_dir=paths["backup_dir"] / "recovery",
    )
    assert failed["status"] == "failed"
    assert failed["rollback_performed"] is True
    assert canonical.read_bytes() == before

    monkeypatch.setattr(importer, "_replace_canonical_bytes", original_replace)
    completed = apply_proven_historical_intraday_import(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
        expected_plan_digest=plan["plan_digest"],
        recovery_backup_dir=paths["backup_dir"] / "recovery-success",
    )
    assert completed["status"] == "completed"
    assert completed["applied"] is True
    with canonical.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert {row["price_basis"] for row in rows} == {"raw"}


def test_historical_raw_apply_preserves_later_canonical_provider_rows(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        apply_proven_historical_intraday_import,
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path)
    canonical = paths["canonical_dir"] / "600000.csv"
    appended = _row("09:32:00", close="10.08")
    appended["source"] = "mootdx_intraday"
    _write_csv(canonical, [_row("09:30:00"), appended], staged=False)
    plan = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    completed = apply_proven_historical_intraday_import(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
        expected_plan_digest=plan["plan_digest"],
        recovery_backup_dir=paths["backup_dir"] / "recovery-mixed-source",
    )

    assert completed["status"] == "completed"
    with canonical.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert {row["source"] for row in rows} == {
        "tushare_proxy_stk_mins",
        "mootdx_intraday",
    }
    assert {row["price_basis"] for row in rows} == {"raw"}


def _market_row(**over: str) -> dict[str, str]:
    row = {
        "open": "10.0",
        "high": "10.1",
        "low": "9.9",
        "close": "10.0",
        "volume": "1000",
        "amount": "10000",
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    ("column", "left", "right", "difference"),
    [
        # Real Round 17 false conflicts. Both differences are far inside
        # COMPARE_QUANTUM["amount"]=1E-6 but land either side of a rounding
        # bucket edge, so quantize-then-compare-text called them different.
        ("amount", "63307628.6200005", "63307628.620000504", "4E-9"),
        ("amount", "2527966793.72", "2527966793.7199993", "7E-7"),
        # Exactly at the quantum must still count as the same value.
        ("amount", "10000", "10000.000001", "1E-6"),
        ("volume", "1000", "1000.000001", "1E-6"),
        ("close", "10.0", "10.00000001", "1E-8"),
    ],
)
def test_same_market_values_treats_sub_quantum_difference_as_identical(
    column: str, left: str, right: str, difference: str
) -> None:
    """A difference at or below the column quantum is not a conflict.

    Quantizing each side independently puts values that straddle a bucket edge
    into different buckets, so comparing the rounded text reported a conflict for
    a difference of 4E-9 against a 1E-6 tolerance.
    """
    from decimal import Decimal

    from quantdesk.intraday.stage6_append_only_import import COMPARE_QUANTUM
    from quantdesk.intraday.stage6_historical_raw_import import _same_market_values

    assert abs(Decimal(left) - Decimal(right)) == Decimal(difference)
    assert Decimal(difference) <= COMPARE_QUANTUM[column]
    assert _same_market_values(
        _market_row(**{column: left}), _market_row(**{column: right})
    )


@pytest.mark.parametrize(
    ("column", "left", "right"),
    [
        ("amount", "10000", "10000.0000011"),
        ("volume", "1000", "1000.0000011"),
        ("amount", "10000", "10001"),
        ("volume", "1000", "1001"),
    ],
)
def test_same_market_values_blocks_difference_above_quantum(
    column: str, left: str, right: str
) -> None:
    from decimal import Decimal

    from quantdesk.intraday.stage6_append_only_import import COMPARE_QUANTUM
    from quantdesk.intraday.stage6_historical_raw_import import _same_market_values

    assert abs(Decimal(left) - Decimal(right)) > COMPARE_QUANTUM[column]
    assert not _same_market_values(
        _market_row(**{column: left}), _market_row(**{column: right})
    )


@pytest.mark.parametrize(
    ("column", "left", "right"),
    [
        ("open", "10.0", "10.000000011"),
        ("high", "10.1", "10.100000011"),
        ("low", "9.9", "9.900000011"),
        ("close", "10.0", "10.000000011"),
    ],
)
def test_same_market_values_blocks_ohlc_difference_above_one_e_minus_eight(
    column: str, left: str, right: str
) -> None:
    from decimal import Decimal

    from quantdesk.intraday.stage6_append_only_import import COMPARE_QUANTUM
    from quantdesk.intraday.stage6_historical_raw_import import _same_market_values

    assert COMPARE_QUANTUM[column] == Decimal("0.00000001")
    assert abs(Decimal(left) - Decimal(right)) > Decimal("0.00000001")
    assert not _same_market_values(
        _market_row(**{column: left}), _market_row(**{column: right})
    )


@pytest.mark.parametrize(
    "bad",
    ["nan", "NaN", "inf", "-inf", "Infinity", "", "   ", "abc", "1,000", "None"],
)
def test_same_market_values_blocks_non_finite_or_unparsable_values(bad: str) -> None:
    """An unusable figure is never proof that two rows agree."""

    from quantdesk.intraday.stage6_historical_raw_import import _same_market_values

    assert not _same_market_values(_market_row(amount=bad), _market_row(amount="10000"))
    assert not _same_market_values(_market_row(amount="10000"), _market_row(amount=bad))
    # Equally unusable on both sides is still not agreement.
    assert not _same_market_values(_market_row(amount=bad), _market_row(amount=bad))


def test_same_market_values_missing_column_is_not_agreement() -> None:
    from quantdesk.intraday.stage6_historical_raw_import import _same_market_values

    complete = _market_row()
    partial = {key: value for key, value in complete.items() if key != "amount"}

    assert not _same_market_values(partial, complete)
    assert not _same_market_values(complete, partial)


def test_historical_raw_plan_accepts_sub_quantum_amount_drift(tmp_path: Path) -> None:
    """End-to-end: the two Round 17 symbols must audit ready, not conflict.

    Uses the observed canonical/staging amount pair from `000725` at
    `2026-07-01 13:55:00`, which differs by 4E-9 against a 1E-6 tolerance.
    """
    from quantdesk.intraday.stage6_append_only_import import (
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path)
    canonical = paths["canonical_dir"] / "600000.csv"
    staging = paths["staging_dir"] / "600000.csv"
    canonical_row = _row("09:30:00")
    canonical_row["amount"] = "63307628.6200005"
    _write_csv(canonical, [canonical_row], staged=False)
    staged_row = _row("09:30:00")
    staged_row["amount"] = "63307628.620000504"
    _write_csv(staging, [staged_row, _row("09:31:00", close="10.05")], staged=True)
    _repoint_provenance(paths["provenance_path"], staging)

    payload = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    assert payload["reason_codes"] == []
    assert payload["status"] == "ready"


def test_historical_raw_plan_still_blocks_above_quantum_amount_conflict(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        build_proven_historical_intraday_import_plan,
    )

    paths = _fixture(tmp_path)
    canonical = paths["canonical_dir"] / "600000.csv"
    staging = paths["staging_dir"] / "600000.csv"
    canonical_row = _row("09:30:00")
    canonical_row["amount"] = "63307628.62"
    _write_csv(canonical, [canonical_row], staged=False)
    staged_row = _row("09:30:00")
    staged_row["amount"] = "63307629.62"
    _write_csv(staging, [staged_row, _row("09:31:00", close="10.05")], staged=True)
    _repoint_provenance(paths["provenance_path"], staging)
    before = canonical.read_bytes()

    payload = build_proven_historical_intraday_import_plan(
        staging_dir=paths["staging_dir"],
        canonical_dir=paths["canonical_dir"],
        provenance_manifest_path=paths["provenance_path"],
        symbol="600000",
    )

    assert payload["status"] == "blocked"
    assert "canonical_staging_timestamp_conflict" in payload["reason_codes"]
    assert canonical.read_bytes() == before
