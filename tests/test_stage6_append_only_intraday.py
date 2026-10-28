from __future__ import annotations

import json
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from tests.minute_evidence_fixture import final_minute_fixture


def _legacy_session_frame(symbol: str, target_date: str, *, close_offset: float = 0.0) -> pd.DataFrame:
    times = list(pd.date_range(f"{target_date} 09:31", periods=120, freq="min"))
    times += list(pd.date_range(f"{target_date} 13:01", periods=120, freq="min"))
    rows: list[dict[str, object]] = []
    for index, timestamp in enumerate(times):
        price = 10.0 + index * 0.001
        rows.append(
            {
                "symbol": symbol,
                "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "date": target_date,
                "time": timestamp.strftime("%H:%M:%S"),
                "open": price,
                "high": price + 0.02,
                "low": price - 0.01,
                "close": price + 0.01 + close_offset,
                "volume": 100,
                "amount": (price + 0.01 + close_offset) * 100,
                "price_basis": "raw",
                "source": "akshare_intraday",
                "updated_at": "2026-07-18T08:00:00+00:00",
            }
        )
    return pd.DataFrame(rows)


def _session_frame(symbol: str, target_date: str, *, close_offset: float = 0.0) -> pd.DataFrame:
    return final_minute_fixture(
        _legacy_session_frame(symbol, target_date, close_offset=close_offset), convention="minute_end")


def _write_stage(
    staging_dir: Path,
    frame: pd.DataFrame,
    *,
    price_only_intraday: bool = False,
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    symbol = str(frame.iloc[0]["symbol"])
    staged = frame.copy()
    staged["price_only_intraday"] = bool(price_only_intraday)
    staged.to_csv(
        staging_dir / f"{symbol}.csv",
        index=False,
        encoding="utf-8-sig",
    )


def _passed_audit(symbols: list[str]) -> dict[str, object]:
    return {
        "summary": {
            "complete_session": len(symbols),
            "formal_execution_eligible": len(symbols),
            "missing": 0,
            "partial_session": 0,
            "invalid_session": 0,
        },
        "items": [
            {
                "symbol": symbol,
                "status": "usable",
                "complete_session": True,
                "formal_execution_eligible": True,
                "price_basis": "raw",
                "session_reason_codes": [],
            }
            for symbol in symbols
        ],
    }


def test_concurrent_different_date_appends_do_not_lose_either_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical lock must cover planning, not only the final file swap."""

    from quantdesk.intraday import stage6_append_only_import as importer

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    dates = ("2026-07-13", "2026-07-14")
    staging_by_date: dict[str, Path] = {}
    for target_date in dates:
        staging = tmp_path / f"staging-{target_date}"
        _write_stage(staging, _session_frame("600000", target_date))
        staging_by_date[target_date] = staging

    original_append_candidate = importer._append_candidate
    candidate_count = 0
    candidate_count_guard = threading.Lock()
    release_first_candidate = threading.Event()

    def hold_first_built_candidate(*args: object, **kwargs: object) -> None:
        nonlocal candidate_count
        original_append_candidate(*args, **kwargs)
        with candidate_count_guard:
            candidate_count += 1
            current = candidate_count
        if current == 1:
            release_first_candidate.wait(timeout=1.0)
        else:
            release_first_candidate.set()

    monkeypatch.setattr(importer, "_append_candidate", hold_first_built_candidate)
    start = threading.Barrier(2)

    def append_date(target_date: str) -> dict[str, object]:
        start.wait(timeout=5.0)
        return importer.apply_audited_intraday_batch(
            staging_dir=staging_by_date[target_date],
            canonical_dir=canonical,
            symbols=["600000"],
            target_date=target_date,
            run_id=f"concurrent-{target_date}",
            quality_audit=_passed_audit(["600000"]),
            apply=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        payloads = list(pool.map(append_date, dates))

    saved = pd.read_csv(target, encoding="utf-8-sig", dtype={"symbol": str})
    row_counts = saved.groupby(saved["date"].astype(str)).size().to_dict()
    assert [payload["status"] for payload in payloads] == ["completed", "completed"]
    assert row_counts == {
        "2026-07-10": 240,
        "2026-07-13": 240,
        "2026-07-14": 240,
    }


def test_concurrent_different_date_label_repairs_do_not_revert_each_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_conflict_resolution as resolver

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    target = canonical / "600000.csv"
    dates = ("2026-07-15", "2026-07-16")
    pd.concat(
        [
            _legacy_session_frame("600000", target_date).assign(
                price_basis="unknown",
                source="mootdx_intraday",
            )
            for target_date in dates
        ],
        ignore_index=True,
    ).to_csv(target, index=False, encoding="utf-8-sig")
    staging_by_date: dict[str, Path] = {}
    for target_date in dates:
        staging = tmp_path / f"staging-label-{target_date}"
        _write_stage(staging, _session_frame("600000", target_date))
        staging_by_date[target_date] = staging

    original_replace = resolver._replace_target_basis_bytes
    candidate_count = 0
    candidate_guard = threading.Lock()
    release_first = threading.Event()

    def hold_first_candidate(*args: object, **kwargs: object) -> tuple[bytes, int]:
        nonlocal candidate_count
        candidate = original_replace(*args, **kwargs)
        with candidate_guard:
            candidate_count += 1
            current = candidate_count
        if current == 1:
            release_first.wait(timeout=1.0)
        else:
            release_first.set()
        return candidate

    monkeypatch.setattr(resolver, "_replace_target_basis_bytes", hold_first_candidate)
    start = threading.Barrier(2)

    def repair_date(target_date: str) -> dict[str, object]:
        start.wait(timeout=5.0)
        return resolver.resolve_stage6_intraday_conflicts(
            canonical_dir=canonical,
            staging_dir=staging_by_date[target_date],
            symbols=["600000"],
            target_date=target_date,
            run_id=f"concurrent-label-{target_date}",
            backup_dir=tmp_path / "backups",
            output_path=tmp_path / f"resolution-{target_date}.json",
            apply=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        payloads = list(pool.map(repair_date, dates))

    saved = pd.read_csv(target, encoding="utf-8-sig", dtype={"symbol": str})
    assert [payload["status"] for payload in payloads] == ["completed", "completed"]
    assert set(saved["price_basis"].astype(str)) == {"raw"}


def test_append_importer_reaudits_staging_instead_of_trusting_supplied_pass(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    before = target.read_bytes()
    _write_stage(staging, _session_frame("600000", "2026-07-17").iloc[:10])

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="stale-audit-must-not-pass",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["batch_quality_audit_failed"]
    assert target.read_bytes() == before


def test_append_importer_rejects_price_only_staging_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as importer

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    before = target.read_bytes()
    flat = _session_frame("600000", "2026-07-17").assign(
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
    )
    _write_stage(staging, flat, price_only_intraday=True)
    # Even if an obsolete/tampered audit claims green, the unique canonical
    # importer must independently inspect persisted staging provenance.
    monkeypatch.setattr(
        importer,
        "audit_intraday_batch",
        lambda *_args, **_kwargs: _passed_audit(["600000"]),
    )

    payload = importer.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="price-only-provenance-must-block",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["items"][0]["reason_codes"] == [
        "staging_price_only_provenance_true"
    ]
    assert target.read_bytes() == before


def test_existing_date_unknown_basis_is_conflict_not_idempotent(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    frame = _session_frame("600000", "2026-07-17")
    frame.assign(price_basis="unknown").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    before = target.read_bytes()
    _write_stage(staging, frame)

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="basis-is-part-of-idempotency",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["items"][0]["operation"] == "conflict_existing_date"
    assert target.read_bytes() == before


def test_append_rejects_canonical_header_missing_basis_and_provenance(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").drop(
        columns=["price_basis", "source", "updated_at"]
    ).to_csv(target, index=False, encoding="utf-8-sig")
    before = target.read_bytes()
    _write_stage(staging, _session_frame("600000", "2026-07-17"))

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="canonical-header-contract",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["items"][0]["operation"] == "conflict_invalid_canonical_header"
    assert "canonical_header_missing_required_fields" in payload["items"][0]["reason_codes"]
    assert target.read_bytes() == before


def test_append_failed_rollback_retains_recovery_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as importer

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    before_by_symbol: dict[str, bytes] = {}
    for symbol in ("600000", "600001"):
        target = canonical / f"{symbol}.csv"
        _session_frame(symbol, "2026-07-10").to_csv(
            target,
            index=False,
            encoding="utf-8-sig",
        )
        before_by_symbol[symbol] = target.read_bytes()
        _write_stage(staging, _session_frame(symbol, "2026-07-17"))

    real_replace = importer.os.replace

    def fault_replace(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name == "600001.csv" and "candidates" in source_path.parts:
            raise OSError("injected_second_commit_failure")
        if target_path.name == "600000.csv" and (
            "backups" in source_path.parts or source_path.name.startswith(".restore-")
        ):
            raise OSError("injected_restore_failure")
        real_replace(source, target)

    monkeypatch.setattr(importer.os, "replace", fault_replace)
    payload = importer.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000", "600001"],
        target_date="2026-07-17",
        run_id="retain-failed-rollback",
        quality_audit=_passed_audit(["600000", "600001"]),
        apply=True,
    )

    assert payload["status"] == "failed"
    assert payload["rollback_performed"] is False
    recovery = Path(payload["recovery_transaction_path"])
    assert recovery.exists()
    assert (recovery / "backups" / "600000.csv").read_bytes() == before_by_symbol[
        "600000"
    ]


def test_append_fails_closed_when_canonical_device_differs_from_transaction_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as importer

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    before = target.read_bytes()
    _write_stage(staging, _session_frame("600000", "2026-07-17"))

    real_stat = importer.os.stat
    canonical_resolved = os.path.normcase(str(canonical.resolve()))

    def report_different_canonical_device(path: object, *args: object, **kwargs: object):
        observed = real_stat(path, *args, **kwargs)
        if os.path.normcase(str(path)) == canonical_resolved:
            fields = list(observed)
            fields[2] = observed.st_dev + 1
            return os.stat_result(fields)
        return observed

    monkeypatch.setattr(importer.os, "stat", report_different_canonical_device)
    payload = importer.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="device-mismatch-must-block",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["canonical_transaction_volume_mismatch"]
    assert target.read_bytes() == before
    assert not list(canonical.parent.glob(".append_transaction_*"))


def test_append_reports_pending_transaction_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as importer

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    output = tmp_path / "append.json"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-10").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    _write_stage(staging, _session_frame("600000", "2026-07-17"))

    real_rmtree = importer.shutil.rmtree

    def fail_transaction_cleanup(path: object, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".append_transaction_"):
            raise OSError("injected_transaction_cleanup_failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(importer.shutil, "rmtree", fail_transaction_cleanup)
    payload = importer.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-17",
        run_id="cleanup-failure-is-visible",
        quality_audit=_passed_audit(["600000"]),
        output_path=output,
        apply=True,
    )

    volume_preflight = payload["transaction_volume_preflight"]
    pending = Path(payload["pending_cleanup_path"])
    assert payload["status"] == "completed"
    assert volume_preflight["status"] == "passed"
    assert volume_preflight["canonical_device"] == volume_preflight[
        "transaction_parent_device"
    ]
    assert payload["cleanup_status"] == "failed"
    assert payload["cleanup_warning"] == "append_transaction_cleanup_failed"
    assert payload["warnings"] == ["append_transaction_cleanup_failed"]
    assert payload["cleanup_error"] == "OSError:injected_transaction_cleanup_failure"
    assert pending.exists()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["pending_cleanup_path"] == str(pending)
    assert written["cleanup_warning"] == "append_transaction_cleanup_failed"


def test_conflict_batch_rollback_resets_item_state_and_verifies_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_conflict_resolution as resolver

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    before_by_symbol: dict[str, bytes] = {}
    for symbol in ("600000", "600001"):
        frame = _session_frame(symbol, "2026-07-16")
        target = canonical / f"{symbol}.csv"
        _legacy_session_frame(symbol, "2026-07-16").assign(price_basis="unknown", source="mootdx_intraday").to_csv(
            target,
            index=False,
            encoding="utf-8-sig",
        )
        before_by_symbol[symbol] = target.read_bytes()
        _write_stage(staging, frame)

    real_audit = resolver.audit_intraday_file

    def fail_second_post_audit(symbol: str, target_date: str, **kwargs: object):
        result = real_audit(symbol, target_date, **kwargs)
        intraday_dir = Path(kwargs.get("intraday_dir") or "")
        if symbol == "600001" and intraday_dir.resolve() == canonical.resolve():
            frame = pd.read_csv(canonical / "600001.csv", encoding="utf-8-sig")
            if set(frame["price_basis"].astype(str)) == {"raw"}:
                return {**result, "formal_execution_eligible": False}
        return result

    monkeypatch.setattr(resolver, "audit_intraday_file", fail_second_post_audit)
    payload = resolver.resolve_stage6_intraday_conflicts(
        canonical_dir=canonical,
        staging_dir=staging,
        symbols=["600000", "600001"],
        target_date="2026-07-16",
        run_id="rollback-item-state",
        backup_dir=tmp_path / "backups",
        output_path=tmp_path / "resolution.json",
        apply=True,
    )

    assert payload["status"] == "failed"
    assert payload["rollback_performed"] is True
    for item in payload["items"]:
        symbol = item["symbol"]
        assert (canonical / f"{symbol}.csv").read_bytes() == before_by_symbol[symbol]
        assert item["applied"] is False
        assert item["post_repair_audit"] == {}
        assert item["canonical_sha256_after"] == ""
        assert item["rollback_verified"] is True
    assert not list((canonical / "lifecycle_v1").glob("*.csv"))


def test_section16_label_repair_dry_run_is_ready_and_preserves_apply_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import run_stage6_section16_label_repairs as runner

    target_date = "2026-07-17"
    run_name = "dry-run-evidence"
    canonical = tmp_path / "canonical"
    staging = tmp_path / "runs" / run_name / "intraday_staging" / "critical"
    output = tmp_path / "output"
    canonical.mkdir()
    frame = _session_frame("600000", target_date)
    _legacy_session_frame("600000", target_date).assign(price_basis="unknown", source="mootdx_intraday").to_csv(
        canonical / "600000.csv",
        index=False,
        encoding="utf-8-sig",
    )
    _write_stage(staging, frame)
    monkeypatch.setattr(runner, "DATE_RUNS", {target_date: run_name})
    monkeypatch.setattr(
        runner,
        "AUTHORIZED_SYMBOLS_BY_DATE",
        {target_date: ("600000",)},
    )
    monkeypatch.setattr(runner, "CRITICAL_SYMBOLS", ("600000",))

    payload = runner.run_repairs(
        canonical_dir=canonical,
        runs_root=tmp_path / "runs",
        output_dir=output,
        backup_dir=tmp_path / "backups",
        apply=False,
    )

    assert payload["status"] == "ready"
    assert payload["results"][0]["status"] == "ready"
    assert (output / "preflight_summary.json").exists()
    assert (output / "preflight_quality_audit.json").exists()
    assert not (output / "summary.json").exists()
    assert not (output / "post_repair_quality_audit.json").exists()


def test_append_new_date_preserves_existing_bytes_and_records_provenance(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "data" / "intraday" / "1m"
    staging = tmp_path / "staging"
    canonical.mkdir(parents=True)
    original = (
        "symbol,datetime,date,time,open,high,low,close,volume,amount,price_basis,source,updated_at\n"
        "600000,2026-07-10 15:00:00,2026-07-10,15:00:00,9.9,10.0,9.9,10.0,100,1000,raw,legacy,2026-07-10T08:00:00+00:00\n"
    ).encode("utf-8")
    target = canonical / "600000.csv"
    target.write_bytes(original)
    _write_stage(staging, _session_frame("600000", "2026-07-13"))

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-13",
        run_id="stage6-test",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "completed"
    assert payload["applied"] is True
    assert payload["appended_symbol_count"] == 1
    assert target.read_bytes().startswith(original)
    item = payload["items"][0]
    assert item["operation"] == "append_new_date"
    assert item["target_date"] == "2026-07-13"
    assert item["quality_status"] == "passed"
    assert item["source"] == "akshare_intraday"
    assert item["retrieved_at"] == "2026-07-18T08:00:00+00:00"
    assert len(item["staging_sha256"]) == 64
    assert len(item["canonical_sha256_after"]) == 64


def test_append_missing_older_date_preserves_later_rows_and_audits_target(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    target = canonical / "600000.csv"
    _session_frame("600000", "2026-07-17").to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )
    later_bytes = target.read_bytes()
    _write_stage(staging, _session_frame("600000", "2026-07-13"))

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-13",
        run_id="stage6-historical-gap",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "completed"
    assert payload["items"][0]["operation"] == "append_missing_historical_date"
    assert target.read_bytes().startswith(later_bytes)
    audit = audit_intraday_batch(
        ["600000"],
        "2026-07-13",
        intraday_dir=canonical,
        write=False,
    )
    assert audit["items"][0]["formal_execution_eligible"] is True


def test_one_symbol_date_conflict_quarantines_entire_batch(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    clean_before = (
        "symbol,datetime,date,time,open,high,low,close,volume,amount,source,updated_at\n"
        "600000,2026-07-10 15:00:00,2026-07-10,15:00:00,9.9,10,9.9,10,100,1000,legacy,x\n"
    ).encode("utf-8")
    conflict_frame = _session_frame("600001", "2026-07-13")
    canonical_conflict = conflict_frame.copy()
    canonical_conflict.loc[0, "close"] = 99.0
    (canonical / "600000.csv").write_bytes(clean_before)
    canonical_conflict.drop(columns=["price_basis"]).to_csv(
        canonical / "600001.csv", index=False, encoding="utf-8"
    )
    conflict_before = (canonical / "600001.csv").read_bytes()
    _write_stage(staging, _session_frame("600000", "2026-07-13"))
    _write_stage(staging, conflict_frame)

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000", "600001"],
        target_date="2026-07-13",
        run_id="stage6-conflict",
        quality_audit=_passed_audit(["600000", "600001"]),
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["applied"] is False
    assert payload["quarantined"] is True
    assert "600001" in payload["conflict_symbols"]
    assert (canonical / "600000.csv").read_bytes() == clean_before
    assert (canonical / "600001.csv").read_bytes() == conflict_before
    assert staging.exists()


def test_existing_date_zero_volume_round_trip_is_idempotent(tmp_path: Path) -> None:
    from quantdesk.intraday.intraday_schema import read_intraday_csv
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    unsealed = _legacy_session_frame("600000", "2026-07-13")
    unsealed.loc[unsealed.index[-1], ["volume", "amount"]] = 0
    frame = final_minute_fixture(unsealed, convention="minute_end")
    target = canonical / "600000.csv"
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    before = target.read_bytes()
    normalized = read_intraday_csv(
        target,
        default_symbol="600000",
        target_date="2026-07-13",
    )["frame"]
    _write_stage(staging, normalized)

    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-13",
        run_id="stage6-zero-idempotent",
        quality_audit=_passed_audit(["600000"]),
        apply=True,
    )

    assert payload["status"] == "completed"
    assert payload["items"][0]["operation"] == "unchanged_identical_date"
    assert target.read_bytes() == before


def test_quality_failure_blocks_every_canonical_write(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    _write_stage(staging, _session_frame("600000", "2026-07-13"))
    failed_audit = _passed_audit(["600000"])
    failed_audit["summary"]["complete_session"] = 0
    failed_audit["summary"]["formal_execution_eligible"] = 0
    failed_audit["items"][0]["complete_session"] = False
    failed_audit["items"][0]["formal_execution_eligible"] = False
    failed_audit["items"][0]["session_reason_codes"] = ["session_coverage_below_0_95"]

    output_path = tmp_path / "audit_result.json"
    payload = apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-07-13",
        run_id="stage6-quality-fail",
        quality_audit=failed_audit,
        output_path=output_path,
        apply=True,
    )

    assert payload["status"] == "blocked"
    assert payload["applied"] is False
    assert payload["reason_codes"] == ["batch_quality_audit_failed"]
    assert not (canonical / "600000.csv").exists()
    assert json.loads(output_path.read_text(encoding="utf-8"))["quarantined"] is True


def test_update_engine_reads_canonical_but_materializes_local_evidence_to_staging(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    frame = _session_frame("600000", "2026-07-13")
    target = canonical / "600000.csv"
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    canonical_before = target.read_bytes()

    payload = update_intraday_1m_for_symbols(
        ["600000"],
        "2026-07-13",
        provider_order=["local_existing"],
        source="symbols",
        network=False,
        dry_run=False,
        intraday_dir=staging,
        existing_intraday_dir=canonical,
        materialize_local_existing=True,
        output_path=tmp_path / "update.json",
        require_raw_price_basis=True,
    )

    assert payload["status"] == "ok"
    assert payload["unchanged_symbol_list"] == ["600000"]
    assert (staging / "600000.csv").exists()
    assert target.read_bytes() == canonical_before


def test_canonical_provider_update_uses_audited_importer_and_is_byte_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public provider entrypoint must never append directly to canonical."""

    from quantdesk.intraday import intraday_update_engine as engine

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(engine, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(engine, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(
        engine,
        "INTRADAY_UPDATE_LATEST_JSON",
        tmp_path / "results" / "intraday_update_latest.json",
    )
    calls = {"count": 0}

    def fake_fetch(provider: str, symbol: str, target_date: str, **kwargs: object) -> dict[str, object]:
        calls["count"] += 1
        frame = _session_frame("600000", target_date).assign(
            source="mootdx_intraday",
            updated_at=f"2026-07-19T08:00:{calls['count']:02d}+00:00",
        )
        return {
            "provider": provider,
            "status": "ok",
            "symbol": symbol,
            "target_date": target_date,
            "data": frame,
            "warnings": [],
            "price_basis": "raw",
            "ohlcv_complete": True,
            "signal_eligible": True,
            "signal_capability": "full_minute_ohlcv",
            "volume_semantics": "per_minute",
            "price_only_intraday": False,
            "source_time_order_valid": True,
        }

    monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
    kwargs = {
        "symbols": ["600000"],
        "target_date": "2026-07-17",
        "provider_order": ["mootdx_intraday"],
        "source": "symbols",
        "network": True,
        "intraday_dir": canonical,
        "existing_intraday_dir": canonical,
        "output_path": tmp_path / "results" / "update.json",
        "run_id": "fixed-idempotent-run",
    }

    first = engine.update_intraday_1m_for_symbols(**kwargs)
    assert first["canonical_import"]["status"] == "completed"
    target = canonical / "600000.csv"
    before = target.read_bytes()

    for _ in range(3):
        repeated = engine.update_intraday_1m_for_symbols(**kwargs)
        assert repeated["canonical_import"]["status"] == "completed"
        assert (
            repeated["canonical_import"]["items"][0]["operation"]
            == "unchanged_identical_date"
        )
        assert target.read_bytes() == before


def test_low_level_merge_rejects_real_canonical_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk.intraday import intraday_update_engine as engine

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    monkeypatch.setattr(engine, "INTRADAY_1M_DIR", canonical)
    frame = _session_frame("600000", "2026-07-17")

    with pytest.raises(PermissionError, match="canonical_intraday_direct_write_forbidden"):
        engine._merge_and_write(
            "600000",
            frame,
            append=True,
            overwrite=False,
            base_dir=canonical,
        )


def test_manual_canonical_import_is_audited_and_byte_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk.intraday import intraday_import

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    results = tmp_path / "results"
    source = tmp_path / "manual.csv"
    _session_frame("600000", "2026-07-17").to_csv(
        source, index=False, encoding="utf-8-sig"
    )
    monkeypatch.setattr(intraday_import, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(
        intraday_import,
        "INTRADAY_IMPORT_LATEST_JSON",
        results / "intraday_import_latest.json",
    )

    first = intraday_import.import_intraday_1m_file(
        source,
        target_date="2026-07-17",
        output_path=results / "manual-import.json",
    )
    assert first["canonical_import"]["status"] == "completed"
    target = canonical / "600000.csv"
    before = target.read_bytes()

    second = intraday_import.import_intraday_1m_file(
        source,
        target_date="2026-07-17",
        output_path=results / "manual-import.json",
    )
    assert second["canonical_import"]["items"][0]["operation"] == "unchanged_identical_date"
    assert target.read_bytes() == before


def test_manual_price_only_import_is_rejected_before_staging_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk.intraday import intraday_import

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    results = tmp_path / "results"
    source = tmp_path / "manual-price-only.csv"
    price_only = _session_frame("600000", "2026-07-17")[
        ["symbol", "datetime", "close", "volume"]
    ].rename(columns={"close": "price"})
    price_only.to_csv(source, index=False, encoding="utf-8-sig")
    monkeypatch.setattr(intraday_import, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)

    payload = intraday_import.import_intraday_1m_file(
        source,
        target_date="2026-07-17",
        output_path=results / "manual-price-only.json",
    )

    assert payload["ok"] is False
    assert payload["reason_codes"] == ["price_only_intraday"]
    assert "staging_dir" not in payload
    assert not list((tmp_path / "data" / "vendor_staging").rglob("*.csv"))
    assert not (canonical / "600000.csv").exists()


def test_manual_canonical_import_rejects_replace_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantdesk.intraday import intraday_import

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    source = tmp_path / "manual.csv"
    _session_frame("600000", "2026-07-17").to_csv(
        source, index=False, encoding="utf-8-sig"
    )
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)

    payload = intraday_import.import_intraday_1m_file(
        source,
        target_date="2026-07-17",
        append=False,
        overwrite=True,
        output_path=tmp_path / "result.json",
    )

    assert payload["ok"] is False
    assert payload["reason_codes"] == ["canonical_replace_semantics_forbidden"]
    assert not (canonical / "600000.csv").exists()


def test_formal_update_continues_after_partial_session_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk.intraday import intraday_update_engine as engine

    full = _session_frame("600000", "2026-07-14")
    partial = full.tail(80).reset_index(drop=True)
    calls: list[str] = []

    def fake_fetch(provider, symbol, target_date, **kwargs):
        calls.append(provider)
        frame = partial.copy() if provider == "partial_provider" else full.copy()
        return {
            "status": "ok",
            "data": frame,
            "ohlcv_complete": True,
            "signal_eligible": True,
            "signal_capability": "full_minute_ohlcv",
            "volume_semantics": "per_minute",
            "price_only_intraday": False,
            "source_time_order_valid": True,
            "price_basis": "raw",
            "suggested_next_action": "已获取目标日期分钟线，可执行条件交易计划。",
            "warnings": [],
        }

    monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
    staging = tmp_path / "staging"
    payload = engine.update_intraday_1m_for_symbols(
        ["600000"],
        "2026-07-14",
        provider_order=["partial_provider", "full_provider"],
        source="symbols",
        network=True,
        dry_run=False,
        intraday_dir=staging,
        existing_intraday_dir=tmp_path / "canonical",
        require_raw_price_basis=True,
        require_complete_session=True,
        output_path=tmp_path / "update.json",
    )

    assert calls == ["partial_provider", "full_provider"]
    assert payload["updated_symbol_list"] == ["600000"]
    assert payload["failed_symbol_list"] == []
    staged = pd.read_csv(staging / "600000.csv")
    assert len(staged) == 240
    partial_row = next(
        row for row in payload["provider_results"] if row["provider"] == "partial_provider"
    )
    assert partial_row["formal_execution_eligible"] is False
    assert partial_row["reason_code"] == "formal_session_ineligible"
    assert "可执行条件交易计划" not in partial_row["suggested_next_action"]
    assert "not formal" in partial_row["suggested_next_action"].lower()


def test_batch_audit_accepts_raw_basis_only_for_symbols_proven_by_stage6_manifest(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    frame = _legacy_session_frame("600000", "2026-07-13").drop(columns=["price_basis"])
    frame.to_csv(canonical / "600000.csv", index=False, encoding="utf-8")

    without_provenance = audit_intraday_batch(
        ["600000"], "2026-07-13", intraday_dir=canonical, write=False
    )
    with_provenance = audit_intraday_batch(
        ["600000"],
        "2026-07-13",
        intraday_dir=canonical,
        write=False,
        price_basis_by_symbol={"600000": "raw"},
    )

    assert without_provenance["items"][0]["formal_execution_eligible"] is False
    # Explicit basis provenance supplies only the basis. It never supplies
    # finality, units, or a signed observation chain.
    assert with_provenance["items"][0]["formal_execution_eligible"] is False
    assert with_provenance["items"][0]["price_basis"] == "raw"


def test_stage6_directory_fingerprint_records_files_and_bytes(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import _directory_fingerprint

    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "a.csv").write_bytes(b"abc")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "b.csv").write_bytes(b"12345")

    assert _directory_fingerprint(folder) == {
        "path": str(folder),
        "exists": True,
        "files": 2,
        "bytes": 8,
    }


def test_free_window_status_marks_only_dates_outside_last_five_sessions(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import _free_window_gap_status

    calendar = tmp_path / "calendar.csv"
    calendar.write_text(
        "date,is_open\n"
        "2026-07-13,1\n2026-07-14,1\n2026-07-15,1\n"
        "2026-07-16,1\n2026-07-17,1\n2026-07-20,1\n",
        encoding="utf-8",
    )
    within = _free_window_gap_status(
        "2026-07-13",
        ["600000"],
        calendar_path=calendar,
        current_date=date(2026, 7, 19),
    )
    expired = _free_window_gap_status(
        "2026-07-13",
        ["600000"],
        calendar_path=calendar,
        current_date=date(2026, 7, 20),
    )

    assert within["status"] == "within_free_window"
    assert expired["status"] == "permanently_missing_free_window"
    assert expired["symbols"] == ["600000"]


def test_runtime_state_update_is_structured_and_does_not_edit_governance_docs(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_data_chain import _update_current_state

    path = tmp_path / "docs" / "CURRENT_STATE.md"
    path.parent.mkdir()
    original = (
        "| 日线 `data/daily/` | scope | 2026-07-10 | old |\n"
        "| 1 分钟 `data/intraday/` | scope | 2026-07-10 | old |\n"
        "\n最后更新：2026-07-18 / Codex / old checkpoint\n"
    )
    path.write_text(original, encoding="utf-8")
    payload = _update_current_state(
        tmp_path,
        {
            "run_id": "run-stage6",
            "target_date": "2026-07-17",
            "status": "completed",
            "ok": True,
            "coverage": {
                "daily": {"fresh_count": 10, "requested_count": 10},
                "current_analysis_pool": {"complete_count": 8, "requested_count": 10},
            },
        },
    )
    runtime_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "daily_data_chain_runtime_status_latest.json"
    )
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

    assert payload["status"] == "updated"
    assert payload["path"] == str(runtime_path)
    assert runtime["schema_version"] == "daily_data_chain_runtime_status_v1"
    assert runtime["run_id"] == "run-stage6"
    assert runtime["target_date"] == "2026-07-17"
    assert runtime["source_status"] == "completed"
    assert runtime["coverage"]["daily"]["fresh_count"] == 10
    assert path.read_text(encoding="utf-8") == original


def test_runtime_state_historical_catchup_records_exact_target_without_doc_write(
    tmp_path: Path,
) -> None:
    from quantdesk.daily_data_chain import _update_current_state

    path = tmp_path / "docs" / "CURRENT_STATE.md"
    path.parent.mkdir()
    path.write_text(
        "| 日线 `data/daily/` | scope | 2026-07-17 | old |\n"
        "| 1 分钟 `data/intraday/` | scope | 2026-07-17 | old |\n",
        encoding="utf-8",
    )
    original = path.read_text(encoding="utf-8")

    payload = _update_current_state(
        tmp_path,
        {
            "run_id": "historical-catchup",
            "target_date": "2026-07-14",
            "status": "completed",
            "coverage": {
                "daily": {"fresh_count": 10, "requested_count": 10},
                "current_analysis_pool": {
                    "complete_count": 8,
                    "requested_count": 10,
                },
            },
        },
    )
    runtime = json.loads(Path(payload["path"]).read_text(encoding="utf-8"))

    assert runtime["run_id"] == "historical-catchup"
    assert runtime["target_date"] == "2026-07-14"
    assert path.read_text(encoding="utf-8") == original


def test_sector_refresh_is_network_gated(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import _refresh_sector_snapshot

    payload = _refresh_sector_snapshot(tmp_path, network=False)

    assert payload["status"] == "skipped"
    assert payload["network"] is False


def test_stage6_scheduled_default_is_one_symbol_per_atomic_import_batch() -> None:
    from quantdesk.daily_data_chain import DEFAULT_BATCH_SIZE
    from scripts.run_daily_data_chain import build_parser

    assert DEFAULT_BATCH_SIZE == 1
    assert build_parser().parse_args([]).batch_size == 1


def test_historical_daily_catchup_fetches_the_target_gap_audit_window() -> None:
    from quantdesk.daily_price_update_engine import _fetch_start_date

    assert _fetch_start_date("2026-07-17", "2026-07-13") == "2026-07-06"


def test_section15_cross_source_compare_excludes_opening_and_absent_bars(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_conflict_resolution import compare_intraday_sources

    canonical = tmp_path / "canonical.csv"
    staging = tmp_path / "staging.csv"
    left = _legacy_session_frame("600000", "2026-07-14")
    right = left.copy()
    right = right.loc[right["time"] != "14:59:00"].copy()
    right.loc[right["time"] == "09:31:00", "close"] += 9.0
    right.loc[right["time"] == "09:32:00", "close"] += 0.01
    right.loc[right["time"] == "09:33:00", "high"] += 0.02
    right.loc[right["time"] == "13:01:00", "close"] += 9.0
    right.loc[right["time"] == "10:00:00", "close"] += 0.04
    right.loc[right["time"] == "10:01:00", "close"] += 0.02
    left.to_csv(canonical, index=False, encoding="utf-8-sig")
    right.to_csv(staging, index=False, encoding="utf-8-sig")

    payload = compare_intraday_sources(
        canonical,
        staging,
        symbol="600000",
        target_date="2026-07-14",
    )

    assert payload["common_timestamp_count"] == 239
    assert payload["compared_timestamp_count"] == 233
    assert payload["canonical_only_times"] == ["14:59:00"]
    assert payload["large_diff_count"] == 1
    assert payload["large_diff_timestamps"] == ["10:00:00"]
    assert payload["minimum_absolute_tolerance"] == 0.02
    assert payload["relative_close_tolerance"] == 0.003
    assert payload["excluded_times"] == [
        "09:31:00",
        "09:32:00",
        "09:33:00",
        "09:34:00",
        "09:35:00",
        "13:01:00",
    ]


def test_section15_basis_only_conflict_repairs_only_target_labels_with_backup(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_conflict_resolution import (
        resolve_stage6_intraday_conflicts,
    )

    canonical_dir = tmp_path / "canonical"
    staging_dir = tmp_path / "staging"
    canonical_dir.mkdir()
    frame = _session_frame("600000", "2026-07-14")
    canonical_frame = pd.concat(
        [
            _legacy_session_frame("600000", "2026-07-13").assign(price_basis="unknown"),
            _legacy_session_frame("600000", "2026-07-14").assign(price_basis="unknown", source="mootdx_intraday"),
        ],
        ignore_index=True,
    )
    target = canonical_dir / "600000.csv"
    canonical_frame.to_csv(target, index=False, encoding="utf-8-sig")
    _write_stage(staging_dir, frame)
    before = target.read_bytes()

    payload = resolve_stage6_intraday_conflicts(
        canonical_dir=canonical_dir,
        staging_dir=staging_dir,
        symbols=["600000"],
        target_date="2026-07-14",
        run_id="section15-label-repair",
        backup_dir=tmp_path / "backups",
        output_path=tmp_path / "resolution.json",
        apply=True,
    )

    assert payload["status"] == "completed"
    assert payload["items"][0]["decision"] == "bounded_price_basis_label_repair"
    assert payload["items"][0]["large_diff_count"] == 0
    assert Path(payload["items"][0]["comparison_artifact_path"]).exists()
    assert payload["items"][0]["non_label_sha256_before"] == payload["items"][0][
        "non_label_sha256_after"
    ]
    assert payload["items"][0]["canonical_sha256_before"] == hashlib.sha256(before).hexdigest()
    assert Path(payload["items"][0]["backup_path"]).read_bytes() == before
    assert payload["items"][0]["post_repair_audit"]["formal_execution_eligible"] is True
    repaired = pd.read_csv(target, dtype={"symbol": str})
    assert set(repaired.loc[repaired["date"] == "2026-07-13", "price_basis"]) == {
        "unknown"
    }
    assert set(repaired.loc[repaired["date"] == "2026-07-14", "price_basis"]) == {
        "raw"
    }


def test_section16_label_repair_supports_approved_symbol_date_scope_atomically(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_conflict_resolution import (
        resolve_stage6_intraday_conflicts,
    )

    canonical_dir = tmp_path / "canonical"
    staging_root = tmp_path / "staging"
    canonical_dir.mkdir()
    target = canonical_dir / "600000.csv"
    canonical = pd.concat(
        [
            _legacy_session_frame("600000", date_text).assign(
                price_basis="unknown", source="mootdx_intraday"
            )
            for date_text in ("2026-07-14", "2026-07-15", "2026-07-16")
        ],
        ignore_index=True,
    )
    canonical.to_csv(target, index=False, encoding="utf-8-sig")
    for date_text in ("2026-07-14", "2026-07-15", "2026-07-16"):
        staging_dir = staging_root / date_text
        _write_stage(staging_dir, _session_frame("600000", date_text))
    payloads = []
    for date_text in ("2026-07-14", "2026-07-15", "2026-07-16"):
        payloads.append(
            resolve_stage6_intraday_conflicts(
                canonical_dir=canonical_dir,
                staging_dir=staging_root / date_text,
                symbols=["600000"],
                target_date=date_text,
                run_id=f"section16-label-{date_text}",
                backup_dir=tmp_path / "backups",
                output_path=tmp_path / f"resolution-{date_text}.json",
                apply=True,
            )
        )

    assert all(payload["status"] == "completed" for payload in payloads)
    items = [payload["items"][0] for payload in payloads]
    assert len({item["comparison_artifact_path"] for item in items}) == 3
    assert all(item["decision"] == "bounded_price_basis_label_repair" for item in items)
    assert all(
        item["non_label_sha256_before"] == item["non_label_sha256_after"]
        for item in items
    )
    assert all(Path(item["backup_path"]).exists() for item in items)
    repaired = pd.read_csv(target, dtype={"symbol": str})
    assert set(repaired["price_basis"]) == {"raw"}


def test_section15_large_diff_is_zero_write_and_stays_quarantined(tmp_path: Path) -> None:
    from quantdesk.intraday.stage6_conflict_resolution import (
        resolve_stage6_intraday_conflicts,
    )

    canonical_dir = tmp_path / "canonical"
    staging_dir = tmp_path / "staging"
    canonical_dir.mkdir()
    canonical = _legacy_session_frame("600000", "2026-07-14").assign(
        price_basis="unknown", source="mootdx_intraday"
    )
    staging = _legacy_session_frame("600000", "2026-07-14")
    staging.loc[staging["time"] == "10:00:00", "close"] += 0.04
    target = canonical_dir / "600000.csv"
    canonical.to_csv(target, index=False, encoding="utf-8-sig")
    _write_stage(staging_dir, staging)
    before = target.read_bytes()

    payload = resolve_stage6_intraday_conflicts(
        canonical_dir=canonical_dir,
        staging_dir=staging_dir,
        symbols=["600000"],
        target_date="2026-07-14",
        run_id="section15-unclassified",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert payload["status"] == "external_review_required"
    assert payload["items"][0]["decision"] == "unclassified_cross_source_mismatch"
    assert payload["items"][0]["quarantine_status"] == "redundant_alternative_source_evidence"
    assert payload["items"][0]["large_diff_count"] == 1
    assert target.read_bytes() == before
    assert not list((tmp_path / "backups").rglob("*.csv"))
    assert Path(payload["items"][0]["comparison_artifact_path"]).exists()


def test_section15_true_quality_failure_allows_bounded_target_date_replacement(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_conflict_resolution import (
        resolve_stage6_intraday_conflicts,
    )

    canonical_dir = tmp_path / "canonical"
    staging_dir = tmp_path / "staging"
    canonical_dir.mkdir()
    prior = _legacy_session_frame("600000", "2026-07-13").assign(price_basis="raw")
    bad = _legacy_session_frame("600000", "2026-07-14").assign(price_basis="raw")
    bad.loc[bad["time"] == "10:00:00", "high"] = 1.0
    good = _session_frame("600000", "2026-07-14")
    target = canonical_dir / "600000.csv"
    pd.concat([prior, bad], ignore_index=True).to_csv(
        target, index=False, encoding="utf-8-sig"
    )
    _write_stage(staging_dir, good)
    before = target.read_bytes()

    payload = resolve_stage6_intraday_conflicts(
        canonical_dir=canonical_dir,
        staging_dir=staging_dir,
        symbols=["600000"],
        target_date="2026-07-14",
        run_id="section15-bounded-replacement",
        backup_dir=tmp_path / "backups",
        apply=True,
    )

    assert payload["status"] == "completed"
    assert payload["items"][0]["decision"] == "bounded_target_date_replacement"
    assert Path(payload["items"][0]["backup_path"]).read_bytes() == before
    repaired = pd.read_csv(target, dtype={"symbol": str})
    prior_after = repaired.loc[repaired["date"] == "2026-07-13"].reset_index(drop=True)
    pd.testing.assert_frame_equal(prior.reset_index(drop=True), prior_after, check_dtype=False)
    assert float(repaired.loc[repaired["time"] == "10:00:00", "high"].iloc[-1]) > 1.0


def test_section15_critical_append_conflict_isolated_from_global_fetch_gate() -> None:
    from quantdesk.daily_data_chain import _isolated_critical_append_conflict

    isolated = _isolated_critical_append_conflict(
        {
            "stage6_append_only": True,
            "canonical_import": {
                "status": "blocked",
                "quarantined": True,
                "reason_codes": ["batch_contains_canonical_conflict"],
                "conflict_symbols": ["600000"],
            },
        }
    )
    quality_failure = _isolated_critical_append_conflict(
        {
            "stage6_append_only": True,
            "canonical_import": {
                "status": "blocked",
                "quarantined": True,
                "reason_codes": ["batch_quality_audit_failed"],
            },
        }
    )

    assert isolated["status"] == "isolated"
    assert isolated["global_fetch_blocked"] is False
    assert isolated["conflict_symbols"] == ["600000"]
    assert quality_failure["status"] == "not_applicable"
    assert quality_failure["global_fetch_blocked"] is True
