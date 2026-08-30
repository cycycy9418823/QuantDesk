from __future__ import annotations

import csv
import json
from pathlib import Path

from quantdesk.full_a_daily_reconciliation import (
    build_reconciliation_partition,
    run_full_a_daily_reconciliation,
)


def _write_universe(root: Path, symbols: list[str]) -> None:
    path = root / "data/universe/a_share_symbols.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name"])
        for symbol in symbols:
            writer.writerow([symbol, symbol])


def _write_daily(root: Path, symbol: str, day: str) -> None:
    path = root / f"data/daily/{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "date,symbol,open,high,low,close,volume,amount,adjust\n"
        f"{day},{symbol},10,10,10,10,1000,10000,raw\n",
        encoding="utf-8",
    )


def _write_research_pool(root: Path, symbols: list[str]) -> None:
    path = root / "data/universe/low_price_learning_symbols.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "included"])
        for symbol in symbols:
            writer.writerow([symbol, "true"])


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    _write_universe(root, ["000001", "000002", "000003"])
    _write_research_pool(root, ["000001"])
    _write_daily(root, "000001", "2026-08-28")
    _write_daily(root, "000002", "2026-08-27")
    return root


def test_partition_covers_current_universe_once_including_missing_canonical_files(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    partition = build_reconciliation_partition(root, target_date="2026-08-28")

    assert partition["research_pool_symbols"] == ["000001"]
    assert partition["residual_existing_symbols"] == ["000002"]
    assert partition["missing_canonical_symbols"] == ["000003"]
    assert partition["pending_symbols"] == ["000002", "000003"]
    assert partition["accepted_symbols"] == ["000001"]
    assert partition["partition_complete"] is True
    assert partition["partition_overlap"] == []


def test_partial_run_resumes_only_unaccepted_symbols(tmp_path: Path) -> None:
    root = _root(tmp_path)
    update_calls: list[list[str]] = []

    def update_runner(**kwargs):
        symbols = list(kwargs["symbols"])
        update_calls.append(symbols)
        if "000002" in symbols:
            _write_daily(root, "000002", "2026-08-28")
        return {
            "status": "partial",
            "updated_symbol_list": ["000002"],
            "failed_symbol_list": ["000003"],
            "failed_symbol_reasons": {"000003": "provider_missing"},
        }

    first = run_full_a_daily_reconciliation(
        project_root=root,
        target_date="2026-08-28",
        network=True,
        apply=True,
        update_runner=update_runner,
        health_refresher=lambda **_kwargs: {"data_alerts": {"current": [], "resolved": []}},
        alert_dispatcher=lambda *_args, **_kwargs: {"status": "skipped"},
    )
    second = run_full_a_daily_reconciliation(
        project_root=root,
        target_date="2026-08-28",
        network=True,
        apply=True,
        update_runner=update_runner,
        health_refresher=lambda **_kwargs: {"data_alerts": {"current": [], "resolved": []}},
        alert_dispatcher=lambda *_args, **_kwargs: {"status": "skipped"},
    )

    assert first["status"] == "partial"
    assert second["resumed_from_run_id"] == first["run_id"]
    assert update_calls == [["000002", "000003"], ["000003"]]


def test_lock_busy_returns_deferred_without_provider_or_daily_write(tmp_path: Path) -> None:
    root = _root(tmp_path)
    calls: list[str] = []

    def busy_lock(*_args, **_kwargs):
        raise TimeoutError("busy")

    result = run_full_a_daily_reconciliation(
        project_root=root,
        target_date="2026-08-28",
        network=True,
        apply=True,
        update_runner=lambda **_kwargs: calls.append("provider") or {},
        lock_factory=busy_lock,
    )

    assert result["status"] == "deferred_upstream_busy"
    assert calls == []
    assert not (root / "data/daily/000003.csv").exists()


def test_provider_failure_keeps_full_a_status_partial_not_completed(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = run_full_a_daily_reconciliation(
        project_root=root,
        target_date="2026-08-28",
        network=True,
        apply=True,
        update_runner=lambda **_kwargs: {
            "status": "failed",
            "updated_symbol_list": [],
            "failed_symbol_list": ["000002", "000003"],
            "failed_symbol_reasons": {"000002": "transport", "000003": "missing"},
        },
        health_refresher=lambda **_kwargs: {"data_alerts": {"current": [], "resolved": []}},
        alert_dispatcher=lambda *_args, **_kwargs: {"status": "skipped"},
    )

    assert result["status"] == "partial"
    assert result["coverage"]["at_target_date_count"] == 1
    assert result["coverage"]["missing_count"] == 1
    assert result["coverage"]["stale_count"] == 1
    manifest = root / result["manifest_path"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "partial"


def test_terminal_run_writes_checkpoint_and_passes_alert_flags_only_when_explicit(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    observed: list[dict] = []

    result = run_full_a_daily_reconciliation(
        project_root=root,
        target_date="2026-08-28",
        network=True,
        apply=True,
        alert_network=True,
        alert_push=True,
        alert_dry_run=False,
        update_runner=lambda **_kwargs: {
            "updated_symbol_list": [],
            "failed_symbol_list": ["000002", "000003"],
        },
        health_refresher=lambda **_kwargs: {"data_alerts": {"current": [], "resolved": []}},
        alert_dispatcher=lambda *_args, **kwargs: observed.append(kwargs) or {"status": "sent"},
    )

    run_dir = root / Path(result["manifest_path"]).parent
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["initial_pending_symbols"] == ["000002", "000003"]
    assert observed == [{"health_payload": {"data_alerts": {"current": [], "resolved": []}}, "network": True, "push": True, "dry_run": False}]
