from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.candidate_monitoring import PROHIBITED_USAGE, read_json, write_json, write_text


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def run_step(name: str, command: list[str]) -> dict[str, Any]:
    started = datetime.now()
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except Exception as exc:
        returncode = 99
        stdout = ""
        stderr = str(exc)
    finished = datetime.now()
    return {
        "step": name,
        "command": " ".join(command),
        "status": "completed" if returncode == 0 else "failed",
        "returncode": returncode,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "elapsed_seconds": (finished - started).total_seconds(),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def refresh_review_pack(root: Path) -> dict[str, Any]:
    include = [
        "dist/portfolio_low_vol_rev120_candidate_monitoring_review_manifest.json",
        "results/research_candidate_registry_latest.json",
        "reports/research_candidate_registry_latest.md",
        "results/dashboard_candidate_card_portfolio_low_vol_rev120_latest.json",
        "reports/dashboard_candidate_card_portfolio_low_vol_rev120_latest.md",
        "results/portfolio_candidate_monitoring_latest.json",
        "reports/portfolio_candidate_monitoring_latest.md",
        "results/portfolio_candidate_monitoring_spec_latest.json",
        "reports/portfolio_candidate_monitoring_spec_latest.md",
        "results/portfolio_candidate_monitoring_refresh_latest.json",
        "reports/portfolio_candidate_monitoring_refresh_latest.md",
        "results/dashboard_candidate_cards_latest.json",
        "reports/dashboard_candidate_cards_latest.md",
        "reports/research_dashboard_candidate_summary_latest.md",
        "results/dashboard_watchlist_cards_latest.json",
        "reports/dashboard_watchlist_cards_latest.md",
        "results/research_candidate_dashboard_board_latest.json",
        "reports/research_candidate_dashboard_board_latest.md",
        "reports/research_candidate_dashboard_board_latest.html",
        "results/review_pack_inspection_latest.json",
        "reports/review_pack_inspection_latest.md",
        "results/research_boundary_scan_latest.json",
        "reports/research_boundary_scan_latest.md",
        "results/docs_boundary_consistency_latest.json",
        "reports/docs_boundary_consistency_latest.md",
        "results/final_warning_source_audit_latest.json",
        "reports/final_warning_source_audit_latest.md",
        "results/docs_boundary_cleanup_latest.json",
        "reports/docs_boundary_cleanup_latest.md",
        "results/final_review_readiness_validation_latest.json",
        "reports/final_review_readiness_validation_latest.md",
        "results/final_research_candidate_system_handoff_latest.json",
        "reports/final_research_candidate_system_handoff_latest.md",
        "results/final_review_pack_traceability_cleanup_latest.json",
        "reports/final_review_pack_traceability_cleanup_latest.md",
        "results/candidate_status_reconciliation_latest.json",
        "reports/candidate_status_reconciliation_latest.md",
        "results/data_evidence_registry_latest.json",
        "reports/data_evidence_registry_latest.md",
        "results/data_metadata_validation_latest.json",
        "reports/data_metadata_validation_latest.md",
        "results/data_gap_tracker_latest.json",
        "reports/data_gap_tracker_latest.md",
        "results/candidate_data_truth_final_verdict_latest.json",
        "reports/candidate_data_truth_final_verdict_latest.md",
        "results/stricter_candidate_robustness_recheck_latest.json",
        "reports/stricter_candidate_robustness_recheck_latest.md",
        "results/candidate_data_truth_handoff_latest.json",
        "reports/candidate_data_truth_handoff_latest.md",
        "results/data_evidence_backfill_test_report_latest.json",
        "reports/data_evidence_backfill_test_report_latest.md",
        "results/data_evidence_backfill_handoff_latest.json",
        "reports/data_evidence_backfill_handoff_latest.md",
        "results/dashboard_visual_polish_latest.json",
        "reports/dashboard_visual_polish_latest.md",
        "results/research_candidate_daily_maintenance_latest.json",
        "reports/research_candidate_daily_maintenance_latest.md",
        "results/long_goal_extended_test_report_latest.json",
        "reports/long_goal_extended_test_report_latest.md",
        "results/long_goal_e2e_validation_latest.json",
        "reports/long_goal_e2e_validation_latest.md",
        "results/overnight_monitoring_run_summary_latest.json",
        "reports/overnight_monitoring_run_summary_latest.md",
        "docs/research_candidate_monitoring.md",
        "docs/research_candidate_monitoring_runbook.md",
        "docs/dashboard_candidate_card_data_contract.md",
        "docs/research_candidate_daily_maintenance.md",
        "docs/research_candidate_troubleshooting_matrix.md",
        "docs/candidate_data_truth_and_robustness_audit.md",
        "docs/data_metadata_schema.md",
        "docs/data_evidence_requirements.md",
        "docs/templates/pit_universe_template.csv",
        "docs/templates/security_master_template.csv",
        "docs/templates/st_status_template.csv",
        "docs/templates/suspension_status_template.csv",
        "docs/templates/delist_events_template.csv",
        "docs/templates/adjustment_factor_template.csv",
        "docs/templates/benchmark_metadata_template.csv",
        "docs/templates/holdings_snapshot_template.csv",
        "agent_workspace/monitoring_loop_heartbeat.json",
        "agent_workspace/next_codex_task_after_final_review_readiness.md",
        "agent_workspace/next_codex_task_after_data_evidence_backfill.md",
        "reports/portfolio_candidate_one_page_review_latest.md",
        "results/strategy_pool_state_latest.json",
        "reports/strategy_pool_state_latest.md",
        "reports/portfolio_low_vol_rev120_candidate_risk_disclaimer.md",
        "reports/portfolio_low_vol_rev120_candidate_usage_boundary.md",
    ]
    readme = root / "dist" / "portfolio_low_vol_rev120_candidate_monitoring_review_README.md"
    write_text(
        readme,
        "\n".join(
            [
                "# Portfolio Low Vol Rev120 Candidate Monitoring Review Pack",
                "",
                "This is a monitoring review pack, not a release.",
                "",
                "Research-only: true.",
                "",
                "No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction.",
                "",
                "Candidate: portfolio_low_vol_rev120_candidate",
                "Primary combo: combo_50_50",
                "",
                "Suggested review order: dashboard board, monitoring refresh, schema validation, boundary scan, review pack inspection, runbook.",
            ]
        )
        + "\n",
    )
    include.append("dist/portfolio_low_vol_rev120_candidate_monitoring_review_README.md")
    manifest_path = root / "dist" / "portfolio_low_vol_rev120_candidate_monitoring_review_manifest.json"
    zip_path = root / "dist" / "portfolio_low_vol_rev120_candidate_monitoring_review_pack.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    included: list[str] = []
    unique_include = list(dict.fromkeys(include))
    for rel in unique_include:
        path = root / rel
        if path.exists() or rel == "dist/portfolio_low_vol_rev120_candidate_monitoring_review_manifest.json":
            included.append(rel)
        else:
            missing.append(rel)
    core = [
        "results/portfolio_candidate_monitoring_refresh_latest.json",
        "results/dashboard_candidate_cards_latest.json",
        "results/research_candidate_dashboard_board_latest.json",
        "docs/research_candidate_monitoring_runbook.md",
    ]
    core_missing = [path for path in core if path in missing]
    status = "human_review_required_core_missing" if core_missing else ("completed_with_warnings" if missing else "completed")
    manifest = {
        "pack_name": "portfolio_low_vol_rev120_candidate_monitoring_review_pack",
        "not_a_release": True,
        "research_only": True,
        "candidate_id": "portfolio_low_vol_rev120_candidate",
        "data_truth_status": "candidate_valid_but_data_risk_high",
        "display_status": "candidate_watch_due_to_data_gaps",
        "included_files": included,
        "missing_files": missing,
        "file_count": len(included),
        "zip_size_bytes": zip_path.stat().st_size if zip_path.exists() else 0,
        "status": status,
        "prohibited_usage": PROHIBITED_USAGE,
    }
    previous_size = None
    for _ in range(5):
        manifest["zip_size_bytes"] = 0 if previous_size is None else previous_size
        write_json(manifest_path, manifest)
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in unique_include:
                path = root / rel
                if path.exists():
                    zf.write(path, rel)
        actual_size = zip_path.stat().st_size
        if actual_size == previous_size:
            break
        previous_size = actual_size
    manifest["zip_size_bytes"] = zip_path.stat().st_size
    write_json(manifest_path, manifest)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in unique_include:
            path = root / rel
            if path.exists():
                zf.write(path, rel)
    return manifest


def make_next_task(root: Path) -> None:
    write_text(
        root / "agent_workspace" / "next_codex_task_after_daily_maintenance.md",
        """# Next Codex Task After Research Candidate Daily Maintenance

Allowed next directions:
- human review pack inspection
- dashboard UI integration with human approval
- monitoring refresh after new data
- docs cleanup
- optional test coverage improvement

Do not:
- expand the strategy pool
- restart volume_dry_rebound
- optimize rev120 parameters
- tune low-vol gate
- build live trading, broker integration, or auto execution
- package a release
""",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run research-only candidate daily maintenance workflow.")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--refresh-review-pack", default="true")
    parser.add_argument("--run-tests", default="false")
    parser.add_argument("--stop-on-human-review", default="true")
    parser.add_argument("--strict", default="false")
    parser.add_argument("--history-dir", default="results/monitoring_history")
    parser.add_argument("--heartbeat-file", default="agent_workspace/monitoring_loop_heartbeat.json")
    args = parser.parse_args()
    strict = parse_bool(args.strict)
    run_tests = parse_bool(args.run_tests)
    refresh_pack_enabled = parse_bool(args.refresh_review_pack)
    started = datetime.now()
    python = sys.executable
    commands = [
        ("canonical_input_health", [python, "scripts/check_candidate_canonical_inputs.py"]),
        ("build_research_candidate_registry", [python, "scripts/build_research_candidate_registry.py"]),
        ("candidate_monitoring_refresh", [python, "scripts/run_candidate_monitoring_refresh.py"]),
        ("schema_validation", [python, "scripts/validate_research_candidate_outputs.py"]),
        ("dashboard_candidate_card_build", [python, "scripts/build_dashboard_candidate_cards.py"]),
        ("dashboard_board_build", [python, "scripts/build_research_candidate_dashboard_board.py"]),
        ("review_pack_inspection", [python, "scripts/inspect_research_candidate_review_pack.py"]),
        ("boundary_scan", [python, "scripts/scan_research_only_boundaries.py"]),
    ]
    if run_tests:
        commands.append(("targeted_tests", [python, "-m", "pytest", "tests/test_candidate_monitoring_pipeline.py", "tests/test_dashboard_candidate_cards.py", "-q"]))
    steps = []
    stopped = False
    for name, command in commands:
        result = run_step(name, command)
        steps.append(result)
        if strict and result["returncode"] != 0:
            stopped = True
            break
    pack_manifest = None
    if refresh_pack_enabled and not stopped:
        pack_manifest = refresh_review_pack(ROOT)
        steps.append({"step": "refresh_review_pack", "status": pack_manifest["status"], "returncode": 0 if pack_manifest["status"] != "human_review_required_core_missing" else 2})
    make_next_task(ROOT)
    monitoring = read_json(ROOT / "results" / "portfolio_candidate_monitoring_refresh_latest.json")
    review = read_json(ROOT / "results" / "review_pack_inspection_latest.json") if (ROOT / "results" / "review_pack_inspection_latest.json").exists() else {}
    warnings = monitoring.get("warnings", []) or []
    human_review = bool(monitoring.get("human_review_required_reasons", []))
    failed = [step for step in steps if step.get("returncode", 0) != 0]
    status = "completed" if not failed else ("completed_with_warnings" if not strict else "failed")
    payload = {
        "task_id": "research_candidate_daily_maintenance",
        "status": status,
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "step_status": steps,
        "files_written": [
            "results/research_candidate_daily_maintenance_latest.json",
            "reports/research_candidate_daily_maintenance_latest.md",
            "results/research_candidate_dashboard_board_latest.json",
            "reports/research_candidate_dashboard_board_latest.md",
            "reports/research_candidate_dashboard_board_latest.html",
            "agent_workspace/next_codex_task_after_daily_maintenance.md",
        ],
        "warning_count": len(warnings),
        "human_review_required": human_review,
        "final_monitoring_status": monitoring.get("monitoring_status"),
        "review_pack_status": (pack_manifest or review).get("status"),
        "research_only_boundary_check": set(PROHIBITED_USAGE).issubset(set(monitoring.get("prohibited_usage", []))),
        "next_recommended_action": "monitoring refresh after new data or dashboard UI integration with human approval",
        "strict": strict,
        "stopped_early": stopped,
        "failure_reasons": [f"{step['step']}: returncode={step.get('returncode')}" for step in failed],
    }
    output = ROOT / args.output_dir / "research_candidate_daily_maintenance_latest.json"
    report = ROOT / args.report_dir / "research_candidate_daily_maintenance_latest.md"
    write_json(output, payload)
    lines = [
        "# Research Candidate Daily Maintenance",
        "",
        f"Status: `{payload['status']}`",
        f"Final monitoring status: `{payload['final_monitoring_status']}`",
        f"Warnings: `{payload['warning_count']}`",
        f"Human review required: `{payload['human_review_required']}`",
        f"Review pack status: `{payload['review_pack_status']}`",
        "",
        "| step | status | returncode |",
        "|---|---|---:|",
    ]
    for step in steps:
        lines.append(f"| {step.get('step')} | {step.get('status')} | {step.get('returncode')} |")
    lines += [
        "",
        "Boundary: research-only; no live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation instruction.",
    ]
    write_text(report, "\n".join(lines) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if status != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
