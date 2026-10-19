from __future__ import annotations

import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from quantdesk.data_truth.reference_refresh import (
    FAMILIES, PROVIDER, SOURCE, ReferenceRefreshConfig, plan_reference_refresh,
    prepare_reference_refresh, stage_reference_batch,
)
from quantdesk.data_metadata_schema import TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS
from quantdesk.data_truth import reference_refresh


def _fetch(api_name: str, trade_date: str, **kwargs: object) -> list[dict]:
    if api_name == "suspend_d":
        return []
    row = {"ts_code": "600000.SH", "trade_date": trade_date}
    row.update({"adj_factor": "1.01"} if api_name == "adj_factor" else
               {"pre_close": "10", "up_limit": "11", "down_limit": "9"})
    return [row]


def _config(root: Path) -> ReferenceRefreshConfig:
    return ReferenceRefreshConfig(root / "staging", root / "metadata", ("2026-10-12",))


def _staged(root: Path) -> ReferenceRefreshConfig:
    config = _config(root)
    result = stage_reference_batch(config, _fetch, enable_network=True)
    assert result["status"] == "staged"
    return replace(config, staging_dir=Path(result["staging_dir"]))


def _baseline(config: ReferenceRefreshConfig, day: str = "2026-10-09", price: str = "10") -> dict[str, str]:
    config.metadata_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for family, api in FAMILIES.items():
        path = config.metadata_dir / (family + ".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            columns = TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS[family + ".csv"]
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            if api != "suspend_d":
                row = {"trade_date": day, "symbol": "600000", "provider": PROVIDER, "source": SOURCE}
                row.update({"adj_factor": "1.01"} if api == "adj_factor" else {"pre_close": price, "up_limit": "11", "down_limit": "9"})
                writer.writerow({name: row[name] for name in columns})
        hashes[family] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_network_defaults_off_and_missing_staging_is_a_plan_failure(tmp_path: Path) -> None:
    fetcher = Mock(side_effect=AssertionError("must not fetch"))
    config = _config(tmp_path)
    assert stage_reference_batch(config, fetcher)["request_count"] == 0
    fetcher.assert_not_called()
    assert not config.staging_dir.exists()
    plan = plan_reference_refresh(config)
    assert plan["status"] == "blocked"
    assert plan["canonical_write"] is False
    assert not config.metadata_dir.exists()


def test_all_four_candidates_preserve_baseline_and_empty_suspension_receipt(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    before = _baseline(config)
    plan = plan_reference_refresh(config)
    assert plan["status"] == "ready_for_isolated_preparation"
    assert "not_performed" in plan["baseline_duplicate_verification"]
    result = prepare_reference_refresh(config, tmp_path / "candidates")
    assert result["status"] == "prepared_not_applied"
    assert len(result["files"]) == 4
    for family, proof in result["files"].items():
        assert hashlib.sha256((config.metadata_dir / (family + ".csv")).read_bytes()).hexdigest() == before[family]
        assert proof["baseline_sha256"] == before[family]
    events = result["families"]["suspension_history"]
    assert events["empty_response_dates"] == ["2026-10-12"]
    assert result["files"]["suspension_history"]["appended_rows"] == 0


def test_overlapping_keys_are_idempotent_and_conflicts_block_whole_package(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    original = _baseline(config, day="2026-10-12")
    first = prepare_reference_refresh(config, tmp_path / "candidates")
    assert first["status"] == "prepared_not_applied"
    assert first["files"]["adjustment_factors"]["reused_rows"] == 1
    assert first["files"]["pre_close_history"]["appended_rows"] == 0
    _baseline(config, day="2026-10-12", price="10.5")
    changed = {family: (config.metadata_dir / (family + ".csv")).read_bytes() for family in FAMILIES}
    blocked = prepare_reference_refresh(config, tmp_path / "candidates")
    assert blocked["status"] == "blocked" and blocked["files"] == {}
    assert "existing_key_conflict" in blocked["errors"][0]
    for family in FAMILIES:
        assert (config.metadata_dir / (family + ".csv")).read_bytes() == changed[family]


@pytest.mark.parametrize("change", ["wrong_day", "provider", "empty", "nonpositive"])
def test_bad_response_cannot_create_staging_or_touch_baseline(tmp_path: Path, change: str) -> None:
    config = _config(tmp_path)
    def bad(**kwargs):
        rows = _fetch(**kwargs)
        if change == "empty":
            return []
        if rows:
            rows[0].update({"trade_date": "20260101"} if change == "wrong_day" else
                           {"provider": "baostock"} if change == "provider" else {"adj_factor": "0"})
        return rows
    result = stage_reference_batch(config, bad, enable_network=True)
    assert result["status"] == "blocked"
    assert not config.staging_dir.exists()
    assert not config.metadata_dir.exists()


def test_changed_staging_hash_and_legacy_success_record_are_not_accepted(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    path = config.staging_dir / "adj_factor/20261012.csv"
    path.write_bytes(path.read_bytes().replace(b"1.01", b"2.01"))
    assert plan_reference_refresh(config)["status"] == "blocked"
    receipt = config.staging_dir / "suspend_d/20261012.receipt.json"
    receipt.write_text(json.dumps({"ok": True, "rows": 0}), encoding="utf-8")
    assert plan_reference_refresh(config)["status"] == "blocked"


def test_output_cannot_be_canonical_metadata_and_date_budget_is_bounded(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    _baseline(config)
    with pytest.raises(ValueError, match="disjoint"):
        prepare_reference_refresh(config, config.metadata_dir)
    with pytest.raises(ValueError, match="date_budget"):
        plan_reference_refresh(replace(config, trade_dates=tuple(f"2026-10-{day:02d}" for day in range(1, 7))))


def test_row_budget_stops_before_staging(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_rows_per_response=1)
    def oversized(**kwargs):
        row = _fetch(**kwargs)
        return row * 2
    assert stage_reference_batch(config, oversized, enable_network=True)["status"] == "blocked"
    assert not config.staging_dir.exists()


def test_missing_earlier_dates_are_inserted_before_later_baseline_rows(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    _baseline(config, day="2026-10-13")
    result = prepare_reference_refresh(config, tmp_path / "candidates")
    assert result["status"] == "prepared_not_applied"
    proof = result["files"]["pre_close_history"]
    assert proof["appended_rows"] == 1
    with Path(proof["path"]).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["trade_date"] for row in rows] == ["2026-10-12", "2026-10-13"]


@pytest.mark.parametrize("location", ["staging", "baseline"])
def test_duplicate_column_cannot_silently_choose_a_different_price(tmp_path: Path, location: str) -> None:
    config = _staged(tmp_path)
    _baseline(config)
    path = config.staging_dir / "daily/20261012.csv" if location == "staging" else config.metadata_dir / "pre_close_history.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    rows[0].append("pre_close")
    rows[1].append("900")
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    if location == "staging":
        receipt = path.with_suffix(".receipt.json")
        meta = json.loads(receipt.read_text(encoding="utf-8"))
        meta["csv_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt.write_text(json.dumps(meta), encoding="utf-8")
    assert plan_reference_refresh(config)["status"] == "blocked"
    assert prepare_reference_refresh(config, tmp_path / "candidates")["status"] == "blocked"


def test_old_empty_suspension_receipt_does_not_prove_a_later_day(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    path = config.staging_dir / "suspend_d/20261012.receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["fetched_at"] = "2026-10-09T18:00:00+08:00"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    result = plan_reference_refresh(config)
    assert result["status"] == "blocked"
    assert any("reference_receipt_time_unverified" in reason for reason in result["errors"])


def test_small_files_and_fsync_are_part_of_preparation_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(_staged(tmp_path), budget_seconds=.05)
    _baseline(config)
    elapsed = [0.0]
    fsync = reference_refresh.os.fsync
    def slow_fsync(fd):
        fsync(fd)
        elapsed[0] += .06
    monkeypatch.setattr(reference_refresh.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(reference_refresh.os, "fsync", slow_fsync)
    result = prepare_reference_refresh(config, tmp_path / "candidates")
    assert result["status"] == "blocked" and result["files"] == {}
    assert "reference_prepare_budget_exhausted" in result["errors"][0]


def test_response_byte_limit_is_enforced_before_success_or_staging(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_response_bytes=64)
    result = stage_reference_batch(config, _fetch, enable_network=True)
    assert result["status"] == "blocked"
    assert result["failed_api"] == "stk_limit"
    assert not config.staging_dir.exists()


def test_incomplete_staging_batch_cannot_be_used_even_with_all_receipts(tmp_path: Path) -> None:
    config = _staged(tmp_path)
    (config.staging_dir / "batch.json").write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
    assert plan_reference_refresh(config)["status"] == "blocked"


@pytest.mark.parametrize("invalid", [[], None])
def test_non_object_receipt_returns_blocked_diagnostic(tmp_path: Path, invalid: object) -> None:
    config = _staged(tmp_path)
    (config.staging_dir / "suspend_d/20261012.receipt.json").write_text(json.dumps(invalid), encoding="utf-8")
    result = plan_reference_refresh(config)
    assert result["status"] == "blocked"
    assert any("reference_receipt_must_be_object" in error for error in result["errors"])
