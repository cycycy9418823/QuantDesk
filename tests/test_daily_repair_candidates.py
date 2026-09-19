"""Offline fixtures for the NUL repair candidate's preservation contract."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from quantdesk.daily_repair_candidate import prepare_daily_repair_candidate

HEADER = "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at"
NUL = b"\x00" * 227 + b"\n"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _row(day: str, **changes: str) -> dict[str, str]:
    row = dict(zip(HEADER.split(","), (
        day, "002633", "10.00", "11", "9", "10", "100", "1000",
        "0", "0", "raw", "baostock", "2026-09-10T16:00:00+08:00",
    )))
    row.update(changes)
    return row


def _csv(*rows: dict[str, str], eol: str = "\n", bom: bool = False) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=HEADER.split(","), lineterminator=eol)
    writer.writeheader()
    writer.writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + handle.getvalue().encode("utf-8")


def _damage(clean: bytes, nul: bytes = NUL) -> bytes:
    lines = clean.splitlines(keepends=True)
    return b"".join(lines[:2]) + nul + b"".join(lines[2:])


def _calendar(*days: tuple[str, bool], source: str = "baostock.query_trade_dates") -> bytes:
    rows = ["date,is_open,source,note\n"]
    for day, is_open in days or (("2026-09-09", True), ("2026-09-10", True)):
        rows.append(f"{day},{str(is_open).lower()},{source},{'trading_day' if is_open else 'non_trading_day'}\n")
    return "".join(rows).encode()


def _prepare(original: bytes | None = None, **changes: Any):
    original = original if original is not None else _damage(_csv(_row("2026-09-07"), _row("2026-09-08")))
    arguments = dict(
        symbol="002633", original_bytes=original, expected_sha256=_sha(original),
        start_date="2026-09-09", target_date="2026-09-10", calendar_bytes=_calendar(),
        supplemental_bytes=_csv(_row("2026-09-09"), _row("2026-09-10")),
    )
    arguments.update(changes)
    return prepare_daily_repair_candidate(**arguments)


def _blocked(result, reason: str) -> None:
    assert result.candidate_bytes is None
    assert result.manifest["status"] == "blocked"
    assert result.manifest["ok"] is False
    assert any(reason in item for item in result.manifest["reasons"]), result.manifest
    assert result.manifest["applied"] is False
    assert result.manifest["canonical_write"] is False


def test_ready_candidate_preserves_original_bytes_bom_eol_and_quarantine_offsets() -> None:
    clean = _csv(_row("2026-09-07", source="历史行情"), _row("2026-09-08"), eol="\r\n", bom=True)
    nul = b"\x00" * 227 + b"\r\n"
    original = _damage(clean, nul)
    result = _prepare(original)
    manifest = result.manifest
    assert manifest["status"] == "candidate_ready"
    assert manifest["ok"] is True
    assert result.candidate_bytes.startswith(clean)
    assert b"\x00" not in result.candidate_bytes
    assert manifest["candidate_sha256"] == _sha(result.candidate_bytes)
    assert manifest["retained_history_sha256"] == _sha(clean)
    assert manifest["added_dates"] == ["2026-09-09", "2026-09-10"]
    assert manifest["missing_dates"] == []
    assert manifest["coverage_complete"] is True
    assert manifest["original_status"] == "damaged"
    for name in ("canonical_write", "network_effective", "applied", "repair_complete"):
        assert manifest[name] is False
    nul_evidence = manifest["quarantined_records"][0]
    offset, length = nul_evidence["byte_offset"], nul_evidence["byte_length"]
    assert original[offset:offset + length] == nul
    assert nul_evidence["sha256"] == _sha(nul)
    retained = []
    for evidence in manifest["retained_original_segments"]:
        before, after, size = evidence["original_offset"], evidence["candidate_offset"], evidence["byte_length"]
        before_bytes = original[before:before + size]
        after_bytes = result.candidate_bytes[after:after + size]
        assert before_bytes == after_bytes
        assert evidence["sha256"] == _sha(before_bytes)
        retained.append(after_bytes)
    assert b"".join(retained) == clean
    json.dumps(manifest)  # The CLI can persist the evidence without an encoder.


def test_missing_middle_dates_are_inserted_without_rewriting_history() -> None:
    clean = _csv(_row("2026-09-07"), _row("2026-09-11")).removesuffix(b"\n")
    result = _prepare(_damage(clean))
    rows = list(csv.DictReader(io.StringIO(result.candidate_bytes.decode())))
    assert [row["date"] for row in rows] == ["2026-09-07", "2026-09-09", "2026-09-10", "2026-09-11"]
    assert result.candidate_bytes.endswith(clean.splitlines()[-1])
    assert result.manifest["history_preserved"] is True


def test_source_without_final_newline_gets_a_separator_without_changing_old_bytes() -> None:
    clean = _csv(_row("2026-09-07"), _row("2026-09-08")).removesuffix(b"\n")
    original = _damage(clean)
    result = _prepare(original)
    assert result.manifest["status"] == "candidate_ready"
    assert result.candidate_bytes.startswith(clean + b"\n")
    assert result.manifest["retained_history_sha256"] == _sha(clean)


@pytest.mark.parametrize("nul", [b"\x00", b"\x00\n\x00\n", b"\x00\r\n"])
def test_independent_terminal_or_repeated_nul_records_are_quarantined(nul: bytes) -> None:
    clean = _csv(_row("2026-09-07"), _row("2026-09-08"))
    result = _prepare(clean + nul)
    assert result.manifest["status"] == "candidate_ready"
    assert result.candidate_bytes.startswith(clean)


@pytest.mark.parametrize("bad_record,reason", [
    (b"2026-09-08,002633,10\x00,11,9,10,100,1000,0,0,raw,baostock,x\n", "nul_in_record"),
    (b'"2026-09-08\n\x00\x00\n",002633,10,11,9,10,100,1000,0,0,raw,baostock,x\n', "csv_unreadable"),
    (b"\xff\n", "csv_unreadable"),
    (b"2026-09-08,002633,10\n", "column_mismatch"),
    (b"\n", "column_mismatch"),
    (b'"unfinished\n', "csv_unreadable"),
])
def test_other_bad_original_records_never_get_removed(bad_record: bytes, reason: str) -> None:
    original = _damage(_csv(_row("2026-09-07"))) + bad_record
    _blocked(_prepare(original), reason)


@pytest.mark.parametrize("header", [
    HEADER.replace("adjust,", "price_basis,"),
    HEADER + ",adj_close",
    HEADER.replace("source,", "adjust,"),
    "date,symbol,open,high,low,close,volume",
])
def test_unsupported_or_ambiguous_header_is_not_normalized(header: str) -> None:
    original = _damage(_csv(_row("2026-09-07")))
    original = original.replace(HEADER.encode(), header.encode(), 1)
    _blocked(_prepare(original), "mismatch")


@pytest.mark.parametrize("role", ["original", "supplemental"])
@pytest.mark.parametrize("adjust", ["unknown", "", "qfq", "hfq"])
def test_entire_input_price_basis_is_checked_before_filtering(role: str, adjust: str) -> None:
    # A bad row outside the requested dates must not disappear in date filtering.
    rows = _csv(_row("2026-09-07", adjust=adjust), _row("2026-09-09"), _row("2026-09-10"))
    if role == "original":
        result = _prepare(_damage(rows))
    else:
        result = _prepare(supplemental_bytes=rows)
    _blocked(result, f"{role}_price_basis_rejected")


def test_unknown_basis_on_an_invalid_date_is_not_hidden_by_date_cleanup() -> None:
    rows = _csv(_row("", adjust="unknown"), _row("2026-09-09"), _row("2026-09-10"))
    _blocked(_prepare(supplemental_bytes=rows), "supplemental_price_basis_rejected")


def test_invalid_supplemental_bar_outside_requested_range_is_not_filtered_away() -> None:
    rows = _csv(_row("2026-09-07", close="inf"), _row("2026-09-09"), _row("2026-09-10"))
    _blocked(_prepare(supplemental_bytes=rows), "supplemental_nonfinite_or_invalid")


def test_nul_in_supplemental_artifact_is_not_repaired_implicitly() -> None:
    rows = _damage(_csv(_row("2026-09-09"), _row("2026-09-10")))
    _blocked(_prepare(supplemental_bytes=rows), "supplemental_nul_in_record")


@pytest.mark.parametrize("field,value,reason", [
    ("open", "inf", "nonfinite_or_invalid"),
    ("close", "NaN", "nonfinite_or_invalid"),
    ("volume", "1e400", "nonfinite_or_invalid"),
    ("amount", "-inf", "nonfinite_or_invalid"),
    ("change", "inf", "nonfinite_or_invalid"),
    ("pct_chg", "-inf", "nonfinite_or_invalid"),
    ("amount", "-1", "negative_amount"),
    ("volume", "0", "invalid_bar"),
    ("high", "9.5", "invalid_bar"),
    ("low", "10.5", "invalid_bar"),
    ("symbol", "000001", "symbol_mismatch"),
    ("date", "2026-09-31", "invalid_date"),
    ("source", "", "source_missing"),
])
def test_invalid_historical_record_blocks_even_outside_requested_interval(field: str, value: str, reason: str) -> None:
    bad = _row("2026-09-07")
    bad[field] = value
    _blocked(_prepare(_damage(_csv(bad, _row("2026-09-08")))), reason)


@pytest.mark.parametrize("role", ["original", "supplemental"])
@pytest.mark.parametrize("days,reason", [
    (("2026-09-09", "2026-09-09"), "duplicate_dates"),
    (("2026-09-10", "2026-09-09"), "unordered_dates"),
])
def test_duplicate_or_unsorted_dates_are_not_silently_normalized(role: str, days: tuple[str, str], reason: str) -> None:
    content = _csv(*(_row(day) for day in days))
    result = _prepare(_damage(content)) if role == "original" else _prepare(supplemental_bytes=content)
    _blocked(result, reason)


def test_empty_optional_metadata_is_preserved_without_filling_zeroes() -> None:
    clean = _csv(_row("2026-09-07", amount="", change="", pct_chg=""), _row("2026-09-08"))
    result = _prepare(_damage(clean))
    assert result.manifest["status"] == "candidate_ready"
    assert result.candidate_bytes.startswith(clean)


def test_overlap_price_conflicts_block_but_equivalent_decimal_spelling_does_not() -> None:
    conflict = _csv(_row("2026-09-08", close="10.1"), _row("2026-09-09"), _row("2026-09-10"))
    _blocked(_prepare(supplemental_bytes=conflict), "supplemental_history_conflict:2026-09-08:close")
    equivalent = _csv(_row("2026-09-08", close="10.000000", source="another_local_provider"), _row("2026-09-09"), _row("2026-09-10"))
    assert _prepare(supplemental_bytes=equivalent).manifest["status"] == "candidate_ready"


@pytest.mark.parametrize("role", ["original", "supplemental"])
def test_decimal_unrepresentable_numeric_is_blocked_without_escaping(role: str) -> None:
    # float accepts this as finite zero, but Decimal cannot represent its
    # exponent. Neither a retained row nor overlapping evidence may pass it.
    bad = _row("2026-09-08", pct_chg="0e9999999999999999999999999")
    if role == "original":
        result = _prepare(_damage(_csv(_row("2026-09-07"), bad)))
    else:
        result = _prepare(supplemental_bytes=_csv(bad, _row("2026-09-09"), _row("2026-09-10")))
    _blocked(result, f"{role}_nonfinite_or_invalid:2026-09-08:pct_chg")


@pytest.mark.parametrize("calendar", [
    None,
    b"date,is_open,source,note\n2026-09-09,true,unknown,trading_day\n",
    _calendar(source="weekend_only"),
    _calendar().replace(b"trading_day", b"provisional_trading_day"),
    _calendar().replace(b",true,", b",perhaps,"),
    _calendar() + b"2026-09-09,true,baostock.query_trade_dates,trading_day\n",
    b"date,is_open,source,note\n2026-09-09,true\n",
])
def test_unverified_calendar_returns_only_structural_candidate(calendar: bytes | None) -> None:
    clean = _csv(_row("2026-09-07"), _row("2026-09-08"))
    result = _prepare(_damage(clean), calendar_bytes=calendar)
    assert result.manifest["status"] == "structural_candidate_only"
    assert result.manifest["ok"] is False
    assert result.candidate_bytes == clean
    assert result.manifest["added_dates"] == []
    assert result.manifest["missing_dates"] is None
    assert result.manifest["coverage_verified"] is False
    assert result.manifest["coverage_complete"] is False


def test_calendar_must_cover_every_requested_day_not_just_endpoints() -> None:
    calendar = _calendar(("2026-09-09", True), ("2026-09-11", True))
    result = _prepare(calendar_bytes=calendar, target_date="2026-09-11")
    assert result.manifest["status"] == "structural_candidate_only"
    assert result.manifest["calendar"]["missing_dates"] == ["2026-09-10"]
    assert "calendar_coverage_incomplete" in result.manifest["reasons"]


def test_provisional_calendar_outside_requested_interval_does_not_hide_valid_evidence() -> None:
    calendar = _calendar() + b"2027-01-01,false,provisional,unverified\n"
    assert _prepare(calendar_bytes=calendar).manifest["status"] == "candidate_ready"


def test_verified_closed_days_are_not_fabricated_as_missing_bars() -> None:
    calendar = _calendar(("2026-09-11", True), ("2026-09-12", False), ("2026-09-13", False), ("2026-09-14", True))
    supplement = _csv(_row("2026-09-11"), _row("2026-09-14"))
    result = _prepare(start_date="2026-09-11", target_date="2026-09-14", calendar_bytes=calendar, supplemental_bytes=supplement)
    assert result.manifest["status"] == "candidate_ready"
    assert result.manifest["added_dates"] == ["2026-09-11", "2026-09-14"]
    bad = _csv(_row("2026-09-11"), _row("2026-09-12"), _row("2026-09-14"))
    _blocked(_prepare(start_date="2026-09-11", target_date="2026-09-14", calendar_bytes=calendar, supplemental_bytes=bad), "bar_on_verified_closed_session")


@pytest.mark.parametrize("supplement", [None, _csv(_row("2026-09-09")), _csv(_row("2026-09-11"))])
def test_missing_or_partial_supplement_is_not_presented_as_complete(supplement: bytes | None) -> None:
    clean = _csv(_row("2026-09-07"), _row("2026-09-08"))
    result = _prepare(_damage(clean), supplemental_bytes=supplement)
    assert result.manifest["status"] == "structural_candidate_only"
    assert result.candidate_bytes == clean
    assert result.manifest["missing_dates"] == ["2026-09-09", "2026-09-10"]
    assert result.manifest["added_dates"] == []
    assert result.manifest["coverage_complete"] is False


def test_removing_nul_is_not_enough_to_assert_repair_without_external_evidence() -> None:
    result = _prepare(_damage(_csv(_row("2026-09-09"), _row("2026-09-10"))), supplemental_bytes=None)
    assert result.manifest["status"] == "structural_candidate_only"
    assert result.manifest["missing_dates"] == []
    assert "supplemental_missing" in result.manifest["reasons"]


@pytest.mark.parametrize("changes,reason", [
    ({"expected_sha256": "0" * 64}, "original_hash_mismatch"),
    ({"expected_sha256": "not-a-hash"}, "invalid_expected_sha256"),
    ({"symbol": "../../002633"}, "invalid_symbol"),
    ({"start_date": "2026-9-9"}, "start_invalid_date"),
    ({"target_date": "2026-09-31"}, "target_invalid_date"),
    ({"start_date": "2026-09-11"}, "reversed_date_range"),
    ({"supplemental_bytes": "path.csv"}, "input_bytes_required"),
])
def test_bad_request_never_produces_candidate(changes: dict[str, Any], reason: str) -> None:
    _blocked(_prepare(**changes), reason)


def test_repeating_repair_on_an_intact_candidate_does_not_produce_another_repair() -> None:
    result = _prepare()
    _blocked(_prepare(result.candidate_bytes), "no_isolated_nul_records")


@contextmanager
def _path_io_forbidden():
    """Forbid Path-level file I/O for the duration of the block.

    Scoped deliberately instead of using monkeypatch: monkeypatch would keep the
    patch alive into teardown, where the session state tripwire hashes its own
    protected files, so its own ``Path.open`` call would explode as an
    INTERNALERROR and abort the whole pytest session.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate generation attempted file I/O")

    originals = (Path.open, Path.read_bytes, Path.write_bytes)
    Path.open = forbidden
    Path.read_bytes = forbidden
    Path.write_bytes = forbidden
    try:
        yield
    finally:
        Path.open, Path.read_bytes, Path.write_bytes = originals


def test_candidate_generation_is_deterministic_and_does_not_open_paths() -> None:
    with _path_io_forbidden():
        first = _prepare()
        second = _prepare()
    assert first == second
    assert first.manifest["status"] == "candidate_ready"


def test_final_candidate_validation_failure_cannot_leave_a_success_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    import quantdesk.daily_repair_candidate as module

    classify = module.classify_frame_price_basis

    def reject_combined_candidate(frame):
        if len(frame) == 4:
            return "mixed_or_unknown", "unknown_price_basis"
        return classify(frame)

    monkeypatch.setattr(module, "classify_frame_price_basis", reject_combined_candidate)
    result = _prepare()
    _blocked(result, "candidate_price_basis_rejected")
    assert result.manifest["coverage_complete"] is False
    assert result.manifest["added_dates"] == []
    assert result.manifest["missing_dates"] == ["2026-09-09", "2026-09-10"]
    assert result.manifest["candidate_sha256"] is None
