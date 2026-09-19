from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest


def test_default_close_never_enters_minute_collection_or_processing(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain
    from tests.test_research_pool_separation import _install_chain_fakes

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n600001\n", encoding="utf-8")

    def minute_call(*args, **kwargs):
        pytest.fail("daily close entered the on-demand minute subsystem")

    for name in ("collect_intraday_required_symbols", "update_intraday_1m_for_symbols", "audit_intraday_batch"):
        monkeypatch.setattr(chain, name, minute_call)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date="2026-07-17", network=False,
        refresh_universe=False, rebuild_features=False, run_id="daily-only-close",
    )
    assert result["status"] == "completed"
    assert result["coverage"]["critical"]["status"] == "not_requested"
    assert result["coverage"]["current_analysis_pool"]["status"] == "not_requested"
    assert not list((tmp_path / "data/intraday").glob("**/*.csv"))


@pytest.mark.parametrize("lock_status", ["abandoned", "failed", "interrupted"])
def test_morning_reads_terminal_archive_after_abnormal_producer_exit(tmp_path: Path, lock_status: str):
    from quantdesk.morning_brief import load_morning_brief_snapshot
    from tests.test_morning_brief import _seed_root

    _seed_root(tmp_path)
    lock = tmp_path / "state/daily_data_chain.lock.json"
    metadata = json.loads(lock.read_text(encoding="utf-8"))
    metadata["status"] = lock_status
    lock.write_text(json.dumps(metadata), encoding="utf-8")
    result = load_morning_brief_snapshot(tmp_path)
    assert result["status"] == "degraded"
    assert result["source_mode"] == "archived_terminal_run"
    assert result["chain"]["run_id"]
    assert result["workflow"] == {}
    assert result["lock_snapshot"]["status"] == lock_status
    assert f"chain_lock_{lock_status}" in result["reason_codes"]


def test_close_reconciliation_reports_gaps_without_rewriting_prices(tmp_path, monkeypatch):
    from quantdesk import full_a_daily_reconciliation as reconciliation

    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n600001\n", encoding="utf-8")
    daily = tmp_path / "data/daily/600000.csv"
    daily.parent.mkdir(parents=True)
    daily.write_text("date,open,high,low,close,volume,price_basis\n2026-09-08,10,11,9,10,1000,raw\n", encoding="utf-8")
    before = daily.read_bytes()

    def unexpected_update(**kwargs):
        pytest.fail("reconciliation must not start another daily writer")

    result = reconciliation.run_full_a_daily_reconciliation(
        project_root=tmp_path, target_date="2026-09-08", audit_only=True,
        update_runner=unexpected_update,
    )
    assert result["status"] == "completed_with_warnings"
    assert result["retry_symbols"] == ["600001"]
    assert result["canonical_write"] is False
    assert result["network"] is False
    assert daily.read_bytes() == before
    assert not (tmp_path / "data/daily/600001.csv").exists()


def test_real_features_quarantine_corrupt_input_without_blocking_valid_symbols(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain
    from quantdesk.data_chain_consumer_gate import evaluate_data_chain_consumer_gate
    from tests.test_research_pool_separation import _install_chain_fakes

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol,name\n600000,Valid\n600001,Corrupt\n", encoding="utf-8")
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    content = "date,symbol,open,high,low,close,volume,amount,adjust\n2026-07-17,{symbol},10,11,9,10,1000,10000,raw\n"
    (daily / "600000.csv").write_text(content.format(symbol="600000"), encoding="utf-8")
    damaged = content.format(symbol="600001").encode() + b"\0" * 100 + b"\n"
    (daily / "600001.csv").write_bytes(damaged)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date="2026-07-17", network=True,
        refresh_universe=False, run_id="real-quarantined-features",
    )
    assert result["status"] == "completed", result.get("errors")
    assert result["coverage"]["features"]["excluded_symbols"] == ["600001"]
    assert result["components"]["features"]["feature_count"] == 1
    assert (daily / "600001.csv").read_bytes() == damaged
    consumer = evaluate_data_chain_consumer_gate(result)
    assert consumer["allowed"] is True, consumer
    assert consumer["research_eligibility"]["eligible_symbols"] == ["600000"]
    assert consumer["execution_authorized"] is False
    from quantdesk.daily_candidate_refresh import run_formal_candidate_refresh, resolve_chain_bound_formal_candidate_batch
    from tests.test_daily_candidate_refresh import _strategy_payload

    published = run_formal_candidate_refresh(
        project_root=tmp_path, data_chain_manifest=result,
        strategy_payload=_strategy_payload("2026-07-17"), captured_at="2026-07-17T18:00:00+08:00", apply=True,
    )
    assert published["applied"] is True
    resolved = resolve_chain_bound_formal_candidate_batch(tmp_path, data_chain_manifest=result)
    assert resolved["status"] == "passed", resolved["reason_codes"]
    assert resolved["batch"]["provenance"]["source_run_id"] == result["run_id"]
    from datetime import datetime
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan
    from quantdesk.trading_calendar import clear_calendar_cache

    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n2026-07-17,true,test\n2026-07-20,true,test\n", encoding="utf-8")
    clear_calendar_cache()
    legacy_universe = tmp_path / "data/universe/low_price_learning_symbols.csv"
    legacy_universe.write_text("symbol,included\n600002,true\n", encoding="utf-8")
    prepared = freeze_t_plus_one_plan(
        project_root=tmp_path, data_chain_manifest=result,
        now=datetime.fromisoformat("2026-07-17T18:00:00+08:00"), write=False,
    )
    assert prepared["status"] == "prepared", (prepared.get("reason_codes"), prepared.get("error"))
    assert prepared["data_chain_run_id"] == result["run_id"]
    assert {row["symbol"] for row in prepared["prepared_plan"]["symbols"]} == {"600000"}
    assert prepared["prepared_plan"]["scope"] == "formal_candidates_and_positions"
    from quantdesk.paper.conditional_trade_plan import build_conditional_trade_plan

    legacy = build_conditional_trade_plan(
        auto_strategy_payload=_strategy_payload("2026-07-17"), state={},
        candidate_mode="legacy_diagnostic",
        plan_date="2026-07-17", trade_date="2026-07-20", source_data_cutoff="2026-07-17",
        daily_dir=daily, daily_feature_path=tmp_path / "data/features/daily_features_latest.csv",
        low_price_universe_path=legacy_universe, write=False,
    )
    assert legacy["symbols"] == []
    assert legacy["scope"] == "legacy_diagnostic"
    result["coverage"]["features"]["exclusion_output_sha256"] = "0" * 64
    assert resolve_chain_bound_formal_candidate_batch(tmp_path, data_chain_manifest=result)["status"] == "blocked"
    tampered = freeze_t_plus_one_plan(
        project_root=tmp_path, data_chain_manifest=result,
        now=datetime.fromisoformat("2026-07-17T18:00:00+08:00"), write=False,
    )
    assert tampered["status"] == "blocked"
    assert "upstream_feature_per_symbol_price_basis_not_raw" in tampered["reason_codes"]


def test_missing_candidates_do_not_fail_the_completed_review(tmp_path):
    from quantdesk.daily_review_orchestrator import run_evening_review
    from scripts.run_daily_review_orchestrator import run_candidate_tracking_and_validate

    calls = []

    def daily():
        calls.append("daily")
        return 0

    def tracking():
        return run_candidate_tracking_and_validate(tmp_path, "2026-09-08", "missing-candidates", detailed=True)

    result = run_evening_review(
        project_root=tmp_path, target_date="2026-09-08", authoritative_chain_run_id="missing-candidates",
        daily_runner=daily, candidate_tracking_runner=tracking,
    )
    assert result["status"] == "completed_with_skips"
    assert result["returncode"] == 0
    assert result["candidate_tracking_completed_at"] is None
    assert result["candidate_tracking_returncode"] is None
    assert result["candidate_tracking_result"]["reason_codes"]
    again = run_evening_review(
        project_root=tmp_path, target_date="2026-09-08", authoritative_chain_run_id="missing-candidates",
        daily_runner=daily, candidate_tracking_runner=tracking,
    )
    assert calls == ["daily"]
    assert again["returncode"] == 0


def test_morning_consumes_formal_candidates_when_legacy_strategy_is_disabled():
    from quantdesk.morning_brief import _candidate_rows

    workflow = {
        "auto_strategy": {"status": "skipped"},
        "formal_candidate_intake": {"status": "passed", "candidate_count": 1,
                                    "candidates": [{"symbol": "600000", "name": "Valid"}]},
    }
    rows, count = _candidate_rows(workflow)
    assert count == 1
    assert rows[0]["symbol"] == "600000"
    workflow["formal_candidate_intake"] = {"status": "blocked", "candidate_count": None, "candidates": []}
    assert _candidate_rows(workflow) == ([], None)


@pytest.mark.parametrize("exclusion_reason", ["missing_or_invalid_daily", "unknown_price_basis"])
@pytest.mark.parametrize("corruption", ["hash", "duplicate", "excluded_row"])
def test_feature_exclusion_never_hides_invalid_published_output(tmp_path, exclusion_reason, corruption):
    from quantdesk.daily_data_chain import _feature_coverage

    content = "symbol\n600000\n"
    if corruption == "duplicate":
        content += "600000\n"
    elif corruption == "excluded_row":
        content += "600001\n"
    output = tmp_path / "features.csv"
    output.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    payload = {
        "ok": True, "status": "partial", "requested_count": 2, "feature_count": 1,
        "skipped_count": 1, "output_row_count": 1,
        "output_sha256": "0" * 64 if corruption == "hash" else digest,
        "accepted_price_basis": "raw", "feature_input_snapshot_stable": True,
        "price_policy_validation": {"status": "passed", "run_status": "partial", "eligible": True,
                                    "accepted_price_basis": "raw", "blocked_symbols": []},
        "per_symbol": {
            "600000": {"eligible": True, "price_basis": "raw", "latest_date": "2026-09-08"},
            "600001": {"eligible": False, "reason_code": exclusion_reason},
        },
    }
    result = _feature_coverage(payload, ["600000", "600001"], "2026-09-08", output_path=output)
    assert "feature_output_integrity_invalid" in result["reason_codes"]
    assert result["excluded_symbols"] == []


def test_partial_daily_update_retains_symbol_gaps_without_component_failure():
    from quantdesk.daily_data_chain import _daily_coverage

    result = _daily_coverage({
        "status": "partial", "total_symbols": 2, "stale_count_after": 1, "missing_count_after": 1,
        "stale_symbol_list_after": ["600001"], "missing_symbol_list_after": ["600001"],
        "failed_symbols": 1, "failed_symbol_list": ["600001"],
    }, 2)
    assert "daily_symbols_missing" in result["reason_codes"]
    assert "daily_component_not_completed" not in result["reason_codes"]


def test_busy_cli_cannot_relabel_a_previous_pipeline_artifact(tmp_path, monkeypatch, capsys):
    from scripts import run_daily_data_chain as cli

    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"run_id": "previous", "pipeline_run_id": "previous-pipeline"}), encoding="utf-8")
    before = latest.read_bytes()
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "run_daily_data_chain", lambda **kw: {
        "status": "busy", "run_id": "current", "published_latest": False,
    })
    assert cli.main(["--pipeline-run-id", "current-pipeline"]) == 3
    assert latest.read_bytes() == before
    assert "artifact_path" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("failure", ["prepare_exception", "prepared_empty", "publish_failure", "status_summary_failure"])
def test_plan_and_status_failures_preserve_completed_data_and_previous_plan(tmp_path, monkeypatch, failure):
    from quantdesk import daily_data_chain as chain
    from tests.test_research_pool_separation import _install_chain_fakes

    _install_chain_fakes(monkeypatch)
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n", encoding="utf-8")
    daily = tmp_path / "data/daily/600000.csv"
    daily.parent.mkdir(parents=True)
    daily.write_text("date,symbol,open,high,low,close,volume,amount,adjust\n2026-07-17,600000,10,11,9,10,1000,10000,raw\n", encoding="utf-8")
    previous = tmp_path / "results/conditional_trade_plans_latest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"plan_id":"prior"}', encoding="utf-8")
    prior_bytes = previous.read_bytes()
    if failure == "prepare_exception":
        def prepare(**kwargs):
            raise ValueError("fixture plan failure")
        monkeypatch.setattr(chain, "freeze_t_plus_one_plan", prepare)
    elif failure == "prepared_empty":
        monkeypatch.setattr(chain, "freeze_t_plus_one_plan", lambda **kw: {"status": "prepared"})
    elif failure == "publish_failure":
        monkeypatch.setattr(chain, "freeze_t_plus_one_plan", lambda **kw: {
            "status": "prepared", "prepared_plan": {"plan_id": "new"},
        })
        monkeypatch.setattr(chain, "publish_archived_conditional_plan", lambda *a, **kw: {
            "status": "blocked", "reason_codes": ["immutable_archive_conflict"],
        })
    else:
        monkeypatch.setattr(chain, "_update_current_state", lambda *a: {"ok": False, "status": "failed"})
    result = chain.run_daily_data_chain(project_root=tmp_path, target_date="2026-07-17", network=True,
                                       refresh_universe=False, run_id=failure, pipeline_run_id="fixture-pipeline")
    assert result["status"] == "completed", result.get("errors")
    assert result["components"]["features"]["feature_count"] == 1
    assert previous.read_bytes() == prior_bytes
    assert result["warnings"]
    if failure != "status_summary_failure":
        assert result["components"]["conditional_plan_freeze"]["status"] == "blocked"
        assert result["components"]["conditional_plan_freeze"].get("written") is not True
    latest = tmp_path / "results/data_chain/daily_data_chain_latest.json"
    archive = tmp_path / "results/data_chain/runs" / failure / "manifest.json"
    assert latest.read_bytes() == archive.read_bytes()
    assert json.loads(latest.read_text(encoding="utf-8"))["pipeline_run_id"] == "fixture-pipeline"


def test_status_summary_write_exception_is_visible_and_contained(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain

    def denied(*args, **kwargs):
        raise PermissionError("fixture derived status is read-only")

    monkeypatch.setattr(chain, "atomic_write_json", denied)
    result = chain._update_current_state(tmp_path, {"run_id": "finished", "status": "completed"})
    assert result["ok"] is False and result["status"] == "failed"
    assert "PermissionError" in result["error"]


def test_scheduled_policy_upgrade_starts_a_new_run_and_preserves_old_checkpoint(tmp_path):
    from scripts import run_data_close_pipeline as pipeline
    from tests.test_pipeline_terminal_reentry import _runner_set, TARGET_DATE

    runners, calls = _runner_set()
    first = pipeline.run_pipeline(tmp_path, network=True, run_id="previous-policy", target_date=TARGET_DATE, stage_runner=runners)
    assert first["status"] == "completed_all_executed"
    checkpoint_path = pipeline.checkpoint_path(tmp_path, "previous-policy")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    for stage in ("universe", "research_chain", "reconcile"):
        checkpoint["stages"][stage]["stage_version"] = f"{pipeline.SCHEMA_VERSION}:{stage}:2"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    before = checkpoint_path.read_bytes()
    calls.clear()
    upgraded = pipeline.run_pipeline(tmp_path, network=True, run_id="upgraded-policy", target_date=TARGET_DATE,
                                     trigger="scheduled", stage_runner=runners)
    # A scheduled policy upgrade is evidence to reject or explicitly recover,
    # never permission to silently start every stage again. The failed previous
    # checkpoint is preserved and nothing is re-run.
    assert upgraded["run_id"] == "previous-policy"
    assert upgraded["invocation"]["requested_run_id"] == "upgraded-policy"
    assert upgraded["status"] == "failed"
    assert "scheduled_checkpoint_lacks_execution_proof" in upgraded["reason_codes"]
    assert calls == []
    assert checkpoint_path.read_bytes() == before


def test_upstream_reuse_rechecks_only_unresolved_session_evidence(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain
    from quantdesk import session_expectation as sessions

    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    header = "date,symbol,close,volume,adjust\n"
    rows = {
        "600000": "2026-09-08,600000,10,100,raw\n",
        "600001": "invalid,600001,10,100,qfq\n2026-09-08,600001,10,100,raw\n",
        "600002": "2026-09-08,600002,10,0,raw\n",
        "600003": "2026-09-07,600003,10,100,raw\n",
        "600004": "2026-09-08,600004,10,100,raw\n" + "\0" * 40 + "\n",
    }
    for symbol, content in rows.items():
        (daily / f"{symbol}.csv").write_text(header + content, encoding="utf-8")
    result = chain._reused_daily_payload(tmp_path, target_date="2026-09-08", symbols=list(rows), source_run_id="upstream")
    assert result["status"] == "partial"
    assert result["source_status"] == "reused_verified_upstream"
    assert result["provider_calls"] == 0 and result["require_raw_price_basis"] is True
    assert result["raw_positive_volume_target_symbol_list"] == ["600000"]
    assert result["raw_zero_volume_target_symbol_list"] == ["600002"]
    assert result["invalid_price_basis_symbols"] == ["600001", "600004"]
    assert "600003" in result["stale_symbol_list_after"]
    rechecked = []
    original_daily_info = sessions._daily_info

    def recheck(symbol, *args, **kwargs):
        assert symbol not in {"600000", "600002"}, "reparsed conclusive target-volume evidence"
        rechecked.append(symbol)
        return original_daily_info(symbol, *args, **kwargs)

    monkeypatch.setattr(sessions, "_daily_info", recheck)
    expectation = sessions.build_session_expectation(list(rows), "2026-09-08", project_root=tmp_path,
                                                     daily_status=result, fast_daily_status=True, network=False)
    assert expectation["per_symbol"]["600000"]["classification"] == "expected_trade"
    assert "600001" in expectation["unknown_symbols"]
    assert rechecked == ["600001", "600003", "600004"]
    for symbol in rechecked:
        assert expectation["per_symbol"][symbol]["evidence"][0]["exists"] is True
    assert expectation["per_symbol"]["600003"]["evidence"][0]["target_count"] == 0


def test_session_fast_path_distinguishes_history_coverage_from_target_bars(tmp_path):
    from quantdesk.session_expectation import build_session_expectation

    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    header = "date,symbol,close,volume,adjust\n"
    (daily / "600000.csv").write_text(header + "2026-09-08,600000,10,100,raw\n", encoding="utf-8")
    (daily / "600001.csv").write_text(header + "2026-09-07,600001,10,100,raw\n", encoding="utf-8")
    result = build_session_expectation(
        ["600000", "600001"], "2026-09-08", project_root=tmp_path, network=False,
        fast_daily_status=True, daily_status={
            "status": "partial", "target_date": "2026-09-08", "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["600001"], "stale_symbol_list_after": ["600000"],
            "raw_positive_volume_target_symbol_list": ["600000"],
        },
    )
    current = result["per_symbol"]["600000"]
    absent = result["per_symbol"]["600001"]
    assert current["classification"] == "expected_trade"
    assert current["evidence"][0]["exists"] is True
    assert current["evidence"][0]["target_count"] == 1
    assert absent["classification"] == "unknown"
    assert absent["evidence"][0]["exists"] is True
    assert absent["evidence"][0]["target_count"] == 0


@pytest.mark.parametrize("historical_adjust", ["qfq", "unknown"])
def test_session_reread_cannot_override_invalid_original_basis(tmp_path, historical_adjust):
    from quantdesk.session_expectation import build_session_expectation

    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    (daily / "600000.csv").write_text(
        "date,symbol,close,volume,adjust,price_basis\n"
        f"invalid,600000,10,100,{historical_adjust},raw\n"
        "2026-09-08,600000,10,100,raw,raw\n", encoding="utf-8",
    )
    result = build_session_expectation(
        ["600000"], "2026-09-08", project_root=tmp_path, network=False,
        fast_daily_status=True, daily_status={
            "status": "partial", "target_date": "2026-09-08", "require_raw_price_basis": True,
            "fresh_symbol_list_after": [], "stale_symbol_list_after": ["600000"],
        },
    )
    item = result["per_symbol"]["600000"]
    assert item["classification"] == "unknown"
    assert item["reason_code"] == "target_bar_price_basis_not_raw"
    assert item["evidence"][0]["price_basis"] == "mixed_or_unknown"
