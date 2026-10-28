"""Offline minute observations: revision provenance and quality propagation."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.intraday.minute_bar_lifecycle import (
    FORMAL_EVIDENCE_STANDARD, bar_evidence_status, normalize_bar_observation, observation_hash,
)
from quantdesk.intraday.intraday_quality_audit import audit_intraday_file
from quantdesk.intraday.intraday_schema import read_intraday_csv, build_intraday_quality_gate
from quantdesk.intraday.intraday_schema import normalize_intraday_frame
from quantdesk.intraday.minute_pool_window_audit import (
    audit_minute_pool_window, load_window_verified_minute_pool, window_pool_covers,
)
from quantdesk.intraday_refresh import collector
from quantdesk.tushare_minute_quality import _is_clean_pass_quality_payload, _overall_status

DAY = "2026-10-23"


def bar(stamp: str = f"{DAY} 09:31:00", *, finality: str = "provisional", **extra):
    end = datetime.fromisoformat(stamp)
    return dict(symbol="600000", datetime=stamp, date=stamp[:10], time=stamp[11:],
                open=10.0, high=10.5, low=9.5, close=10.0, volume=100.0,
                amount=1000.0, price_basis="raw", source="offline_fixture", updated_at=f"{DAY}T15:01:00+08:00",
                provider="offline_fixture", finality=finality,
                timestamp_convention="minute_end", volume_unit="shares", amount_unit="CNY",
                volume_semantics="per_minute", aggregation_method="provider_bar",
                source_quote_time=end.isoformat() + "+08:00", **extra)


def append(path, rows, now=f"{DAY}T15:01:00+08:00"):
    return collector._append_rows_with_latest(path, rows, now_iso=now)


def view(path, as_of=""):
    payload = read_intraday_csv(path, default_symbol="600000", as_of=as_of)
    assert payload["ok"], payload.get("errors")
    return payload["frame"]


def test_provisional_updates_and_explicit_final_keep_prior_versions(tmp_path):
    path = tmp_path / "600000.csv"
    first = bar()
    assert append(path, [first], f"{DAY}T09:31:10+08:00")[0] == 1
    old_bytes = path.read_bytes()
    second = {**first, "close": 10.2}
    assert append(path, [second], f"{DAY}T09:31:20+08:00")[0] == 1
    final = {**second, "close": 10.3, "finality": "final"}
    assert append(path, [final], f"{DAY}T09:31:30+08:00")[0] == 1
    assert path.read_bytes().startswith(old_bytes)
    assert len(list(csv.DictReader(path.open(encoding="utf-8")))) == 3
    assert view(path).iloc[0]["close"] == 10.3
    assert view(path, f"{DAY}T09:31:15+08:00").iloc[0]["close"] == 10.0
    assert view(path, f"{DAY}T09:31:25+08:00").iloc[0]["close"] == 10.2
    assert view(path, f"{DAY}T09:30:00+08:00").empty


def test_final_duplicate_idempotent_and_correction_requires_parent_proof(tmp_path):
    path = tmp_path / "600000.csv"
    first = bar(finality="final")
    assert append(path, [first])[0] == 1
    original = path.read_bytes()
    assert append(path, [first])[0] == 0
    assert path.read_bytes() == original
    assert append(path, [{**first, "close": 10.3}])[0] == 0
    assert append(path, [{**first, "finality": "provisional"}])[0] == 0
    assert path.read_bytes() == original
    parent = view(path).iloc[0]["evidence_hash"]
    corrected = {**first, "close": 10.3, "revision": 2,
                 "supersedes_hash": parent, "revision_reason": "official_post_close_correction"}
    assert append(path, [corrected], f"{DAY}T16:01:00+08:00")[0] == 1
    latest = view(path).iloc[0]
    assert latest["finality"] == "revised" and int(latest["revision"]) == 2
    assert view(path, f"{DAY}T15:30:00+08:00").iloc[0]["close"] == 10.0
    before = path.read_bytes()
    assert append(path, [corrected], f"{DAY}T16:02:00+08:00")[0] == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("flag", ["false", "0", False])
def test_false_is_not_final_and_contradictory_flags_rejected(flag):
    row = bar()
    row["is_final"] = flag
    item = normalize_bar_observation(row, fetched_at=f"{DAY}T15:01:00+08:00")
    assert item["is_final"] is False
    assert not bar_evidence_status(item)["signal_eligible"]
    with pytest.raises(ValueError, match="conflicting_finality"):
        normalize_bar_observation({**row, "finality": "final"}, fetched_at=f"{DAY}T15:01:00+08:00")


def test_elapsed_time_and_provider_name_never_supply_finality_or_units():
    row = bar()
    for key in ("finality", "volume_unit", "timestamp_convention"):
        row.pop(key)
    item = normalize_bar_observation(row, fetched_at="2026-10-25T15:05:00+08:00")
    assert item["finality"] == "unknown" and item["is_final"] is False
    assert item["volume_unit"] == "unknown" and item["bar_start"] == ""
    assert not bar_evidence_status(item)["signal_eligible"]


@pytest.mark.parametrize("stamp,convention,valid", [
    (f"{DAY} 11:30:00", "minute_end", True),
    (f"{DAY} 11:30:00", "minute_start", False),
    (f"{DAY} 12:00:00", "minute_end", False),
    (f"{DAY} 13:00:00", "minute_start", True),
    (f"{DAY} 15:00:00", "minute_end", True),
    (f"{DAY} 15:00:00", "minute_start", False),
])
def test_session_profiles_and_lunch_boundaries(stamp, convention, valid):
    row = bar(stamp, finality="final")
    row["timestamp_convention"] = convention
    if convention == "minute_start":
        row["source_quote_time"] = (datetime.fromisoformat(stamp) + timedelta(minutes=1)).isoformat() + "+08:00"
    item = normalize_bar_observation(row, fetched_at=f"{DAY}T15:05:00+08:00")
    assert bar_evidence_status(item)["signal_eligible"] is valid


def test_current_unfinished_bar_never_stored_as_final(tmp_path):
    path = tmp_path / "600000.csv"
    count, reasons, _ = append(path, [bar(finality="final")], f"{DAY}T09:30:30+08:00")
    assert count == 0 and any("invalid_final_bar_bounds" in r for r in reasons)
    assert not path.exists()


@pytest.mark.parametrize("change,reason", [
    ({"volume_unit": "unknown"}, "volume_unit_unknown"),
    ({"volume_unit": "hands"}, "volume_unit_not_normalized"),
    ({"amount_unit": "thousand_CNY"}, "amount_unit_unknown_or_not_normalized"),
    ({"volume_semantics": "cumulative"}, "volume_semantics_unknown"),
    ({"volume": 0}, "zero_volume_no_trade_evidence"),
    ({"price_basis": "qfq"}, "price_basis_not_raw"),
    ({"price_basis": "hfq"}, "price_basis_not_raw"),
    ({"source_quote_time": ""}, "source_quote_time_unknown"),
])
def test_fields_are_not_signal_evidence(change, reason):
    item = normalize_bar_observation({**bar(finality="final"), **change}, fetched_at=f"{DAY}T15:05:00+08:00")
    result = bar_evidence_status(item)
    assert not result["signal_eligible"] and reason in result["reason_codes"]


def test_legacy_csv_is_preserved_and_new_segment_is_selected(tmp_path):
    path = tmp_path / "600000.csv"
    pd.DataFrame([{k: v for k, v in bar().items() if k in collector.MINUTE_COLUMNS}]).to_csv(path, index=False)
    original = path.read_bytes()
    assert append(path, [bar()])[0] == 1
    assert path.read_bytes() == original
    assert view(path).iloc[0]["finality"] == "provisional"
    with pytest.raises(ValueError, match="legacy_minute_as_of_unverified"):
        from quantdesk.intraday.minute_bar_lifecycle import select_bar_observations
        select_bar_observations(pd.read_csv(path), as_of=f"{DAY}T15:05:00+08:00")


def test_tampered_event_is_not_readable_evidence(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    frame = pd.read_csv(path)
    frame.loc[0, "close"] = 10.3
    frame.to_csv(path, index=False)
    result = read_intraday_csv(path, default_symbol="600000")
    assert result["ok"] is False and result["frame"].empty
    assert "minute_event_hash_mismatch" in result["errors"]


def test_observation_time_is_bound_into_event_hash(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    frame = pd.read_csv(path)
    frame.loc[0, "fetched_at"] = f"{DAY}T09:31:01+08:00"
    frame.to_csv(path, index=False)
    result = read_intraday_csv(path, default_symbol="600000")
    assert not result["ok"] and "minute_event_hash_mismatch" in result["errors"]


def test_read_materialized_revision_can_be_normalized_again(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    append(path, [{**bar(), "close": 10.3}])
    result = normalize_intraday_frame(view(path), default_symbol="600000")
    assert result["ok"] and result["frame"].iloc[0]["close"] == 10.3


def test_prior_raw_history_remains_observable_in_new_segment(tmp_path):
    path = tmp_path / "600000.csv"
    old = {**bar("2026-10-22 09:31:00"), "close": 10.1}
    pd.DataFrame([old])[list(collector.MINUTE_COLUMNS)].to_csv(path, index=False)
    original = path.read_bytes()
    append(path, [bar()])
    result = read_intraday_csv(path, default_symbol="600000", target_date="2026-10-22")
    assert result["ok"] and len(result["frame"]) == 1
    assert result["frame"].iloc[0]["close"] == 10.1 and len(result["input_paths"]) == 2
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", [{"provider": "other_source"}, {"timestamp_convention": "minute_start"}])
def test_different_provider_or_bar_interval_is_not_a_revision(tmp_path, change):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    original = path.read_bytes()
    count, reasons, _ = append(path, [{**bar(), **change}])
    assert count == 0 and any("bar_identity_changed" in reason for reason in reasons)
    assert path.read_bytes() == original


def test_incomplete_old_revision_chain_blocks_any_new_write(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    append(path, [{**bar(), "close": 10.3}])
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[2])
    original = path.read_bytes()
    count, reasons, newest = append(path, [bar(f"{DAY} 09:32:00")])
    assert count == 0 and newest == "" and any("lifecycle_integrity" in r for r in reasons)
    assert path.read_bytes() == original


def test_collection_observation_time_is_after_the_provider_response(tmp_path, monkeypatch):
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {"movers": [], "reason_codes": []})
    def fetch(*args, **kwargs):
        clock["elapsed"] = 6.0
        return [bar(f"{DAY} 09:36:00")], ""
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=datetime.fromisoformat(f"{DAY}T09:35:55+08:00"),
        fetcher=fetch, scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}]},
        monotonic=lambda: clock["elapsed"], max_symbol_seconds=120,
    )
    assert result["appended_rows"] == 1
    path = tmp_path / "data/intraday/1m/600000.csv"
    assert view(path, f"{DAY}T09:35:59+08:00").empty
    assert len(view(path, f"{DAY}T09:36:01+08:00")) == 1


def test_observation_anchor_includes_preflight_time_and_validation_uses_arrival(tmp_path, monkeypatch):
    clock = {"elapsed": 0.0}
    def reconcile(*args, **kwargs):
        clock["elapsed"] = 180.0
        return {}
    monkeypatch.setattr(collector, "reconcile_abandoned_round", reconcile)
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {"movers": [], "reason_codes": []})
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=datetime.fromisoformat(f"{DAY}T09:35:00+08:00"),
        fetcher=lambda *a, **k: ([bar(f"{DAY} 09:38:00")], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}]},
        monotonic=lambda: clock["elapsed"],
    )
    assert result["appended_rows"] == 1
    path = tmp_path / "data/intraday/1m/600000.csv"
    assert view(path, f"{DAY}T09:35:30+08:00").empty
    assert len(view(path, f"{DAY}T09:38:00+08:00")) == 1


def test_minute_start_grid_counts_only_finished_bars():
    rows = [bar(f"{DAY} 09:{minute:02d}:00") for minute in range(30, 35)]
    result = collector._response_minute_window(
        rows, as_of=datetime.fromisoformat(f"{DAY}T09:35:00+08:00"), convention="minute_start")
    assert result["expected_session_minutes"] == 5 and result["missing_session_minutes"] == 0
    assert result["validation_scope"] == "coverage_grid_only"


def test_failed_atomic_append_preserves_existing_evidence(tmp_path, monkeypatch):
    path = tmp_path / "600000.csv"
    append(path, [bar()])
    original = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("fixture_io_failure")
    monkeypatch.setattr(collector, "_atomic_append_bytes", fail)
    count, reasons, _ = append(path, [{**bar(), "close": 10.1}])
    assert count == 0 and any("minute_append_failed" in r for r in reasons)
    assert path.read_bytes() == original


def full_day():
    return [bar((datetime.fromisoformat(f"{DAY} {start}") + timedelta(minutes=i)).isoformat(sep=" "), finality="final")
            for start in ("09:31:00", "13:01:00") for i in range(120)]


@pytest.mark.parametrize("damage", ["none", "missing_minute", "provisional", "zero_volume"])
def test_finality_and_exact_session_grid_are_independent(tmp_path, damage):
    rows = full_day()
    if damage == "missing_minute":
        rows.pop(20)
    elif damage == "provisional":
        rows[20]["finality"] = "provisional"
    elif damage == "zero_volume":
        rows[20]["volume"] = 0
    append(tmp_path / "600000.csv", rows)
    result = audit_intraday_file("600000", DAY, intraday_dir=tmp_path)
    assert result["formal_execution_eligible"] is (damage == "none"), result
    assert result["signal_eligible"] is (damage == "none")
    assert result["status"] == "usable", "field/row diagnostics are retained separately"
    gate = build_intraday_quality_gate(view(tmp_path / "600000.csv"), provider="fixture", target_date=DAY)
    if damage in {"provisional", "zero_volume"}:
        assert not gate["signal_eligible"]


def test_180_readable_bars_never_upgrade_to_verified_window_pool(tmp_path):
    directory = tmp_path / "minute"
    directory.mkdir()
    rows = full_day()[:180]
    # This counterexample is deliberately the original legacy schema.
    pd.DataFrame(rows)[list(collector.MINUTE_COLUMNS[:-1])].to_csv(directory / "600000.csv", index=False)
    frame = pd.read_csv(directory / "600000.csv")
    frame["price_basis"] = "raw"
    frame.to_csv(directory / "600000.csv", index=False)
    single = audit_intraday_file("600000", DAY, intraday_dir=directory)
    assert single["status"] == "usable" and not single["formal_execution_eligible"]
    calendar = tmp_path / "calendar.csv"
    calendar.write_text(f"date,is_open\n{DAY},1\n", encoding="utf-8")
    snapshot = tmp_path / "window.json"
    audit_minute_pool_window(window_start=DAY, window_end=DAY, symbols=["600000"],
                             intraday_dir=directory, calendar_path=calendar, output_path=snapshot)
    pool = load_window_verified_minute_pool(snapshot_path=snapshot)
    assert pool["snapshot_valid"] and not window_pool_covers(pool, "600000", DAY)


def test_legacy_window_snapshot_has_no_final_session_authority(tmp_path):
    snapshot = tmp_path / "window.json"
    snapshot.write_text(json.dumps({"task_type": "minute_pool_window_audit", "window_start": DAY,
        "window_end": DAY, "symbols": [{"symbol": "600000", "day_status": {DAY: "usable"}}]}), encoding="utf-8")
    pool = load_window_verified_minute_pool(snapshot_path=snapshot)
    assert pool["snapshot_valid"] and not pool["passed"] and pool["warnings"]


def test_tushare_cannot_certify_usable_without_formal_session_count():
    payload = {"task_type": "tushare_minute_quality_audit", "status": "passed", "total_symbols": 1,
               "eligibility_standard": FORMAL_EVIDENCE_STANDARD,
               "quality_summary": {"usable": 1, "formal_execution_eligible": 0}}
    assert not _is_clean_pass_quality_payload(payload)
    assert _overall_status(payload["quality_summary"]) == "partial"
    payload["quality_summary"]["formal_execution_eligible"] = 1
    assert _is_clean_pass_quality_payload(payload)
    payload["quality_summary"]["formal_execution_eligible"] = True
    assert not _is_clean_pass_quality_payload(payload)


def test_old_95_percent_snapshot_cannot_reuse_a_legacy_formal_count():
    legacy = {"task_type": "tushare_minute_quality_audit", "status": "passed", "total_symbols": 1,
              "quality_summary": {"usable": 1, "formal_execution_eligible": 1}}
    assert not _is_clean_pass_quality_payload(legacy)


def test_legacy_238_bars_remain_partial_despite_passing_95_percent(tmp_path):
    rows = full_day()
    rows.pop(20)
    rows.pop(70)
    pd.DataFrame(rows)[list(collector.MINUTE_COLUMNS)].to_csv(tmp_path / "600000.csv", index=False)
    result = audit_intraday_file("600000", DAY, intraday_dir=tmp_path)
    assert result["status"] == "usable" and result["session_coverage_ratio"] > .95
    assert result["session_status"] == "partial_session" and not result["reliable_no_fill_eligible"]
    assert "session_minute_grid_incomplete" in result["session_reason_codes"]


def test_exact_legacy_grid_is_observable_but_never_formal_evidence(tmp_path):
    path = tmp_path / "600000.csv"
    pd.DataFrame(full_day())[list(collector.MINUTE_COLUMNS)].to_csv(path, index=False)
    result = audit_intraday_file("600000", DAY, intraday_dir=tmp_path)
    assert result["status"] == "usable" and result["complete_session"]
    assert result["session_coverage_ratio"] == 1.0
    assert not any(result[key] for key in (
        "formal_execution_eligible", "reliable_no_fill_eligible",
        "can_execute_condition_plan", "is_reliable", "signal_eligible"))
    assert result["quality_status"] == "execution_blocked"
    assert "legacy_bar_lifecycle_unverified" in result["session_reason_codes"]


def test_legacy_field_gate_never_promotes_complete_ohlcv_to_final_signal(tmp_path):
    path = tmp_path / "600000.csv"
    pd.DataFrame(full_day()[:2])[list(collector.MINUTE_COLUMNS)].to_csv(path, index=False)
    gate = build_intraday_quality_gate(view(path), provider="fixture", target_date=DAY)
    assert gate["ohlcv_complete"] and gate["data_quality"] == "full_ohlcv"
    assert not gate["signal_eligible"] and not gate["ohlcv_signal_eligible"]
    assert not gate["finalized_signal_eligible"]
    assert gate["signal_capability"] == "none" and gate["volume_semantics"] == "unknown"


def test_removed_event_hash_never_downgrades_a_tampered_log_to_legacy(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar(finality="final")])
    frame = pd.read_csv(path).drop(columns=["evidence_hash"])
    frame.loc[0, "close"] = 10.3
    frame.to_csv(path, index=False)
    result = read_intraday_csv(path, default_symbol="600000")
    assert not result["ok"] and result["frame"].empty
    assert "minute_event_integrity_missing" in result["errors"]


@pytest.mark.parametrize("observed", ["", "bogus"])
def test_invalid_observation_time_is_rejected_before_creating_evidence(tmp_path, observed):
    path = tmp_path / "600000.csv"
    count, reasons, newest = append(path, [bar()], observed)
    assert count == 0 and newest == "" and not path.exists()
    assert any("observation_time_invalid" in reason for reason in reasons)


def test_orphan_revision_is_rejected_before_writing(tmp_path):
    path = tmp_path / "600000.csv"
    count, reasons, _ = append(path, [bar(finality="revised", revision_reason="provider_correction")])
    assert count == 0 and not path.exists()
    assert any("revision_requires_parent" in reason for reason in reasons)


def test_orphan_revision_in_existing_log_is_rejected_even_with_valid_hash(tmp_path):
    path = tmp_path / "600000.csv"
    append(path, [bar(finality="final")])
    frame = pd.read_csv(path)
    frame["revision_reason"] = frame["revision_reason"].astype(object)
    frame.loc[0, "finality"] = "revised"
    frame.loc[0, "revision_reason"] = "provider_correction"
    frame.loc[0, "evidence_hash"] = observation_hash(frame.iloc[0].to_dict())
    frame.to_csv(path, index=False)
    result = read_intraday_csv(path, default_symbol="600000")
    assert not result["ok"] and "minute_event_revision_requires_parent" in result["errors"]


def test_verified_pool_never_publishes_symbols_outside_audited_count(tmp_path, monkeypatch):
    from quantdesk import tushare_minute_quality as quality
    snapshot = {"task_type": "tushare_minute_quality_audit", "status": "passed",
        "total_symbols": 1, "eligibility_standard": FORMAL_EVIDENCE_STANDARD,
        "job": {"path": "fixture_job"},
        "quality_summary": {"usable": 1, "formal_execution_eligible": 1}}
    monkeypatch.setattr(quality, "read_json", lambda *a, **k: {"symbols": ["600000", "000001"]})
    result = quality._pool_payload_from_snapshot(tmp_path, snapshot, source_snapshot="fixture.json")
    assert result["status"] == "warning" and result["symbols"] == []
    assert result["symbol_count"] == 2 and result["total_symbols"] == 1
    assert "verified_minute_pool_symbol_count_mismatch:2/1" in result["warnings"]


def test_old_exact_grid_snapshot_cannot_claim_finality_under_current_contract():
    payload = {"task_type": "tushare_minute_quality_audit", "status": "passed",
        "total_symbols": 1, "eligibility_standard": "exact_session_grid_v2",
        "quality_summary": {"usable": 1, "formal_execution_eligible": 1}}
    assert not _is_clean_pass_quality_payload(payload)


def test_update_engine_preserves_already_finalized_minute_evidence(tmp_path):
    from quantdesk.intraday import intraday_update_engine as engine
    staging = tmp_path / "staging"
    staging.mkdir()
    append(staging / "600000.csv", full_day())
    source = view(staging / "600000.csv")
    assert build_intraday_quality_gate(source, provider="fixture", target_date=DAY)["signal_eligible"]
    target = engine._merge_and_write_unlocked(
        "600000", source, append=True, overwrite=False, base_dir=tmp_path / "output")
    result = audit_intraday_file("600000", DAY, intraday_dir=target.parent)
    assert result["formal_execution_eligible"] and result["signal_eligible"], result


def test_audited_importer_preserves_already_finalized_minute_evidence(tmp_path):
    from quantdesk.intraday import stage6_append_only_import as importer
    staging = tmp_path / "staging"
    staging.mkdir()
    append(staging / "600000.csv", full_day())
    staged = view(staging / "600000.csv")
    staged["price_only_intraday"] = False
    staged.to_csv(staging / "600000.csv", index=False, encoding="utf-8-sig")
    before = audit_intraday_file("600000", DAY, intraday_dir=staging)
    assert before["formal_execution_eligible"]
    canonical = tmp_path / "canonical"
    result = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"], target_date=DAY,
        run_id="synthetic_finality_preservation", quality_audit={"items": [before]}, apply=True,
        output_path=tmp_path / "import_receipt.json")
    assert result["status"] == "completed", result
    after = audit_intraday_file("600000", DAY, intraday_dir=canonical)
    assert after["formal_execution_eligible"] and after["signal_eligible"], after


def _staged_final_history(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    path = staging / "600000.csv"
    assert append(path, full_day())[0] == 240
    with path.open(encoding="utf-8-sig", newline="") as stream:
        events = pd.DataFrame(list(csv.DictReader(stream)))
    events["price_only_intraday"] = False
    events.to_csv(path, index=False, encoding="utf-8-sig")
    return staging, path


def test_import_appends_explicit_revisions_without_rewriting_prior_events(tmp_path):
    from quantdesk.intraday import stage6_append_only_import as importer
    staging, path = _staged_final_history(tmp_path)
    canonical = tmp_path / "canonical"
    first = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"],
        target_date=DAY, run_id="first_final_history")
    assert first["status"] == "completed", first
    target = Path(first["items"][0]["canonical_path"])
    original = target.read_bytes()
    parent = view(path).iloc[0]["evidence_hash"]
    correction = {**full_day()[0], "close": 10.3, "revision": 2,
                  "supersedes_hash": parent, "revision_reason": "official_correction"}
    # The staging provenance column is outside the signed event identity.
    events = pd.read_csv(path, dtype=str, keep_default_na=False)
    row = normalize_bar_observation(correction, fetched_at=f"{DAY}T16:01:00+08:00")
    row.update(revision=2, supersedes_hash=parent, revision_reason="official_correction",
               finality="revised", price_only_intraday=False)
    row["evidence_hash"] = observation_hash(row)
    pd.concat([events, pd.DataFrame([row])], ignore_index=True).to_csv(
        path, index=False, encoding="utf-8-sig")
    second = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"],
        target_date=DAY, run_id="explicit_revision")
    assert second["status"] == "completed", second
    assert target.read_bytes().startswith(original)
    assert len(pd.read_csv(target)) == 241
    assert view(target).iloc[0]["close"] == 10.3
    assert view(target, f"{DAY}T15:30:00+08:00").iloc[0]["close"] == 10.0
    before_repeat = target.read_bytes()
    repeated = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"],
        target_date=DAY, run_id="same_revision_history")
    assert repeated["status"] == "completed", repeated
    assert repeated["unchanged_symbol_count"] == 1
    assert target.read_bytes() == before_repeat


def test_manual_import_preserves_final_revision_evidence(tmp_path, monkeypatch):
    from quantdesk.intraday import intraday_import as manual
    staging, path = _staged_final_history(tmp_path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    monkeypatch.setattr(manual, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manual, "INTRADAY_1M_DIR", canonical)
    result = manual.import_intraday_1m_file(
        path, target_date=DAY, output_path=tmp_path / "manual_receipt.json")
    assert result["ok"], result
    assert result["canonical_import"]["status"] == "completed", result
    assert audit_intraday_file("600000", DAY, intraday_dir=canonical)["formal_execution_eligible"]


def test_signed_basis_change_requires_new_evidence_before_any_legacy_repair(tmp_path, monkeypatch):
    from quantdesk.intraday import stage6_conflict_resolution as resolver
    staging, path = _staged_final_history(tmp_path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    target = canonical / "600000.csv"
    events = pd.read_csv(path, dtype=str, keep_default_na=False)
    events["price_basis"] = "unknown"
    events["evidence_hash"] = [observation_hash(row) for row in events.to_dict("records")]
    events.to_csv(target, index=False, encoding="utf-8-sig")
    before = target.read_bytes()
    mutations = []
    original_replace = resolver.os.replace
    def record_replace(*args, **kwargs):
        mutations.append(args)
        return original_replace(*args, **kwargs)
    monkeypatch.setattr(resolver.os, "replace", record_replace)
    result = resolver.resolve_stage6_intraday_conflicts(
        canonical_dir=canonical, staging_dir=staging, symbols=["600000"],
        target_date=DAY, run_id="signed_basis_conflict", backup_dir=tmp_path / "backups", apply=True)
    assert result["status"] == "external_review_required", result
    assert target.read_bytes() == before
    assert not [args for args in mutations if Path(args[1]) == target]


def _revise_staged_history(path):
    events = pd.read_csv(path, dtype=str, keep_default_na=False)
    prior = view(path).iloc[0].to_dict()
    row = {**prior, "close": 10.3, "revision": int(prior["revision"]) + 1,
           "supersedes_hash": prior["evidence_hash"], "revision_reason": "official_correction",
           "finality": "revised", "fetched_at": f"{DAY}T16:01:00+08:00"}
    if "price_only_intraday" in events.columns:
        parent_event = events.loc[events["evidence_hash"] == prior["evidence_hash"]].iloc[-1]
        row["price_only_intraday"] = parent_event["price_only_intraday"]
    row["evidence_hash"] = observation_hash(row)
    pd.concat([events, pd.DataFrame([row])], ignore_index=True).to_csv(path, index=False, encoding="utf-8-sig")
    return events, row


def test_legacy_archive_does_not_block_valid_revisions_in_existing_new_segment(tmp_path):
    from quantdesk.intraday import stage6_append_only_import as importer
    from quantdesk.intraday.intraday_schema import STANDARD_COLUMNS
    staging, path = _staged_final_history(tmp_path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    legacy = canonical / "600000.csv"
    pd.DataFrame(full_day())[STANDARD_COLUMNS].to_csv(legacy, index=False, encoding="utf-8-sig")
    original = legacy.read_bytes()
    first = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"], target_date=DAY, run_id="new_segment")
    assert first["status"] == "completed", first
    signed = Path(first["items"][0]["canonical_path"])
    signed_before = signed.read_bytes()
    _revise_staged_history(path)
    second = importer.apply_audited_intraday_batch(
        staging_dir=staging, canonical_dir=canonical, symbols=["600000"], target_date=DAY, run_id="new_segment_revision")
    assert second["status"] == "completed", second
    assert legacy.read_bytes() == original
    assert signed.read_bytes().startswith(signed_before)
    assert view(legacy).iloc[0]["close"] == 10.3
    assert view(legacy, f"{DAY}T15:30:00+08:00").iloc[0]["close"] == 10.0


def test_manual_import_keeps_all_revisions_and_repeat_is_byte_identical(tmp_path, monkeypatch):
    from quantdesk.intraday import intraday_import as manual
    staging, path = _staged_final_history(tmp_path)
    _revise_staged_history(path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    monkeypatch.setattr(manual, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manual, "INTRADAY_1M_DIR", canonical)
    first = manual.import_intraday_1m_file(path, target_date=DAY, output_path=tmp_path / "first_import.json")
    assert first["ok"], first
    target = Path(first["canonical_import"]["items"][0]["canonical_path"])
    before = target.read_bytes()
    assert len(pd.read_csv(target)) == 241
    assert view(target).iloc[0]["close"] == 10.3
    assert view(target, f"{DAY}T15:30:00+08:00").iloc[0]["close"] == 10.0
    repeated = manual.import_intraday_1m_file(path, target_date=DAY, output_path=tmp_path / "second_import.json")
    assert repeated["ok"], repeated
    assert repeated["canonical_import"]["unchanged_symbol_count"] == 1
    assert target.read_bytes() == before


@pytest.mark.parametrize("change", [{"provider": "other_provider"}, {"volume_semantics": "cumulative"}])
def test_imported_revision_cannot_change_bar_identity(tmp_path, change):
    from quantdesk.intraday.minute_bar_lifecycle import select_bar_observations
    staging, path = _staged_final_history(tmp_path)
    events, row = _revise_staged_history(path)
    row.update(change)
    row["evidence_hash"] = observation_hash(row)
    with pytest.raises(ValueError, match="bar_identity_changed"):
        select_bar_observations(pd.concat([events, pd.DataFrame([row])], ignore_index=True))


def test_staging_writer_appends_history_and_preserves_bytes_on_failed_replace(tmp_path, monkeypatch):
    from quantdesk.intraday import intraday_update_engine as engine
    staging, path = _staged_final_history(tmp_path)
    events = pd.read_csv(path, dtype=str, keep_default_na=False)
    events = events.drop(columns="price_only_intraday")
    target = engine._merge_and_write("600000", events, append=True, overwrite=False, base_dir=tmp_path / "output")
    before = target.read_bytes()
    _revise_staged_history(path)
    revised = pd.read_csv(path, dtype=str, keep_default_na=False).drop(columns="price_only_intraday")
    original_replace = engine.os.replace
    def fail_replace(source, destination):
        if Path(destination) == target:
            raise OSError("injected_write_failure")
        return original_replace(source, destination)
    monkeypatch.setattr(engine.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected_write_failure"):
        engine._merge_and_write("600000", revised, append=True, overwrite=False, base_dir=target.parent)
    assert target.read_bytes() == before
    monkeypatch.setattr(engine.os, "replace", original_replace)
    engine._merge_and_write("600000", revised, append=True, overwrite=False, base_dir=target.parent)
    assert target.read_bytes().startswith(before)
    after = target.read_bytes()
    engine._merge_and_write("600000", revised, append=True, overwrite=False, base_dir=target.parent)
    assert target.read_bytes() == after
