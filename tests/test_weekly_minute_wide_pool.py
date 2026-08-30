from __future__ import annotations

import csv
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

import quantdesk.intraday.weekly_minute_wide_pool as weekly_minute_module

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.weekly_minute_wide_pool import (
    build_weekly_minute_wide_pool_contract,
    load_weekly_minute_exclusion_policy,
    run_weekly_minute_wide_pool,
)


def _fixture_root(tmp_path: Path, *, formal_symbols: tuple[str, ...] = ("000001", "920685")) -> Path:
    canonical = tmp_path / "data" / "intraday" / "1m"
    universe = tmp_path / "data" / "universe"
    canonical.mkdir(parents=True)
    universe.mkdir(parents=True)
    for symbol in ("000001", "000002", "920685"):
        (canonical / f"{symbol}.csv").write_text(
            "datetime,symbol,open,high,low,close,volume,amount,adjust,source,price_basis\n",
            encoding="utf-8",
        )
    (canonical / "not-a-symbol.csv").write_text("ignored\n", encoding="utf-8")
    with (universe / "low_price_learning_symbols.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "included"])
        writer.writeheader()
        for symbol in formal_symbols:
            writer.writerow({"symbol": symbol, "included": "True"})
        writer.writerow({"symbol": "000003", "included": "False"})
    return tmp_path


def _contract(root: Path, *, exclusions: tuple[str, ...] = ()) -> dict:
    return build_weekly_minute_wide_pool_contract(
        root,
        canonical_dir=root / "data" / "intraday" / "1m",
        formal_pool_path=root / "data" / "universe" / "low_price_learning_symbols.csv",
        explicit_exclusions=exclusions,
        target_date="2026-07-24",
    )


def _passed_audit(symbols: list[str], target_date: str, **kwargs) -> dict:
    return {
        "ok": True,
        "target_date": target_date,
        "summary": {
            "usable": len(symbols),
            "complete_session": len(symbols),
            "formal_execution_eligible": len(symbols),
            "missing": 0,
        },
        "items": [
            {
                "symbol": symbol,
                "complete_session": True,
                "formal_execution_eligible": True,
                "price_basis": "raw",
                "reason_codes": [],
            }
            for symbol in symbols
        ],
    }


def test_contract_is_deterministic_residual_of_existing_canonical_pool(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)

    first = _contract(root)
    second = _contract(root)

    assert first["symbols"] == ["000002"]
    assert first["counts"] == {
        "canonical": 3,
        "formal": 2,
        "intersection": 2,
        "pre_exclusion_weekly_wide": 1,
        "weekly_wide": 1,
        "explicit_exclusions": 0,
        "applied_explicit_exclusions": 0,
    }
    assert first["hashes"] == second["hashes"]
    assert set(first["symbols"]).isdisjoint(first["formal_symbols"])
    assert first["boundaries"]["full_a_scan"] is False
    assert first["boundaries"]["canonical_write"] is False


def test_920685_requires_separate_minute_exclusion_after_formal_removal(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, formal_symbols=("000001",))

    without_exclusion = _contract(root)
    with_exclusion = _contract(root, exclusions=("920685",))

    assert "920685" in without_exclusion["symbols"]
    assert "920685" not in with_exclusion["symbols"]
    assert with_exclusion["explicit_exclusions"] == ["920685"]
    assert with_exclusion["counts"]["weekly_wide"] == 1


def _write_exclusion_policy(
    root: Path,
    *,
    include_delist_row: bool = True,
    orphan_in_universe: bool = False,
) -> Path:
    metadata = root / "data" / "metadata"
    config = root / "config"
    metadata.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    orphan = root / "data" / "intraday" / "1m" / "003849.csv"
    orphan.write_text("symbol,datetime\n", encoding="utf-8")
    delist_path = metadata / "delist_history.csv"
    with delist_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "name", "delist_date", "source"],
        )
        writer.writeheader()
        if include_delist_row:
            writer.writerow(
                {
                    "symbol": "000002",
                    "name": "Fixture Delisted",
                    "delist_date": "2025-12-31",
                    "source": "fixture",
                }
            )
    universe_path = root / "data" / "universe" / "a_share_symbols.csv"
    with universe_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "name"])
        writer.writeheader()
        writer.writerow({"symbol": "000001", "name": "Fixture Active"})
        if orphan_in_universe:
            writer.writerow({"symbol": "003849", "name": "Unexpected Active"})
    policy_path = config / "weekly_minute_wide_pool_exclusions.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "weekly_minute_wide_pool_exclusions_v1",
                "task_type": "weekly_minute_wide_pool_exclusions",
                "effective_from": "2026-01-01",
                "evidence_sources": {
                    "delist_history": "data/metadata/delist_history.csv",
                    "current_a_share_universe": "data/universe/a_share_symbols.csv",
                },
                "entries": [
                    {
                        "symbol": "000002",
                        "reason_code": "delisted",
                        "expected_name": "Fixture Delisted",
                        "expected_delist_date": "2025-12-31",
                    },
                    {
                        "symbol": "003849",
                        "reason_code": "outside_current_universe_orphan_stub",
                        "canonical_path": "data/intraday/1m/003849.csv",
                        "expected_bytes": orphan.stat().st_size,
                        "expected_sha256": hashlib.sha256(orphan.read_bytes()).hexdigest(),
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return policy_path


def test_exclusion_policy_verifies_delist_rows_and_orphan_stub(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, formal_symbols=("000001", "920685"))
    policy_path = _write_exclusion_policy(root)

    audit = load_weekly_minute_exclusion_policy(
        root,
        policy_path=policy_path,
        target_date="2026-07-24",
    )
    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        exclusion_policy_path=policy_path,
    )

    assert audit["status"] == "passed"
    assert audit["symbols"] == ["000002", "003849"]
    assert audit["counts"] == {"requested": 2, "verified": 2}
    assert payload["status"] == "planned_offline"
    assert payload["pool_contract"]["symbols"] == []
    assert payload["pool_contract"]["counts"]["pre_exclusion_weekly_wide"] == 2
    assert payload["pool_contract"]["counts"]["explicit_exclusions"] == 2
    assert payload["pool_contract"]["counts"]["weekly_wide"] == 0
    assert payload["pool_contract"]["exclusion_audit"]["status"] == "passed"


@pytest.mark.parametrize(
    ("include_delist_row", "orphan_in_universe", "reason_fragment"),
    [
        (False, False, "delist_evidence_missing"),
        (True, True, "orphan_symbol_still_in_current_universe"),
    ],
)
def test_exclusion_policy_missing_evidence_blocks_entire_run(
    tmp_path: Path,
    include_delist_row: bool,
    orphan_in_universe: bool,
    reason_fragment: str,
) -> None:
    root = _fixture_root(tmp_path, formal_symbols=("000001", "920685"))
    policy_path = _write_exclusion_policy(
        root,
        include_delist_row=include_delist_row,
        orphan_in_universe=orphan_in_universe,
    )

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        exclusion_policy_path=policy_path,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["exclusion_policy_invalid"]
    assert reason_fragment in payload["error"]
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_contract_rejects_paths_outside_injected_root(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="path_outside_project_root"):
        build_weekly_minute_wide_pool_contract(
            root,
            canonical_dir=outside,
            formal_pool_path=root / "data" / "universe" / "low_price_learning_symbols.csv",
            target_date="2026-07-24",
        )


def test_contract_rejects_symlink_escape_when_supported(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "data" / "intraday" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink unavailable on this Windows host")

    with pytest.raises(ValueError, match="path_outside_project_root"):
        build_weekly_minute_wide_pool_contract(
            root,
            canonical_dir=link,
            formal_pool_path=root / "data" / "universe" / "low_price_learning_symbols.csv",
            target_date="2026-07-24",
        )


def test_offline_default_is_plan_only_and_has_no_side_effect_calls(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    provider = Mock(side_effect=AssertionError("provider must stay offline"))
    auditor = Mock(side_effect=AssertionError("auditor must stay offline"))
    importer = Mock(side_effect=AssertionError("importer must stay offline"))

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        provider_updater=provider,
        quality_auditor=auditor,
        append_importer=importer,
    )

    assert payload["status"] == "planned_offline"
    assert payload["network"] is False
    assert payload["apply"] is False
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()
    provider.assert_not_called()
    auditor.assert_not_called()
    importer.assert_not_called()


def test_apply_without_network_blocks_before_provider_or_output(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    provider = Mock()

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=False,
        apply=True,
        provider_updater=provider,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["apply_requires_network"]
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_legacy_price_basis_preflight_blocks_before_network_or_staging(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    legacy = root / "data" / "intraday" / "1m" / "000002.csv"
    legacy.write_text(
        "datetime,symbol,open,high,low,close,volume,amount,adjust,source\n",
        encoding="utf-8",
    )
    provider = Mock(side_effect=AssertionError("preflight must run before provider"))

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=True,
        provider_updater=provider,
    )

    assert payload["status"] == "blocked_legacy_price_basis_migration_required"
    assert payload["reason_codes"] == [
        "legacy_price_basis_migration_required"
    ]
    assert payload["legacy_price_basis_preflight"] == {
        "status": "blocked",
        "checked_symbol_count": 1,
        "missing_price_basis_count": 1,
        "missing_price_basis_symbols": ["000002"],
        "unreadable_or_malformed_count": 0,
        "unreadable_or_malformed_symbols": [],
        "errors": [],
    }
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_effective_pool_and_preflight_are_rebuilt_after_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_root(tmp_path)
    legacy = root / "data" / "intraday" / "1m" / "000002.csv"
    provider = Mock(side_effect=AssertionError("locked preflight must block provider"))

    @contextmanager
    def mutate_after_lock(*args, **kwargs):
        legacy.write_text(
            "datetime,symbol,open,high,low,close,volume,amount,adjust,source\n",
            encoding="utf-8",
        )
        yield

    monkeypatch.setattr(
        weekly_minute_module,
        "interprocess_file_lock",
        mutate_after_lock,
    )

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=False,
        provider_updater=provider,
    )

    assert payload["status"] == "blocked_legacy_price_basis_migration_required"
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_online_exclusion_policy_is_reaudited_after_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_root(tmp_path, formal_symbols=("000001", "920685"))
    policy_path = _write_exclusion_policy(root)
    universe_path = root / "data" / "universe" / "a_share_symbols.csv"
    provider = Mock(side_effect=AssertionError("invalid locked policy must block provider"))

    @contextmanager
    def mutate_universe_after_lock(*args, **kwargs):
        with universe_path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["symbol", "name"])
            writer.writerow({"symbol": "003849", "name": "Now Active"})
        yield

    monkeypatch.setattr(
        weekly_minute_module,
        "interprocess_file_lock",
        mutate_universe_after_lock,
    )

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=False,
        exclusion_policy_path=policy_path,
        provider_updater=provider,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["exclusion_policy_invalid"]
    assert "orphan_symbol_still_in_current_universe:003849" in payload["error"]
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


@pytest.mark.parametrize("failure_kind", ["missing", "invalid_utf8", "malformed_csv"])
def test_preflight_csv_read_failures_are_structured_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    root = _fixture_root(tmp_path)
    target = root / "data" / "intraday" / "1m" / "000002.csv"
    if failure_kind == "invalid_utf8":
        target.write_bytes(b"datetime,price_basis\n\xff\xfe")
    elif failure_kind == "malformed_csv":
        target.write_text('datetime,"price_basis', encoding="utf-8")
    else:
        original = weekly_minute_module._canonical_symbols

        def remove_after_inventory(canonical_dir: Path) -> list[str]:
            symbols = original(canonical_dir)
            target.unlink()
            return symbols

        monkeypatch.setattr(
            weekly_minute_module,
            "_canonical_symbols",
            remove_after_inventory,
        )
    provider = Mock(side_effect=AssertionError("preflight failure must block provider"))

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=False,
        provider_updater=provider,
    )

    assert payload["status"] == "blocked_canonical_price_basis_preflight_failed"
    assert payload["reason_codes"] == [
        "canonical_intraday_csv_unreadable_or_malformed"
    ]
    preflight = payload["legacy_price_basis_preflight"]
    assert preflight["status"] == "blocked"
    assert preflight["unreadable_or_malformed_count"] == 1
    assert preflight["unreadable_or_malformed_symbols"] == ["000002"]
    assert preflight["errors"][0]["reason_code"] == (
        "canonical_intraday_csv_unreadable_or_malformed"
    )
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_network_without_apply_runs_staging_and_audit_only(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    order: list[str] = []

    def provider(symbols: list[str], target_date: str, **kwargs) -> dict:
        order.append("provider")
        assert kwargs["intraday_dir"] != root / "data" / "intraday" / "1m"
        return {"ok": True, "status": "completed", "updated_symbol_list": symbols}

    def auditor(symbols: list[str], target_date: str, **kwargs) -> dict:
        order.append("audit")
        return _passed_audit(symbols, target_date, **kwargs)

    importer = Mock(side_effect=AssertionError("no apply means no importer"))
    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=False,
        provider_updater=provider,
        quality_auditor=auditor,
        append_importer=importer,
    )

    assert payload["status"] == "completed_staging_only"
    assert order == ["provider", "audit"]
    importer.assert_not_called()
    assert payload["run_dir"].startswith(
        str(root / "results" / "data_chain" / "weekly_minute_wide_pool" / "runs")
    )
    assert not (root / "results" / "data_chain" / "daily_data_chain_latest.json").exists()


def test_apply_order_is_provider_audit_append_only_importer(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    canonical = root / "data" / "intraday" / "1m" / "000002.csv"
    before = canonical.read_bytes()
    order: list[str] = []

    def provider(symbols: list[str], target_date: str, **kwargs) -> dict:
        order.append("provider")
        return {"ok": True, "status": "completed", "updated_symbol_list": symbols}

    def auditor(symbols: list[str], target_date: str, **kwargs) -> dict:
        order.append("audit")
        return _passed_audit(symbols, target_date, **kwargs)

    def importer(**kwargs) -> dict:
        order.append("importer")
        assert kwargs["apply"] is True
        assert kwargs["canonical_dir"] == root / "data" / "intraday" / "1m"
        return {"status": "completed", "applied": True, "appended_symbol_count": 1}

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=True,
        provider_updater=provider,
        quality_auditor=auditor,
        append_importer=importer,
    )

    assert payload["status"] == "completed"
    assert payload["ok"] is True
    assert order == ["provider", "audit", "importer"]
    assert canonical.read_bytes() == before


def test_failed_audit_never_calls_importer_or_changes_canonical(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    canonical = root / "data" / "intraday" / "1m" / "000002.csv"
    before = canonical.read_bytes()
    importer = Mock()

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=True,
        provider_updater=lambda symbols, target_date, **kwargs: {
            "ok": False,
            "status": "failed",
            "failed_symbol_list": symbols,
        },
        quality_auditor=lambda symbols, target_date, **kwargs: {
            "ok": True,
            "summary": {"usable": 0, "complete_session": 0, "formal_execution_eligible": 0},
            "items": [
                {
                    "symbol": symbols[0],
                    "complete_session": False,
                    "formal_execution_eligible": False,
                    "price_basis": "unknown",
                }
            ],
        },
        append_importer=importer,
    )

    assert payload["status"] == "partial"
    assert payload["ok"] is False
    assert payload["summary"]["quality_failed_batches"] == 1
    importer.assert_not_called()
    assert canonical.read_bytes() == before


def test_one_symbol_failure_does_not_stop_later_batch(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, formal_symbols=("000001",))
    calls: list[str] = []

    def provider(symbols: list[str], target_date: str, **kwargs) -> dict:
        calls.append(symbols[0])
        if symbols[0] == "000002":
            raise RuntimeError("fixture provider failure")
        return {"ok": True, "status": "completed", "updated_symbol_list": symbols}

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=False,
        provider_updater=provider,
        quality_auditor=_passed_audit,
    )

    assert calls == ["000002", "920685"]
    assert payload["status"] == "partial"
    assert payload["ok"] is False
    assert payload["summary"]["batch_count"] == 2
    assert payload["summary"]["failed_batches"] == 1


def test_importer_completed_without_applied_true_is_import_blocked(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)

    payload = run_weekly_minute_wide_pool(
        root,
        target_date="2026-07-24",
        network=True,
        apply=True,
        provider_updater=lambda symbols, target_date, **kwargs: {
            "ok": True,
            "status": "completed",
            "updated_symbol_list": symbols,
        },
        quality_auditor=_passed_audit,
        append_importer=lambda **kwargs: {
            "status": "completed",
            "applied": False,
            "appended_symbol_count": 0,
        },
    )

    assert payload["status"] == "partial"
    assert payload["ok"] is False
    assert payload["summary"]["import_failed_batches"] == 1
    assert payload["batches"][0]["status"] == "import_blocked"


def test_daily_lock_contention_returns_busy_before_provider_or_run_dir(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    lock_path = root / "state" / "daily_data_chain.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(b"0")
    provider = Mock()

    with interprocess_file_lock(lock_path, timeout_seconds=0):
        payload = run_weekly_minute_wide_pool(
            root,
            target_date="2026-07-24",
            network=True,
            apply=False,
            provider_updater=provider,
        )

    assert payload["status"] == "busy"
    provider.assert_not_called()
    assert not (root / "results" / "data_chain" / "weekly_minute_wide_pool").exists()


def test_results_root_escape_is_rejected_before_provider_or_mkdir(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path / "project")
    outside = tmp_path / "outside-results"
    provider = Mock()

    with pytest.raises(ValueError, match="path_outside_project_root"):
        run_weekly_minute_wide_pool(
            root,
            target_date="2026-07-24",
            network=True,
            apply=False,
            results_root=outside,
            provider_updater=provider,
        )

    provider.assert_not_called()
    assert not outside.exists()
