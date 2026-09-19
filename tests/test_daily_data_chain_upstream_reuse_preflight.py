from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantdesk.daily_data_chain import run_daily_data_chain
from tests.test_daily_data_chain_universe_refresh import _install_minimal_chain_fakes

TARGET = "2026-08-14"
COMPLETED_RUN_ID = "daily-data-chain-20260814T074500Z-completed"


def _write_completed_run(root: Path, *, target_date: str = TARGET) -> None:
    run_dir = root / "results" / "data_chain" / "runs" / COMPLETED_RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": COMPLETED_RUN_ID,
        "task_type": "daily_data_chain",
        "daily_data_chain_contract_version": 10,
        "status": "completed",
        "target_date": target_date,
        "started_at": f"{target_date}T15:45:00",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "components": {
            "sector_history_archive": {
                "snapshot_hash": "sha256:" + "a" * 64,
            }
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "sector_history_archive_attempt.json").write_text(
        json.dumps({"snapshot_hash": "sha256:" + "a" * 64}),
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_upstream_reuse(root: Path, *, target: str = TARGET, run_id: str = "pipe-1") -> dict:
    universe = root / "data" / "universe" / "a_share_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text("symbol,name,source,updated_at\n600000,TEST,stock_zh_a_spot,"
                        f"{target}\n", encoding="utf-8")
    refresh = root / "results" / "data_refresh" / "data_refresh_latest.json"
    refresh.parent.mkdir(parents=True, exist_ok=True)
    refresh.write_text(
        json.dumps({"status": "completed", "target_date": target}),
        encoding="utf-8",
    )
    return {
        "schema_version": "data_close_upstream_reuse_v1",
        "run_id": run_id,
        "target_date": target,
        "status": "accepted",
        "stages": {
            "universe": {
                "status": "completed",
                "path": "data/universe/a_share_symbols.csv",
                "sha256": _sha(universe),
            },
            "market_refresh": {
                "status": "completed",
                "path": "results/data_refresh/data_refresh_latest.json",
                "sha256": _sha(refresh),
            },
        },
    }


def test_same_day_completed_still_skips_without_upstream_reuse(tmp_path: Path) -> None:
    _write_completed_run(tmp_path)

    result = run_daily_data_chain(
        tmp_path,
        target_date=TARGET,
        network=True,
        retry_from_latest=True,
        trigger="windows_scheduled_task",
    )

    assert result["status"] == "skipped_already_completed"
    assert result["reason_codes"] == ["same_day_run_already_completed"]
    assert not (tmp_path / "state" / "daily_data_chain.lock.json").exists()


def test_invalid_upstream_reuse_bypasses_same_day_skip_and_blocks(tmp_path: Path) -> None:
    _write_completed_run(tmp_path)

    result = run_daily_data_chain(
        tmp_path,
        target_date=TARGET,
        network=True,
        retry_from_latest=True,
        trigger="manual",
        upstream_reuse={"schema_version": "not_a_reuse_manifest"},
    )

    assert result["status"] != "skipped_already_completed"
    assert result["status"] == "blocked"
    assert result.get("ok") is False
    assert "schema_version_invalid" in (result.get("reason_codes") or [])
    reuse = (result.get("components") or {}).get("upstream_reuse") or {}
    assert reuse.get("blocked") is True
    assert reuse.get("reused") is not True


def test_verified_upstream_reuse_runs_chain_despite_same_day_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_completed_run(tmp_path)
    reuse = _valid_upstream_reuse(tmp_path)
    chain = _install_minimal_chain_fakes(monkeypatch, ["600000"])
    universe_calls: list[str] = []
    daily_calls: list[str] = []
    monkeypatch.setattr(
        chain,
        "build_default_fetchers",
        lambda: universe_calls.append("build") or {"eastmoney": object()},
    )
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: universe_calls.append("refresh") or {"status": "completed"},
    )
    original_daily = chain.update_daily_prices

    def counting_daily(**kwargs):
        daily_calls.append("daily")
        return original_daily(**kwargs)

    monkeypatch.setattr(chain, "update_daily_prices", counting_daily)

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date=TARGET,
        network=True,
        dry_run=False,
        retry_from_latest=True,
        rebuild_features=False,
        trigger="manual",
        upstream_reuse=reuse,
        run_id="reuse-after-completed",
    )

    assert payload["status"] != "skipped_already_completed"
    assert payload.get("reason_codes") != ["same_day_run_already_completed"]
    assert universe_calls == []
    assert daily_calls == []
    assert payload["components"]["universe_refresh"]["reason"] == "verified_upstream_reuse"
    assert payload["components"]["daily_update"].get("upstream_reuse") is True
    assert payload["components"]["upstream_reuse"]["reused"] is True
    assert payload["components"]["upstream_reuse"]["run_id"] == "pipe-1"


def test_pipeline_run_id_is_published_with_latest_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_daily_data_chain as runner

    run_id = "daily-data-chain-stamp-test"
    chain = _install_minimal_chain_fakes(monkeypatch, ["600000"])
    monkeypatch.setattr(chain, "new_run_id", lambda: run_id)
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol,name\n600000,TEST\n", encoding="utf-8")
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    archive = tmp_path / "results" / "data_chain" / "runs" / run_id / "manifest.json"
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)

    assert runner.main([
        "--json", "false", "--pipeline-run-id", "pipe-stamp", "--target-date", TARGET,
        "--enable-network", "--refresh-universe", "false", "--rebuild-features", "false",
    ]) == 0

    latest_doc = json.loads(latest.read_text(encoding="utf-8"))
    archive_doc = json.loads(archive.read_text(encoding="utf-8"))
    assert latest_doc == archive_doc
    assert latest_doc["run_id"] == run_id
    assert latest_doc["published_latest"] is True
    assert latest_doc["pipeline_run_id"] == "pipe-stamp"
