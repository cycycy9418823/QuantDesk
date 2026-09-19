from __future__ import annotations

import csv
import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_weekly_wide_pool_maintenance as weekly_cli
from quantdesk.daily_price_providers import STANDARD_DAILY_COLUMNS
from quantdesk.daily_session_evidence import build_baostock_session_evidence
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.weekly_daily_wide_pool import (
    apply_audited_weekly_daily_batch,
    build_weekly_daily_wide_pool_contract,
    coordinate_weekly_daily_update,
    freeze_weekly_daily_wide_pool_contract,
    run_weekly_daily_wide_pool,
    run_weekly_wide_pool_maintenance,
    validate_weekly_daily_contract,
    write_weekly_daily_wide_pool_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _daily_frame(symbol: str, target_date: str = "2026-07-30") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": target_date,
                "symbol": symbol,
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 240000.0,
                "amount": 2448000.0,
                "change": 0.1,
                "pct_chg": 1.0,
                "adjust": "raw",
                "source": "mootdx_daily",
                "updated_at": "2026-07-31T16:00:00+08:00",
            }
        ],
        columns=STANDARD_DAILY_COLUMNS,
    )


def _fixture_root(
    tmp_path: Path,
    *,
    market_symbols: tuple[str, ...] = ("000001", "000002", "000003", "000004"),
    canonical_symbols: tuple[str, ...] = ("000001", "000002", "000003", "900001", "920999"),
) -> Path:
    root = tmp_path / "project"
    config = root / "config"
    config.mkdir(parents=True)
    shutil.copy2(ROOT / "config" / "weekly_daily_wide_pool_contract.json", config)
    _write_csv(
        root / "data" / "universe" / "a_share_symbols.csv",
        ["symbol", "name", "source", "updated_at"],
        [
            {
                "symbol": symbol,
                "name": symbol,
                "source": "baostock.query_stock_basic",
                "updated_at": "2026-07-31T08:00:00+08:00",
            }
            for symbol in market_symbols
        ],
    )
    _write_csv(
        root / "data" / "universe" / "low_price_learning_symbols.csv",
        ["symbol", "included"],
        [
            {"symbol": "000001", "included": "True"},
            {"symbol": "920999", "included": "yes"},
            {"symbol": "000004", "included": "False"},
        ],
    )
    daily = root / "data" / "daily"
    daily.mkdir(parents=True)
    for symbol in canonical_symbols:
        _daily_frame(symbol).to_csv(
            daily / f"{symbol}.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
        )
    return root


def _contract(root: Path, *, exclusions: tuple[str, ...] = ()) -> dict:
    return build_weekly_daily_wide_pool_contract(
        root,
        target_date="2026-07-31",
        explicit_exclusions=exclusions,
    )


def _fresh_provider(
    provider: str,
    symbol: str,
    start_date: str,
    target_date: str,
    *,
    network: bool,
) -> tuple[pd.DataFrame, list[str]]:
    assert network is True
    frame = _daily_frame(symbol, target_date)
    frame["source"] = provider
    return frame, []


def _baostock_empty_frame_with_session_evidence(
    symbol: str,
    start_date: str,
    target_date: str,
    *,
    evidence_symbol: str | None = None,
    evidence_start_date: str | None = None,
    evidence_end_date: str | None = None,
) -> pd.DataFrame:
    evidence_symbol = evidence_symbol or symbol
    evidence_start_date = evidence_start_date or start_date
    evidence_end_date = evidence_end_date or target_date
    dates = pd.date_range(evidence_start_date, evidence_end_date, freq="D")
    frame = pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)
    frame.attrs["suspension_evidence"] = build_baostock_session_evidence(
        symbol=evidence_symbol,
        provider_code=("sh." if evidence_symbol.startswith("6") else "sz.")
        + evidence_symbol,
        query_start_date=evidence_start_date,
        query_end_date=evidence_end_date,
        records=[
            {"trade_date": value.strftime("%Y-%m-%d"), "tradestatus": "0"}
            for value in dates
        ],
        captured_at="2026-08-09T08:00:00+08:00",
    )
    return frame


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    captured: dict[str, object] = {}
    from quantdesk import full_a_daily_reconciliation
    audit_calls = []
    monkeypatch.setattr(full_a_daily_reconciliation, "run_full_a_daily_reconciliation", lambda **kw:
                        audit_calls.append(kw) or {"status": "completed_with_warnings", "target_date": kw["target_date"]})
    monkeypatch.setattr(
        weekly_cli,
        "resolve_target_complete_trade_date",
        lambda value, calendar_path: {"target_date": "2026-07-31"},
    )
    monkeypatch.setattr(
        weekly_cli,
        "run_weekly_wide_pool_maintenance",
        lambda root, **kwargs: captured.update(kwargs)
        or {
            "status": "planned_offline",
            "target_date": kwargs["target_date"],
            "counts": {},
        },
    )

    assert weekly_cli.main([*apply_args, "--json", "false"]) == 0

    if expected_apply:
        assert captured == {}
        assert audit_calls == [{"project_root": weekly_cli.PROJECT_ROOT, "target_date": "2026-07-31", "audit_only": True}]
    else:
        assert captured["apply"] is False
        assert audit_calls == []


def test_cli_summary_accepts_bare_flag_and_explicit_false() -> None:
    assert weekly_cli.parse_args(["--summary"]).summary == "true"
    assert weekly_cli.parse_args(["--summary", "false"]).summary == "false"


def test_contract_freezes_current_market_intersection_and_excludes_legacy_daily(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)

    contract = _contract(root, exclusions=("000003",))

    assert contract["schema_version"] == "weekly_daily_wide_pool_contract_v2"
    assert contract["symbols"] == ["000002"]
    assert contract["counts"] == {
        "market_universe": 4,
        "canonical_daily": 5,
        "market_with_canonical_daily": 3,
        "market_missing_canonical_daily": 1,
        "canonical_daily_outside_market": 2,
        "formal_daily_analysis": 2,
        "formal_in_market_with_canonical": 1,
        "explicit_exclusions": 1,
        "weekly_daily_wide": 1,
    }
    assert contract["formal_eligible"] is True
    assert contract["market_universe_audit"]["status"] == "passed"
    assert contract["canonical_daily_audit"]["status"] == "passed"
    assert contract["canonical_daily_audit"]["checked_file_count"] == 3
    assert validate_weekly_daily_contract(contract, project_root=root) == []


@pytest.mark.parametrize(
    "field",
    [
        "task_type",
        "paths",
        "rules",
        "required_flow",
        "boundaries",
        "counts",
        "formal_symbols",
        "explicit_exclusions",
        "symbols",
    ],
)
def test_contract_digest_covers_every_governed_field(tmp_path: Path, field: str) -> None:
    root = _fixture_root(tmp_path)
    contract = _contract(root)
    if isinstance(contract[field], list):
        contract[field] = [*contract[field], "tampered"]
    elif isinstance(contract[field], dict):
        contract[field] = {**contract[field], "tampered": True}
    else:
        contract[field] = "tampered"

    reasons = validate_weekly_daily_contract(contract)

    assert "contract_digest_mismatch" in reasons


def test_contract_binds_config_spec_hash_and_ordered_membership(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    first = _contract(root)
    second = _contract(root)

    assert first["contract_sha256"] == second["contract_sha256"]
    assert first["symbols"] == sorted(first["symbols"])
    assert first["hashes"]["config_spec_sha256"]

    spec = root / "config" / "weekly_daily_wide_pool_contract.json"
    spec.write_text(spec.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert "contract_spec_hash_mismatch" in validate_weekly_daily_contract(
        first, project_root=root
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("stale", "market_universe_stale"),
        ("unapproved_source", "market_universe_source_not_approved"),
        ("missing_source", "market_universe_rows_invalid"),
    ],
)
def test_market_universe_source_and_freshness_fail_closed(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    root = _fixture_root(tmp_path)
    path = root / "data" / "universe" / "a_share_symbols.csv"
    rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig")))
    if mutation == "stale":
        rows[0]["updated_at"] = "2020-01-01T00:00:00+08:00"
    elif mutation == "unapproved_source":
        rows[0]["source"] = "local_fallback_universe"
    else:
        rows[0]["source"] = ""
    _write_csv(path, ["symbol", "name", "source", "updated_at"], rows)

    contract = _contract(root)

    assert contract["formal_eligible"] is False
    assert reason in contract["reason_codes"]


@pytest.mark.parametrize("mutation", ["empty", "bad_header", "symlink"])
def test_canonical_entity_header_and_nonempty_validation(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _fixture_root(tmp_path)
    path = root / "data" / "daily" / "000002.csv"
    if mutation == "empty":
        path.write_text(",".join(STANDARD_DAILY_COLUMNS) + "\n", encoding="utf-8")
    elif mutation == "bad_header":
        path.write_text("date,symbol,close\n2026-07-30,000002,10\n", encoding="utf-8")
    else:
        outside = tmp_path / "outside.csv"
        _daily_frame("000002").to_csv(outside, index=False)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("file symlink unavailable")

    contract = _contract(root)

    assert contract["formal_eligible"] is False
    assert "canonical_daily_files_invalid" in contract["reason_codes"]
    assert "000002" in contract["canonical_daily_audit"]["invalid_symbols"]


def test_contract_can_be_written_only_inside_the_authorized_run_root(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    contract = _contract(root)
    run_dir = root / "results" / "data_chain" / "weekly_daily_wide_pool" / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    output = write_weekly_daily_wide_pool_contract(
        contract, run_dir / "pool_contract.json", allowed_root=run_dir
    )

    assert json.loads(output.read_text(encoding="utf-8"))["contract_sha256"] == contract["contract_sha256"]
    with pytest.raises(ValueError, match="path_outside_project_root"):
        write_weekly_daily_wide_pool_contract(
            contract, root / "data" / "contract.json", allowed_root=run_dir
        )


def test_measured_contract_freeze_is_append_only_idempotent_and_conflict_closed(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    contract = _contract(root)

    first_contract, first = freeze_weekly_daily_wide_pool_contract(root, contract)
    second_contract, second = freeze_weekly_daily_wide_pool_contract(root, contract)

    assert first["status"] == "written"
    assert second["status"] == "reused_identical"
    assert first_contract["contract_sha256"] == second_contract["contract_sha256"]
    assert first["path"].endswith(
        "data\\universe\\weekly_daily_wide_pool_contract_2026-07-31.json"
    )
    frozen_bytes = Path(first["path"]).read_bytes()

    conflicting = dict(contract)
    conflicting["contract_sha256"] = "0" * 64
    _candidate, conflict = freeze_weekly_daily_wide_pool_contract(root, conflicting)

    assert conflict["status"] == "conflict"
    assert conflict["reason_codes"] == ["same_day_frozen_contract_hash_conflict"]
    assert Path(first["path"]).read_bytes() == frozen_bytes


def test_end_to_end_staging_reaudits_real_file_without_touching_canonical(
    tmp_path: Path,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )
    canonical = root / "data" / "daily" / "000002.csv"
    before = canonical.read_bytes()

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id="fixture-stage",
        workers=2,
        provider_fetcher=_fresh_provider,
    )

    assert payload["status"] == "completed_staging_only"
    assert payload["frozen_contract"]["status"] == "written"
    assert payload["frozen_contract"]["bytes"] > 0
    assert payload["frozen_contract"]["sha256"]
    assert payload["stage_audit"]["summary"] == {"requested": 1, "passed": 1, "blocked": 0}
    item = payload["stage_audit"]["items"][0]
    assert Path(item["staging_path"]).is_file()
    assert item["staging_sha256"]
    assert item["source"] == "mootdx_daily"
    assert canonical.read_bytes() == before


def test_empty_baostock_frame_with_matching_suspension_evidence_passes(
    tmp_path: Path,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )

    def provider(name: str, symbol: str, start: str, target: str, *, network: bool):
        assert name == "baostock_daily"
        return (
            _baostock_empty_frame_with_session_evidence(symbol, start, target),
            ["provider returned empty dataframe"],
        )

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id="fixture-empty-suspension",
        workers=1,
        provider_order=("baostock_daily",),
        provider_fetcher=provider,
    )

    assert payload["status"] == "completed_staging_only"
    item = payload["stage_audit"]["items"][0]
    assert item["status"] == "passed"
    assert item["operation"] == "verified_no_session_only"
    assert item["verified_no_session_dates"] == ["2026-07-31"]
    assert item["staging_path"] == ""


@pytest.mark.parametrize("evidence_mismatch", ["symbol", "window"])
def test_empty_baostock_frame_rejects_mismatched_session_evidence(
    tmp_path: Path,
    evidence_mismatch: str,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )

    def provider(name: str, symbol: str, start: str, target: str, *, network: bool):
        kwargs = (
            {"evidence_symbol": "000003"}
            if evidence_mismatch == "symbol"
            else {"evidence_start_date": "2026-07-30"}
        )
        return (
            _baostock_empty_frame_with_session_evidence(
                symbol,
                start,
                target,
                **kwargs,
            ),
            ["provider returned empty dataframe"],
        )

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id=f"fixture-reject-{evidence_mismatch}",
        workers=1,
        provider_order=("baostock_daily",),
        provider_fetcher=provider,
    )

    assert payload["status"] == "blocked"
    item = payload["stage_audit"]["items"][0]
    assert item["status"] == "blocked"
    assert item["operation"] == "blocked"
    assert item["verified_no_session_dates"] == []


def test_baostock_verified_suspension_rejects_mootdx_zero_sentinel_row(
    tmp_path: Path,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )
    canonical = root / "data" / "daily" / "000002.csv"
    existing = _daily_frame("000002", "2026-07-29")
    existing.to_csv(canonical, index=False, encoding="utf-8-sig", lineterminator="\n")
    before = canonical.read_bytes()

    def provider(name: str, symbol: str, start: str, target: str, *, network: bool):
        if name == "mootdx_daily":
            sentinel = _daily_frame(symbol, "2026-07-31")
            sentinel[["open", "high", "low", "close", "volume", "amount"]] = 0.0
            return sentinel, ["mootdx_zero_decoder_sentinel_normalized: rows=1; fields=3"]
        assert name == "baostock_daily"
        return (
            _baostock_empty_frame_with_session_evidence(symbol, start, target),
            ["provider returned empty dataframe"],
        )

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=True,
        run_id="fixture-sentinel-suspension",
        workers=1,
        provider_order=("mootdx_daily", "baostock_daily"),
        provider_fetcher=provider,
    )

    assert payload["status"] == "completed"
    item = payload["stage_audit"]["items"][0]
    assert item["operation"] == "verified_no_session_only"
    assert item["verified_no_session_dates"] == ["2026-07-30", "2026-07-31"]
    assert item["staging_path"] == ""
    assert canonical.read_bytes() == before


def test_apply_is_append_only_and_records_before_after_provenance(tmp_path: Path) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )
    canonical = root / "data" / "daily" / "000002.csv"
    before = canonical.read_bytes()

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=True,
        run_id="fixture-apply",
        workers=1,
        provider_fetcher=_fresh_provider,
    )

    assert payload["status"] == "completed"
    item = payload["canonical_import"]["items"][0]
    assert item["canonical_sha256_before"] != item["canonical_sha256_after"]
    assert item["canonical_row_count_before"] == 1
    assert item["canonical_row_count_after"] == 2
    assert canonical.read_bytes().startswith(before)


def test_append_accepts_reordered_matching_headers_without_rewriting_canonical_history(
    tmp_path: Path,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )
    canonical = root / "data" / "daily" / "000002.csv"
    reordered_header = ["symbol", "date", *STANDARD_DAILY_COLUMNS[2:]]
    existing = pd.read_csv(canonical, encoding="utf-8-sig", dtype={"symbol": str})
    existing[reordered_header].to_csv(
        canonical,
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    before = canonical.read_bytes()

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id="fixture-reordered-header",
        workers=1,
        provider_fetcher=_fresh_provider,
    )

    assert payload["status"] == "completed_staging_only"
    item = payload["canonical_import"]["items"][0]
    candidate = Path(item["candidate_path"])
    candidate_bytes = candidate.read_bytes()
    assert candidate_bytes.startswith(before)
    with candidate.open("r", newline="", encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == reordered_header


def test_failed_item_does_not_suppress_later_valid_candidate_plan(tmp_path: Path) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002", "000003"),
        canonical_symbols=("000001", "000002", "000003"),
    )
    contract = _contract(root)
    run_dir = root / "results" / "data_chain" / "weekly_daily_wide_pool" / "runs" / "reaudit"
    staging_dir = run_dir / "staging"
    items: list[dict[str, object]] = []
    before = {
        symbol: (root / "data" / "daily" / f"{symbol}.csv").read_bytes()
        for symbol in contract["symbols"]
    }
    for symbol in contract["symbols"]:
        canonical = root / "data" / "daily" / f"{symbol}.csv"
        target = "2026-07-30" if symbol == "000002" else "2026-07-31"
        staged = _daily_frame(symbol, target)
        if symbol == "000002":
            staged.loc[0, "close"] = 99.0
        staging = staging_dir / f"{symbol}.csv"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staged.to_csv(staging, index=False, encoding="utf-8-sig", lineterminator="\n")
        items.append(
            {
                "symbol": symbol,
                "status": "passed",
                "operation": "stage_append",
                "canonical_sha256_before": _sha256(canonical),
                "staging_path": str(staging),
                "staging_sha256": _sha256(staging),
            }
        )

    payload = apply_audited_weekly_daily_batch(
        project_root=root,
        contract=contract,
        stage_audit={"status": "passed", "items": items},
        run_dir=run_dir,
        apply=False,
    )

    assert payload["status"] == "blocked"
    assert "canonical_daily_date_conflict" in payload["reason_codes"]
    assert [item["symbol"] for item in payload["items"]] == ["000003"]
    assert (run_dir / "canonical_candidates" / "000003.csv").is_file()
    for symbol, content in before.items():
        assert (root / "data" / "daily" / f"{symbol}.csv").read_bytes() == content


def test_full_pool_gate_blocks_apply_when_one_staged_file_is_not_raw(tmp_path: Path) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002", "000003"),
        canonical_symbols=("000001", "000002", "000003"),
    )
    before = {
        symbol: (root / "data" / "daily" / f"{symbol}.csv").read_bytes()
        for symbol in ("000002", "000003")
    }

    def provider(name: str, symbol: str, start: str, target: str, *, network: bool):
        frame, warnings = _fresh_provider(name, symbol, start, target, network=network)
        if symbol == "000003":
            frame["adjust"] = "qfq"
        return frame, warnings

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=True,
        run_id="fixture-block",
        workers=2,
        provider_fetcher=provider,
    )

    assert payload["status"] == "blocked"
    assert payload["stage_audit"]["summary"]["blocked"] == 1
    for symbol, content in before.items():
        assert (root / "data" / "daily" / f"{symbol}.csv").read_bytes() == content


def test_append_failure_rolls_back_every_previously_replaced_symbol(tmp_path: Path) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002", "000003"),
        canonical_symbols=("000001", "000002", "000003"),
    )
    before = {
        symbol: (root / "data" / "daily" / f"{symbol}.csv").read_bytes()
        for symbol in ("000002", "000003")
    }
    calls = 0

    def fail_second(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture replace failure")
        source.replace(target)

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=True,
        run_id="fixture-rollback",
        workers=2,
        provider_fetcher=_fresh_provider,
        replace_func=fail_second,
    )

    assert payload["status"] == "blocked"
    assert payload["canonical_import"]["rollback_verified"] is True
    for symbol, content in before.items():
        assert (root / "data" / "daily" / f"{symbol}.csv").read_bytes() == content


def test_workers_are_bounded_and_real_staging_runs_concurrently(tmp_path: Path) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002", "000003", "000004"),
        canonical_symbols=("000001", "000002", "000003", "000004"),
    )
    active = 0
    peak = 0
    mutex = threading.Lock()

    def provider(name: str, symbol: str, start: str, target: str, *, network: bool):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        try:
            return _fresh_provider(name, symbol, start, target, network=network)
        finally:
            with mutex:
                active -= 1

    payload = run_weekly_daily_wide_pool(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id="fixture-concurrency",
        workers=2,
        provider_fetcher=provider,
    )

    assert payload["status"] == "completed_staging_only"
    assert 1 < peak <= 2
    with pytest.raises(ValueError, match="workers_outside_bounded_range"):
        run_weekly_daily_wide_pool(root, target_date="2026-07-31", workers=9)


def test_shared_daily_lock_blocks_before_contract_or_run_output(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    lock_path = root / "state" / "daily_data_chain.lock"
    lock_path.parent.mkdir(parents=True)

    with interprocess_file_lock(lock_path, timeout_seconds=0):
        payload = run_weekly_daily_wide_pool(
            root,
            target_date="2026-07-31",
            network=True,
            apply=False,
            run_id="busy-run",
            provider_fetcher=_fresh_provider,
        )

    assert payload["status"] == "busy"
    assert not (root / "results").exists()


def test_combined_weekly_runner_reports_daily_and_555_minute_counts_separately(
    tmp_path: Path,
) -> None:
    root = _fixture_root(
        tmp_path,
        market_symbols=("000001", "000002"),
        canonical_symbols=("000001", "000002"),
    )

    def minute_runner(project_root: Path, **kwargs) -> dict:
        return {
            "status": "completed_staging_only",
            "pool_contract": {"counts": {"weekly_wide": 555}},
        }

    payload = run_weekly_wide_pool_maintenance(
        root,
        target_date="2026-07-31",
        network=True,
        apply=False,
        run_id="combined-run",
        daily_workers=2,
        daily_provider_fetcher=_fresh_provider,
        minute_runner=minute_runner,
    )

    assert payload["status"] == "completed_staging_only"
    assert payload["counts"] == {"daily_wide": 1, "minute_residual": 555}
    assert payload["count_scopes_must_not_be_combined"] is True


def test_pure_coordinator_rejects_digest_tamper_before_dto_checks(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    contract = _contract(root)
    contract["rules"]["whole_pool_quality_gate"] = False

    result = coordinate_weekly_daily_update(contract, apply_requested=True)

    assert result["status"] == "blocked"
    assert "contract_digest_mismatch" in result["reason_codes"]
