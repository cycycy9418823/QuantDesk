from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from scripts import run_round13_minute_gap_backfill as backfill_cli
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.historical_gap_backfill import (
    APPROVED_ROUND13_DAILY_SOURCES,
    APPROVED_ROUND13_MINUTE_SOURCES,
    ROUND13_CROSSCHECK_THRESHOLDS,
    ROUND13_CROSSCHECK_THRESHOLDS_SHA256,
    ROUND13_MINUTE_BACKFILL_ALLOWLIST,
    DailyMinuteCrosscheckThresholds,
    audit_round13_minute_backfill_batch,
    audit_round13_minute_backfill_file,
    audit_round13_minute_backfill_item,
    run_round13_minute_gap_backfill,
    validate_round13_backfill_scope,
)
from quantdesk.intraday.intraday_schema import STANDARD_COLUMNS
from quantdesk.intraday.stage6_append_only_import import (
    apply_audited_intraday_batch,
    canonical_intraday_write_lock_path,
)
from tests.minute_evidence_fixture import final_minute_fixture


@pytest.mark.parametrize(
    ("apply_args", "expected_apply"),
    [
        (["--apply"], True),
        (["--apply", "false"], False),
    ],
)
def test_cli_apply_accepts_bare_flag_and_explicit_false(
    apply_args: list[str],
    expected_apply: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Mock(return_value={"status": "planned_offline"})
    monkeypatch.setattr(backfill_cli, "run_round13_minute_gap_backfill", runner)

    assert backfill_cli.main([*apply_args, "--json", "false"]) == 0

    assert runner.call_args.kwargs["apply"] is expected_apply


def test_cli_summary_accepts_bare_flag_and_explicit_false() -> None:
    assert backfill_cli.parse_args(["--summary"]).summary == "true"
    assert backfill_cli.parse_args(["--summary", "false"]).summary == "false"


def _session(symbol: str, target_date: str) -> pd.DataFrame:
    morning = pd.date_range(
        f"{target_date} 09:31:00", f"{target_date} 11:30:00", freq="min"
    )
    afternoon = pd.date_range(
        f"{target_date} 13:01:00", f"{target_date} 15:00:00", freq="min"
    )
    timestamps = morning.append(afternoon)
    frame = pd.DataFrame(
        {
            "symbol": symbol,
            "datetime": timestamps.strftime("%Y-%m-%d %H:%M:%S"),
            "date": target_date,
            "time": timestamps.strftime("%H:%M:%S"),
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 1000.0,
            "amount": 10100.0,
            "price_basis": "raw",
            "source": "mootdx_intraday",
            "updated_at": "2026-08-03T10:00:00+08:00",
            "price_only_intraday": False,
        }
    )
    frame.loc[0, ["open", "high", "low", "close"]] = [10.0, 10.0, 9.8, 9.9]
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [10.2, 10.5, 10.0, 10.3]
    return frame


def _daily(symbol: str, target_date: str, minute: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-07-01",
                "symbol": symbol,
                "open": 9.0,
                "high": 9.2,
                "low": 8.8,
                "close": 9.1,
                "volume": 10000.0,
                "amount": 91000.0,
                "adjust": "raw",
                "source": "mootdx_daily",
                "updated_at": "2026-07-01T16:00:00+08:00",
            },
            {
                "date": target_date,
                "symbol": symbol,
                "open": float(minute.iloc[0]["open"]),
                "high": float(minute["high"].max()),
                "low": float(minute["low"].min()),
                "close": float(minute.iloc[-1]["close"]),
                "volume": float(minute["volume"].sum()),
                "amount": float(minute["amount"].sum()),
                "adjust": "raw",
                "source": "mootdx_daily",
                "updated_at": "2026-08-03T10:00:00+08:00",
            },
        ]
    )


def _write_bound_files(
    tmp_path: Path,
    symbol: str,
    target_date: str,
    *,
    run_id: str = "round13-fixture-run",
) -> tuple[Path, Path, pd.DataFrame, pd.DataFrame]:
    minute = _session(symbol, target_date)
    daily = _daily(symbol, target_date, minute)
    staging = tmp_path / run_id / "staging" / target_date / f"{symbol}.csv"
    daily_path = tmp_path / run_id / "daily" / f"{symbol}.csv"
    staging.parent.mkdir(parents=True, exist_ok=True)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    minute.to_csv(staging, index=False, encoding="utf-8-sig", lineterminator="\n")
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig", lineterminator="\n")
    return staging, daily_path, minute, daily


def _binding_roots(staging: Path, daily: Path) -> tuple[Path, Path]:
    return staging.parents[1], daily.parent


def test_allowlist_is_exact_and_rejects_extra_duplicate_or_incomplete_scope() -> None:
    expected = {
        ("920000", "2026-07-28"),
        ("920204", "2026-07-28"),
        ("920496", "2026-07-28"),
        ("920627", "2026-07-28"),
        ("920694", "2026-07-29"),
    }
    assert ROUND13_MINUTE_BACKFILL_ALLOWLIST == frozenset(expected)
    assert validate_round13_backfill_scope(
        list(expected), require_complete_scope=True
    ) == sorted(expected)
    with pytest.raises(ValueError, match="backfill_scope_not_authorized"):
        validate_round13_backfill_scope([("920001", "2026-07-28")])
    with pytest.raises(ValueError, match="duplicate_backfill_pair"):
        validate_round13_backfill_scope(
            [("920000", "2026-07-28"), ("920000", "2026-07-28")]
        )
    with pytest.raises(ValueError, match="backfill_scope_incomplete"):
        validate_round13_backfill_scope(
            [("920000", "2026-07-28")], require_complete_scope=True
        )


def test_thresholds_are_frozen_finite_and_hash_bound() -> None:
    expected = {
        "ohlc_absolute": 1e-8,
        "volume_absolute": 100.0,
        "volume_relative": 0.001,
        "amount_absolute": 2000.0,
        "amount_relative": 0.001,
    }
    assert ROUND13_CROSSCHECK_THRESHOLDS.to_dict() == expected
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert ROUND13_CROSSCHECK_THRESHOLDS_SHA256 == hashlib.sha256(canonical).hexdigest()
    with pytest.raises(ValueError, match="invalid_crosscheck_threshold"):
        DailyMinuteCrosscheckThresholds(amount_relative=float("nan"))


def test_file_audit_passes_only_with_bound_real_provider_provenance(tmp_path: Path) -> None:
    staging, daily, _minute, _daily_frame = _write_bound_files(
        tmp_path, "920000", "2026-07-28"
    )

    audit = audit_round13_minute_backfill_file(
        "920000",
        "2026-07-28",
        staging_path=staging,
        daily_path=daily,
        staging_root=_binding_roots(staging, daily)[0],
        daily_root=_binding_roots(staging, daily)[1],
        run_id="round13-fixture-run",
    )

    assert audit["status"] == "passed"
    assert audit["canonical_import_eligible"] is True
    assert audit["metrics"]["minute_row_count"] == 240
    assert audit["staging_sha256"] == hashlib.sha256(staging.read_bytes()).hexdigest()
    assert audit["minute_sources"] == ["mootdx_intraday"]
    assert audit["daily_sources"] == ["mootdx_daily"]
    assert audit["checks"] == {
        "exact_session_grid": True,
        "timestamp_unique": True,
        "raw_price_basis": True,
        "positive_provider_provenance": True,
        "price_only_intraday_false": True,
        "staging_path_hash_run_bound": True,
        "captured_at_bound": True,
        "daily_row_unique": True,
        "ohlc_match": True,
        "volume_match": True,
        "amount_match": True,
    }


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing_minute", "minute_session_grid_mismatch"),
        ("duplicate_timestamp", "duplicate_minute_timestamp"),
        ("price_mismatch", "daily_minute_ohlc_mismatch"),
        ("volume_mismatch", "daily_minute_volume_mismatch"),
        ("amount_mismatch", "daily_minute_amount_mismatch"),
        ("minute_source", "minute_provider_provenance_not_approved"),
        ("daily_source", "daily_provider_provenance_not_approved"),
        ("minute_time", "minute_provider_provenance_not_approved"),
        ("price_only", "price_only_intraday_not_false"),
        ("adjusted_daily", "daily_price_basis_not_raw"),
    ],
)
def test_strict_file_gate_fails_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    staging, daily_path, minute, daily = _write_bound_files(
        tmp_path, "920000", "2026-07-28"
    )
    if mutation == "missing_minute":
        minute = minute.iloc[:-1].copy()
    elif mutation == "duplicate_timestamp":
        minute.loc[1, "datetime"] = minute.loc[0, "datetime"]
        minute.loc[1, "time"] = minute.loc[0, "time"]
    elif mutation == "price_mismatch":
        daily.loc[daily["date"] == "2026-07-28", "high"] += 0.01
    elif mutation == "volume_mismatch":
        daily.loc[daily["date"] == "2026-07-28", "volume"] += 1000.0
    elif mutation == "amount_mismatch":
        daily.loc[daily["date"] == "2026-07-28", "amount"] += 5000.0
    elif mutation == "minute_source":
        minute["source"] = "synthetic_fixture"
    elif mutation == "daily_source":
        daily.loc[daily["date"] == "2026-07-28", "source"] = "local_existing"
    elif mutation == "minute_time":
        minute["updated_at"] = "not-a-time"
    elif mutation == "price_only":
        minute["price_only_intraday"] = True
    elif mutation == "adjusted_daily":
        daily.loc[0, "adjust"] = "qfq"
    minute.to_csv(staging, index=False, encoding="utf-8-sig", lineterminator="\n")
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig", lineterminator="\n")

    audit = audit_round13_minute_backfill_file(
        "920000",
        "2026-07-28",
        staging_path=staging,
        daily_path=daily_path,
        staging_root=_binding_roots(staging, daily_path)[0],
        daily_root=_binding_roots(staging, daily_path)[1],
        run_id="round13-fixture-run",
    )

    assert audit["status"] == "blocked"
    assert reason in audit["reason_codes"]
    assert audit["canonical_import_eligible"] is False


def test_daily_price_basis_is_classified_on_original_frame_before_date_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quantdesk.intraday.historical_gap_backfill as module

    staging, daily_path, _minute, _daily_frame = _write_bound_files(
        tmp_path, "920000", "2026-07-28"
    )
    observed_lengths: list[int] = []
    original = module.classify_frame_price_basis

    def classify(frame: pd.DataFrame):
        observed_lengths.append(len(frame))
        return original(frame)

    monkeypatch.setattr(module, "classify_frame_price_basis", classify)

    audit = audit_round13_minute_backfill_file(
        "920000",
        "2026-07-28",
        staging_path=staging,
        daily_path=daily_path,
        staging_root=_binding_roots(staging, daily_path)[0],
        daily_root=_binding_roots(staging, daily_path)[1],
        run_id="round13-fixture-run",
    )

    assert audit["status"] == "passed"
    assert observed_lengths[0] == 2
    assert audit["daily_price_basis_preflight"]["validation"]["consumer"] == (
        "historical_intraday_replay"
    )


def test_item_gate_rejects_wrong_path_hash_run_or_captured_at_binding(tmp_path: Path) -> None:
    staging, _daily_path, minute, daily = _write_bound_files(
        tmp_path, "920000", "2026-07-28"
    )
    sha = hashlib.sha256(staging.read_bytes()).hexdigest()

    audit = audit_round13_minute_backfill_item(
        "920000",
        "2026-07-28",
        minute_frame=minute,
        daily_frame=daily,
        staging_path=staging,
        daily_path=_daily_path,
        staging_root=_binding_roots(staging, _daily_path)[0],
        daily_root=_binding_roots(staging, _daily_path)[1],
        staging_sha256="0" * 64,
        run_id="wrong-run",
        captured_at="2026-08-03T11:00:00+08:00",
    )

    assert "staging_path_or_run_binding_invalid" in audit["reason_codes"]
    assert "staging_hash_binding_invalid" in audit["reason_codes"]
    assert "captured_at_binding_invalid" in audit["reason_codes"]
    assert sha != "0" * 64


def test_file_gate_rejects_parent_symlink_escape_for_stage_or_daily(tmp_path: Path) -> None:
    run_id = "round13-link-run"
    staging, daily, _minute, _daily_frame = _write_bound_files(
        tmp_path / "outside", "920000", "2026-07-28", run_id=run_id
    )
    allowed_stage = tmp_path / "allowed" / run_id / "staging"
    allowed_daily = tmp_path / "allowed" / run_id / "daily"
    allowed_stage.parent.mkdir(parents=True, exist_ok=True)
    allowed_daily.parent.mkdir(parents=True, exist_ok=True)
    try:
        allowed_stage.symlink_to(staging.parents[1], target_is_directory=True)
        allowed_daily.symlink_to(daily.parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink unavailable")

    audit = audit_round13_minute_backfill_file(
        "920000",
        "2026-07-28",
        staging_path=allowed_stage / "2026-07-28" / "920000.csv",
        daily_path=allowed_daily / "920000.csv",
        staging_root=allowed_stage,
        daily_root=allowed_daily,
        run_id=run_id,
    )

    assert audit["status"] == "blocked"
    assert audit["reason_codes"] == ["staging_or_daily_path_escape"]


def test_positive_source_allowlists_are_explicit_not_substring_based() -> None:
    assert APPROVED_ROUND13_MINUTE_SOURCES == frozenset(
        {"akshare_intraday", "mootdx_intraday"}
    )
    assert "local_existing" not in APPROVED_ROUND13_MINUTE_SOURCES
    assert "local_existing" not in APPROVED_ROUND13_DAILY_SOURCES


def test_batch_gate_requires_all_five_actual_files(tmp_path: Path) -> None:
    run_id = "round13-batch-run"
    pairs = sorted(ROUND13_MINUTE_BACKFILL_ALLOWLIST)
    staging_paths: dict[tuple[str, str], Path] = {}
    daily_paths: dict[tuple[str, str], Path] = {}
    for symbol, target in pairs:
        staging, daily, _minute, _daily_frame = _write_bound_files(
            tmp_path, symbol, target, run_id=run_id
        )
        staging_paths[(symbol, target)] = staging
        daily_paths[(symbol, target)] = daily

    passed = audit_round13_minute_backfill_batch(
        pairs,
        staging_paths=staging_paths,
        daily_paths=daily_paths,
        staging_root=tmp_path / run_id / "staging",
        daily_root=tmp_path / run_id / "daily",
        run_id=run_id,
        require_complete_scope=True,
    )
    assert passed["status"] == "passed"
    assert passed["summary"] == {"requested": 5, "passed": 5, "blocked": 0}

    staging_paths[("920694", "2026-07-29")].unlink()
    blocked = audit_round13_minute_backfill_batch(
        pairs,
        staging_paths=staging_paths,
        daily_paths=daily_paths,
        staging_root=tmp_path / run_id / "staging",
        daily_root=tmp_path / run_id / "daily",
        run_id=run_id,
        require_complete_scope=True,
    )
    assert blocked["status"] == "blocked"
    assert blocked["summary"] == {"requested": 5, "passed": 4, "blocked": 1}


def _runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    daily_dir = root / "data" / "daily"
    minute_dir = root / "data" / "intraday" / "1m"
    daily_dir.mkdir(parents=True)
    minute_dir.mkdir(parents=True)
    for symbol, target in sorted(ROUND13_MINUTE_BACKFILL_ALLOWLIST):
        minute = _session(symbol, target)
        _daily(symbol, target, minute).to_csv(
            daily_dir / f"{symbol}.csv", index=False, encoding="utf-8-sig"
        )
        previous = minute.iloc[[0]].copy()
        previous["datetime"] = "2026-07-01 09:31:00"
        previous["date"] = "2026-07-01"
        previous["time"] = "09:31:00"
        previous[STANDARD_COLUMNS].to_csv(
            minute_dir / f"{symbol}.csv", index=False, encoding="utf-8-sig"
        )
    return root


def _provider_updater(symbols: list[str], target_date: str, **kwargs) -> dict:
    staging = Path(kwargs["intraday_dir"])
    staging.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        final_minute_fixture(_session(symbol, target_date), convention="minute_end").to_csv(
            staging / f"{symbol}.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
        )
    return {"status": "completed", "updated_symbol_list": list(symbols)}


def _write_existing_lifecycle(root: Path) -> None:
    directory = root / "data" / "intraday" / "1m" / "lifecycle_v1"
    directory.mkdir(parents=True, exist_ok=True)
    for symbol in sorted({symbol for symbol, _target in ROUND13_MINUTE_BACKFILL_ALLOWLIST}):
        final_minute_fixture(_session(symbol, "2026-07-01"), convention="minute_end").to_csv(
            directory / f"{symbol}.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
        )


def test_end_to_end_staging_only_is_whole_batch_and_canonical_read_only(
    tmp_path: Path,
) -> None:
    root = _runtime_root(tmp_path)
    before = {
        path.name: path.read_bytes()
        for path in (root / "data" / "intraday" / "1m").glob("*.csv")
    }

    payload = run_round13_minute_gap_backfill(
        root,
        network=True,
        apply=False,
        run_id="round13-stage-only",
        provider_updater=_provider_updater,
    )

    assert payload["status"] == "completed_staging_only"
    assert payload["quality_audit"]["summary"] == {
        "requested": 5,
        "passed": 5,
        "blocked": 0,
    }
    assert payload["canonical_import"]["status"] == "ready"
    for name, content in before.items():
        assert (root / "data" / "intraday" / "1m" / name).read_bytes() == content


def test_end_to_end_apply_uses_stage6_and_records_five_before_after_pairs(
    tmp_path: Path,
) -> None:
    root = _runtime_root(tmp_path)
    minute_dir = root / "data" / "intraday" / "1m"
    legacy_before = {path: path.read_bytes() for path in minute_dir.glob("*.csv")}

    payload = run_round13_minute_gap_backfill(
        root,
        network=True,
        apply=True,
        run_id="round13-apply",
        provider_updater=_provider_updater,
    )

    assert payload["status"] == "completed"
    provenance = payload["canonical_import"]["provenance"]
    assert len(provenance) == 5
    assert all(item["canonical_sha256_before"] != item["canonical_sha256_after"] for item in provenance)
    assert all(item["canonical_row_count_after"] == item["canonical_row_count_before"] + 240 for item in provenance)
    assert all(
        item["staging_target_row_set_sha256"]
        == item["canonical_target_row_set_sha256"]
        for item in provenance
    )
    assert all(Path(item["canonical_path"]).parent == minute_dir / "lifecycle_v1" for item in provenance)
    assert all(item["legacy_canonical_sha256_before"] == item["legacy_canonical_sha256_after"] for item in provenance)
    assert all(path.read_bytes() == content for path, content in legacy_before.items())


def test_end_to_end_apply_avoids_windows_max_path_for_stage6_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as importer

    run_id = "round13-long-path"

    def legacy_candidate_path(root: Path) -> Path:
        return (
            root
            / "results"
            / "data_chain"
            / "round13_minute_gap_backfill"
            / "runs"
            / run_id
            / "staging"
            / "2026-07-28"
            / f".append_transaction_{'0' * 32}"
            / "candidates"
            / "920000.csv"
        )

    probe_root = tmp_path / "project"
    padding_length = max(0, 270 - len(str(legacy_candidate_path(probe_root))))
    padded_parent = tmp_path / ("x" * padding_length) if padding_length else tmp_path
    root = _runtime_root(padded_parent)
    # Keep this a real append: the new contract stores signed observations in
    # their own schema segment instead of rewriting an old CSV header.
    _write_existing_lifecycle(root)
    legacy_candidate = legacy_candidate_path(root)
    staged_file = (
        root
        / "results"
        / "data_chain"
        / "round13_minute_gap_backfill"
        / "runs"
        / run_id
        / "staging"
        / "2026-07-28"
        / "920000.csv"
    )
    assert len(str(staged_file)) < 260
    assert len(str(legacy_candidate)) >= 260

    real_append_candidate = importer._append_candidate
    observed_candidates: list[Path] = []

    def append_with_win32_path_limit(
        target: Path,
        candidate: Path,
        header: list[str],
        stage: pd.DataFrame,
    ) -> None:
        observed_candidates.append(candidate)
        if len(str(candidate)) >= 260:
            raise OSError(206, "The filename or extension is too long", str(candidate))
        real_append_candidate(target, candidate, header, stage)

    monkeypatch.setattr(importer, "_append_candidate", append_with_win32_path_limit)

    payload = run_round13_minute_gap_backfill(
        root,
        network=True,
        apply=True,
        run_id=run_id,
        provider_updater=_provider_updater,
    )

    assert payload["status"] == "completed"
    assert observed_candidates
    assert all(len(str(path)) < 260 for path in observed_candidates)
    canonical_dir = root / "data" / "intraday" / "1m"
    staging_root = (
        root
        / "results"
        / "data_chain"
        / "round13_minute_gap_backfill"
        / "runs"
        / run_id
        / "staging"
    )
    assert all(not path.is_relative_to(canonical_dir) for path in observed_candidates)
    assert all(not path.is_relative_to(staging_root) for path in observed_candidates)


def test_missing_one_stage_file_blocks_whole_batch_before_import(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    importer = Mock(side_effect=AssertionError("importer must not run"))

    def provider(symbols: list[str], target_date: str, **kwargs) -> dict:
        _provider_updater(symbols, target_date, **kwargs)
        if target_date == "2026-07-29":
            (Path(kwargs["intraday_dir"]) / "920694.csv").unlink()
        return {"status": "partial"}

    payload = run_round13_minute_gap_backfill(
        root,
        network=True,
        apply=True,
        run_id="round13-missing",
        provider_updater=provider,
        append_importer=importer,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["whole_batch_quality_gate_failed"]
    importer.assert_not_called()


@pytest.mark.parametrize("existing_segments", [False, True])
def test_second_date_import_failure_restores_first_date_canonical_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_segments: bool,
) -> None:
    from quantdesk.intraday import historical_gap_backfill as module

    root = _runtime_root(tmp_path)
    if existing_segments:
        _write_existing_lifecycle(root)
    minute_dir = root / "data" / "intraday" / "1m"
    before = {
        path.relative_to(minute_dir).as_posix(): path.read_bytes()
        for path in minute_dir.rglob("*.csv")
    }
    apply_calls = 0
    restore_calls = 0

    def assert_singleton_lock_held() -> None:
        with pytest.raises(TimeoutError):
            with interprocess_file_lock(canonical_intraday_write_lock_path(minute_dir), timeout_seconds=0):
                pass

    original_restore = module._restore_snapshot

    def restore_while_locked(*args, **kwargs):
        nonlocal restore_calls
        restore_calls += 1
        assert_singleton_lock_held()
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(module, "_restore_snapshot", restore_while_locked)

    def importer(**kwargs) -> dict:
        nonlocal apply_calls
        assert_singleton_lock_held()
        if kwargs["apply"] is False:
            return apply_audited_intraday_batch(**kwargs)
        apply_calls += 1
        if apply_calls == 2:
            return {"status": "failed", "applied": False}
        return apply_audited_intraday_batch(**kwargs)

    payload = run_round13_minute_gap_backfill(
        root,
        network=True,
        apply=True,
        run_id="round13-outer-rollback",
        provider_updater=_provider_updater,
        append_importer=importer,
    )

    assert payload["status"] == "failed"
    assert payload["canonical_import"]["rollback_verified"] is True
    assert restore_calls == 1
    after = {
        path.relative_to(minute_dir).as_posix(): path.read_bytes()
        for path in minute_dir.rglob("*.csv")
    }
    assert after == before, "whole-batch rollback must also remove or restore new-schema segments"
    for name, content in before.items():
        assert (root / "data" / "intraday" / "1m" / name).read_bytes() == content
    with interprocess_file_lock(canonical_intraday_write_lock_path(minute_dir), timeout_seconds=0):
        pass


def test_post_import_evidence_corruption_restores_every_schema_segment(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    minute_dir = root / "data" / "intraday" / "1m"
    before = {path.relative_to(minute_dir): path.read_bytes() for path in minute_dir.rglob("*.csv")}

    def importer(**kwargs) -> dict:
        result = apply_audited_intraday_batch(**kwargs)
        if kwargs["apply"] and kwargs["target_date"] == "2026-07-28" and result["status"] == "completed":
            target = Path(result["items"][0]["canonical_path"])
            events = pd.read_csv(target, dtype=str, keep_default_na=False)
            events.loc[0, "evidence_hash"] = "0" * 64
            events.to_csv(target, index=False, encoding="utf-8-sig", lineterminator="\n")
        return result

    result = run_round13_minute_gap_backfill(
        root, network=True, apply=True, run_id="round13-post-read-corruption",
        provider_updater=_provider_updater, append_importer=importer,
    )

    assert result["status"] == "failed", result
    assert "minute_event_hash_mismatch" in result["canonical_import"]["error"]
    assert result["canonical_import"]["rollback_verified"] is True
    assert {path.relative_to(minute_dir): path.read_bytes() for path in minute_dir.rglob("*.csv")} == before


def test_preflight_rejects_receipt_target_outside_requested_schema_paths(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    minute_dir = root / "data" / "intraday" / "1m"
    before = {path.relative_to(minute_dir): path.read_bytes() for path in minute_dir.rglob("*.csv")}

    def importer(**kwargs) -> dict:
        assert kwargs["apply"] is False, "a mismatched target must be rejected before any write"
        result = apply_audited_intraday_batch(**kwargs)
        result["items"][0]["canonical_path"] = str(minute_dir / "unrequested.csv")
        return result

    result = run_round13_minute_gap_backfill(
        root, network=True, apply=True, run_id="round13-target-mismatch",
        provider_updater=_provider_updater, append_importer=importer,
    )

    assert result["status"] == "blocked", result
    assert result["canonical_import"]["error"] == "canonical_import_path_mismatch"
    assert {path.relative_to(minute_dir): path.read_bytes() for path in minute_dir.rglob("*.csv")} == before


def test_outer_transaction_lock_ownership_is_local_to_the_holding_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as module

    canonical, staging = tmp_path / "canonical", tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(module, "CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS", 0)

    def competing_append() -> None:
        module.apply_audited_intraday_batch(
            canonical_dir=canonical, staging_dir=staging, symbols=["600000"],
            target_date="2026-07-28", run_id="competing-thread", apply=True,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with module.canonical_intraday_write_transaction(canonical):
            pending = executor.submit(competing_append)
            with pytest.raises(TimeoutError, match="interprocess_file_lock_timeout:"):
                pending.result(timeout=5)
    with interprocess_file_lock(canonical_intraday_write_lock_path(canonical), timeout_seconds=0):
        pass


def test_offline_apply_gate_and_shared_lock_precede_provider_or_output(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    provider = Mock(side_effect=AssertionError("provider must stay off"))

    offline = run_round13_minute_gap_backfill(
        root, network=False, apply=False, provider_updater=provider
    )
    assert offline["status"] == "planned_offline"
    blocked = run_round13_minute_gap_backfill(
        root, network=False, apply=True, provider_updater=provider
    )
    assert blocked["reason_codes"] == ["apply_requires_network"]
    provider.assert_not_called()

    lock = root / "state" / "daily_data_chain.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(lock, timeout_seconds=0):
        busy = run_round13_minute_gap_backfill(
            root,
            network=True,
            apply=False,
            run_id="round13-busy",
            provider_updater=provider,
        )
    assert busy["status"] == "busy"
    provider.assert_not_called()
    assert not (root / "results").exists()
