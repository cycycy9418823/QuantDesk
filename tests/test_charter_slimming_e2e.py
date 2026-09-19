from __future__ import annotations

import json
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests._isolated_project import IsolatedProject


def seed_automation_fixture(root: Path, *, target: str) -> None:
    previous = (datetime.fromisoformat(target) - timedelta(days=1)).date().isoformat()
    fixture = {
        "target_date": target,
        "universe": [{"code": symbol, "name": name} for symbol, name in
                     [("600000", "Valid"), ("600001", "Corrupt"), ("600002", "Missing")]],
        "quotes": [{"code": symbol, "name": name, "open": 10, "high": 11, "low": 9,
                    "trade": 10, "volume": 1000, "amount": 10000, "pricechange": 0, "changepercent": 0}
                   for symbol, name in [("600000", "Valid"), ("600001", "Corrupt")]],
    }
    (root / "provider_fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    (root / "data/universe/a_share_symbols.csv").write_text(
        "symbol,name,source,updated_at\n" + "".join(
            f"{row['code']},{row['name']},stock_info_a_code_name,{previous}\n" for row in fixture["universe"]
        ), encoding="utf-8",
    )
    header = "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at\n"
    for symbol in ("600000", "600001"):
        content = header + f"{previous},{symbol},10,11,9,10,1000,10000,0,0,raw,fixture,{previous}\n"
        (root / f"data/daily/{symbol}.csv").write_bytes(content.encode() + (b"\0" * 100 + b"\n" if symbol == "600001" else b""))
    (root / "data/calendar/a_share_trading_calendar.csv").write_text(
        f"date,is_open\n{previous},1\n{target},1\n", encoding="utf-8",
    )
    (root / "config/data_refresh_news_pool.txt").write_text("600000\n", encoding="utf-8")
    # Benchmark updates append to an existing local file; a missing file is
    # reported as local_file_missing, so seed a prior session for each index.
    benchmark_dir = root / "data/benchmark"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        (benchmark_dir / filename).write_text(
            "date,open,high,low,close,volume\n"
            f"{previous},10,11,9,10,1000\n",
            encoding="utf-8",
        )


def test_real_cli_pipeline_review_and_morning_share_the_same_artifacts(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with IsolatedProject(source, temp_parent=tmp_path.parent) as project:
        root = project.root
        assert root is not None
        project.install_startup_fixture(source / "tests/fixtures/charter_automation_provider.py")
        seed_automation_fixture(root, target=target)
        damaged = (root / "data/daily/600001.csv").read_bytes()
        env = project.subprocess_env()
        env["QUANTDESK_AUTOMATION_FIXTURE_ROOT"] = str(root)
        env["TEMP"] = env["TMP"] = str(root / "test_runtime")
        env["QUANTDESK_OPERATION_LOG_DIR"] = str(root / "results/operation_log")

        def run(script: str, *args: str) -> subprocess.CompletedProcess:
            completed = subprocess.run(
                [sys.executable, "-B", str(root / "scripts" / script), *args],
                cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            )
            (root / "results" / (script + ".stdout.txt")).write_text(completed.stdout, encoding="utf-8")
            (root / "results" / (script + ".stderr.txt")).write_text(completed.stderr, encoding="utf-8")
            return completed

        pipeline = run("run_data_close_pipeline.py", "--enable-network", "--trigger", "scheduled", "--target-date", target)
        pipeline_path = root / "results/data_close_pipeline/data_close_pipeline_latest.json"
        report = json.loads(pipeline_path.read_text(encoding="utf-8")) if pipeline_path.exists() else {}
        evidence = os.environ.get("QUANTDESK_AUTOMATION_EVIDENCE_DIR", "")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "fixture_pipeline_attempt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            for relative in ("results/data_chain/daily_data_chain_latest.json", "provider_calls.jsonl"):
                if (root / relative).exists():
                    shutil.copyfile(root / relative, destination / Path(relative).name)
        stage_failures = {
            name: {"status": value.get("status"), "reasons": value.get("reason_codes"),
                   "stderr": value.get("payload", {}).get("stderr_tail"),
                   "errors": value.get("payload", {}).get("stdout_json", {}).get("errors"),
                   "error": value.get("payload", {}).get("stdout_json", {}).get("error"),
                   "child_reason": value.get("payload", {}).get("stdout_json", {}).get("reason_code"),
                   "child_reasons": value.get("payload", {}).get("stdout_json", {}).get("reason_codes")}
            for name, value in report.get("stages", {}).items()
        }
        assert pipeline.returncode == 0, json.dumps({"stages": stage_failures, "stderr": pipeline.stderr[-4000:]}, ensure_ascii=False, indent=2)
        assert report["status"] in {"completed_all_executed", "completed_with_skips"}
        assert report["stages"]["reconcile"]["status"] == "completed_with_warnings"
        chain = json.loads((root / "results/data_chain/daily_data_chain_latest.json").read_text(encoding="utf-8"))
        assert chain["status"] == "completed", chain.get("errors")
        assert chain["components"]["features"]["feature_count"] == 1
        assert chain["coverage"]["critical"]["status"] == "not_requested"
        assert chain["coverage"]["current_analysis_pool"]["status"] == "not_requested"
        assert (root / "data/daily/600001.csv").read_bytes() == damaged
        assert not list((root / "data/intraday").glob("**/*.csv"))
        review = run("run_daily_review_orchestrator.py", "--project-root", str(root), "--chain-wait-seconds", "0", "--json", "true")
        assert review.returncode == 0, review.stdout[-5000:] + review.stderr[-4000:]
        morning = run("run_morning_brief.py", "--dry-run", "true", "--json", "true")
        assert morning.returncode == 0, morning.stdout[-4000:] + morning.stderr[-2000:]
        brief = json.loads((root / "results/morning_brief/morning_brief_latest.json").read_text(encoding="utf-8"))
        workflow = json.loads((root / "results/daily_workflow_latest.json").read_text(encoding="utf-8"))
        assert brief["run_id"] == chain["run_id"]
        assert brief["as_of"] == target
        assert brief["status"] in {"passed", "warning"}
        assert "chain_lock_not_released" not in brief["reason_codes"]
        assert not list((root / "results/notifications").glob("serverchan_morning_brief_*.json"))
        evidence = os.environ.get("QUANTDESK_AUTOMATION_EVIDENCE_DIR", "")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            for relative in ("results/data_close_pipeline/data_close_pipeline_latest.json", "results/data_chain/daily_data_chain_latest.json", "results/daily_workflow_latest.json", "results/morning_brief/morning_brief_latest.json", "reports/morning_brief/morning_brief_latest.md", "provider_calls.jsonl"):
                shutil.copyfile(root / relative, destination / Path(relative).name)
        assert chain["components"]["candidate_refresh"]["applied"] is True
        assert workflow["formal_candidate_intake"]["source_run_id"] == chain["run_id"]
        assert brief["candidate_count"] == workflow["formal_candidate_intake"]["candidate_count"] == 0


@pytest.mark.skipif(not os.environ.get("QUANTDESK_RECORDED_REPLAY_SOURCE"), reason="explicit local evidence replay only")
def test_recorded_full_a_evidence_runs_through_review_and_morning(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    canonical = Path(os.environ["QUANTDESK_RECORDED_REPLAY_SOURCE"]).resolve()
    reuse_relative = Path(os.environ["QUANTDESK_RECORDED_UPSTREAM_MANIFEST"])
    assert not reuse_relative.is_absolute() and ".." not in reuse_relative.parts
    upstream = json.loads((canonical / reuse_relative).read_text(encoding="utf-8"))
    target = upstream["target_date"]
    compact_date = target.replace("-", "")
    destination = Path(os.environ["QUANTDESK_AUTOMATION_EVIDENCE_DIR"]) / "recorded_replay"
    destination.mkdir(parents=True, exist_ok=True)
    with (canonical / "data/universe/a_share_symbols.csv").open(encoding="utf-8-sig", newline="") as handle:
        symbols = sorted({row["symbol"].zfill(6) for row in csv.DictReader(handle)})

    def digest(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    with IsolatedProject(source, temp_parent=tmp_path.parent) as project:
        root = project.root
        assert root is not None and not canonical.is_relative_to(root)
        project.install_startup_fixture(source / "tests/fixtures/charter_automation_replay_guard.py")
        relative_files = [Path(f"data/daily/{symbol}.csv") for symbol in symbols]
        relative_files += [reuse_relative, Path("data/universe/a_share_symbols.csv"),
                          Path("results/data_refresh/data_refresh_latest.json"),
                          Path("data/calendar/a_share_trading_calendar.csv"),
                          Path("data/cache/spot_snapshot_latest.csv"),
                          Path("state/auto_paper_portfolio.json")]
        relative_files += [Path("data/metadata") / name for name in (
            "security_master.csv", "st_history.csv", "delist_history.csv", "suspension_status.csv",
            "suspension_history.csv", "adjustment_factors.csv",
            f"suspension_evidence/{compact_date}.json", f"valuation_snapshots/{compact_date}.csv",
        )]
        for directory, pattern in (("data/benchmark", "*.csv"), ("data/sector_history", "**/*"),
                                   ("results/conditional_trade_plans_daily", "*.json")):
            relative_files += [path.relative_to(canonical) for path in (canonical / directory).glob(pattern) if path.is_file()]
        before = {}
        nul_symbols = []
        for relative in relative_files:
            original = canonical / relative
            if not original.is_file():
                continue
            before[relative.as_posix()] = digest(original)
            copied = root / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, copied)
            assert digest(copied) == before[relative.as_posix()]
            if relative.parent.as_posix() == "data/daily" and b"\0" in copied.read_bytes():
                nul_symbols.append(relative.stem)
        for stage in upstream["stages"].values():
            assert digest(root / stage["path"]) == stage["sha256"]
        env = project.subprocess_env()
        for key in list(env):
            if any(token in key.upper() for token in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "SENDKEY")):
                env.pop(key)
        env["QUANTDESK_AUTOMATION_FIXTURE_ROOT"] = str(root)
        env["QUANTDESK_OPERATION_LOG_DIR"] = str(root / "results/operation_log")
        env["TEMP"] = env["TMP"] = str(root / "test_runtime")
        command_results = []

        def run(script: str, *args: str):
            started = time.monotonic()
            completed = subprocess.run([sys.executable, "-B", str(root / "scripts" / script), *args],
                                       cwd=root, env=env, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=1800)
            for stream in ("stdout", "stderr"):
                (destination / f"{script}.{stream}.txt").write_text(getattr(completed, stream), encoding="utf-8")
            command_results.append({"script": script, "args": args, "returncode": completed.returncode,
                                    "duration_seconds": round(time.monotonic() - started, 2)})
            for relative in ("results/data_chain/daily_data_chain_latest.json",
                             "data/features/daily_features_latest.csv", "data/features/daily_features_summary.json",
                             "results/data_chain/full_a_daily_reconciliation_latest.json",
                             "results/daily_workflow_latest.json", "results/morning_brief/morning_brief_latest.json",
                             "reports/morning_brief/morning_brief_latest.md"):
                if (root / relative).is_file():
                    shutil.copyfile(root / relative, destination / Path(relative).name)
            chain_path = root / "results/data_chain/daily_data_chain_latest.json"
            if chain_path.is_file():
                current = json.loads(chain_path.read_text(encoding="utf-8"))
                archive = root / "results/data_chain/runs" / current["run_id"] / "manifest.json"
                if archive.is_file():
                    shutil.copyfile(archive, destination / "data_chain_archive.json")
                batch = root / "results/candidate_refresh/daily" / f"{target}.json"
                if batch.is_file():
                    shutil.copyfile(batch, destination / "formal_candidate_batch.json")
            assert completed.returncode == 0, completed.stdout[-3500:] + completed.stderr[-3500:]

        try:
            run("run_daily_data_chain.py", "--target-date", target, "--enable-network",
                "--reuse-upstream-manifest", reuse_relative.as_posix(), "--refresh-universe", "false",
                "--retry-from-latest", "false", "--trigger", "manual", "--json", "true")
            chain = json.loads((root / "results/data_chain/daily_data_chain_latest.json").read_text(encoding="utf-8"))
            assert chain["status"] == "completed", chain.get("errors")
            assert chain["universe"]["current_analysis_pool"]["symbol_count"] == len(symbols)
            assert chain["coverage"]["critical"]["status"] == "not_requested"
            assert chain["coverage"]["current_analysis_pool"]["status"] == "not_requested"
            assert set(chain["coverage"]["features"]["excluded_symbols"]) == set(nul_symbols)
            assert chain["components"]["features"]["feature_count"] == len(symbols) - len(nul_symbols)
            assert chain["components"]["candidate_refresh_gate"]["status"] == "passed", chain["components"]["candidate_refresh_gate"]["reason_codes"]
            assert chain["components"]["candidate_refresh"]["applied"] is True, chain["components"]["candidate_refresh"]
            run("run_full_a_daily_reconciliation.py", "--target-date", target, "--audit-only", "--network", "false")
            run("run_daily_review_orchestrator.py", "--project-root", str(root), "--chain-wait-seconds", "0", "--json", "true")
            run("run_morning_brief.py", "--dry-run", "true", "--json", "true")
            brief = json.loads((root / "results/morning_brief/morning_brief_latest.json").read_text(encoding="utf-8"))
            assert brief["run_id"] == chain["run_id"] and brief["as_of"] == target
            assert brief["status"] in {"passed", "warning"}
            workflow = json.loads((root / "results/daily_workflow_latest.json").read_text(encoding="utf-8"))
            assert workflow["formal_candidate_intake"]["source_run_id"] == chain["run_id"]
            assert brief["candidate_count"] == workflow["formal_candidate_intake"]["candidate_count"]
            assert not list((root / "data/intraday").glob("**/*.csv"))
            assert not list((root / "results/notifications").glob("serverchan_*.json"))
            for path in (root / "state/daily_review_runs").glob("*.json"):
                shutil.copyfile(path, destination / ("review_receipt_" + path.name))
            (destination / "features_summary.json").write_text(json.dumps(chain["components"]["features"], ensure_ascii=False, indent=2), encoding="utf-8")
            (destination / "candidate_refresh.json").write_text(json.dumps(chain["components"].get("candidate_refresh"), ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            canonical_changed = [relative for relative, sha in before.items() if digest(canonical / relative) != sha]
            copied_daily_changed = [relative for relative, sha in before.items()
                                    if relative.startswith("data/daily/") and digest(root / relative) != sha]
            summary = {"evidence_mode": "recorded_local_input_replay", "target_date": target,
                       "input_symbol_count": len(symbols), "nul_input_symbols": nul_symbols,
                       "commands": command_results, "canonical_changed": canonical_changed,
                       "copied_daily_changed": copied_daily_changed, "input_sha256": before,
                       "real_network_allowed": False, "real_notifications_allowed": False}
            (destination / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            assert canonical_changed == [] and copied_daily_changed == []
