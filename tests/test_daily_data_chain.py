from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


def _full_session_frame(
    symbol: str = "600000",
    target_date: str = "2026-07-16",
) -> pd.DataFrame:
    times = list(pd.date_range(f"{target_date} 09:31", periods=120, freq="min"))
    times += list(pd.date_range(f"{target_date} 13:01", periods=120, freq="min"))
    rows = []
    for index, timestamp in enumerate(times):
        open_ = 10.0 + index * 0.001
        rows.append(
            {
                "symbol": symbol,
                "datetime": timestamp,
                "date": target_date,
                "time": timestamp.strftime("%H:%M:%S"),
                "open": open_,
                "high": open_ + 0.02,
                "low": open_ - 0.01,
                "close": open_ + 0.01,
                "volume": 100,
                "amount": (open_ + 0.01) * 100,
                "source": "mootdx_intraday",
            }
        )
    return pd.DataFrame(rows)


def test_daily_chain_provider_order_prefers_mootdx() -> None:
    from quantdesk.daily_data_chain import DAILY_PROVIDER_ORDER, INTRADAY_PROVIDER_ORDER

    assert DAILY_PROVIDER_ORDER == (
        "local_existing",
        "mootdx_daily",
        "baostock_daily",
        "tencent_daily",
        "akshare_daily",
        "sina_daily",
    )
    assert INTRADAY_PROVIDER_ORDER == (
        "local_existing",
        "mootdx_intraday",
        "tencent_intraday",
        "akshare_intraday",
        "sina_intraday",
    )
    assert not any("tushare" in name for name in INTRADAY_PROVIDER_ORDER)


def test_runtime_preflight_prefers_project_venv_with_mootdx(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import build_runtime_preflight

    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    current_python = tmp_path / "miniconda" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    current_python.parent.mkdir(parents=True)
    venv_python.touch()
    current_python.touch()
    availability = {
        str(venv_python): {"pandas": True, "mootdx": True, "akshare": True},
        str(current_python): {"pandas": True, "mootdx": False, "akshare": True},
    }

    result = build_runtime_preflight(
        tmp_path,
        current_executable=current_python,
        env={},
        module_probe=lambda executable, modules: availability[str(executable)],
    )

    assert result["status"] == "passed"
    assert result["ready"] is True
    assert Path(result["selected_executable"]) == venv_python
    assert result["runtime_source"] == "project_venv"
    assert result["modules"]["mootdx"] is True


def test_runtime_preflight_blocks_without_mootdx(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import build_runtime_preflight

    current_python = tmp_path / "python.exe"
    current_python.touch()
    result = build_runtime_preflight(
        tmp_path,
        current_executable=current_python,
        env={},
        module_probe=lambda executable, modules: {
            "pandas": True,
            "mootdx": False,
            "akshare": True,
        },
    )

    assert result["status"] == "blocked"
    assert result["ready"] is False
    assert "required_module_missing:mootdx" in result["reason_codes"]


def test_complete_session_requires_95_percent_and_both_sessions() -> None:
    from quantdesk.intraday.intraday_quality_audit import assess_complete_session

    full = _full_session_frame()
    complete = assess_complete_session(
        full,
        symbol="600000",
        target_date="2026-07-16",
        price_basis="raw",
    )
    partial = assess_complete_session(
        full.drop(index=range(20, 33)).reset_index(drop=True),
        symbol="600000",
        target_date="2026-07-16",
        price_basis="raw",
    )

    assert complete["status"] == "complete_session"
    assert complete["unique_session_minutes"] == 240
    assert complete["session_coverage_ratio"] == 1.0
    assert complete["formal_execution_eligible"] is True
    assert complete["reliable_no_fill_eligible"] is True
    assert partial["status"] == "partial_session"
    assert partial["unique_session_minutes"] == 227
    assert partial["formal_execution_eligible"] is False
    assert partial["reliable_no_fill_eligible"] is False
    assert "session_coverage_below_0_95" in partial["reason_codes"]


def test_universe_contract_is_deterministic_and_versioned() -> None:
    from quantdesk.daily_data_chain import build_universe_contract

    left = build_universe_contract(
        [{"symbol": "600000"}, {"symbol": "920266"}, {"symbol": "600000"}],
        target_date="2026-07-16",
        name="current_analysis_pool",
    )
    right = build_universe_contract(
        [{"symbol": "920266"}, {"symbol": "600000"}],
        target_date="2026-07-16",
        name="current_analysis_pool",
    )
    changed = build_universe_contract(
        [{"symbol": "600000"}],
        target_date="2026-07-16",
        name="current_analysis_pool",
    )

    assert left == right
    assert left["symbol_count"] == 2
    assert left["symbols"] == ["600000", "920266"]
    assert len(left["sha256"]) == 64
    assert left["version"].startswith("current_analysis_pool:2026-07-16:")
    assert changed["sha256"] != left["sha256"]


def test_dry_run_never_replaces_authoritative_latest(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import daily_data_chain as chain

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    previous = {"run_id": "previous-authoritative-run"}
    latest.write_text(json.dumps(previous), encoding="utf-8")
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "selected_executable": "project/.venv/Scripts/python.exe",
            "runtime_source": "project_venv",
            "modules": {"pandas": True, "mootdx": True, "akshare": True},
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: calls.append(("daily", kwargs))
        or {
            "status": "dry_run",
            "target_date": kwargs["target_date"],
            "symbols_checked": len(kwargs["symbols"]),
            "after": {"fresh_count": 2, "stale_count": 0, "missing_count": 0},
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [{"symbol": "600000"}]},
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda *args, **kwargs: calls.append(("intraday", kwargs))
        or {
            "status": "dry_run",
            "updated_symbols": [],
            "unchanged_symbols": [],
            "failed_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "missing": 0,
                "partial_session": 0,
                "invalid": 0,
            },
            "items": [],
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=["600000", "920266"],
        network=True,
        dry_run=True,
        run_id="dry-run-fixed",
        rebuild_features=False,
    )

    assert payload["status"] == "dry_run"
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False
    assert payload["components"]["sector_history_archive"]["status"] == "skipped"
    assert payload["components"]["sector_history_archive"]["append_only"] is True
    assert json.loads(latest.read_text(encoding="utf-8")) == previous
    assert next(kwargs for name, kwargs in calls if name == "daily")["write_result"] is False
    assert next(kwargs for name, kwargs in calls if name == "daily")["require_raw_price_basis"] is True
    assert all(
        kwargs["dry_run"] is True
        for name, kwargs in calls
        if name == "intraday"
    )


@pytest.mark.parametrize(
    ("failure_mode", "expected_fetched_board_count"),
    [
        ("provider_unavailable", 0),
        ("partial_board_failure", 1),
    ],
)
def test_sector_membership_failure_degrades_archive_without_blocking_chain_or_writing_universe(
    tmp_path: Path,
    monkeypatch,
    failure_mode: str,
    expected_fetched_board_count: int,
) -> None:
    from quantdesk import daily_data_chain as chain
    from quantdesk.data_truth import sector_history_archive as archive

    root = tmp_path / failure_mode
    universe_dir = root / "data" / "universe"
    universe_dir.mkdir(parents=True)
    sentinel = universe_dir / "canonical_sentinel.csv"
    sentinel.write_bytes(b"symbol,name\n600000,untouched\n")
    universe_before = {
        path.relative_to(universe_dir).as_posix(): path.read_bytes()
        for path in universe_dir.rglob("*")
        if path.is_file()
    }
    captured_at = "2026-07-22T18:00:00+08:00"
    board_path = root / "data" / "cache" / "industry_board_latest.csv"
    member_calls: list[tuple[str, str]] = []
    candidate_refresh_calls: list[dict[str, object]] = []

    def refresh_board_cache() -> dict[str, object]:
        board_path.parent.mkdir(parents=True, exist_ok=True)
        board_path.write_text(
            "name,pct_chg,amount,leading_stock\n"
            "Finance,1.2,100,Alpha\n"
            "Energy,-0.4,80,Beta\n",
            encoding="utf-8",
        )
        source_time = datetime.fromisoformat(captured_at).timestamp()
        __import__("os").utime(board_path, (source_time, source_time))
        return {
            "ok": True,
            "source": "ths",
            "cache_path": str(board_path),
            "board_count": 2,
        }

    def fetch_members(provider: str, board: str) -> pd.DataFrame:
        member_calls.append((provider, board))
        if failure_mode == "provider_unavailable":
            raise RuntimeError("membership_provider_unavailable:ths")
        if board == "Finance":
            raise RuntimeError("one_board_failed")
        return pd.DataFrame([{"代码": "000002", "名称": "Beta"}])

    monkeypatch.setattr(chain, "PROJECT_ROOT", root)
    monkeypatch.setattr(chain, "now_text", lambda: captured_at)
    monkeypatch.setattr(archive, "_local_today", lambda: date(2026, 7, 22))
    monkeypatch.setattr(archive, "_default_member_fetcher", fetch_members)
    # Exercise the full custom-root membership path through the explicit
    # dependency seam.  Replacing the process-global market helper used to
    # bypass the production-root guard and could touch the real cache.
    original_sector_refresh = chain._refresh_sector_snapshot
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda scoped_root, *, network: original_sector_refresh(
            scoped_root,
            network=network,
            board_refresher=refresh_board_cache,
        ),
    )
    monkeypatch.setattr(chain, "build_default_fetchers", lambda: {"provider": object()})
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "completed",
            "formal_eligible": True,
            "provider": "fixture",
            "quote_date": "2026-07-22",
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "skipped_test"},
    )
    monkeypatch.setattr(
        chain,
        "_update_current_state",
        lambda root, manifest: {"ok": True, "status": "skipped_test"},
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(chain, "resolve_symbols", lambda **kwargs: [{"symbol": "600000"}])
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "symbols_checked": 1,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "invalid_price_basis_symbols": [],
            "fresh_symbol_list_after": ["600000"],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "unknown_symbols": [],
            "legitimate_no_session_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda symbols, target_date, **kwargs: {
            "status": "completed",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "updated_symbol_list": list(symbols),
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "apply_audited_intraday_batch",
        lambda **kwargs: {
            "status": "completed",
            "applied": False,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "written": True,
            "plan_id": "sector-degraded-test-plan",
            "reason_codes": [],
        },
    )

    def publish_candidates(**kwargs) -> dict[str, object]:
        candidate_refresh_calls.append(
            {
                "project_root": kwargs["project_root"],
                "manifest_status": kwargs["data_chain_manifest"]["status"],
                "apply": kwargs["apply"],
            }
        )
        if kwargs["apply"] is not True:
            return {"status": "staged", "applied": False}
        latest = (
            root
            / "results"
            / "candidate_refresh"
            / "formal_candidate_batch_latest.json"
        )
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text('{"status":"passed"}\n', encoding="utf-8")
        return {
            "status": "completed",
            "applied": True,
            "latest_path": str(latest.relative_to(root)).replace("\\", "/"),
        }

    monkeypatch.setattr(chain, "run_formal_candidate_refresh", publish_candidates)

    result = chain.run_daily_data_chain(
        project_root=root,
        target_date="2026-07-22",
        network=True,
        dry_run=False,
        run_id=f"sector-{failure_mode}",
        batch_size=1,
        batch_workers=1,
        rebuild_features=False,
    )

    manifest_path = (
        root
        / "results"
        / "data_chain"
        / "runs"
        / f"sector-{failure_mode}"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    membership = manifest["components"]["sector_refresh"]["membership_observation"]
    sector_history = manifest["components"]["sector_history_archive"]
    universe_after = {
        path.relative_to(universe_dir).as_posix(): path.read_bytes()
        for path in universe_dir.rglob("*")
        if path.is_file()
    }

    assert result["status"] == "completed"
    assert manifest["status"] == "completed"
    assert manifest["ok"] is True
    assert manifest["components"]["sector_refresh"]["status"] == "completed"
    assert membership["status"] == "degraded"
    assert membership["fetched_board_count"] == expected_fetched_board_count
    assert membership["canonical_write"] is False
    assert membership["data_universe_written"] is False
    assert membership["path"] == ""
    assert sector_history["status"] == "degraded"
    assert sector_history["evidence_eligible"] is False
    assert sector_history["written"] is False
    assert sector_history["canonical_write"] is False
    assert "sector_membership_source_missing" in sector_history["reason_codes"]
    assert "sector_history_archive_degraded" in manifest["warnings"]
    assert "sector_history_archive_degraded" not in manifest["errors"]
    assert candidate_refresh_calls == []
    assert manifest["components"]["candidate_refresh_gate"]["status"] == "blocked"
    assert "upstream_feature_component_not_passed" in manifest["components"][
        "candidate_refresh_gate"
    ]["reason_codes"]
    assert manifest["components"]["candidate_refresh"]["status"] == "blocked"
    assert not any(
        warning.startswith("candidate_refresh_unexpected_status:")
        for warning in manifest["warnings"]
    )
    assert not (
        root
        / "results"
        / "candidate_refresh"
        / "formal_candidate_batch_latest.json"
    ).is_file()
    assert member_calls == [("ths", "Energy"), ("ths", "Finance")]
    assert universe_after == universe_before
    assert not (root / "data" / "sector_history").exists()


def test_data_health_minute_coverage_preserves_three_distinct_tiers() -> None:
    from quantdesk.data_health_dashboard import build_minute_universe_coverage

    payload = build_minute_universe_coverage(
        {
            "exists": True,
            "run_id": "chain-run-1",
            "status": "partial",
            "target_date": "2026-07-16",
            "generated_at": "2026-07-16 18:31:00",
            "universe": {
                "critical": {"version": "critical:v1", "sha256": "a" * 64},
                "current_analysis_pool": {
                    "version": "current_analysis_pool:v1",
                    "sha256": "b" * 64,
                },
            },
            "coverage": {
                "daily": {"coverage_ratio": 1.0},
                "critical": {
                    "requested_count": 19,
                    "complete_session_count": 19,
                    "coverage_ratio": 1.0,
                    "status": "passed",
                },
                "current_analysis_pool": {
                    "requested_count": 914,
                    "complete_session_count": 900,
                    "coverage_ratio": 900 / 914,
                    "status": "partial",
                },
            },
        }
    )

    assert payload["run_id"] == "chain-run-1"
    assert payload["target_trade_date"] == "2026-07-16"
    assert payload["tiers"]["critical"]["requested_count"] == 19
    assert payload["tiers"]["current_analysis_pool"]["requested_count"] == 914
    assert payload["tiers"]["extended_research_pool"]["status"] == "separate_historical_queue"
    assert payload["tiers"]["extended_research_pool"]["requested_count"] is None


def test_data_health_formal_chain_status_surfaces_scheduler_freeze_and_gaps() -> None:
    from quantdesk.data_health_dashboard import build_data_health_dashboard

    chain_payload = {
        "exists": True,
        "run_id": "run-formal",
        "status": "blocked",
        "target_date": "2026-07-16",
        "authoritative": True,
        "daily_data_chain_contract_version": 10,
        "coverage": {
            "daily": {"status": "passed", "requested_count": 914, "coverage_ratio": 1.0},
            "critical_daily": {"status": "passed", "requested_count": 1, "coverage_ratio": 1.0},
            "critical": {"status": "passed", "requested_count": 2, "complete_session_count": 2, "coverage_ratio": 1.0},
            "current_analysis_pool": {"status": "partial", "requested_count": 914, "complete_session_count": 913, "coverage_ratio": 913 / 914},
        },
        "universe": {
            "current_analysis_pool": {"symbol_count": 914, "sha256": "a" * 64, "version": "current_analysis_pool:v1"},
            "critical_daily_outside_analysis_pool": {"symbol_count": 1, "symbols": ["600405"]},
        },
        "components": {
            "conditional_plan_freeze": {"status": "blocked", "written": False, "reason_codes": ["retroactive_generation_window_closed"]}
        },
        "retry_symbols": ["600288"],
        "errors": ["conditional_plan_freeze_blocked"],
    }
    scheduler_payload = {
        "status": "disabled",
        "task_name": "QuantDesk Daily Data Chain",
        "enabled": False,
        "first_run_pending": True,
        "last_task_result": "0x00041303",
        "next_run_time": "N/A",
    }

    payload = build_data_health_dashboard(
        daily_price_payload={"total_symbols": 1, "stale_count": 0, "missing_count": 0},
        freshness_payload={"target_complete_trade_date": "2026-07-16", "local_daily_latest_date": "2026-07-16"},
        intraday_payload={"total_symbols": 0, "missing_symbols": []},
        theme_payload={},
        current_candidate_theme_payload={},
        provider_capability_payload={},
        portfolio_payload={},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={},
        extended_gates_payload={},
        daily_data_chain_payload=chain_payload,
        daily_scheduler_payload=scheduler_payload,
        theme_map={},
        write=False,
    )

    status = payload["formal_daily_chain_status"]
    assert status["run_id"] == "run-formal"
    assert status["target_date"] == "2026-07-16"
    assert status["pool_status"] == "partial_pool"
    assert status["missing_or_retry_symbols"] == ["600288"]
    assert status["critical_daily_status"] == "passed"
    assert status["plan_freeze_status"] == "blocked"
    assert status["scheduler"]["enabled"] is False
    assert "retroactive_generation_window_closed" in status["plan_freeze_reason_codes"]


def test_analysis_minute_batches_use_bounded_concurrency(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import daily_data_chain as chain

    active = 0
    max_active = 0
    guard = __import__("threading").Lock()

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "total_symbols": len(kwargs["symbols"]),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )

    def fake_intraday(symbols, target_date, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time_module = __import__("time")
        time_module.sleep(0.08)
        with guard:
            active -= 1
        return {
            "status": "ok",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "failed_symbol_list": [],
        }

    monkeypatch.setattr(chain, "update_intraday_1m_for_symbols", fake_intraday)
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {"symbol": symbol, "complete_session": True}
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=[f"6000{index:02d}" for index in range(8)],
        network=False,
        dry_run=True,
        batch_size=2,
        batch_workers=4,
        rebuild_features=False,
    )

    assert payload["status"] == "dry_run"
    assert max_active >= 2


def test_beginner_dashboard_adds_chain_without_changing_legacy_cards() -> None:
    from quantdesk.data_health_dashboard import build_data_health_dashboard

    chain_payload = {
        "exists": True,
        "run_id": "run-ui",
        "status": "blocked",
        "target_date": "2026-07-16",
        "coverage": {
            "daily": {"coverage_ratio": 1.0},
            "critical": {
                "requested_count": 2,
                "complete_session_count": 1,
                "coverage_ratio": 0.5,
                "status": "blocked",
            },
            "current_analysis_pool": {
                "requested_count": 914,
                "complete_session_count": 800,
                "coverage_ratio": 800 / 914,
                "status": "blocked",
            },
        },
        "universe": {},
    }
    payload = build_data_health_dashboard(
        daily_price_payload={"total_symbols": 1, "stale_count": 0, "missing_count": 0},
        freshness_payload={
            "target_complete_trade_date": "2026-07-16",
            "local_daily_latest_date": "2026-07-16",
        },
        intraday_payload={"total_symbols": 0, "missing_symbols": []},
        theme_payload={},
        current_candidate_theme_payload={},
        provider_capability_payload={},
        portfolio_payload={},
        daily_workflow_payload={},
        real_sample_payload={},
        self_validation_payload={},
        strategy_payload={},
        extended_gates_payload={},
        theme_map={},
        daily_data_chain_payload=chain_payload,
        write=False,
    )

    assert set(payload["cards"]) == {
        "daily_data",
        "intraday_data",
        "theme_map",
        "portfolio_review",
    }
    assert payload["daily_data_chain"]["run_id"] == "run-ui"
    assert payload["minute_universe_coverage"]["tiers"]["critical"]["status"] == "blocked"
    assert payload["source_status"]["daily_data_chain"]["run_id"] == "run-ui"


def test_formal_intraday_update_rejects_unknown_basis_local_cache(monkeypatch, tmp_path: Path) -> None:
    from quantdesk.intraday import intraday_update_engine as engine

    calls: list[str] = []
    written: list[str] = []
    full = _full_session_frame()

    monkeypatch.setattr(
        engine,
        "_resolve_symbols",
        lambda *args, **kwargs: (
            [{"symbol": "600000", "name": "600000"}],
            "2026-07-16",
        ),
    )

    def fake_fetch(provider, symbol, target_date, **kwargs):
        calls.append(provider)
        frame = full.copy()
        if provider == "mootdx_intraday":
            frame["price_basis"] = "raw"
        return {
            "provider": provider,
            "status": "ok",
            "data": frame,
            "ohlcv_complete": True,
            "signal_eligible": True,
            "price_only_intraday": False,
            "price_basis": "raw" if provider == "mootdx_intraday" else "unknown",
        }

    monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
    monkeypatch.setattr(
        engine,
        "_merge_and_write",
        lambda symbol, frame, **kwargs: written.append(symbol) or tmp_path / f"{symbol}.csv",
    )

    payload = engine.update_intraday_1m_for_symbols(
        ["600000"],
        "2026-07-16",
        provider_order=["local_existing", "mootdx_intraday"],
        network=True,
        intraday_dir=tmp_path,
        output_path=tmp_path / "result.json",
        require_raw_price_basis=True,
    )

    assert calls == ["local_existing", "mootdx_intraday"]
    assert written == ["600000"]
    assert payload["updated_symbol_list"] == ["600000"]
    local = payload["provider_results"][0]
    assert local["reason_code"] == "price_basis_not_raw"


def test_intraday_schema_persists_explicit_raw_price_basis() -> None:
    from quantdesk.intraday.intraday_schema import normalize_intraday_frame

    payload = normalize_intraday_frame(
        _full_session_frame(),
        default_symbol="600000",
        source="mootdx_intraday",
        target_date="2026-07-16",
        price_basis="raw",
    )

    frame = payload["frame"]
    assert "price_basis" in frame.columns
    assert set(frame["price_basis"].astype(str)) == {"raw"}
    assert payload["price_basis"] == "raw"


def test_complete_session_reads_price_basis_from_target_rows() -> None:
    from quantdesk.intraday.intraday_quality_audit import assess_complete_session

    frame = _full_session_frame()
    frame["price_basis"] = "raw"

    result = assess_complete_session(
        frame,
        symbol="600000",
        target_date="2026-07-16",
    )

    assert result["status"] == "complete_session"
    assert result["price_basis"] == "raw"


def test_latest_chain_reports_live_lock_when_terminal_latest_missing(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import daily_data_chain as chain

    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "live-run",
                "pid": 123,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T18:02:18",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "_lock_is_held", lambda path: True)

    payload = chain.latest_daily_data_chain(tmp_path)

    assert payload["exists"] is True
    assert payload["status"] == "running"
    assert payload["run_id"] == "live-run"
    assert payload["target_date"] == "2026-07-16"
    assert payload["published_latest"] is False


def test_latest_chain_rejects_pre_contract_terminal_manifest(tmp_path: Path) -> None:
    from quantdesk.daily_data_chain import latest_daily_data_chain

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "task_type": "daily_data_chain",
                "run_id": "old-running-code",
                "target_date": "2026-07-16",
                "status": "completed",
                "published_latest": True,
            }
        ),
        encoding="utf-8",
    )

    payload = latest_daily_data_chain(tmp_path)

    assert payload["exists"] is True
    assert payload["status"] == "superseded_contract"
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False


def test_cached_dashboard_overlays_live_chain_without_rescanning(tmp_path: Path) -> None:
    from quantdesk.data_health_dashboard import build_cached_data_health_dashboard

    snapshot = tmp_path / "data_health_dashboard_latest.json"
    snapshot.write_text(
        json.dumps(
            {
                "ok": True,
                "health_level": "green",
                "generated_at": "2026-07-16T18:00:00",
                "cards": {"daily_data": {"status": "green"}},
                "source_status": {},
            }
        ),
        encoding="utf-8",
    )
    payload = build_cached_data_health_dashboard(
        snapshot_path=snapshot,
        daily_data_chain_payload={
            "exists": True,
            "status": "running",
            "run_id": "live-run",
            "target_date": "2026-07-16",
        },
    )

    assert payload["ok"] is True
    assert payload["snapshot_mode"] == "persisted_overlay"
    assert payload["health_level"] == "yellow"
    assert payload["daily_data_chain"]["run_id"] == "live-run"
    assert payload["source_status"]["daily_data_chain"]["run_id"] == "live-run"


def test_refresh_data_health_snapshot_writes_terminal_chain_overlay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain
    from quantdesk import data_health_dashboard as dashboard

    calls: dict[str, object] = {}
    manifest = {"run_id": "run-health", "status": "completed"}

    def fake_builder(**kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "health_level": "green",
            "generated_at": "2026-07-16T20:00:00",
        }

    monkeypatch.setattr(dashboard, "build_data_health_dashboard", fake_builder)

    result = chain._refresh_data_health_snapshot(tmp_path, manifest)

    assert result["ok"] is True
    assert result["status"] == "refreshed"
    assert calls["daily_data_chain_payload"] is manifest
    assert calls["write"] is True
    assert calls["output_path"] == tmp_path / "results" / "data_health" / "data_health_dashboard_latest.json"


def test_refresh_data_health_snapshot_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain
    from quantdesk import data_health_dashboard as dashboard

    def failing_builder(**kwargs):
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(dashboard, "build_data_health_dashboard", failing_builder)

    result = chain._refresh_data_health_snapshot(
        tmp_path,
        {"run_id": "run-health", "status": "completed"},
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "snapshot boom" in result["error"]


def test_authoritative_run_downgrades_when_health_snapshot_refresh_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    existing_plan = tmp_path / "results" / "conditional_trade_plans_latest.json"
    existing_plan.parent.mkdir(parents=True)
    old_plan_bytes = b'{"plan_id":"previous-valid-plan"}\n'
    existing_plan.write_bytes(old_plan_bytes)
    candidate_refresh_calls: list[dict[str, object]] = []

    def forbidden_candidate_refresh(**kwargs):
        candidate_refresh_calls.append(kwargs)
        raise AssertionError("partial chain must not refresh formal candidates")

    monkeypatch.setattr(
        chain,
        "run_formal_candidate_refresh",
        forbidden_candidate_refresh,
    )

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [
            {"symbol": "600000"},
            {"symbol": "600001"},
        ],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [{"symbol": "600000"}]},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "target_date": kwargs["target_date"],
            "symbols_checked": len(kwargs["symbols"]),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda symbols, target_date, **kwargs: {
            "status": "completed",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "missing": 0,
                "partial_session": 0,
                "invalid_session": 0,
            },
            "items": [
                {"symbol": symbol, "complete_session": True}
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_daily_features",
        lambda **kwargs: {
            "ok": True,
            "status": "passed",
            "requested_count": 2,
            "feature_count": 2,
            "output_row_count": 2,
            "output_sha256": "a" * 64,
            "skipped_count": 0,
            "accepted_price_basis": "raw",
            "feature_input_snapshot_stable": True,
            "price_policy_validation": {
                "status": "passed",
                "run_status": "passed",
                "eligible": True,
                "accepted_price_basis": "raw",
                "blocked_symbols": [],
                "reason_codes": [],
            },
            "per_symbol": {
                symbol: {
                    "eligible": True,
                    "price_basis": "raw",
                    "latest_date": "2026-07-16",
                }
                for symbol in kwargs["symbols"]
            },
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "target_date": target_date,
            "symbols": list(symbols),
            "legitimate_no_session_symbols": [],
            "unknown_symbols": [],
            "status": "passed",
            "formal_eligible": True,
        },
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "prepared",
            "prepared": True,
            "written": False,
            "plan_id": "plan_20260716_for_20260717",
            "prepared_plan": {
                "plan_id": "plan_20260716_for_20260717",
                "data_chain_run_id": "health-failure-run",
            },
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {
            "ok": False,
            "status": "failed",
            "error": "snapshot boom",
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=["600000", "600001"],
        network=True,
        dry_run=False,
        rebuild_features=True,
        run_id="health-failure-run",
    )

    assert payload["status"] == "partial"
    assert payload["ui_snapshot_ready"] is False
    assert payload["components"]["data_health_snapshot"]["status"] == "failed"
    assert "data_health_snapshot_refresh_failed" in payload["warnings"]
    assert payload["published_latest"] is True
    assert existing_plan.read_bytes() == old_plan_bytes
    assert candidate_refresh_calls == []


def test_unexpected_component_failure_publishes_failed_terminal_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    refresh_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": "600000"}],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )

    def fail_daily(**kwargs):
        raise RuntimeError("daily provider boom")

    monkeypatch.setattr(chain, "update_daily_prices", fail_daily)
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: refresh_calls.append(dict(manifest))
        or {"ok": True, "status": "refreshed"},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=["600000"],
        network=True,
        dry_run=False,
        rebuild_features=False,
        run_id="unexpected-failure-run",
    )

    latest = json.loads(
        (
            tmp_path
            / "results"
            / "data_chain"
            / "daily_data_chain_latest.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["ok"] is False
    assert payload["published_latest"] is True
    assert payload["error_type"] == "RuntimeError"
    assert "daily provider boom" in payload["error"]
    assert latest["run_id"] == "unexpected-failure-run"
    assert latest["status"] == "failed"
    assert refresh_calls[0]["status"] == "failed"
    assert payload["protected_state_verified"] is True
    assert payload["protected_state_unchanged"] is True
    assert payload["boundaries"]["paper_ledger_mutated"] is False
    assert chain._lock_is_held(tmp_path / "state" / "daily_data_chain.lock") is False


def test_latest_chain_prefers_live_lock_over_previous_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "daily_data_chain_contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": "previous-run",
                "status": "completed",
                "target_date": "2026-07-15",
                "generated_at": "2026-07-15T20:00:00",
                "published_latest": True,
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "live-run",
                "pid": 123,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T15:45:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "_lock_is_held", lambda path: True)

    payload = chain.latest_daily_data_chain(tmp_path)

    assert payload["status"] == "running"
    assert payload["run_id"] == "live-run"
    assert payload["target_date"] == "2026-07-16"
    assert payload["previous_terminal"]["run_id"] == "previous-run"
    assert payload["previous_terminal"]["status"] == "completed"


def test_running_chain_card_uses_processing_copy_before_terminal_coverage() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "app" / "static" / "js" / "daily_research.js").read_text(
        encoding="utf-8"
    )
    start = script.index("const chainStatus =")
    end = script.index("const dailyChainHomeCardHtml", start)
    renderer = script[start:end]

    assert 'chainStatus === "running"' in renderer
    assert "terminal coverage" in renderer
    assert "previous_terminal" in renderer


def test_busy_chain_returns_before_universe_and_fingerprint_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("universe should not load")),
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: (_ for _ in ()).throw(AssertionError("fingerprints should not hash")),
    )

    with chain._daily_chain_lock(
        tmp_path,
        "active-run",
        target_date="2026-07-16",
    ) as active_lock:
        assert active_lock["acquired"] is True
        payload = chain.run_daily_data_chain(
            project_root=tmp_path,
            target_date="2026-07-16",
            network=False,
            dry_run=True,
            run_id="busy-probe",
        )

    assert payload["status"] == "busy"
    assert payload["active_run_id"] == "active-run"
    assert payload["published_latest"] is False


def test_process_lock_covers_universe_refresh_before_network_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    refresh_entered = threading.Event()
    release_first = threading.Event()
    refresh_calls: list[str] = []
    feature_calls: list[dict[str, object]] = []
    calls_lock = threading.Lock()
    first_result: dict[str, object] = {}

    monkeypatch.setattr(chain, "build_default_fetchers", lambda: {"provider": object()})
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "skipped_test"},
    )
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda root, *, network: {
            "ok": True,
            "status": "completed" if network else "skipped",
            "network": bool(network),
        },
    )
    monkeypatch.setattr(
        chain,
        "_update_current_state",
        lambda root, manifest: {"ok": True, "status": "updated"},
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(chain, "resolve_symbols", lambda **kwargs: [{"symbol": "600000"}])
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "symbols_checked": 1,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "invalid_price_basis_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda symbols, target_date, **kwargs: {
            "unknown_symbols": [],
            "legitimate_no_session_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda symbols, target_date, **kwargs: {
            "status": "completed",
            "updated_symbols": len(symbols),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **kwargs: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in symbols
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "_feature_coverage",
        lambda *args, **kwargs: {"status": "passed", "missing_symbols": []},
    )

    def build_scoped_features(**kwargs):
        feature_calls.append(dict(kwargs))
        return {"ok": True, "status": "passed", "feature_count": 1}

    monkeypatch.setattr(chain, "build_daily_features", build_scoped_features)
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )
    monkeypatch.setattr(
        chain,
        "freeze_t_plus_one_plan",
        lambda **kwargs: {
            "ok": True,
            "status": "completed",
            "written": True,
            "plan_id": "lock-test-plan",
            "reason_codes": [],
        },
    )

    def blocking_refresh(*args, **kwargs):
        with calls_lock:
            refresh_calls.append(str(kwargs.get("target_date") or args[1]))
            call_number = len(refresh_calls)
        if call_number == 1:
            refresh_entered.set()
            assert release_first.wait(5), "test did not release first refresh"
        return {
            "ok": True,
            "status": "completed",
            "formal_eligible": True,
            "provider": "test",
            "quote_date": "2026-07-17",
        }

    monkeypatch.setattr(chain, "refresh_universe_snapshot", blocking_refresh)

    def run_first() -> None:
        first_result.update(
            chain.run_daily_data_chain(
                project_root=tmp_path,
                target_date="2026-07-17",
                network=True,
                dry_run=False,
                run_id="lock-first",
            )
        )

    worker = threading.Thread(target=run_first, daemon=True)
    worker.start()
    assert refresh_entered.wait(5), "first run did not reach universe refresh"
    try:
        second = chain.run_daily_data_chain(
            project_root=tmp_path,
            target_date="2026-07-17",
            network=True,
            dry_run=False,
            run_id="lock-second",
        )
    finally:
        release_first.set()
        worker.join(timeout=10)

    assert worker.is_alive() is False
    assert first_result["status"] == "completed"
    assert second["status"] == "busy"
    assert second["active_run_id"] == "lock-first"
    assert refresh_calls == ["2026-07-17"]
    assert feature_calls == [
        {
            "only_low_price": True,
            "symbols": ["600000"],
            "daily_dir": tmp_path / "data" / "daily",
            "low_price_universe_path": (
                tmp_path / "data" / "universe" / "low_price_learning_symbols.csv"
            ),
            "output_path": tmp_path / "data" / "features" / "daily_features_latest.csv",
            "summary_path": tmp_path / "data" / "features" / "daily_features_summary.json",
        }
    ]


def test_formal_network_subset_cannot_publish_authoritative_latest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    monkeypatch.setattr(chain, "_lock_is_held", lambda path: False)
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [
            {"symbol": "600000"},
            {"symbol": "600001"},
        ],
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: (_ for _ in ()).throw(
            AssertionError("formal subset must block before fingerprints")
        ),
    )
    candidate_refresh_calls: list[dict[str, object]] = []

    def forbidden_candidate_refresh(**kwargs):
        candidate_refresh_calls.append(kwargs)
        raise AssertionError("blocked chain must not refresh formal candidates")

    monkeypatch.setattr(
        chain,
        "run_formal_candidate_refresh",
        forbidden_candidate_refresh,
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=["600000"],
        network=True,
        dry_run=False,
        run_id="subset-run",
    )

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    assert payload["status"] == "blocked"
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False
    assert "formal_scope_not_full_current_analysis_pool" in payload["errors"]
    assert latest.exists() is False
    assert candidate_refresh_calls == []


def test_candidate_refresh_step_runs_for_symbol_degradation_without_upgrading_chain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    manifest = {
        "daily_data_chain_contract_version": 10,
        "run_id": "daily-data-chain-20260806T103006Z-fixture",
        "target_date": "2026-08-06",
        "status": "blocked",
        "authoritative": True,
        "network": True,
        "dry_run": False,
        "protected_state_unchanged": True,
        "universe": {
            "current_analysis_pool": chain.build_universe_contract(
                ["600000", "600001"],
                target_date="2026-08-06",
                name="current_analysis_pool",
            )
        },
        "coverage": {
            "daily": {
                "status": "blocked",
                "requested_count": 2,
                "reported_count": 2,
                "fresh_count": 1,
                "stale_count": 1,
                "stale_symbols": ["600001"],
                "missing_count": 0,
                "missing_symbols": [],
                "failed_count": 0,
                "failed_symbols": [],
                "invalid_price_basis_count": 0,
                "invalid_price_basis_symbols": [],
            },
            "features": {
                "status": "blocked",
                "output_sha256": "b" * 64,
                "reason_codes": ["feature_target_date_mismatch"],
            },
            "current_analysis_pool": {
                "status": "blocked",
                "failed_symbols": ["600001"],
            },
            "session_expectation": {
                "status": "blocked",
                "unknown_count": 1,
                "unknown_symbols": ["600001"],
            },
        },
        "components": {
            "features": {
                "status": "passed",
                "latest_date": "2026-08-06",
                "accepted_price_basis": "raw",
                "output_sha256": "b" * 64,
                "per_symbol": {
                    "600000": {
                        "eligible": True,
                        "price_basis": "raw",
                        "latest_date": "2026-08-06",
                    }
                },
            },
            "session_expectation": {
                "target_date": "2026-08-06",
                "status": "blocked",
                "formal_eligible": False,
                "symbols": ["600000", "600001"],
                "classification_counts": {
                    "expected_trade": 1,
                    "unknown": 1,
                },
                "counts": {"expected_trade": 1, "unknown": 1},
                "unknown_symbols": ["600001"],
            },
            "critical_sources": {
                "status": "passed",
                "formal_eligible": True,
                "failed_sources": [],
            },
        },
        "warnings": [],
    }
    calls: list[dict[str, object]] = []

    def candidate_refresh(**kwargs):
        calls.append(kwargs)
        return {"status": "completed", "applied": True, "candidate_count": 1}

    monkeypatch.setattr(chain, "run_formal_candidate_refresh", candidate_refresh)

    result = chain._run_candidate_refresh_step(
        root=tmp_path,
        manifest=manifest,
        captured_at="2026-08-06T20:00:00+08:00",
    )

    assert manifest["status"] == "blocked"
    assert result["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["data_chain_manifest"] is manifest
    assert manifest["components"]["candidate_refresh_gate"]["degraded"] is True
    assert manifest["components"]["candidate_refresh_gate"]["eligible_symbols"] == [
        "600000"
    ]
    assert manifest["components"]["candidate_refresh"] == result

    manifest["protected_state_unchanged"] = False
    calls.clear()
    blocked = chain._run_candidate_refresh_step(
        root=tmp_path,
        manifest=manifest,
        captured_at="2026-08-06T20:01:00+08:00",
    )
    assert blocked["status"] == "blocked"
    assert calls == []
    assert "upstream_protected_state_not_unchanged" in blocked["reason_codes"]


def test_candidate_refresh_step_isolates_gate_exceptions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    manifest = {"components": {}, "warnings": []}

    def broken_gate(_manifest):
        raise ValueError("malformed fixture")

    monkeypatch.setattr(chain, "candidate_refresh_gate", broken_gate)
    monkeypatch.setattr(
        chain,
        "run_formal_candidate_refresh",
        lambda **_kwargs: pytest.fail("refresh must not run after gate failure"),
    )

    result = chain._run_candidate_refresh_step(
        root=tmp_path,
        manifest=manifest,
        captured_at="2026-08-06T20:00:00+08:00",
    )

    assert result == {
        "status": "blocked",
        "applied": False,
        "reason_codes": ["candidate_refresh_gate_exception:ValueError"],
        "error": "ValueError:malformed fixture",
    }
    assert manifest["components"]["candidate_refresh_gate"] == result
    assert manifest["components"]["candidate_refresh"] == result


def test_daily_coverage_rejects_partial_and_invalid_basis_even_when_fresh() -> None:
    from quantdesk.daily_data_chain import _daily_coverage

    payload = {
        "status": "partial",
        "total_symbols": 914,
        "stale_count_after": 0,
        "missing_count_after": 0,
        "failed_symbols": 1,
        "failed_symbol_list": ["600000"],
        "require_raw_price_basis": True,
        "invalid_price_basis_symbols": ["600000"],
    }

    gate = _daily_coverage(payload, 914)

    assert gate["status"] == "blocked"
    assert gate["reported_count"] == 914
    assert gate["failed_count"] == 1
    assert gate["invalid_price_basis_count"] == 1
    assert "daily_component_not_completed" in gate["reason_codes"]
    assert "daily_price_basis_not_raw" in gate["reason_codes"]


def test_daily_coverage_accepts_verified_upstream_reuse_as_completed() -> None:
    from quantdesk.daily_data_chain import _daily_coverage

    payload = {
        "status": "reused_verified_upstream",
        "upstream_reuse": True,
        "total_symbols": 914,
        "stale_count_after": 0,
        "missing_count_after": 0,
        "stale_symbol_list_after": [],
        "missing_symbol_list_after": [],
        "failed_symbols": 0,
        "failed_symbol_list": [],
        "invalid_price_basis_symbols": [],
        "require_raw_price_basis": True,
    }

    gate = _daily_coverage(payload, 914)

    assert gate["status"] == "passed"
    assert gate["fresh_count"] == 914
    assert "daily_component_not_completed" not in gate["reason_codes"]


def test_daily_update_subset_keeps_critical_symbols_out_of_analysis_coverage() -> None:
    from quantdesk.daily_data_chain import _subset_daily_update_payload

    raw = {
        "status": "completed",
        "require_raw_price_basis": True,
        "fresh_symbol_list_after": ["600000", "999999"],
        "stale_symbol_list_after": [],
        "missing_symbol_list_after": [],
        "failed_symbol_list": [],
        "invalid_price_basis_symbols": [],
        "raw_zero_volume_target_symbol_list": ["600000", "999999"],
        "latest_by_symbol_after": {"600000": "2026-07-17", "999999": "2026-07-17"},
    }

    analysis = _subset_daily_update_payload(raw, ["600000"])

    assert analysis["raw_zero_volume_target_symbol_list"] == ["600000"]
    assert analysis["raw_zero_volume_target_symbols"] == 1
    critical = _subset_daily_update_payload(raw, ["999999"])

    assert analysis["total_symbols"] == 1
    assert analysis["fresh_symbol_list_after"] == ["600000"]
    assert critical["total_symbols"] == 1
    assert critical["fresh_symbol_list_after"] == ["999999"]


def test_analysis_coverage_913_of_914_is_partial_not_passed() -> None:
    from quantdesk.daily_data_chain import _coverage_from_audit

    audit = {
        "summary": {
            "complete_session": 913,
            "partial_session": 0,
            "invalid_session": 0,
            "missing": 1,
        },
        "items": [
            {"symbol": "600000", "complete_session": False},
        ],
    }

    coverage = _coverage_from_audit(
        audit,
        requested_count=914,
        tier="current_analysis_pool",
    )

    assert coverage["status"] == "partial"
    assert coverage["complete_session_count"] == 913
    assert coverage["failed_count"] == 1


def test_feature_coverage_requires_full_raw_stable_target_date() -> None:
    from quantdesk.daily_data_chain import _feature_coverage

    symbols = ["600000", "600001"]
    payload = {
        "ok": True,
        "status": "passed",
        "requested_count": 2,
        "feature_count": 2,
        "output_row_count": 2,
        "output_sha256": "a" * 64,
        "skipped_count": 0,
        "accepted_price_basis": "raw",
        "feature_input_snapshot_stable": True,
        "price_policy_validation": {
            "status": "passed",
            "run_status": "passed",
            "eligible": True,
            "accepted_price_basis": "raw",
            "blocked_symbols": [],
            "reason_codes": [],
        },
        "per_symbol": {
            symbol: {
                "eligible": True,
                "price_basis": "raw",
                "latest_date": "2026-07-16",
            }
            for symbol in symbols
        },
    }

    passed = _feature_coverage(payload, symbols, "2026-07-16")
    assert passed["status"] == "passed"

    payload["status"] = "partial"
    payload["feature_count"] = 1
    payload["skipped_count"] = 1
    payload["per_symbol"]["600001"]["eligible"] = False
    payload["per_symbol"]["600001"]["latest_date"] = "2026-07-15"
    blocked = _feature_coverage(payload, symbols, "2026-07-16")



def test_unexpected_failure_reports_protected_state_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": "600000"}],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fingerprints = iter(
        [
            {
                "paper_ledger": {"exists": True, "sha256": "before"},
                "adjustment_factors": {"exists": True, "sha256": "same"},
            },
            {
                "paper_ledger": {"exists": True, "sha256": "after"},
                "adjustment_factors": {"exists": True, "sha256": "same"},
            },
        ]
    )
    monkeypatch.setattr(chain, "_protected_fingerprints", lambda root: next(fingerprints))
    monkeypatch.setattr(
        chain,
        "_refresh_data_health_snapshot",
        lambda root, manifest: {"ok": True, "status": "refreshed"},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        symbols=["600000"],
        network=True,
        dry_run=False,
        run_id="mutation-failure-run",
    )




def test_latest_chain_marks_released_running_metadata_as_abandoned(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "daily_data_chain_contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": "previous-terminal",
                "status": "completed",
                "target_date": "2026-07-15",
                "generated_at": "2026-07-15T20:00:00",
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "abandoned-run",
                "pid": 1234,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T15:45:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "_lock_is_held", lambda path: False)

    payload = chain.latest_daily_data_chain(tmp_path)

    assert payload["status"] == "abandoned"
    assert payload["ok"] is False
    assert payload["authoritative"] is False
    assert payload["run_id"] == "abandoned-run"
    assert payload["previous_terminal"]["run_id"] == "previous-terminal"


def test_daily_and_minute_coverage_exempt_only_verified_legitimate_no_session() -> None:
    from quantdesk.daily_data_chain import _coverage_from_audit, _daily_coverage

    daily = _daily_coverage(
        {
            "status": "partial",
            "total_symbols": 2,
            "stale_count_after": 1,
            "missing_count_after": 0,
            "stale_symbol_list_after": ["000002"],
            "missing_symbol_list_after": [],
            "failed_symbols": 1,
            "failed_symbol_list": ["000002"],
            "require_raw_price_basis": True,
            "invalid_price_basis_symbols": [],
        },
        2,
        ["000002"],
    )
    assert daily["status"] == "passed"
    assert daily["legitimate_no_data_count"] == 1
    assert daily["failed_count"] == 0

    minute = _coverage_from_audit(
        {
            "summary": {
                "complete_session": 1,
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [{"symbol": "000001", "complete_session": True}],
        },
        requested_count=2,
        tier="current_analysis_pool",
        legitimate_no_data_symbols=["000002"],
    )
    assert minute["status"] == "passed"
    assert minute["effective_complete_count"] == 2
    assert minute["legitimate_no_data_count"] == 1



def test_checkpoint_publishes_each_completed_future_before_slower_batches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    threading = __import__("threading")
    time_module = __import__("time")
    fast_finished = threading.Event()
    release_slow = threading.Event()
    result_holder: dict[str, object] = {}
    symbols = ["600000", "600001", "600002", "600003"]

    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": item} for item in symbols],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )
    daily_update_calls: list[dict[str, object]] = []

    def fake_daily_update(**kwargs: object) -> dict[str, object]:
        daily_update_calls.append(dict(kwargs))
        return {
            "status": "completed",
            "total_symbols": len(kwargs["symbols"]),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": list(kwargs["symbols"]),
        }

    monkeypatch.setattr(chain, "update_daily_prices", fake_daily_update)
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda requested, target_date, **kwargs: {
            "target_date": target_date,
            "symbols": list(requested),
            "legitimate_no_session_symbols": [],
            "unknown_symbols": [],
        },
    )

    def fake_intraday(requested, target_date, **kwargs):
        symbol = requested[0]
        if symbol == "600003":
            fast_finished.set()
        else:
            assert release_slow.wait(5)
        payload = {
            "ok": True,
            "status": "ok",
            "run_id": kwargs["run_id"],
            "target_date": target_date,
            "require_raw_price_basis": True,
            "provider_order": list(kwargs["provider_order"]),
            "processed_symbols": list(requested),
            "updated_symbols": len(requested),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "updated_symbol_list": list(requested),
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
        }
        output_path = Path(kwargs["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(chain, "update_intraday_1m_for_symbols", fake_intraday)
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda requested, target_date, **kwargs: {
            "summary": {
                "complete_session": len(requested),
                "formal_execution_eligible": len(requested),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in requested
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )

    def run_chain() -> None:
        result_holder["payload"] = chain.run_daily_data_chain(
            project_root=tmp_path,
            target_date="2026-07-16",
            network=False,
            dry_run=False,
            run_id="checkpoint-progress-run",
            batch_size=1,
            batch_workers=8,
            rebuild_features=False,
        )

    worker = threading.Thread(target=run_chain, daemon=True)
    worker.start()
    checkpoint_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "checkpoint-progress-run"
        / "checkpoint.json"
    )
    try:
        assert fast_finished.wait(3)
        deadline = time_module.monotonic() + 1.5
        while not checkpoint_path.exists() and time_module.monotonic() < deadline:
            time_module.sleep(0.02)
        assert checkpoint_path.exists(), "first completed future must publish progress immediately"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        expected_contract = chain.build_universe_contract(
            [{"symbol": symbol} for symbol in symbols],
            target_date="2026-07-16",
            name="current_analysis_pool",
        )
        assert checkpoint["phase"] == "analysis_intraday_batches"
        assert checkpoint["contract_version"] == chain.DAILY_DATA_CHAIN_CONTRACT_VERSION
        assert checkpoint["run_id"] == "checkpoint-progress-run"
        assert daily_update_calls[0]["workers"] == 8
        assert checkpoint["target_date"] == "2026-07-16"
        assert checkpoint["universe_sha256"] == expected_contract["sha256"]
        assert checkpoint["provider_order"] == list(chain.INTRADAY_PROVIDER_ORDER)
        assert checkpoint["batch_size"] == 1
        assert checkpoint["total_batch_count"] == 4
        assert checkpoint["completed_batch_count"] == 1
        assert checkpoint["processed_symbol_count"] == 1
        assert checkpoint["total_symbol_count"] == 4
        assert checkpoint["progress_percent"] == 25.0
        assert checkpoint["last_batch"] == 4
        assert checkpoint["last_batch_evidence"]["batch"] == 4
        assert checkpoint["accepted_symbols"] == ["600003"]
        assert checkpoint["failed_symbols"] == []
        assert checkpoint["formal_accepted_symbols"] == ["600003"]
        assert checkpoint["formal_rejected_symbols"] == []
        assert len(checkpoint["batch_evidence"]) == 1
        assert checkpoint["batch_evidence"][0]["formal_accepted_symbols"] == ["600003"]
        assert len(checkpoint["batch_evidence"][0]["sha256"]) == 64
        assert checkpoint["updated_at"]
    finally:
        release_slow.set()
        worker.join(10)
    assert not worker.is_alive()
    assert "payload" in result_holder
    assert result_holder["payload"]["authoritative"] is False


def test_latest_running_chain_overlays_matching_checkpoint_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "live-checkpoint-run",
                "pid": 123,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T18:02:18",
            }
        ),
        encoding="utf-8",
    )
    checkpoint = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "live-checkpoint-run"
        / "checkpoint.json"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "phase": "analysis_intraday_batches",
                "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": "live-checkpoint-run",
                "target_date": "2026-07-16",
                "universe_sha256": "a" * 64,
                "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
                "batch_size": 25,
                "total_batch_count": 37,
                "completed_batch_count": 9,
                "processed_symbol_count": 225,
                "total_symbol_count": 914,
                "progress_percent": 24.6171,
                "last_batch": {"batch": 12, "status": "ok"},
                "accepted_symbols": ["600000"],
                "failed_symbols": ["600001"],
                "batch_evidence": [],
                "updated_at": "2026-07-16T18:10:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "_lock_is_held", lambda path: True)

    payload = chain.latest_daily_data_chain(tmp_path)

    assert payload["status"] == "running"
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False
    assert payload["phase"] == "analysis_intraday_batches"
    assert payload["completed_batch_count"] == 9
    assert payload["processed_symbol_count"] == 225
    assert payload["progress_percent"] == 24.6171
    assert payload["checkpoint"]["run_id"] == "live-checkpoint-run"


def test_retry_from_abandoned_run_reuses_only_matching_batch_artifact_accepts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    symbols = ["600000", "600001", "600002", "600003", "600004"]
    contract = chain.build_universe_contract(
        [{"symbol": symbol} for symbol in symbols],
        target_date="2026-07-16",
        name="current_analysis_pool",
    )
    workset_sha256 = chain._symbols_sha256(symbols)
    prior_run_id = "abandoned-batch-run"
    prior_run_dir = tmp_path / "results" / "data_chain" / "runs" / prior_run_id
    batches_dir = prior_run_dir / "batches"
    batches_dir.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": prior_run_id,
                "pid": 999,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T15:45:00",
            }
        ),
        encoding="utf-8",
    )

    def write_batch(index: int, payload: dict[str, object]) -> dict[str, object]:
        path = batches_dir / f"batch_{index:04d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        return {
            "batch": index,
            "path": str(path.relative_to(tmp_path)).replace("\\", "/"),
            "sha256": digest,
            "processed_symbols": list(payload.get("processed_symbols") or []),
        }

    common = {
        "phase": "analysis_intraday_batches",
        "run_id": prior_run_id,
        "target_date": "2026-07-16",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "universe_sha256": contract["sha256"],
        "workset_sha256": workset_sha256,
        "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
        "batch_size": 1,
        "require_raw_price_basis": True,
        "stage6_append_only": True,
        "canonical_import": {"status": "completed", "applied": True},
    }
    batch_payloads = [
        {
            **common,
            "batch_index": 1,
            "status": "ok",
            "processed_symbols": ["600000"],
            "updated_symbol_list": ["600000"],
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
            "formal_accepted_symbols": ["600000"],
            "formal_rejected_symbols": [],
            "formal_quality_reasons": {},
        },
        {
            **common,
            "batch_index": 2,
            "status": "ok",
            "processed_symbols": ["600001"],
            "updated_symbol_list": ["600001"],
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
            "formal_accepted_symbols": ["600001"],
            "formal_rejected_symbols": [],
            "formal_quality_reasons": {},
        },
        {
            **common,
            "batch_index": 3,
            "status": "failed",
            "processed_symbols": ["600002"],
            "updated_symbol_list": [],
            "unchanged_symbol_list": [],
            "failed_symbol_list": ["600002"],
            "formal_accepted_symbols": [],
            "formal_rejected_symbols": ["600002"],
            "formal_quality_reasons": {"600002": ["missing_intraday"]},
        },
        {
            **common,
            "batch_index": 4,
            "status": "degraded",
            "processed_symbols": ["600003"],
            "updated_symbol_list": ["600003"],
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
            "formal_accepted_symbols": [],
            "formal_rejected_symbols": ["600003"],
            "formal_quality_reasons": {"600003": ["price_only_intraday"]},
        },
        {
            **common,
            "batch_index": 5,
            "status": "ok",
            "provider_order": ["local_existing"],
            "processed_symbols": ["600004"],
            "updated_symbol_list": ["600004"],
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
            "formal_accepted_symbols": ["600004"],
            "formal_rejected_symbols": [],
            "formal_quality_reasons": {},
        },
    ]
    evidence = [
        write_batch(index, batch_payload)
        for index, batch_payload in enumerate(batch_payloads, start=1)
    ]
    (prior_run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "phase": "analysis_intraday_batches",
                "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": prior_run_id,
                "target_date": "2026-07-16",
                "universe_sha256": contract["sha256"],
                "workset_sha256": workset_sha256,
                "workset_symbols": symbols,
                "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
                "batch_size": 1,
                "total_batch_count": 5,
                "completed_batch_count": 5,
                "processed_symbol_count": 5,
                "total_symbol_count": 5,
                "progress_percent": 100.0,
                "last_batch": 5,
                # Deliberately untrustworthy: recovery must read batch JSON.
                "accepted_symbols": list(symbols),
                "failed_symbols": [],
                "batch_evidence": evidence,
                "updated_at": "2026-07-16T16:00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": item} for item in symbols],
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "completed",
            "total_symbols": len(kwargs["symbols"]),
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": list(kwargs["symbols"]),
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda requested, target_date, **kwargs: {
            "legitimate_no_session_symbols": [],
            "unknown_symbols": [],
        },
    )
    update_calls: list[list[str]] = []

    def fake_intraday(requested, target_date, **kwargs):
        update_calls.append(list(requested))
        payload = {
            "status": "ok",
            "run_id": kwargs["run_id"],
            "target_date": target_date,
            "provider_order": list(kwargs["provider_order"]),
            "require_raw_price_basis": True,
            "processed_symbols": list(requested),
            "updated_symbols": len(requested),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "updated_symbol_list": list(requested),
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
        }
        output_path = Path(kwargs["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(chain, "update_intraday_1m_for_symbols", fake_intraday)
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda requested, target_date, **kwargs: {
            "summary": {
                "complete_session": len(requested),
                "formal_execution_eligible": len(requested),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in requested
            ],
        },
    )
    monkeypatch.setattr(
        chain,
        "_protected_fingerprints",
        lambda root: {
            "paper_ledger": {"sha256": "same"},
            "adjustment_factors": {"sha256": "same"},
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        network=False,
        dry_run=False,
        run_id="recovered-run",
        batch_size=1,
        batch_workers=2,
        rebuild_features=False,
        retry_from_latest=True,
    )

    retried_symbols = sorted(symbol for call in update_calls for symbol in call)
    assert retried_symbols == ["600002", "600003", "600004"]
    recovery = payload["components"]["analysis_intraday_recovery"]
    assert recovery["source_run_id"] == prior_run_id
    assert recovery["accepted_symbols"] == ["600000", "600001"]
    assert recovery["retry_symbols"] == ["600002", "600003", "600004"]
    assert "artifact_status_not_reusable:degraded" in recovery["reason_codes"]
    assert "artifact_provider_order_mismatch" in recovery["reason_codes"]


def test_abandoned_recovery_rejects_checkpoint_contract_mismatch(tmp_path: Path) -> None:
    from quantdesk import daily_data_chain as chain

    prior_run_dir = tmp_path / "results" / "data_chain" / "runs" / "old-run"
    prior_run_dir.mkdir(parents=True)
    (prior_run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": "old-run",
                "target_date": "2026-07-16",
                "universe_sha256": "a" * 64,
                "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
                "batch_size": 25,
                "batch_evidence": [],
            }
        ),
        encoding="utf-8",
    )

    recovery = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="old-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=20,
        expected_symbols=["600000"],
    )

    assert recovery["status"] == "rejected"
    assert recovery["accepted_symbols"] == []
    assert recovery["retry_symbols"] == ["600000"]
    assert "checkpoint_contract_mismatch:batch_size" in recovery["reason_codes"]


def test_scheduled_cli_retries_latest_by_default_and_allows_explicit_false() -> None:
    from quantdesk.shared import truthy
    from scripts.run_daily_data_chain import build_parser

    parser = build_parser()

    assert truthy(parser.parse_args([]).retry_from_latest) is True
    assert truthy(parser.parse_args(["--retry-from-latest", "false"]).retry_from_latest) is False


def _write_recovery_case(
    tmp_path: Path,
    chain,
    *,
    artifact: dict[str, object],
    evidence_sha256: str | None = None,
    workset_sha256: str | None = None,
) -> dict[str, object]:
    artifact = dict(artifact)
    artifact.setdefault("phase", "analysis_intraday_batches")
    artifact.setdefault("batch_index", 1)
    artifact.setdefault("stage6_append_only", True)
    artifact.setdefault(
        "canonical_import",
        {"status": "completed", "applied": True},
    )
    source_run_id = str(artifact["run_id"])
    source_dir = tmp_path / "results" / "data_chain" / "runs" / source_run_id
    batch_path = source_dir / "batches" / "batch_0001.json"
    batch_path.parent.mkdir(parents=True)
    batch_path.write_text(json.dumps(artifact), encoding="utf-8")
    digest = __import__("hashlib").sha256(batch_path.read_bytes()).hexdigest()
    checkpoint = {
        "phase": "analysis_intraday_batches",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": source_run_id,
        "target_date": artifact["target_date"],
        "universe_sha256": artifact["universe_sha256"],
        "workset_sha256": workset_sha256 or artifact["workset_sha256"],
        "workset_symbols": ["600000"],
        "provider_order": artifact["provider_order"],
        "batch_size": artifact["batch_size"],
        "total_batch_count": 1,
        "completed_batch_count": 1,
        "processed_symbol_count": 1,
        "total_symbol_count": 1,
        "progress_percent": 100.0,
        "last_batch": 1,
        "batch_evidence": [
            {
                "batch": 1,
                "path": str(batch_path.relative_to(tmp_path)).replace("\\", "/"),
                "sha256": digest if evidence_sha256 is None else evidence_sha256,
                "processed_symbols": list(artifact.get("processed_symbols") or []),
            }
        ],
    }
    (source_dir / "checkpoint.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )
    return checkpoint


def test_abandoned_recovery_requires_completed_stage6_append_import(
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    workset_sha256 = chain._symbols_sha256(["600000"])
    artifact = {
        "status": "ok",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": "missing-append-proof-run",
        "target_date": "2026-07-16",
        "universe_sha256": "a" * 64,
        "workset_sha256": workset_sha256,
        "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
        "batch_size": 1,
        "processed_symbols": ["600000"],
        "updated_symbol_list": ["600000"],
        "unchanged_symbol_list": [],
        "failed_symbol_list": [],
        "formal_accepted_symbols": ["600000"],
        "formal_rejected_symbols": [],
        "formal_quality_reasons": {},
        "require_raw_price_basis": True,
        "stage6_append_only": False,
        "canonical_import": {"status": "not_applicable", "applied": False},
    }
    _write_recovery_case(tmp_path, chain, artifact=artifact)

    recovery = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="missing-append-proof-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=1,
        expected_symbols=["600000"],
    )

    assert recovery["accepted_symbols"] == []
    assert recovery["retry_symbols"] == ["600000"]
    assert "artifact_stage6_append_import_missing" in recovery["reason_codes"]


def test_abandoned_recovery_does_not_reuse_provider_accept_without_formal_quality(
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    workset_sha256 = __import__("hashlib").sha256(
        json.dumps(
            ["600000"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    artifact = {
        "status": "ok",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": "formal-reject-run",
        "target_date": "2026-07-16",
        "universe_sha256": "a" * 64,
        "workset_sha256": workset_sha256,
        "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
        "batch_size": 1,
        "processed_symbols": ["600000"],
        "updated_symbol_list": ["600000"],
        "unchanged_symbol_list": [],
        "failed_symbol_list": [],
        "formal_accepted_symbols": [],
        "formal_rejected_symbols": ["600000"],
        "formal_quality_reasons": {"600000": ["price_only_intraday"]},
        "require_raw_price_basis": True,
    }
    _write_recovery_case(tmp_path, chain, artifact=artifact)

    recovery = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="formal-reject-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=1,
        expected_symbols=["600000"],
    )

    assert recovery["accepted_symbols"] == []
    assert recovery["retry_symbols"] == ["600000"]
    assert "artifact_formal_quality_rejected" in recovery["reason_codes"]


def test_abandoned_recovery_rejects_empty_artifact_sha256(tmp_path: Path) -> None:
    from quantdesk import daily_data_chain as chain

    workset_sha256 = __import__("hashlib").sha256(
        json.dumps(
            ["600000"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    artifact = {
        "status": "ok",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": "empty-sha-run",
        "target_date": "2026-07-16",
        "universe_sha256": "a" * 64,
        "workset_sha256": workset_sha256,
        "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
        "batch_size": 1,
        "processed_symbols": ["600000"],
        "updated_symbol_list": ["600000"],
        "unchanged_symbol_list": [],
        "failed_symbol_list": [],
        "formal_accepted_symbols": ["600000"],
        "formal_rejected_symbols": [],
        "formal_quality_reasons": {},
        "require_raw_price_basis": True,
    }
    _write_recovery_case(
        tmp_path,
        chain,
        artifact=artifact,
        evidence_sha256="",
    )

    recovery = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="empty-sha-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=1,
        expected_symbols=["600000"],
    )

    assert recovery["accepted_symbols"] == []
    assert recovery["retry_symbols"] == ["600000"]
    assert "batch_artifact_sha256_missing_or_invalid" in recovery["reason_codes"]


def test_abandoned_recovery_rejects_workset_and_batch_plan_mismatch(
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    workset_sha256 = __import__("hashlib").sha256(
        json.dumps(
            ["600000"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    artifact = {
        "status": "ok",
        "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": "plan-mismatch-run",
        "target_date": "2026-07-16",
        "universe_sha256": "a" * 64,
        "workset_sha256": workset_sha256,
        "provider_order": list(chain.INTRADAY_PROVIDER_ORDER),
        "batch_size": 1,
        "processed_symbols": ["600000"],
        "updated_symbol_list": ["600000"],
        "unchanged_symbol_list": [],
        "failed_symbol_list": [],
        "formal_accepted_symbols": ["600000"],
        "formal_rejected_symbols": [],
        "formal_quality_reasons": {},
        "require_raw_price_basis": True,
    }
    checkpoint = _write_recovery_case(tmp_path, chain, artifact=artifact)
    checkpoint_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "plan-mismatch-run"
        / "checkpoint.json"
    )

    checkpoint["workset_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    workset_mismatch = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="plan-mismatch-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=1,
        expected_symbols=["600000"],
    )
    assert workset_mismatch["accepted_symbols"] == []
    assert "checkpoint_contract_mismatch:workset_sha256" in workset_mismatch["reason_codes"]

    checkpoint["workset_sha256"] = workset_sha256
    checkpoint["total_batch_count"] = 2
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    plan_mismatch = chain._recover_abandoned_batch_artifacts(
        project_root=tmp_path,
        source_run_id="plan-mismatch-run",
        target_date="2026-07-16",
        universe_sha256="a" * 64,
        provider_order=list(chain.INTRADAY_PROVIDER_ORDER),
        batch_size=1,
        expected_symbols=["600000"],
    )
    assert plan_mismatch["accepted_symbols"] == []
    assert "checkpoint_batch_plan_mismatch" in plan_mismatch["reason_codes"]


def test_running_overlay_rejects_checkpoint_target_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "overlay-target-run",
                "pid": 123,
                "status": "running",
                "target_date": "2026-07-16",
                "acquired_at": "2026-07-16T18:02:18",
            }
        ),
        encoding="utf-8",
    )
    checkpoint = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / "overlay-target-run"
        / "checkpoint.json"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "phase": "analysis_intraday_batches",
                "contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "run_id": "overlay-target-run",
                "target_date": "2026-07-15",
                "progress_percent": 99.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "_lock_is_held", lambda path: True)

    payload = chain.latest_daily_data_chain(tmp_path)

    assert payload["status"] == "running"
    assert payload["authoritative"] is False
    assert payload["published_latest"] is False
    assert "checkpoint" not in payload
    assert "progress_percent" not in payload


def test_run_does_not_recover_run_already_present_as_terminal_latest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    symbols = ["600000"]
    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": "already-terminal-run",
                "pid": 999,
                "status": "running",
                "target_date": "2026-07-16",
            }
        ),
        encoding="utf-8",
    )
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "run_id": "already-terminal-run",
                "status": "completed",
                "target_date": "2026-07-16",
                "daily_data_chain_contract_version": chain.DAILY_DATA_CHAIN_CONTRACT_VERSION,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": "600000"}],
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "passed",
            "ready": True,
            "active_runtime_ready": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": []},
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **kwargs: {
            "status": "dry_run",
            "total_symbols": 1,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "failed_symbol_list": [],
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["600000"],
        },
    )
    monkeypatch.setattr(
        chain,
        "build_session_expectation",
        lambda requested, target_date, **kwargs: {
            "legitimate_no_session_symbols": [],
            "unknown_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda requested, target_date, **kwargs: {
            "status": "dry_run",
            "updated_symbols": len(requested),
            "unchanged_symbols": 0,
            "failed_symbols": 0,
            "updated_symbol_list": list(requested),
            "unchanged_symbol_list": [],
            "failed_symbol_list": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda requested, target_date, **kwargs: {
            "summary": {
                "complete_session": len(requested),
                "formal_execution_eligible": len(requested),
                "partial_session": 0,
                "invalid_session": 0,
                "missing": 0,
            },
            "items": [
                {
                    "symbol": symbol,
                    "complete_session": True,
                    "formal_execution_eligible": True,
                    "price_basis": "raw",
                    "session_reason_codes": [],
                }
                for symbol in requested
            ],
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-16",
        network=False,
        dry_run=True,
        run_id="new-terminal-probe",
        symbols=symbols,
        rebuild_features=False,
        retry_from_latest=True,
    )

    assert payload["recovery_source_run_id"] == ""
    assert payload["components"]["analysis_intraday_recovery"]["source_run_id"] == ""


def _write_sealed_sector_recovery_fixture(
    root: Path,
    *,
    source_run_id: str,
    target_date: str,
) -> tuple[Path, dict[str, object]]:
    snapshot_path = root / "data" / "sector_history" / target_date / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot: dict[str, object] = {
        "task_type": "forward_pit_sector_snapshot",
        "target_date": target_date,
        "market_as_of": target_date,
        "usable_from": target_date,
        "observation_mode": "same_day_current_cross_section_capture",
        "historical_backfill": False,
        "append_only": True,
        "canonical_write": False,
        "sector_membership": [{"symbol": "600000", "sector": "Energy"}],
        "sector_rankings": [{"sector": "Energy", "current_rank": 1}],
        "membership_count": 1,
        "membership_input_count": 1,
        "membership_unknown_count": 0,
        "membership_missing_observation_timestamp_count": 0,
        "membership_non_target_observation_timestamp_count": 0,
        "aligned_membership_count": 1,
        "membership_board_alignment_ratio": 1.0,
        "sector_count": 1,
        "reason_codes": [],
        "degraded_reasons": [],
        "ok": True,
        "status": "passed",
        "evidence_eligible": True,
        "pit_eligible": True,
        "snapshot_path": str(snapshot_path.resolve()),
        "content_identity": "sha256:" + "b" * 64,
    }
    canonical = (
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    snapshot["snapshot_hash"] = "sha256:" + __import__("hashlib").sha256(
        canonical
    ).hexdigest()
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    attempt = dict(snapshot)
    attempt["written"] = True
    attempt_path = (
        root
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json"
    )
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        json.dumps(attempt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot_path, snapshot


def test_abandoned_same_target_reuses_verified_sector_snapshot_without_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_data_chain as chain

    source_run_id = "abandoned-sector-run"
    target_date = "2026-07-30"
    snapshot_path, snapshot = _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=source_run_id,
        target_date=target_date,
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "daily_data_chain.lock").write_bytes(b"0")
    (state / "daily_data_chain.lock.json").write_text(
        json.dumps(
            {
                "run_id": source_run_id,
                "pid": 999,
                "status": "running",
                "target_date": target_date,
                "acquired_at": "2026-07-30T15:45:00",
            }
        ),
        encoding="utf-8",
    )

    refresh_calls: list[bool] = []
    archive_calls: list[bool] = []
    monkeypatch.setattr(chain, "build_default_fetchers", lambda: [])
    monkeypatch.setattr(
        chain,
        "refresh_universe_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "completed",
            "formal_eligible": True,
            "quote_date": target_date,
            "provider": "fixture",
        },
    )
    monkeypatch.setattr(
        chain,
        "_refresh_sector_snapshot",
        lambda *args, **kwargs: refresh_calls.append(True)
        or {"ok": True, "status": "completed", "network": True},
    )
    monkeypatch.setattr(
        chain,
        "_archive_forward_sector_snapshot",
        lambda *args, **kwargs: archive_calls.append(True)
        or {
            "ok": True,
            "status": "degraded",
            "evidence_eligible": False,
            "pit_eligible": False,
            "written": False,
            "canonical_write": False,
            "reason_codes": ["unexpected_archive_attempt"],
        },
    )
    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **kwargs: [{"symbol": "600000"}],
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **kwargs: {"items": [], "source_manifest": {}},
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *args, **kwargs: {
            "status": "blocked",
            "ready": False,
            "active_runtime_ready": False,
            "reason_codes": ["fixture_stop_after_archive"],
        },
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date=target_date,
        network=True,
        dry_run=False,
        run_id="sector-recovery-run",
        rebuild_features=False,
        retry_from_latest=True,
    )

    sector = payload["components"]["sector_history_archive"]
    assert refresh_calls == []
    assert archive_calls == []
    assert sector["status"] == "passed"
    assert sector["evidence_eligible"] is True
    assert sector["pit_eligible"] is True
    assert sector["written"] is False
    assert sector["canonical_write"] is False
    assert sector["recovery_reuse"] is True
    assert sector["recovery_source_run_id"] == source_run_id
    assert sector["original_snapshot_hash"] == snapshot["snapshot_hash"]
    assert sector["snapshot_path"] == str(snapshot_path.resolve())
    attempt = json.loads(
        (
            tmp_path
            / "results"
            / "data_chain"
            / "runs"
            / "sector-recovery-run"
            / "sector_history_archive_attempt.json"
        ).read_text(encoding="utf-8")
    )
    assert attempt == sector


def test_abandoned_sector_reuse_rejects_revoked_snapshot(tmp_path: Path) -> None:
    from quantdesk import daily_data_chain as chain

    source_run_id = "abandoned-sector-run"
    target_date = "2026-07-30"
    snapshot_path, _ = _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=source_run_id,
        target_date=target_date,
    )
    (snapshot_path.parent / "revocation.json").write_text(
        json.dumps({"reason_codes": ["fixture_revoked"]}),
        encoding="utf-8",
    )

    payload, diagnostics = chain._reuse_verified_sector_snapshot(
        tmp_path,
        target_date=target_date,
        source_run_id=source_run_id,
    )

    assert payload is None
    assert diagnostics["status"] == "rejected"
    assert diagnostics["reason_codes"] == ["sector_snapshot_revoked"]


def test_abandoned_sector_reuse_rejects_tampered_snapshot(tmp_path: Path) -> None:
    from quantdesk import daily_data_chain as chain

    source_run_id = "abandoned-sector-run"
    target_date = "2026-07-30"
    snapshot_path, snapshot = _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=source_run_id,
        target_date=target_date,
    )
    snapshot["sector_count"] = 999
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    payload, diagnostics = chain._reuse_verified_sector_snapshot(
        tmp_path,
        target_date=target_date,
        source_run_id=source_run_id,
    )

    assert payload is None
    assert "sector_snapshot_self_hash_mismatch" in diagnostics["reason_codes"]


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("written", False, "abandoned_sector_attempt_not_written"),
        ("status", "degraded", "abandoned_sector_attempt_not_evidence_eligible"),
        ("evidence_eligible", False, "abandoned_sector_attempt_not_evidence_eligible"),
        ("pit_eligible", False, "abandoned_sector_attempt_not_evidence_eligible"),
    ],
)
def test_abandoned_sector_reuse_requires_written_eligible_source_attempt(
    tmp_path: Path,
    field: str,
    value: object,
    expected_reason: str,
) -> None:
    from quantdesk import daily_data_chain as chain

    source_run_id = "abandoned-sector-run"
    target_date = "2026-07-30"
    _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=source_run_id,
        target_date=target_date,
    )
    attempt_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json"
    )
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt[field] = value
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")

    payload, diagnostics = chain._reuse_verified_sector_snapshot(
        tmp_path,
        target_date=target_date,
        source_run_id=source_run_id,
    )

    assert payload is None
    assert expected_reason in diagnostics["reason_codes"]


def test_abandoned_sector_reuse_rejects_source_attempt_content_mismatch(
    tmp_path: Path,
) -> None:
    from quantdesk import daily_data_chain as chain

    source_run_id = "abandoned-sector-run"
    target_date = "2026-07-30"
    _write_sealed_sector_recovery_fixture(
        tmp_path,
        source_run_id=source_run_id,
        target_date=target_date,
    )
    attempt_path = (
        tmp_path
        / "results"
        / "data_chain"
        / "runs"
        / source_run_id
        / "sector_history_archive_attempt.json"
    )
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["sector_count"] = 999
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")

    payload, diagnostics = chain._reuse_verified_sector_snapshot(
        tmp_path,
        target_date=target_date,
        source_run_id=source_run_id,
    )

    assert payload is None
    assert "abandoned_sector_attempt_content_mismatch" in diagnostics["reason_codes"]


@pytest.mark.parametrize(
    ("source_run_id", "expected"),
    [
        ("abandoned-sector-run", True),
        ("", False),
        (".", False),
        ("..", False),
        ("parent/child", False),
        (r"parent\child", False),
    ],
)
def test_recovery_source_run_id_validation_is_platform_independent(
    source_run_id: str,
    expected: bool,
) -> None:
    from quantdesk import daily_data_chain as chain

    assert chain._is_valid_recovery_source_run_id(source_run_id) is expected


@pytest.mark.parametrize("source_run_id", ["..\\outside", "../outside", ".", ".."])
def test_abandoned_sector_reuse_rejects_source_run_path_escape(
    tmp_path: Path,
    source_run_id: str,
) -> None:
    from quantdesk import daily_data_chain as chain

    payload, diagnostics = chain._reuse_verified_sector_snapshot(
        tmp_path,
        target_date="2026-07-30",
        source_run_id=source_run_id,
    )

    assert payload is None
    assert diagnostics["reason_codes"] == ["recovery_source_run_id_invalid"]
