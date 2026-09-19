from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk.daily_session_evidence import (
    BSE_SOURCE,
    build_bse_session_interval_evidence,
    build_baostock_session_evidence,
    bse_evidence_path,
    evidence_path,
    load_bse_session_interval_evidence,
    load_baostock_session_evidence,
    load_verified_no_session_dates,
    validate_bse_session_interval_evidence,
    validate_baostock_session_evidence,
    write_bse_session_interval_evidence,
    write_baostock_session_evidence,
)


def _valid_evidence() -> dict[str, object]:
    return build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-01",
        query_end_date="2026-07-16",
        records=[
            {"trade_date": "2026-07-15", "tradestatus": "1"},
            {"trade_date": "2026-07-16", "tradestatus": "0"},
        ],
        captured_at="2026-07-16T18:00:00+08:00",
    )


def test_baostock_session_evidence_round_trip_is_strict_and_atomic(
    tmp_path: Path,
) -> None:
    payload = _valid_evidence()

    path = write_baostock_session_evidence(
        payload,
        metadata_dir=tmp_path,
        expected_symbol="600405",
        expected_start_date="2026-07-01",
        expected_end_date="2026-07-16",
    )

    assert path == evidence_path("600405", metadata_dir=tmp_path)
    loaded = load_baostock_session_evidence(
        "600405",
        metadata_dir=tmp_path,
    )
    assert loaded["valid"] is True
    assert loaded["suspended_dates"] == ["2026-07-16"]
    assert loaded["traded_dates"] == ["2026-07-15"]
    assert list(path.parent.glob("*.tmp")) == []


def test_overlapping_refresh_preserves_older_verified_session_records(
    tmp_path: Path,
) -> None:
    older = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-01",
        query_end_date="2026-07-16",
        records=[
            {"trade_date": "2026-07-06", "tradestatus": "0"},
            {"trade_date": "2026-07-10", "tradestatus": "0"},
            {"trade_date": "2026-07-16", "tradestatus": "1"},
        ],
        captured_at="2026-07-16T18:00:00+08:00",
    )
    newer = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-09",
        query_end_date="2026-07-17",
        records=[
            {"trade_date": "2026-07-10", "tradestatus": "0"},
            {"trade_date": "2026-07-16", "tradestatus": "1"},
            {"trade_date": "2026-07-17", "tradestatus": "1"},
        ],
        captured_at="2026-07-17T18:00:00+08:00",
    )
    write_baostock_session_evidence(older, metadata_dir=tmp_path)
    write_baostock_session_evidence(newer, metadata_dir=tmp_path)

    loaded = load_baostock_session_evidence(
        "600405",
        metadata_dir=tmp_path,
    )

    assert loaded["valid"] is True
    assert loaded["suspended_dates"] == [
        "2026-07-06",
        "2026-07-10",
    ]
    assert loaded["traded_dates"] == [
        "2026-07-16",
        "2026-07-17",
    ]
    assert loaded["evidence"]["query_start_date"] == "2026-07-01"
    assert loaded["evidence"]["query_end_date"] == "2026-07-17"


def test_overlapping_provider_correction_replaces_old_status_with_audit(
    tmp_path: Path,
) -> None:
    older = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-15",
        query_end_date="2026-07-16",
        records=[
            {"trade_date": "2026-07-15", "tradestatus": "1"},
            {"trade_date": "2026-07-16", "tradestatus": "0"},
        ],
        captured_at="2026-07-16T18:00:00+08:00",
    )
    corrected = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-16",
        query_end_date="2026-07-17",
        records=[
            {"trade_date": "2026-07-16", "tradestatus": "1"},
            {"trade_date": "2026-07-17", "tradestatus": "1"},
        ],
        captured_at="2026-07-17T18:00:00+08:00",
    )
    write_baostock_session_evidence(older, metadata_dir=tmp_path)
    write_baostock_session_evidence(corrected, metadata_dir=tmp_path)

    loaded = load_baostock_session_evidence(
        "600405",
        metadata_dir=tmp_path,
    )

    assert loaded["valid"] is True
    assert loaded["suspended_dates"] == []
    assert loaded["traded_dates"] == [
        "2026-07-15",
        "2026-07-16",
        "2026-07-17",
    ]
    assert loaded["evidence"]["correction_audit"][-1] == {
        "trade_date": "2026-07-16",
        "previous_tradestatus": "0",
        "corrected_tradestatus": "1",
        "captured_at": "2026-07-17T18:00:00+08:00",
    }


def test_tampered_or_corrupt_sidecar_fails_closed(tmp_path: Path) -> None:
    path = write_baostock_session_evidence(
        _valid_evidence(),
        metadata_dir=tmp_path,
    )
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["records"][1]["tradestatus"] = "1"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    loaded = load_baostock_session_evidence(
        "600405",
        metadata_dir=tmp_path,
    )

    assert loaded["valid"] is False
    assert "records_sha256_mismatch" in loaded["reason_codes"]
    assert loaded["suspended_dates"] == []

    path.write_text("{broken", encoding="utf-8")
    corrupt = load_baostock_session_evidence(
        "600405",
        metadata_dir=tmp_path,
    )
    assert corrupt["valid"] is False
    assert corrupt["suspended_dates"] == []
    assert any(
        reason.startswith("read_failed:")
        for reason in corrupt["reason_codes"]
    )


def test_wrong_symbol_range_and_conflicting_status_are_rejected() -> None:
    wrong_symbol = _valid_evidence()
    wrong_symbol["symbol"] = "600406"
    checked = validate_baostock_session_evidence(
        wrong_symbol,
        expected_symbol="600405",
    )
    assert checked["valid"] is False
    assert "symbol_mismatch" in checked["reason_codes"]
    assert "provider_code_mismatch" in checked["reason_codes"]

    wrong_range = _valid_evidence()
    wrong_range["query_end_date"] = "2026-07-15"
    checked = validate_baostock_session_evidence(wrong_range)
    assert checked["valid"] is False
    assert any(
        reason.startswith("record_outside_query_range:")
        for reason in checked["reason_codes"]
    )

    conflict = _valid_evidence()
    conflict["records"] = [
        {"trade_date": "2026-07-16", "tradestatus": "0"},
        {"trade_date": "2026-07-16", "tradestatus": "1"},
    ]
    checked = validate_baostock_session_evidence(conflict)
    assert checked["valid"] is False
    assert any(
        reason.startswith("record_conflicting_tradestatus:")
        for reason in checked["reason_codes"]
    )

    with pytest.raises(ValueError, match="invalid_baostock_session_evidence"):
        write_baostock_session_evidence(conflict)


def _valid_bse_interval_evidence() -> dict[str, object]:
    return build_bse_session_interval_evidence(
        symbol="920685",
        suspension_start_date="2026-07-16",
        resumption_date="2026-07-30",
        documents=[
            {
                "fact_type": "suspension_start",
                "effective_date": "2026-07-16",
                "publish_date": "2026-07-15",
                "title": "stock suspension announcement",
                "url": (
                    "https://www.bse.cn/disclosure/2026/2026-07-15/"
                    "fc57a07eac18416eb4a1719f673f3a9e.pdf"
                ),
                "sha256": "1" * 64,
            },
            {
                "fact_type": "resumption",
                "effective_date": "2026-07-30",
                "publish_date": "2026-07-29",
                "title": "stock resumption announcement",
                "url": (
                    "https://www.bse.cn/disclosure/2026/2026-07-29/"
                    "b899aab3f3f142e88c6a3c53505a9b5d.pdf"
                ),
                "sha256": "2" * 64,
            },
        ],
        captured_at="2026-08-04T20:05:00+08:00",
    )


def test_bse_interval_evidence_round_trip_binds_documents_and_dates(
    tmp_path: Path,
) -> None:
    payload = _valid_bse_interval_evidence()

    path = write_bse_session_interval_evidence(
        payload,
        metadata_dir=tmp_path,
        expected_symbol="920685",
    )

    assert path == bse_evidence_path("920685", metadata_dir=tmp_path)
    loaded = load_bse_session_interval_evidence(
        "920685",
        metadata_dir=tmp_path,
    )
    assert loaded["valid"] is True
    assert loaded["evidence"]["source"] == BSE_SOURCE
    assert "2026-07-28" in loaded["no_session_dates"]
    assert "2026-07-29" in loaded["no_session_dates"]
    assert "2026-07-30" not in loaded["no_session_dates"]

    dates, evidence, warnings = load_verified_no_session_dates(
        ["920685"],
        metadata_dir=tmp_path,
    )
    assert warnings == []
    assert {"2026-07-28", "2026-07-29"} <= dates["920685"]
    assert evidence["920685"]["source"] == BSE_SOURCE


def test_bse_interval_evidence_tamper_and_incomplete_facts_fail_closed() -> None:
    tampered = _valid_bse_interval_evidence()
    tampered["documents"][0]["sha256"] = "3" * 64
    checked = validate_bse_session_interval_evidence(tampered)
    assert checked["valid"] is False
    assert "evidence_sha256_mismatch" in checked["reason_codes"]
    assert checked["no_session_dates"] == []

    incomplete = _valid_bse_interval_evidence()
    incomplete["documents"] = incomplete["documents"][:1]
    checked = validate_bse_session_interval_evidence(incomplete)
    assert checked["valid"] is False
    assert "resumption_fact_missing" in checked["reason_codes"]
    assert checked["no_session_dates"] == []
