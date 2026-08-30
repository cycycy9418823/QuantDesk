from __future__ import annotations

import json
import os
import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _freeze_round6_archive_day(monkeypatch) -> None:
    import quantdesk.data_truth.sector_history_archive as archive

    monkeypatch.setattr(archive, "_local_today", lambda: date(2026, 7, 22))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_sector_history_runtime_is_git_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/sector_history/" in gitignore


def test_pit_audit_grades_sources_and_never_promotes_unbundled_membership(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.pit_sector_evidence_audit import (
        VERDICT_NOT_PIT,
        VERDICT_UNCERTAIN,
        run_pit_sector_evidence_audit,
        write_pit_sector_evidence_audit_outputs,
    )

    staging = tmp_path / "staging"
    _write(
        staging / "tushare" / "ths_member" / "eligible.csv",
        "ts_code,con_code,con_name,in_date,out_date\n"
        "700001.TI,000001.SZ,Alpha,2020-01-01,\n"
        "700001.TI,000001.SZ,Alpha,2024-01-01,\n",
    )
    _write(
        staging / "baostock" / "industry_classification.csv",
        "code,industry,updateDate\n000001,Finance,2026-07-06\n000002,Energy,2026-07-06\n",
    )
    _write(
        staging / "local" / "industry_theme.csv",
        "symbol,sector,updated_at\n000001,Finance,2026-07-22T10:00:00\n",
    )

    payload = run_pit_sector_evidence_audit(
        staging_dir=staging,
        replay_window_start="2026-06-22",
        replay_window_end="2026-07-17",
        sample_per_group=10,
        target_symbols=["000001"],
    )

    assert payload["overall_verdict"] == VERDICT_UNCERTAIN
    assert payload["verdict_counts"]["pit_eligible"] == 0
    assert payload["verdict_counts"][VERDICT_UNCERTAIN] == 2
    assert payload["verdict_counts"][VERDICT_NOT_PIT] == 1
    member = next(
        source for source in payload["sources"] if source["kind"] == "index_member_history"
    )
    assert member["covered_symbol_count"] == 1
    assert member["target_symbol_count"] == 1
    assert member["target_symbols_missing"] == []
    assert member["verdict"] == VERDICT_UNCERTAIN
    assert "source_revision_not_proven" in member["reason_codes"]
    assert "verified_membership_heat_bundle_missing" in member["reason_codes"]
    for source in payload["sources"]:
        assert source["provenance_triple"]["source_hash"].startswith("sha256:")
        assert source["provenance_triple"]["source_path"]
        assert source["source_revision_status"]
        assert source["source_revision_basis"]

    apply_boundary = payload["apply_boundary"]
    assert apply_boundary["approval_required"] is True
    assert apply_boundary["approved"] is False
    assert apply_boundary["canonical_write"] is False
    assert apply_boundary["proposed_scope"]["files"] == 0
    assert apply_boundary["proposed_scope"]["bytes"] == 0
    assert apply_boundary["proposed_scope"]["date_start"] == ""
    assert apply_boundary["proposed_scope"]["date_end"] == ""

    output = write_pit_sector_evidence_audit_outputs(
        payload,
        output_path=tmp_path / "results" / "audit.json",
        report_path=tmp_path / "reports" / "audit.md",
    )
    assert Path(output["output_path"]).exists()
    assert Path(output["report_path"]).exists()
    loaded = json.loads(Path(output["output_path"]).read_text(encoding="utf-8"))
    assert loaded["overall_verdict"] == VERDICT_UNCERTAIN
    assert "PIT" in Path(output["report_path"]).read_text(encoding="utf-8")


def test_pit_audit_bounds_large_missing_symbol_serialization(tmp_path: Path) -> None:
    from quantdesk.data_truth.pit_sector_evidence_audit import (
        run_pit_sector_evidence_audit,
        write_pit_sector_evidence_audit_outputs,
    )

    staging = tmp_path / "staging"
    _write(
        staging / "tushare" / "ths_member" / "partial.csv",
        "ts_code,con_code,con_name,in_date,out_date\n"
        "700001.TI,000001.SZ,Alpha,2020-01-01,\n",
    )
    targets = [f"{value:06d}" for value in range(1, 81)]
    complete_missing = targets[1:]
    expected_sample = complete_missing[:32] + complete_missing[-32:]
    expected_sha256 = "sha256:" + hashlib.sha256(
        "\n".join(complete_missing).encode("utf-8")
    ).hexdigest()

    payload = run_pit_sector_evidence_audit(
        staging_dir=staging,
        replay_window_start="2026-06-22",
        replay_window_end="2026-07-17",
        sample_per_group=10,
        target_symbols=targets,
    )

    assert payload["payload_schema_version"] == 2
    member = next(
        source for source in payload["sources"] if source["kind"] == "index_member_history"
    )
    assert member["target_symbols_missing_count"] == len(complete_missing)
    assert member["target_symbols_missing_sha256"] == expected_sha256
    assert member["target_symbols_missing_truncated"] is True
    assert member["target_symbols_missing"] == expected_sample
    assert len(member["target_symbols_missing"]) == 64

    output = write_pit_sector_evidence_audit_outputs(
        payload,
        output_path=tmp_path / "results" / "audit.json",
        report_path=tmp_path / "reports" / "audit.md",
    )
    loaded = json.loads(Path(output["output_path"]).read_text(encoding="utf-8"))
    assert loaded["sources"][0]["target_symbols_missing"] == expected_sample
    report = Path(output["report_path"]).read_text(encoding="utf-8")
    assert "Missing count | Truncated | Missing sample" in report
    assert f"| {len(complete_missing)} | True |" in report


def test_pit_audit_missing_symbol_summary_is_order_and_duplicate_stable(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.pit_sector_evidence_audit import (
        run_pit_sector_evidence_audit,
    )

    staging = tmp_path / "staging"
    _write(
        staging / "tushare" / "ths_member" / "partial.csv",
        "ts_code,con_code,con_name,in_date,out_date\n"
        "700001.TI,000001.SZ,Alpha,2020-01-01,\n",
    )
    targets = [f"{value:06d}" for value in range(1, 81)]
    shuffled_with_duplicates = list(reversed(targets)) + [
        targets[10],
        targets[0],
        targets[-1],
    ]

    def run(target_symbols: list[str]) -> dict:
        return run_pit_sector_evidence_audit(
            staging_dir=staging,
            replay_window_start="2026-06-22",
            replay_window_end="2026-07-17",
            sample_per_group=10,
            target_symbols=target_symbols,
        )

    ordered_payload = run(targets)
    shuffled_payload = run(shuffled_with_duplicates)
    assert ordered_payload["payload_schema_version"] == 2
    assert shuffled_payload["payload_schema_version"] == 2

    ordered_member = ordered_payload["sources"][0]
    shuffled_member = shuffled_payload["sources"][0]
    summary_fields = (
        "target_symbols_missing_count",
        "target_symbols_missing_sha256",
        "target_symbols_missing_truncated",
        "target_symbols_missing",
    )
    assert {key: ordered_member[key] for key in summary_fields} == {
        key: shuffled_member[key] for key in summary_fields
    }
    assert ordered_member["target_symbols_missing_count"] == 79
    assert ordered_member["target_symbols_missing_truncated"] is True

    for payload in (ordered_payload, shuffled_payload):
        assert payload["overall_window_covered"] is False
        assert payload["verdict_counts"]["pit_eligible"] == 0
        proposed_scope = payload["apply_boundary"]["proposed_scope"]
        assert proposed_scope["files"] == 0
        assert proposed_scope["bytes"] == 0


@pytest.mark.parametrize("missing_count", [1, 64, 65])
def test_pit_audit_missing_symbol_sample_threshold(
    tmp_path: Path,
    missing_count: int,
) -> None:
    from quantdesk.data_truth.pit_sector_evidence_audit import (
        run_pit_sector_evidence_audit,
    )

    staging = tmp_path / "staging"
    _write(
        staging / "tushare" / "ths_member" / "partial.csv",
        "ts_code,con_code,con_name,in_date,out_date\n"
        "700001.TI,000001.SZ,Alpha,2020-01-01,\n",
    )
    targets = [f"{value:06d}" for value in range(1, missing_count + 2)]
    complete_missing = targets[1:]

    payload = run_pit_sector_evidence_audit(
        staging_dir=staging,
        replay_window_start="2026-06-22",
        replay_window_end="2026-07-17",
        sample_per_group=10,
        target_symbols=targets,
    )

    member = payload["sources"][0]
    expected_sample = (
        complete_missing
        if missing_count <= 64
        else complete_missing[:32] + complete_missing[-32:]
    )
    assert member["target_symbols_missing_count"] == missing_count
    assert member["target_symbols_missing_sha256"] == (
        "sha256:"
        + hashlib.sha256("\n".join(complete_missing).encode("utf-8")).hexdigest()
    )
    assert member["target_symbols_missing_truncated"] is (missing_count > 64)
    assert member["target_symbols_missing"] == expected_sample


def test_pit_audit_has_zero_apply_scope_without_window_eligible_source(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.pit_sector_evidence_audit import (
        VERDICT_UNCERTAIN,
        run_pit_sector_evidence_audit,
        write_pit_sector_evidence_audit_outputs,
    )

    staging = tmp_path / "staging"
    _write(
        staging / "baostock" / "industry_classification.csv",
        "code,industry,updateDate\n000001,Finance,2026-07-06\n",
    )

    payload = run_pit_sector_evidence_audit(
        staging_dir=staging,
        replay_window_start="2026-06-22",
        replay_window_end="2026-07-17",
        sample_per_group=10,
    )

    assert payload["overall_verdict"] == VERDICT_UNCERTAIN
    assert payload["overall_window_covered"] is False
    assert payload["apply_boundary"]["proposed_scope"] == {
        "directories": [],
        "files": 0,
        "bytes": 0,
        "date_start": "",
        "date_end": "",
        "eligible_source_paths": [],
        "reason": "no_pit_eligible_source_covers_replay_window",
    }
    assert payload["canonical_write"] is False

    output = write_pit_sector_evidence_audit_outputs(
        payload,
        output_path=tmp_path / "results" / "audit.json",
        report_path=tmp_path / "reports" / "audit.md",
    )
    report = Path(output["report_path"]).read_text(encoding="utf-8")
    assert "files: `0`" in report
    assert "bytes: `0`" in report
    assert "date_range: `empty`" in report


def _archive_fixture(root: Path) -> None:
    theme_path = _write(
        root / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Finance,Bank,fixture,2026-07-22T09:00:00\n"
        "000002,Beta,Energy,Oil,fixture,2026-07-22T09:00:00\n",
    )
    board_path = _write(
        root / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg,amount,leading_stock\n"
        "Finance,1.2,100,Alpha\n"
        "Energy,-0.4,80,Beta\n",
    )
    # The archive's same-day guard compares each source file's mtime day against
    # the frozen capture day (2026-07-22).  _write stamps the real wall-clock
    # day, so pin the fixture mtime to the frozen day to keep the date-stable
    # tests deterministic regardless of when the suite runs.
    frozen = datetime(2026, 7, 22, 9, 0, 0).timestamp()
    for path in (theme_path, board_path):
        os.utime(path, (frozen, frozen))


def _refresh_evidence(root: Path, *, board_count: int = 2) -> dict:
    theme_path = root / "data" / "universe" / "stock_theme_map.csv"
    board_path = root / "data" / "cache" / "industry_board_latest.csv"
    return {
        "sector_refresh": {
            "ok": True,
            "status": "completed",
            "network": True,
            "source": "fixture",
            "board_count": board_count,
            "cache_path": str(board_path.resolve()),
            "cache_sha256": f"sha256:{hashlib.sha256(board_path.read_bytes()).hexdigest()}",
            "captured_at": "2026-07-22T15:29:00",
        },
        "membership_observation": {
            "ok": True,
            "status": "completed",
            "source": "fixture_membership_provider",
            "path": str(theme_path.resolve()),
            "sha256": f"sha256:{hashlib.sha256(theme_path.read_bytes()).hexdigest()}",
            "captured_at": "2026-07-22T15:28:00",
            "row_count": sum(1 for _ in theme_path.open(encoding="utf-8")) - 1,
        }
    }


def test_forward_sector_archive_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    _archive_fixture(tmp_path)
    first = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path),
    )
    assert first["status"] == "passed"
    assert first["evidence_eligible"] is True
    assert first["historical_backfill"] is False
    assert first["provenance"]["hash"].startswith("sha256:")
    assert first["provenance"]["captured_at"] == "2026-07-22T15:30:00"
    assert first["provenance"]["cutoff"] == "2026-07-22"
    assert first["membership_input_count"] == 2
    assert first["membership_unknown_count"] == 0
    assert first["aligned_membership_count"] == 2
    assert first["membership_board_alignment_ratio"] == 1.0
    assert all(
        str(source["cutoff"]).startswith("2026-07-22T")
        for source in first["provenance"]["sources"]
    )
    snapshot_path = Path(first["snapshot_path"])
    before = snapshot_path.read_bytes()

    second = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path),
    )
    assert second["status"] == "unchanged_identical"
    assert snapshot_path.read_bytes() == before

    with pytest.raises(ValueError, match="sector_history_same_day_conflict"):
        archive_forward_sector_snapshot(
            tmp_path,
            target_date="2026-07-22",
            captured_at="2026-07-22T15:30:00",
            source_override={**_refresh_evidence(tmp_path), "extra": "conflict"},
        )

    degraded = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-21",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path),
    )
    assert degraded["status"] == "degraded"
    assert degraded["evidence_eligible"] is False
    assert "historical_target_not_current" in degraded["reason_codes"]
    assert not (tmp_path / "data" / "sector_history" / "2026-07-21").exists()


def test_forward_sector_archive_ignores_same_day_volatile_provenance_times(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    _archive_fixture(tmp_path)
    theme_path = tmp_path / "data" / "universe" / "stock_theme_map.csv"
    board_path = tmp_path / "data" / "cache" / "industry_board_latest.csv"
    source_paths = (theme_path, board_path)
    first_source_time = datetime.fromisoformat("2026-07-22T15:00:00").timestamp()
    for path in source_paths:
        os.utime(path, (first_source_time, first_source_time))
    first_evidence = _refresh_evidence(tmp_path)
    first_evidence["sector_refresh"]["updated_at"] = "2026-07-22T15:29:00"
    first_evidence["membership_observation"]["updated_at"] = (
        "2026-07-22T15:28:00"
    )

    first = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=first_evidence,
    )
    snapshot_path = Path(first["snapshot_path"])
    snapshot_before = snapshot_path.read_bytes()

    second_source_time = datetime.fromisoformat("2026-07-22T18:00:00").timestamp()
    for path in source_paths:
        os.utime(path, (second_source_time, second_source_time))
    second_evidence = _refresh_evidence(tmp_path)
    second_evidence["sector_refresh"]["captured_at"] = "2026-07-22T18:29:00"
    second_evidence["sector_refresh"]["updated_at"] = "2026-07-22T18:29:00"
    second_evidence["membership_observation"]["captured_at"] = (
        "2026-07-22T18:28:00"
    )
    second_evidence["membership_observation"]["updated_at"] = (
        "2026-07-22T18:28:00"
    )

    second = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T18:30:00",
        source_override=second_evidence,
    )

    assert second["status"] == "unchanged_identical"
    assert second["written"] is False
    assert snapshot_path.read_bytes() == snapshot_before
    assert second["provenance"]["captured_at"] == "2026-07-22T15:30:00"
    assert second["provenance"]["extra"]["sector_refresh"]["captured_at"] == (
        "2026-07-22T15:29:00"
    )
    assert all("T15:00:00" in source["cutoff"] for source in second["provenance"]["sources"])


def test_forward_sector_archive_missing_sources_degrades_without_write(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override={},
    )

    assert result["status"] == "degraded"
    assert result["evidence_eligible"] is False
    assert result["written"] is False
    assert "sector_membership_source_missing" in result["reason_codes"]
    assert "sector_board_source_missing" in result["reason_codes"]
    assert not (tmp_path / "data" / "sector_history").exists()


def test_forward_sector_archive_rejects_unknown_pending_membership(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    _write(
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,primary_theme,source,updated_at\n"
        "000001,Alpha,未知板块,未知行业,未知题材,pending,2026-07-22T09:00:00\n",
    )
    _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg\nFinance,1.2\n",
    )

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path, board_count=1),
    )

    assert result["status"] == "degraded"
    assert result["written"] is False
    assert result["membership_input_count"] == 1
    assert result["membership_unknown_count"] == 1
    assert "sector_membership_unknown_or_pending" in result["reason_codes"]
    assert not (tmp_path / "data" / "sector_history").exists()


def test_current_membership_capture_stages_same_taxonomy_without_canonical_write(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        capture_current_sector_membership,
    )

    board_path = _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg\nFinance,1.2\nEnergy,-0.4\n",
    )
    canonical_universe = tmp_path / "data" / "universe"
    canonical_universe.mkdir(parents=True)
    before = list(canonical_universe.iterdir())

    def fetcher(provider: str, board: str) -> pd.DataFrame:
        assert provider == "eastmoney"
        rows = {
            "Finance": [{"代码": "000001", "名称": "Alpha"}],
            "Energy": [{"代码": "000002", "名称": "Beta"}],
        }
        return pd.DataFrame(rows[board])

    result = capture_current_sector_membership(
        tmp_path,
        board_cache_path=board_path,
        provider="eastmoney",
        network=True,
        captured_at="2026-07-22T15:28:00",
        member_fetcher=fetcher,
        output_dir=tmp_path / "results" / "sector_membership_capture",
    )

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["board_count"] == 2
    assert result["fetched_board_count"] == 2
    assert result["row_count"] == 2
    assert result["sha256"].startswith("sha256:")
    captured = Path(result["path"])
    assert captured.is_file()
    frame = pd.read_csv(captured, dtype=str)
    assert set(frame["sector"]) == {"Finance", "Energy"}
    # Round 8 裁定十七: the capture artifact self-declares its observation moment
    # in a ``captured_at`` column (renamed from the mislabelled ``updated_at``),
    # so the archive gate reads a true capture timestamp rather than a source
    # revision column.
    assert set(frame["captured_at"]) == {"2026-07-22T15:28:00"}
    assert "updated_at" not in frame.columns
    assert list(canonical_universe.iterdir()) == before


def test_forward_sector_archive_requires_membership_board_taxonomy_alignment(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    _write(
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        "symbol,name,sector,industry,source,updated_at\n"
        "000001,Alpha,Taxonomy A,Industry A,fixture,2026-07-22T09:00:00\n",
    )
    _write(
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg\nTaxonomy B,1.2\n",
    )

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path, board_count=1),
    )

    assert result["status"] == "degraded"
    assert result["written"] is False
    assert result["aligned_membership_count"] == 0
    assert result["membership_board_alignment_ratio"] == 0.0
    assert "sector_membership_board_taxonomy_unaligned" in result["reason_codes"]


def test_forward_sector_archive_rejects_stale_current_cross_sections(
    tmp_path: Path,
) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
    )

    _archive_fixture(tmp_path)
    stale_timestamp = datetime.fromisoformat("2026-07-21T15:30:00").timestamp()
    for path in (
        tmp_path / "data" / "universe" / "stock_theme_map.csv",
        tmp_path / "data" / "cache" / "industry_board_latest.csv",
    ):
        os.utime(path, (stale_timestamp, stale_timestamp))

    result = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path),
    )

    assert result["status"] == "degraded"
    assert result["written"] is False
    assert "sector_membership_source_not_same_day" in result["reason_codes"]
    assert "sector_board_source_not_same_day" in result["reason_codes"]
    assert not (tmp_path / "data" / "sector_history").exists()


def test_revoked_forward_snapshot_is_never_loaded(tmp_path: Path) -> None:
    from quantdesk.data_truth.sector_history_archive import (
        archive_forward_sector_snapshot,
        load_forward_sector_snapshot,
        write_sector_snapshot_revocation,
    )

    _archive_fixture(tmp_path)
    archived = archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        captured_at="2026-07-22T15:30:00",
        source_override=_refresh_evidence(tmp_path),
    )
    assert archived["status"] == "passed"

    revoked = write_sector_snapshot_revocation(
        tmp_path,
        target_date="2026-07-22",
        snapshot_sha256=archived["snapshot_hash"],
        reason_codes=["membership_board_taxonomy_unaligned"],
        audited_at="2026-07-22T16:00:00",
    )
    assert revoked["written"] is True
    assert revoked["status"] == "revoked"
    assert load_forward_sector_snapshot(
        tmp_path / "data" / "sector_history", as_of="2026-07-22"
    ) == {}

    retry = write_sector_snapshot_revocation(
        tmp_path,
        target_date="2026-07-22",
        snapshot_sha256=archived["snapshot_hash"],
        reason_codes=["membership_board_taxonomy_unaligned"],
        audited_at="2026-07-22T16:00:00",
    )
    assert retry["status"] == "unchanged_identical"
    assert retry["written"] is False


def test_daily_chain_records_forward_sector_archive_in_isolated_root(tmp_path: Path, monkeypatch) -> None:
    import quantdesk.daily_data_chain as chain

    _archive_fixture(tmp_path)
    evidence = _refresh_evidence(tmp_path)
    sector_refresh_payload = {
        **evidence["sector_refresh"],
        "membership_observation": evidence["membership_observation"],
    }
    monkeypatch.setattr(chain, "_refresh_sector_snapshot", lambda *args, **kwargs: {"ok": True, "status": "skipped"})
    result = chain._archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        sector_refresh_payload=sector_refresh_payload,
        captured_at="2026-07-22T15:30:00",
    )
    assert result["status"] == "passed"
    assert result["snapshot_path"].replace("\\", "/").endswith(
        "data/sector_history/2026-07-22/snapshot.json"
    )

    blocked = chain._archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-23",
        sector_refresh_payload={"ok": False, "status": "blocked"},
        captured_at="2026-07-23T15:30:00",
    )
    assert blocked["status"] == "degraded"
    assert blocked["written"] is False
    assert blocked["reason_codes"] == ["sector_refresh_not_completed"]
    assert not (tmp_path / "data" / "sector_history" / "2026-07-23").exists()
