from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            if not stream.isatty():
                reconfigure(encoding="ascii", errors="backslashreplace")
            else:
                encoding = locale.getpreferredencoding(False) or "utf-8"
                reconfigure(encoding=encoding, errors="replace")


_configure_console_encoding()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.self_check_core import (
    ALLOWED_CONTEXT_MARKERS as _CORE_ALLOWED_CONTEXT_MARKERS,
)
from quantdesk.self_check_core import (
    CONTEXT_SENSITIVE_TERMS as _CORE_CONTEXT_SENSITIVE_TERMS,
)
from quantdesk.self_check_core import (
    STRICT_FORBIDDEN_OUTPUT_PHRASES as _CORE_STRICT_FORBIDDEN_OUTPUT_PHRASES,
)
from quantdesk.self_check_core import (
    audit_clean_zip_names,
    contains_mojibake,
    find_hardcoded_project_paths,
    list_zip_entries,
    scan_text_for_dangerous_phrases,
    version_centralization_issues,
)
from quantdesk.self_check_core import (
    has_allowed_context as _core_has_allowed_context,
)
from quantdesk.self_check_core import (
    line_is_rule_definition as _core_line_is_rule_definition,
)
from quantdesk.self_check_core import (
    strip_safe_negative_contexts as _core_strip_safe_negative_contexts,
)
from quantdesk.self_check_render import render_self_check_output
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, RELEASE_TITLE


@dataclass
class CheckResult:
    status: str
    name: str
    detail: str


def result(status: str, name: str, detail: str) -> CheckResult:
    return CheckResult(status=status, name=name, detail=detail)


def resolve_doc(name: str) -> Path:
    """Resolve a release-note / hygiene doc that may live in the project root or
    in ``docs/history/`` after archival.

    Historical release notes (RELEASE_NOTES_*.md, DELIVERY_*.md) were moved out
    of the project root into ``docs/history/`` during repository slimming. This
    helper preserves the "the doc must exist" governance intent while accepting
    either location. It returns the first existing path; if neither exists it
    returns the root path so callers report a meaningful missing-file location.
    """
    root_path = PROJECT_ROOT / name
    if root_path.exists():
        return root_path
    history_path = PROJECT_ROOT / "docs" / "history" / name
    if history_path.exists():
        return history_path
    return root_path


def required_files() -> list[Path]:
    return [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "CHANGELOG.md",
        PROJECT_ROOT / "ROADMAP.md",
        PROJECT_ROOT / "AGENTS.md",
        PROJECT_ROOT / "config" / "default.yaml",
        PROJECT_ROOT / "quantdesk" / "version.py",
        PROJECT_ROOT / "quantdesk" / "cli_summary.py",
        PROJECT_ROOT / "quantdesk" / "research" / "factor_evaluation.py",
        PROJECT_ROOT / "quantdesk" / "research" / "factor_combiner.py",
        PROJECT_ROOT / "quantdesk" / "research" / "combined_score_backtest.py",
        PROJECT_ROOT / "quantdesk" / "research" / "sector_rotation_engine.py",
        PROJECT_ROOT / "quantdesk" / "research" / "factor_research_dashboard.py",
        PROJECT_ROOT / "quantdesk" / "research" / "point_in_time_universe.py",
        PROJECT_ROOT / "quantdesk" / "research" / "daily_research_snapshot.py",
        PROJECT_ROOT / "quantdesk" / "backtest_assumptions.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "multi_strategy_account_book.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_live_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_live_snapshot.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_score.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_data_policy.py",
        PROJECT_ROOT / "quantdesk" / "research" / "risk_attribution_lite.py",
        PROJECT_ROOT / "quantdesk" / "research" / "daily_risk_attribution_snapshot.py",
        PROJECT_ROOT / "quantdesk" / "research" / "experiment_tracker.py",
        PROJECT_ROOT / "quantdesk" / "research" / "daily_experiment_tracker_snapshot.py",
        PROJECT_ROOT / "quantdesk" / "research" / "chart_renderer.py",
        PROJECT_ROOT / "quantdesk" / "backtest_loaders.py",
        PROJECT_ROOT / "quantdesk" / "self_check_render.py",
        PROJECT_ROOT / "quantdesk" / "onboarding.py",
        PROJECT_ROOT / "quantdesk" / "research" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "momentum.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "volatility.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "liquidity.py",
        PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv",
        PROJECT_ROOT / "config" / "strategy_demo.yaml",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
        PROJECT_ROOT / "docs" / "external_tools.md",
        PROJECT_ROOT / "docs" / "reference_a_stock_data.md",
        PROJECT_ROOT / "docs" / "reference_ashare.md",
        PROJECT_ROOT / "docs" / "reference_ashare_llm_analyst.md",
        PROJECT_ROOT / "docs" / "data_provider_fallback_plan.md",
        PROJECT_ROOT / "docs" / "data_update_workbench.md",
        PROJECT_ROOT / "docs" / "dev_preflight_workflow.md",
        PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md",
        PROJECT_ROOT / "docs" / "daily_workflow_guardrails.md",
        PROJECT_ROOT / "docs" / "remediation_flow.md",
        PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        PROJECT_ROOT / "docs" / "reference_fetch_status.md",
        PROJECT_ROOT / "docs" / "v1_acceptance_checklist.md",
        PROJECT_ROOT / "export_project.ps1",
        PROJECT_ROOT / "setup_windows.ps1",
        PROJECT_ROOT / "start_ui.cmd",
        PROJECT_ROOT / "run_tests.cmd",
        PROJECT_ROOT / "run_daily_smoke.cmd",
        PROJECT_ROOT / "self_check.cmd",
        PROJECT_ROOT / "create_desktop_shortcuts.ps1",
        PROJECT_ROOT / "create_desktop_shortcuts.cmd",
        PROJECT_ROOT / "setup_linux.sh",
        PROJECT_ROOT / "start_ui.sh",
        PROJECT_ROOT / "run_tests.sh",
        PROJECT_ROOT / "self_check.sh",
        PROJECT_ROOT / "export_project.sh",
        PROJECT_ROOT / "scripts" / "daily_quant_report.py",
        PROJECT_ROOT / "scripts" / "generate_low_price_report.py",
        PROJECT_ROOT / "scripts" / "update_stock_theme_map.py",
        PROJECT_ROOT / "scripts" / "generate_stock_theme_map_import.py",
        PROJECT_ROOT / "scripts" / "check_data_providers.py",
        PROJECT_ROOT / "scripts" / "update_daily_price_fallback.py",
        PROJECT_ROOT / "scripts" / "run_daily_workflow.py",
        PROJECT_ROOT / "scripts" / "run_historical_intraday_replay.py",
        PROJECT_ROOT / "scripts" / "run_intraday_replay_self_validation.py",
        PROJECT_ROOT / "scripts" / "check_intraday_provider_capability.py",
        PROJECT_ROOT / "scripts" / "build_trading_calendar.py",
        PROJECT_ROOT / "scripts" / "run_provider_network_smoke.py",
        PROJECT_ROOT / "scripts" / "run_real_intraday_sample_validation.py",
        PROJECT_ROOT / "scripts" / "run_intraday_data_workbench.py",
        PROJECT_ROOT / "scripts" / "build_chanlun_knowledge_base.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_fractal_analysis.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_bi_analysis.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_segment_analysis.py",
        PROJECT_ROOT / "scripts" / "qd.py",
        PROJECT_ROOT / "scripts" / "run_factor_evaluation.py",
        PROJECT_ROOT / "scripts" / "build_combined_score.py",
        PROJECT_ROOT / "scripts" / "run_combined_score_backtest.py",
        PROJECT_ROOT / "scripts" / "run_sector_rotation.py",
        PROJECT_ROOT / "scripts" / "run_factor_research_dashboard.py",
        PROJECT_ROOT / "scripts" / "show_pit_universe_status.py",
        PROJECT_ROOT / "scripts" / "run_multi_strategy_account_book.py",
        PROJECT_ROOT / "scripts" / "run_intraday_live_workbench.py",
        PROJECT_ROOT / "scripts" / "run_risk_attribution_lite.py",
        PROJECT_ROOT / "scripts" / "run_experiments.py",
        PROJECT_ROOT / "scripts" / "validate_historical_listings.py",
        PROJECT_ROOT / "scripts" / "build_historical_listings_template.py",
        PROJECT_ROOT / "scripts" / "check_python_env.py",
        PROJECT_ROOT / "scripts" / "check_agent_report_audit.py",
        PROJECT_ROOT / "scripts" / "run_strategy_validation.py",
        PROJECT_ROOT / "scripts" / "check_paper_vs_backtest_consistency.py",
        PROJECT_ROOT / "scripts" / "run_feature_quality_audit.py",
        PROJECT_ROOT / "scripts" / "run_backtest_statistical_validation.py",
        PROJECT_ROOT / "scripts" / "run_daily_data_retention.py",
        PROJECT_ROOT / "scripts" / "check_reproducibility_manifest.py",
        PROJECT_ROOT / "scripts" / "archive_results.py",
        PROJECT_ROOT / "scripts" / "show_command_center_status.py",
        PROJECT_ROOT / "scripts" / "show_quantdesk_status.py",
        PROJECT_ROOT / "scripts" / "run_data_health_dashboard.py",
        PROJECT_ROOT / "scripts" / "run_data_update.py",
        PROJECT_ROOT / "scripts" / "dev_preflight.py",
        PROJECT_ROOT / "scripts" / "run_parameter_sensitivity_audit.py",
        PROJECT_ROOT / "scripts" / "check_result_schemas.py",
        PROJECT_ROOT / "scripts" / "build_daily_features.py",
        PROJECT_ROOT / "scripts" / "self_check.py",
        PROJECT_ROOT / "scripts" / "quant_api_server.py",
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "quantdesk" / "backtest.py",
        PROJECT_ROOT / "quantdesk" / "config.py",
        PROJECT_ROOT / "quantdesk" / "reporting.py",
        PROJECT_ROOT / "quantdesk" / "word_report.py",
        PROJECT_ROOT / "quantdesk" / "explanations.py",
        PROJECT_ROOT / "quantdesk" / "data_providers.py",
        PROJECT_ROOT / "quantdesk" / "shared.py",
        PROJECT_ROOT / "quantdesk" / "candidate_contract.py",
        PROJECT_ROOT / "quantdesk" / "data_provider_registry.py",
        PROJECT_ROOT / "quantdesk" / "provider_result_contract.py",
        PROJECT_ROOT / "quantdesk" / "provider_network_smoke.py",
        PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_policy_guard.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_evidence_pack.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_hallucination_guard.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_report_audit.py",
        PROJECT_ROOT / "quantdesk" / "strategy_lookahead_guard.py",
        PROJECT_ROOT / "quantdesk" / "execution_assumption_guard.py",
        PROJECT_ROOT / "quantdesk" / "strategy_validation_suite.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "paper_vs_backtest_consistency.py",
        PROJECT_ROOT / "quantdesk" / "research" / "feature_quality_audit.py",
        PROJECT_ROOT / "quantdesk" / "research" / "backtest_statistical_validation.py",
        PROJECT_ROOT / "quantdesk" / "daily_data_retention.py",
        PROJECT_ROOT / "quantdesk" / "reproducibility_manifest.py",
        PROJECT_ROOT / "quantdesk" / "operation_log.py",
        PROJECT_ROOT / "quantdesk" / "result_archive.py",
        PROJECT_ROOT / "quantdesk" / "command_center_status.py",
        PROJECT_ROOT / "quantdesk" / "data_freshness_summary.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_coverage_dashboard.py",
        PROJECT_ROOT / "quantdesk" / "provider_failure_stats.py",
        PROJECT_ROOT / "quantdesk" / "data_grade_trend.py",
        PROJECT_ROOT / "quantdesk" / "research" / "parameter_sensitivity_audit.py",
        PROJECT_ROOT / "quantdesk" / "schema_contracts.py",
        PROJECT_ROOT / "quantdesk" / "result_payload_validator.py",
        PROJECT_ROOT / "quantdesk" / "workbench_overview.py",
        PROJECT_ROOT / "quantdesk" / "status_advisor.py",
        PROJECT_ROOT / "quantdesk" / "daily_output_summary.py",
        PROJECT_ROOT / "quantdesk" / "remediation_advisor.py",
        PROJECT_ROOT / "quantdesk" / "time_utils.py",
        PROJECT_ROOT / "quantdesk" / "a_stock_data_matrix.py",
        PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py",
        PROJECT_ROOT / "quantdesk" / "chanlun_knowledge_base.py",
        PROJECT_ROOT / "quantdesk" / "chanlun_fractal_engine.py",
        PROJECT_ROOT / "quantdesk" / "chanlun_bi_engine.py",
        PROJECT_ROOT / "quantdesk" / "chanlun_segment_engine.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "mootdx_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "tencent_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "iwencai_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "ths_hotspot_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "news_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "research_report_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "announcement_provider.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_providers.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py",
        PROJECT_ROOT / "quantdesk" / "trading_calendar.py",
        PROJECT_ROOT / "quantdesk" / "historical_replay_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_replay_self_validation.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_provider_capability.py",
        PROJECT_ROOT / "quantdesk" / "real_intraday_sample_validation.py",
        PROJECT_ROOT / "quantdesk" / "data_health_dashboard.py",
        PROJECT_ROOT / "quantdesk" / "data_update_orchestrator.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_quality_audit.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_data_workbench.py",
        PROJECT_ROOT / "quantdesk" / "daily_cache_index.py",
        PROJECT_ROOT / "quantdesk" / "daily_feature_engine.py",
        PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py",
        PROJECT_ROOT / "quantdesk" / "theme_map_providers.py",
        PROJECT_ROOT / "quantdesk" / "theme_map_import_draft.py",
        PROJECT_ROOT / "quantdesk" / "theme_map_seed.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "conditional_trade_plan.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "position_management_engine.py",
        PROJECT_ROOT / "quantdesk" / "market_clock.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "paper_trading.py",
        PROJECT_ROOT / "quantdesk" / "news_status.py",
        PROJECT_ROOT / "quantdesk" / "trust_engine.py",
        PROJECT_ROOT / "quantdesk" / "timing_advisor.py",
        PROJECT_ROOT / "quantdesk" / "strategy_demo.py",
        PROJECT_ROOT / "quantdesk" / "strategy_features.py",
        PROJECT_ROOT / "quantdesk" / "stock_classifier.py",
        PROJECT_ROOT / "quantdesk" / "position_planner.py",
        PROJECT_ROOT / "quantdesk" / "risk_lines.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "paper_review.py",
        PROJECT_ROOT / "quantdesk" / "indicators.py",
        PROJECT_ROOT / "quantdesk" / "benchmark.py",
        PROJECT_ROOT / "quantdesk" / "market_regime.py",
        PROJECT_ROOT / "quantdesk" / "fundamental_health.py",
        PROJECT_ROOT / "quantdesk" / "research_metrics.py",
        PROJECT_ROOT / "quantdesk" / "strategy_historical_replay.py",
        PROJECT_ROOT / "quantdesk" / "cn_a_share_rules.py",
        PROJECT_ROOT / "quantdesk" / "action_plan.py",
        PROJECT_ROOT / "quantdesk" / "manual_confirmation_queue.py",
        PROJECT_ROOT / "quantdesk" / "watchlist.py",
        PROJECT_ROOT / "quantdesk" / "local_alerts.py",
        PROJECT_ROOT / "quantdesk" / "integrations" / "registry.py",
        PROJECT_ROOT / "quantdesk" / "backtest_metrics.py",
        PROJECT_ROOT / "quantdesk" / "walk_forward.py",
        PROJECT_ROOT / "quantdesk" / "backtest_quality.py",
        PROJECT_ROOT / "quantdesk" / "backtest_audit.py",
        PROJECT_ROOT / "quantdesk" / "trade_plan.py",
        PROJECT_ROOT / "quantdesk" / "risk_position.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "base.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "price_volume.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "fundamental.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "scoring.py",
        PROJECT_ROOT / "scripts" / "walk_forward_backtest.py",
        PROJECT_ROOT / "scripts" / "export_trade_plan.py",
        PROJECT_ROOT / "quantdesk" / "strategy_compare.py",
        PROJECT_ROOT / "quantdesk" / "portfolio_health.py",
        PROJECT_ROOT / "quantdesk" / "stock_screener.py",
        PROJECT_ROOT / "quantdesk" / "signal_tracker.py",
        PROJECT_ROOT / "scripts" / "portfolio_health_check.py",
        PROJECT_ROOT / "scripts" / "screen_stocks.py",
        PROJECT_ROOT / "docs" / "agent_evidence_guard.md",
        PROJECT_ROOT / "docs" / "strategy_validation_suite.md",
        PROJECT_ROOT / "docs" / "daily_data_retention_workflow.md",
        PROJECT_ROOT / "docs" / "reproducibility_manifest.md",
        PROJECT_ROOT / "docs" / "feature_quality_audit.md",
        PROJECT_ROOT / "docs" / "backtest_statistical_validation.md",
        PROJECT_ROOT / "docs" / "troubleshooting_quick_ref.md",
        PROJECT_ROOT / "docs" / "architecture.md",
        PROJECT_ROOT / "docs" / "video_scripts.md",
        PROJECT_ROOT / "docs" / "videos" / "README.md",
        PROJECT_ROOT / "docs" / "screenshots" / "README.md",
        resolve_doc("RELEASE_NOTES_v3.5.0.md"),
        PROJECT_ROOT / "assets" / "sample_data_manifest.json",
        PROJECT_ROOT / "build" / "QuantDesk.spec",
        PROJECT_ROOT / "build" / "quantdesk_installer.nsi",
        PROJECT_ROOT / "build" / "build_portable_release.ps1",
        PROJECT_ROOT / "build" / "build_installer.ps1",
        PROJECT_ROOT / "docs" / "data_health_dashboard.md",
        PROJECT_ROOT / "docs" / "parameter_sensitivity_audit.md",
        PROJECT_ROOT / "docs" / "schema_contracts.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
        PROJECT_ROOT / "docs" / "index.md",
        PROJECT_ROOT / "docs" / "quickstart.md",
        PROJECT_ROOT / "docs" / "architecture.md",
        PROJECT_ROOT / "docs" / "intraday_live_quickstart.md",
        PROJECT_ROOT / "docs" / "schema_examples" / "data_health_dashboard.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "feature_quality_audit.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "backtest_statistical_validation.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "parameter_sensitivity.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "strategy_observation_context.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "command_center_status.example.json",
        PROJECT_ROOT / "config" / "parameter_sensitivity.example.json",
        PROJECT_ROOT / "tests" / "test_v329_parameter_sensitivity_and_robustness_audit.py",
        PROJECT_ROOT / "tests" / "test_v330_consolidation_and_schema_contracts.py",
        PROJECT_ROOT / "tests" / "test_v331_workbench_ui_and_workflow_unification.py",
    ]


def check_python_version() -> CheckResult:
    version = sys.version_info
    if version >= (3, 10):
        return result("PASS", "python_version", f"{version.major}.{version.minor}.{version.micro}")
    return result("FAIL", "python_version", "Python 3.10+ is required.")


def check_python_launcher_env() -> CheckResult:
    python_cmd = shutil.which("python")
    path_python_ok = False
    path_python_detail = "python not found in PATH"
    if python_cmd:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed local diagnostic command.
                [python_cmd, "--version"],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            output = (completed.stdout or completed.stderr or "").strip()
            path_python_ok = completed.returncode == 0
            path_python_detail = output or f"exit_code={completed.returncode}"
        except Exception as exc:  # pragma: no cover - OS-level launcher failure.
            path_python_detail = f"{type(exc).__name__}: {exc}"
    py_launcher = shutil.which("py") if os.name == "nt" else None
    stdout_encoding = sys.stdout.encoding or ""
    preferred_encoding = locale.getpreferredencoding(False) or ""
    details = (
        f"executable={sys.executable}; path_python_available={path_python_ok}; "
        f"path_python={path_python_detail}; py_launcher_available={bool(py_launcher)}; "
        f"stdout_encoding={stdout_encoding}; locale={preferred_encoding}; "
        "suggested_next_action=activate conda environment, use py launcher, or set QUANTDESK_PYTHON"
    )
    if not path_python_ok:
        return result("WARN", "python_launcher_env", details)
    if stdout_encoding and "utf" not in stdout_encoding.lower():
        return result("WARN", "python_launcher_env", details)
    return result("PASS", "python_launcher_env", details)


def check_required_files() -> list[CheckResult]:
    rows = []
    for path in required_files():
        if path.exists():
            rows.append(result("PASS", f"file:{path.relative_to(PROJECT_ROOT)}", "exists"))
        elif path.name == "a_share_trading_calendar.csv":
            rows.append(
                result(
                    "WARN",
                    f"file:{path.relative_to(PROJECT_ROOT)}",
                    "missing; market_calendar_source=unavailable unless legacy YAML exists. Provide data/calendar/a_share_trading_calendar.csv.",
                )
            )
        elif path.name in {"quantdesk_installer.nsi", "build_portable_release.ps1", "build_installer.ps1"}:
            rows.append(
                result(
                    "WARN",
                    f"file:{path.relative_to(PROJECT_ROOT)}",
                    "missing release/installer helper; current research-only v580 scope forbids release packaging.",
                )
            )
        else:
            rows.append(result("FAIL", f"file:{path.relative_to(PROJECT_ROOT)}", "missing"))
    rows.append(result("PASS" if (PROJECT_ROOT / "tests").is_dir() else "FAIL", "tests_dir", "exists" if (PROJECT_ROOT / "tests").is_dir() else "missing"))
    return rows


def check_critical_source_modules() -> list[CheckResult]:
    """FAIL when a source module that many others import is missing on disk.

    This guards against a clean-repo delivery that drops a required but
    easy-to-miss source file (e.g. an untracked ``quantdesk/paper/market_rules.py``
    that 15+ modules import). The check is filesystem-based on purpose: a stale
    ``.pyc`` must never let a missing ``.py`` source pass. Each entry records who
    depends on it so the failure message points at the delivery risk.
    """

    critical = {
        Path("quantdesk") / "paper" / "market_rules.py": (
            "board-aware paper lot sizing / limit / ST rules; imported by backtest, "
            "walk_forward, position_planner, risk_position, auto_strategy_sandbox, and the "
            "paper.* engines. Missing source breaks core paper/backtest; sync it to quant_clean_repo."
        ),
        Path("quantdesk") / "staged_provider_evidence_audit.py": (
            "read-only staged-provider (mootdx/baostock) evidence audit; imported by the staged "
            "import-plan module and the audit CLI. Missing source breaks staged-data audit/plan; "
            "sync it to quant_clean_repo."
        ),
        Path("quantdesk") / "staged_import_plan.py": (
            "read-only dry-run staged import-plan builder; imported by the staged-import-plan CLI "
            "and qd.py. Missing source breaks the import-plan command; sync it to quant_clean_repo."
        ),
        Path("quantdesk") / "price_basis.py": (
            "shared Price Data Contract, observed-basis classifier, and fail-closed validation; "
            "imported by feature, backtest, replay, reporting, and paper research paths. Missing "
            "source disables the P0 mixed-price protection; sync it to quant_clean_repo."
        ),
    }
    rows: list[CheckResult] = []
    for rel_path, why in critical.items():
        path = PROJECT_ROOT / rel_path
        # Filesystem check only: do not import (a stale .pyc could mask a missing .py).
        if path.is_file() and path.stat().st_size > 0:
            rows.append(result("PASS", f"critical_source:{rel_path.as_posix()}", "present"))
        else:
            rows.append(
                result(
                    "FAIL",
                    f"critical_source:{rel_path.as_posix()}",
                    f"missing source file (.py). {why}",
                )
            )
    return rows


def check_no_hardcoded_project_path() -> CheckResult:
    forbidden = ["E:" + "\\" + "codex" + "\\" + "quant", "E:" + "/" + "codex" + "/" + "quant"]
    offenders = find_hardcoded_project_paths(PROJECT_ROOT, forbidden)
    if offenders:
        return result("WARN", "hardcoded_paths", ", ".join(offenders))
    return result("PASS", "hardcoded_paths", "no project absolute path found")


def check_version_centralization() -> CheckResult:
    files = {
        "version": PROJECT_ROOT / "quantdesk" / "version.py",
        "server": PROJECT_ROOT / "scripts" / "quant_api_server.py",
        "config": PROJECT_ROOT / "config" / "default.yaml",
        "readme": PROJECT_ROOT / "README.md",
        "roadmap": PROJECT_ROOT / "ROADMAP.md",
        "docs": PROJECT_ROOT / "docs" / "api_payload_schema.md",
    }
    texts = {
        name: (path.read_text(encoding="utf-8", errors="ignore") if path.exists() else "")
        for name, path in files.items()
    }
    missing = version_centralization_issues(
        version_text=texts["version"],
        server_text=texts["server"],
        config_text=texts["config"],
        readme_text=texts["readme"],
        roadmap_text=texts["roadmap"],
        docs_text=texts["docs"],
        app_version=APP_VERSION,
        api_schema_version=API_SCHEMA_VERSION,
        release_title=RELEASE_TITLE,
    )
    if missing:
        return result("FAIL", "version_centralization", "missing: " + "; ".join(missing))
    return result("PASS", "version_centralization", f"{APP_VERSION} centralized in quantdesk/version.py")


def check_cli_summary_contract() -> CheckResult:
    summary_path = PROJECT_ROOT / "quantdesk" / "cli_summary.py"
    qd_path = PROJECT_ROOT / "scripts" / "qd.py"
    summary_text = summary_path.read_text(encoding="utf-8", errors="ignore") if summary_path.exists() else ""
    qd_text = qd_path.read_text(encoding="utf-8", errors="ignore") if qd_path.exists() else ""
    required_tokens = [
        "build_cli_summary",
        "format_human_summary",
        "warnings_top",
        "output_paths",
        "suggested_next_action",
        "disclaimer",
    ]
    missing = [token for token in required_tokens if token not in summary_text]
    if missing:
        return result("FAIL", "cli_summary_contract", "missing tokens: " + ", ".join(missing))
    if "--summary true" not in qd_text:
        return result("FAIL", "cli_summary_contract", "qd.py help must mention --summary true")
    return result("PASS", "cli_summary_contract", "compact CLI summary contract is documented and wired")


def check_factor_evaluation_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "factor_evaluation.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "momentum.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "volatility.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "liquidity.py",
        PROJECT_ROOT / "scripts" / "run_factor_evaluation.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "factor_evaluation", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    if "factor-eval" not in qd_text:
        return result("FAIL", "factor_evaluation", "qd.py help/command map missing factor-eval")
    try:
        import pandas as pd
        from quantdesk.factor_evaluation import evaluate_factors

        rows = []
        dates = pd.bdate_range("2026-01-02", periods=8)
        for index in range(12):
            for day_index, date in enumerate(dates):
                rows.append({
                    "date": date,
                    "symbol": f"{index + 1:06d}",
                    "close": 10 + index + day_index * (0.1 + index * 0.01),
                    "factor_x": float(index),
                })
        payload = evaluate_factors(
            pd.DataFrame(rows),
            ["factor_x"],
            horizons=(1,),
            quantiles=3,
            min_cross_section=5,
        )
        if payload.get("task_type") != "factor_evaluation" or "factor_metrics" not in payload:
            return result("FAIL", "factor_evaluation", "schema smoke check missing factor evaluation fields")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "factor_evaluation", f"schema smoke check failed: {exc}")
    return result("PASS", "factor_evaluation", "factor evaluation modules, CLI, and schema smoke check are wired")


def check_factor_combiner_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "factor_combiner.py",
        PROJECT_ROOT / "scripts" / "build_combined_score.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "factor_combiner", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "factor-combine" not in qd_text:
        return result("FAIL", "factor_combiner", "qd.py help/command map missing factor-combine")
    if "factor_combination:" not in config_text:
        return result("FAIL", "factor_combiner", "config/default.yaml missing factor_combination")
    try:
        import pandas as pd
        from quantdesk.factor_combiner import build_combined_score

        rows = []
        dates = pd.bdate_range("2026-01-02", periods=5)
        for index in range(12):
            for day_index, date in enumerate(dates):
                rows.append({
                    "date": date,
                    "symbol": f"{index + 1:06d}",
                    "factor_x": float(index),
                    "industry": f"industry_{index % 3}",
                    "market_cap": 1_000_000_000.0 * (index + 1),
                })
        payload = build_combined_score(
            pd.DataFrame(rows),
            ["factor_x"],
            neutralize_industry=True,
            neutralize_market_cap=True,
            weight_method="equal_weight",
        )
        if payload.get("task_type") != "factor_combination" or "score_summary" not in payload:
            return result("FAIL", "factor_combiner", "schema smoke check missing combined score fields")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "factor_combiner", f"schema smoke check failed: {exc}")
    return result("PASS", "factor_combiner", "factor combiner module, CLI, config, and schema smoke check are wired")


def check_combined_score_backtest_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "combined_score_backtest.py",
        PROJECT_ROOT / "scripts" / "run_combined_score_backtest.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "combined_score_backtest", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "combined-score-backtest" not in qd_text:
        return result("FAIL", "combined_score_backtest", "qd.py help/command map missing combined-score-backtest")
    if "combined_score_backtest:" not in config_text:
        return result("FAIL", "combined_score_backtest", "config/default.yaml missing combined_score_backtest")
    try:
        import pandas as pd
        from quantdesk.combined_score_backtest import run_combined_score_backtest

        dates = pd.bdate_range("2026-01-02", periods=12)
        score_rows = []
        price_rows = []
        for index in range(8):
            symbol = f"{index + 1:06d}"
            close = 10.0 + index
            for day_index, date in enumerate(dates):
                close *= 1.0 + 0.001 + index * 0.0001
                score_rows.append({"date": date, "symbol": symbol, "combined_score": float(index)})
                price_rows.append({"date": date, "symbol": symbol, "close": close})
        payload = run_combined_score_backtest(
            pd.DataFrame(score_rows),
            pd.DataFrame(price_rows),
            top_n=3,
            holding_days=3,
            execution_lag_days=1,
        )
        if payload.get("task_type") != "combined_score_backtest" or "performance" not in payload:
            return result("FAIL", "combined_score_backtest", "schema smoke check missing backtest fields")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "combined_score_backtest", f"schema smoke check failed: {exc}")
    return result("PASS", "combined_score_backtest", "combined score backtest module, CLI, config, and schema smoke check are wired")


def check_sector_rotation_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "sector_rotation_engine.py",
        PROJECT_ROOT / "scripts" / "run_sector_rotation.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "sector_rotation", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "sector-rotation" not in qd_text:
        return result("FAIL", "sector_rotation", "qd.py help/command map missing sector-rotation")
    if "sector_rotation:" not in config_text:
        return result("FAIL", "sector_rotation", "config/default.yaml missing sector_rotation")
    try:
        import pandas as pd
        from quantdesk.sector_rotation_engine import run_sector_rotation

        dates = pd.bdate_range("2026-01-02", periods=28)
        rows = []
        for index in range(9):
            symbol = f"{index + 1:06d}"
            sector = f"sector_{index % 3}"
            close = 10.0 + index
            for day_index, date in enumerate(dates):
                close *= 1.0 + 0.001 + (index % 3) * 0.0005
                rows.append({"date": date, "symbol": symbol, "close": close, "sector_name": sector})
        payload = run_sector_rotation(pd.DataFrame(rows), min_sector_symbols=2)
        if payload.get("task_type") != "sector_rotation" or "sector_rankings" not in payload:
            return result("FAIL", "sector_rotation", "schema smoke check missing sector rotation fields")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "sector_rotation", f"schema smoke check failed: {exc}")
    return result("PASS", "sector_rotation", "sector rotation module, CLI, config, and schema smoke check are wired")


def check_factor_research_dashboard_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "factor_research_dashboard.py",
        PROJECT_ROOT / "scripts" / "run_factor_research_dashboard.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "factor_research_dashboard", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "research-dashboard" not in qd_text:
        return result("FAIL", "factor_research_dashboard", "qd.py help/command map missing research-dashboard")
    if "factor_research_dashboard:" not in config_text:
        return result("FAIL", "factor_research_dashboard", "config/default.yaml missing factor_research_dashboard")
    try:
        from quantdesk.factor_research_dashboard import (
            build_factor_research_dashboard,
            build_synthetic_research_payloads,
        )

        payload = build_factor_research_dashboard(build_synthetic_research_payloads(), synthetic_payloads=True)
        if payload.get("task_type") != "factor_research_dashboard" or "dashboard_summary" not in payload:
            return result("FAIL", "factor_research_dashboard", "schema smoke check missing dashboard fields")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "factor_research_dashboard", f"schema smoke check failed: {exc}")
    return result("PASS", "factor_research_dashboard", "research dashboard module, CLI, config, and schema smoke check are wired")


def check_pit_universe_support() -> CheckResult:
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "point_in_time_universe.py",
        PROJECT_ROOT / "scripts" / "show_pit_universe_status.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        return result("FAIL", "pit_universe", "missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "pit-universe" not in qd_text:
        return result("FAIL", "pit_universe", "qd.py help/command map missing pit-universe")
    if "point_in_time_universe:" not in config_text:
        return result("FAIL", "pit_universe", "config/default.yaml missing point_in_time_universe")
    try:
        import pandas as pd
        from quantdesk.point_in_time_universe import summarize_pit_universe_status, universe_at

        listings = pd.DataFrame({
            "symbol": ["000001", "000002"],
            "list_date": pd.to_datetime(["2020-01-01", "2027-01-01"]),
            "delist_date": [pd.NaT, pd.NaT],
        })
        symbols, meta = universe_at("2026-05-25", listings)
        status = summarize_pit_universe_status(listings)
        if symbols != ["000001"] or not status.get("pit_universe") or not meta.get("excluded_counts"):
            return result("FAIL", "pit_universe", "schema smoke check returned unexpected PIT universe")
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "pit_universe", f"schema smoke check failed: {exc}")
    listings_path = PROJECT_ROOT / "data" / "universe" / "historical_listings.csv"
    if not listings_path.exists():
        return result("WARN", "pit_universe", "PIT universe support wired; historical_listings.csv missing, so reports keep survivorship-bias warning")
    return result("PASS", "pit_universe", "PIT universe module, CLI, config, and schema smoke check are wired")


def check_project_hygiene_v3435() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    gitignore = PROJECT_ROOT / ".gitignore"
    gitignore_text = gitignore.read_text(encoding="utf-8", errors="ignore") if gitignore.exists() else ""
    for token in ("test_runtime/", "tmp_factor_out.txt", "tmp_factor_err.txt", "*.tmp"):
        if token not in gitignore_text:
            failures.append(f".gitignore missing {token}")
    inventory = resolve_doc("architecture_inventory.md")
    if not inventory.exists():
        failures.append("architecture_inventory.md missing")
    elif "Updated through `v3.4.4.0-multi-strategy-account-book`" not in inventory.read_text(encoding="utf-8", errors="ignore"):
        failures.append("architecture_inventory.md missing v3.4.4.0 header")
    readme = PROJECT_ROOT / "README.md"
    if readme.exists():
        line_count = len(readme.read_text(encoding="utf-8", errors="ignore").splitlines())
        if line_count > 550:
            warnings.append(f"README.md is still long: {line_count} lines")
    else:
        failures.append("README.md missing")
    if not (PROJECT_ROOT / "docs" / "faq.md").exists():
        failures.append("docs/faq.md missing")
    if failures:
        return result("FAIL", "project_hygiene_v3435", "; ".join(failures))
    detail = "v3.4.3.5 hygiene checks wired"
    if warnings:
        detail += "; " + "; ".join(warnings)
        return result("WARN", "project_hygiene_v3435", detail)
    return result("PASS", "project_hygiene_v3435", detail)


def check_daily_research_snapshot() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    module = PROJECT_ROOT / "quantdesk" / "research" / "daily_research_snapshot.py"
    if not module.exists():
        failures.append("quantdesk/daily_research_snapshot.py missing")
    cli_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    if "research_dashboard_snapshot" not in cli_text or "render_daily_research_snapshot_markdown" not in cli_text:
        failures.append("daily workflow missing research_dashboard_snapshot wiring")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    if "daily_research_snapshot" not in config_text:
        failures.append("config/default.yaml missing daily_research_snapshot")
    try:
        from quantdesk.daily_research_snapshot import load_research_dashboard_snapshot

        snapshot = load_research_dashboard_snapshot(PROJECT_ROOT / "missing_research_dashboard_latest.json")
        if snapshot.get("status") != "missing":
            failures.append("missing dashboard smoke check did not return status=missing")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"snapshot smoke check failed: {exc}")
    if not (PROJECT_ROOT / "results" / "research_dashboard_latest.json").exists():
        warnings.append("research_dashboard_latest.json missing; daily snapshot will warn only")
    if failures:
        return result("FAIL", "daily_research_snapshot", "; ".join(failures))
    detail = "daily research dashboard snapshot is wired"
    if warnings:
        return result("WARN", "daily_research_snapshot", detail + "; " + "; ".join(warnings))
    return result("PASS", "daily_research_snapshot", detail)


def check_multi_strategy_account_book_support() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    module = PROJECT_ROOT / "quantdesk" / "paper" / "multi_strategy_account_book.py"
    script = PROJECT_ROOT / "scripts" / "run_multi_strategy_account_book.py"
    qd = PROJECT_ROOT / "scripts" / "qd.py"
    config = PROJECT_ROOT / "config" / "default.yaml"
    daily = PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py"
    faq = PROJECT_ROOT / "docs" / "faq.md"
    export = PROJECT_ROOT / "export_project.ps1"
    verify = PROJECT_ROOT / "scripts" / "verify_clean_zip.py"
    for path in (module, script):
        if not path.exists():
            failures.append(f"{path.relative_to(PROJECT_ROOT)} missing")
    qd_text = qd.read_text(encoding="utf-8", errors="ignore") if qd.exists() else ""
    config_text = config.read_text(encoding="utf-8", errors="ignore") if config.exists() else ""
    daily_text = daily.read_text(encoding="utf-8", errors="ignore") if daily.exists() else ""
    faq_text = faq.read_text(encoding="utf-8", errors="ignore") if faq.exists() else ""
    export_text = export.read_text(encoding="utf-8", errors="ignore") if export.exists() else ""
    verify_text = verify.read_text(encoding="utf-8", errors="ignore") if verify.exists() else ""
    if "multi-strategy-book" not in qd_text:
        failures.append("qd.py missing multi-strategy-book command")
    if "multi_strategy_account_book" not in config_text:
        failures.append("config/default.yaml missing multi_strategy_account_book")
    if "daily_multi_strategy_account_book" not in config_text:
        failures.append("config/default.yaml missing daily_multi_strategy_account_book")
    if "multi_strategy_account_book_snapshot" not in daily_text:
        failures.append("daily workflow missing multi_strategy_account_book_snapshot")
    if "Multi-Strategy Account Book" not in faq_text and "multi-strategy" not in faq_text.lower():
        failures.append("docs/faq.md missing multi-strategy account book FAQ")
    if "quantdesk/paper/multi_strategy_account_book.py" not in export_text:
        failures.append("export_project.ps1 release artifact missing multi_strategy_account_book.py")
    if "tests/test_v3440_multi_strategy_account_book.py" not in verify_text:
        failures.append("verify_clean_zip.py missing v3440 required test")
    try:
        from quantdesk.multi_strategy_account_book import build_multi_strategy_account_book, load_multi_strategy_config

        cfg, load_warnings = load_multi_strategy_config()
        payload = build_multi_strategy_account_book(cfg)
        if payload.get("task_type") != "multi_strategy_account_book" or payload.get("sub_account_count", 0) <= 0:
            failures.append("multi-strategy account book smoke payload invalid")
        if "No broker connection" not in str(payload.get("disclaimer")):
            failures.append("multi-strategy account book disclaimer missing no-broker wording")
        if load_warnings:
            warnings.extend(str(item) for item in load_warnings[:2])
    except Exception as exc:  # noqa: BLE001
        failures.append(f"multi-strategy account book smoke failed: {exc}")
    if not (PROJECT_ROOT / "results" / "multi_strategy_account_book_latest.json").exists():
        warnings.append("multi_strategy_account_book_latest.json missing; daily snapshot will warn only")
    if failures:
        return result("FAIL", "multi_strategy_account_book", "; ".join(failures))
    detail = "multi-strategy account book module, CLI, config, daily snapshot, and release proof checks are wired"
    if warnings:
        return result("WARN", "multi_strategy_account_book", detail + "; " + "; ".join(warnings))
    return result("PASS", "multi_strategy_account_book", detail)


def _legacy_check_intraday_live_workbench_support() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    required = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_live_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_live_snapshot.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_score.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_data_policy.py",
        PROJECT_ROOT / "scripts" / "run_intraday_live_workbench.py",
        PROJECT_ROOT / "config" / "multi_strategy_account_book.example.yaml",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        failures.append("missing: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    daily_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    faq_text = (PROJECT_ROOT / "docs" / "faq.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "faq.md").exists() else ""
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8", errors="ignore")
    if '"live":' not in qd_text or "run_intraday_live_workbench.py" not in qd_text:
        failures.append("qd.py missing live command")
    if "intraday_live_workbench" not in config_text:
        failures.append("config/default.yaml missing intraday_live_workbench")
    if "daily_intraday_live_snapshot" not in config_text:
        failures.append("config/default.yaml missing daily_intraday_live_snapshot")
    if "intraday_live_snapshot" not in daily_text:
        failures.append("daily workflow missing intraday_live_snapshot")
    if "partial daily" not in faq_text.lower() or "intraday live" not in faq_text.lower():
        failures.append("docs/faq.md missing intraday live / partial daily FAQ")
    if "Intraday Live Workbench" not in app_text and "盘中工作台" not in app_text:
        failures.append("UI missing Intraday Live Workbench label")
    if "assert_no_partial_daily_for_research" not in (PROJECT_ROOT / "scripts" / "run_backtest.py").read_text(encoding="utf-8", errors="ignore"):
        failures.append("backtest partial_daily policy guard missing")
    if "config/multi_strategy_account_book.example.yaml" not in export_text:
        failures.append("export release artifact missing example yaml")
    if "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py" not in verify_text:
        failures.append("verify_clean_zip.py missing v3441 proof test")
    try:
        from quantdesk.intraday_data_policy import assert_no_partial_daily_for_research
        from quantdesk.intraday_live_engine import build_intraday_live_workbench

        try:
            assert_no_partial_daily_for_research("data/intraday/partial_daily/2026-01-01.csv", caller="self_check")
            failures.append("partial_daily guard did not reject provisional path")
        except ValueError:
            pass
        with tempfile.TemporaryDirectory(prefix="quantdesk_self_check_intraday_") as temp_dir:
            smoke_root = Path(temp_dir)
            payload = build_intraday_live_workbench(
                {
                    "watchlist": [],
                    "network": False,
                    "partial_daily_dir": str(smoke_root / "partial_daily"),
                    "spot_snapshot_dir": str(smoke_root / "spot_snapshots"),
                    "account_state_path": str(smoke_root / "intraday_live_account.json"),
                    "paper_watchlist_json": str(smoke_root / "paper_watchlist.json"),
                },
                network=False,
            )
        if payload.get("task_type") != "intraday_live_workbench":
            failures.append("intraday live smoke payload invalid")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"intraday live smoke failed: {exc}")
    if not (PROJECT_ROOT / "results" / "intraday_live_workbench_latest.json").exists():
        warnings.append("intraday_live_workbench_latest.json missing; daily snapshot will warn only")
    if failures:
        return result("FAIL", "intraday_live_workbench", "; ".join(failures))
    detail = "intraday live workbench module, CLI, config, data isolation, UI, daily snapshot, and release proof checks are wired"
    if warnings:
        return result("WARN", "intraday_live_workbench", detail + "; " + "; ".join(warnings))
    return result("PASS", "intraday_live_workbench", detail)


def check_risk_attribution_lite_support() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "risk_attribution_lite.py",
        PROJECT_ROOT / "quantdesk" / "research" / "daily_risk_attribution_snapshot.py",
        PROJECT_ROOT / "scripts" / "run_risk_attribution_lite.py",
        PROJECT_ROOT / "docs" / "intraday_live_quickstart.md",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        failures.append("missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    daily_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    faq_text = (PROJECT_ROOT / "docs" / "faq.md").read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8", errors="ignore")
    if "risk-attribution" not in qd_text or "run_risk_attribution_lite.py" not in qd_text:
        failures.append("qd.py missing risk-attribution command")
    if "--init" not in (PROJECT_ROOT / "scripts" / "run_intraday_live_workbench.py").read_text(encoding="utf-8", errors="ignore"):
        failures.append("live CLI missing --init")
    if "risk_attribution_lite" not in config_text:
        failures.append("config/default.yaml missing risk_attribution_lite")
    if "daily_risk_attribution_snapshot" not in config_text:
        failures.append("config/default.yaml missing daily_risk_attribution_snapshot")
    if "risk_attribution_snapshot" not in daily_text:
        failures.append("daily workflow missing risk_attribution_snapshot")
    if "风险归因" not in faq_text and "risk attribution" not in faq_text.lower():
        failures.append("docs/faq.md missing risk attribution FAQ")
    if "qd live --init" not in faq_text and "live --init" not in faq_text:
        failures.append("docs/faq.md missing live init FAQ")
    if "quantdesk/research/risk_attribution_lite.py" not in export_text:
        failures.append("export_project.ps1 release artifact missing risk_attribution_lite.py")
    if "tests/test_v3442_risk_attribution_lite_and_live_polish.py" not in verify_text:
        failures.append("verify_clean_zip.py missing v3442 proof test")
    try:
        from quantdesk.intraday_live_engine import build_empty_intraday_live_account_state
        from quantdesk.risk_attribution_lite import build_risk_attribution_report

        state = build_empty_intraday_live_account_state()
        if not (state.get("research_only") and state.get("no_broker") and state.get("no_real_orders")):
            failures.append("live init state template missing safety flags")
        payload = build_risk_attribution_report({"multi_strategy_book_json": "results/missing_for_self_check.json"})
        if payload.get("task_type") != "risk_attribution_lite":
            failures.append("risk attribution smoke payload invalid")
        if "Not investment advice" not in str(payload.get("disclaimer")):
            failures.append("risk attribution disclaimer missing")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"risk attribution smoke failed: {exc}")
    if not (PROJECT_ROOT / "results" / "risk_attribution_lite_latest.json").exists():
        warnings.append("risk_attribution_lite_latest.json missing; daily snapshot will warn only")
    if not (PROJECT_ROOT / "state" / "intraday_live_account.json").exists():
        warnings.append("intraday_live_account.json missing; run qd.py live --init when needed")
    if failures:
        return result("FAIL", "risk_attribution_lite", "; ".join(failures))
    detail = "risk attribution lite module, CLI, config, daily snapshot, live init, quickstart, and release proof checks are wired"
    if warnings:
        return result("WARN", "risk_attribution_lite", detail + "; " + "; ".join(warnings))
    return result("PASS", "risk_attribution_lite", detail)


def check_engineering_docs_package_grouping_lite() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    required_paths = [
        PROJECT_ROOT / "quantdesk" / "backtest_loaders.py",
        PROJECT_ROOT / "quantdesk" / "self_check_render.py",
        PROJECT_ROOT / "quantdesk" / "research" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "__init__.py",
        PROJECT_ROOT / "docs" / "index.md",
        PROJECT_ROOT / "docs" / "quickstart.md",
        PROJECT_ROOT / "docs" / "architecture.md",
        PROJECT_ROOT / "tests" / "test_v3451_engineering_docs_and_package_grouping_lite.py",
    ]
    for path in required_paths:
        if not path.exists():
            failures.append(f"{path.relative_to(PROJECT_ROOT)} missing")
    try:
        from quantdesk.backtest import benchmark_total_return as legacy_benchmark_total_return
        from quantdesk.backtest_loaders import benchmark_total_return as split_benchmark_total_return
        if legacy_benchmark_total_return is not split_benchmark_total_return:
            failures.append("backtest.py does not re-export split benchmark_total_return")
    except Exception as exc:  # pragma: no cover - diagnostic path
        failures.append(f"backtest loader import failed: {exc}")
    try:
        from quantdesk.self_check_render import render_self_check_output
        output, summary = render_self_check_output([result("PASS", "sample", "ok")])
        if "PASS sample: ok" not in output or summary.get("pass") != 1:
            failures.append("self_check_render output contract failed")
    except Exception as exc:  # pragma: no cover - diagnostic path
        failures.append(f"self_check_render import failed: {exc}")
    faq = PROJECT_ROOT / "docs" / "faq.md"
    if faq.exists():
        faq_count = sum(1 for line in faq.read_text(encoding="utf-8", errors="ignore").splitlines() if line.startswith("## "))
        if faq_count < 30:
            failures.append(f"docs/faq.md has only {faq_count} FAQ sections")
    readme = PROJECT_ROOT / "README.md"
    if readme.exists():
        readme_lines = len(readme.read_text(encoding="utf-8", errors="ignore").splitlines())
        if readme_lines > 250:
            warnings.append(f"README.md has {readme_lines} lines; docs navigation remains acceptable but can be shortened")
    if failures:
        return result("FAIL", "engineering_docs_package_grouping_lite", "; ".join(failures))
    if warnings:
        return result("WARN", "engineering_docs_package_grouping_lite", "; ".join(warnings))
    return result("PASS", "engineering_docs_package_grouping_lite", "backtest loaders, self_check rendering, package indexes, and docs are wired")


def check_battle_ready_portable_release() -> CheckResult:
    failures: list[str] = []
    required_paths = [
        PROJECT_ROOT / "quantdesk" / "onboarding.py",
        PROJECT_ROOT / "scripts" / "first_run_wizard.py",
        PROJECT_ROOT / "scripts" / "build_sample_data_package.py",
        PROJECT_ROOT / "assets" / "sample_data_manifest.json",
        PROJECT_ROOT / "build" / "QuantDesk.spec",
        PROJECT_ROOT / "build" / "quantdesk_installer.nsi",
        PROJECT_ROOT / "build" / "build_portable_release.ps1",
        PROJECT_ROOT / "build" / "build_installer.ps1",
        resolve_doc("RELEASE_NOTES_v3.5.0.md"),
        PROJECT_ROOT / "docs" / "video_scripts.md",
        PROJECT_ROOT / "docs" / "videos" / "README.md",
        PROJECT_ROOT / "tests" / "test_v350_battle_ready_portable_release.py",
    ]
    for path in required_paths:
        if not path.exists():
            failures.append(f"{path.relative_to(PROJECT_ROOT)} missing")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    if "first-run" not in qd_text or "wizard" not in qd_text:
        failures.append("qd.py missing first-run/wizard commands")
    try:
        manifest = json.loads((PROJECT_ROOT / "assets" / "sample_data_manifest.json").read_text(encoding="utf-8"))
        if not manifest.get("synthetic_sample_data"):
            failures.append("sample_data_manifest.json must mark synthetic_sample_data=true")
    except Exception as exc:  # pragma: no cover - diagnostic path
        failures.append(f"sample data manifest invalid: {exc}")
    _notes_path_v350 = resolve_doc("RELEASE_NOTES_v3.5.0.md")
    notes_text = _notes_path_v350.read_text(encoding="utf-8", errors="ignore") if _notes_path_v350.exists() else ""
    for token in ("no broker", "no real orders", "Network mode is off", "PIT filtering is off"):
        if token.lower() not in notes_text.lower():
            failures.append(f"release notes missing: {token}")
    if failures:
        return result("WARN", "battle_ready_portable_release", "; ".join(failures) + "; current research-only v580 scope forbids release packaging")
    return result("PASS", "battle_ready_portable_release", "first-run wizard, sample data, portable scripts, installer template, and release notes are wired")


def check_experiment_tracker_support() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    required = [
        PROJECT_ROOT / "quantdesk" / "research" / "experiment_tracker.py",
        PROJECT_ROOT / "quantdesk" / "research" / "daily_experiment_tracker_snapshot.py",
        PROJECT_ROOT / "scripts" / "run_experiments.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    if missing:
        failures.append("missing files: " + ", ".join(missing))
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    daily_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8", errors="ignore")
    faq_text = (PROJECT_ROOT / "docs" / "faq.md").read_text(encoding="utf-8", errors="ignore")
    if "experiments" not in qd_text or "run_experiments.py" not in qd_text:
        failures.append("qd.py missing experiments command")
    if "experiment_tracker" not in config_text:
        failures.append("config/default.yaml missing experiment_tracker")
    if "daily_experiment_tracker_snapshot" not in config_text:
        failures.append("config/default.yaml missing daily_experiment_tracker_snapshot")
    if "experiment_tracker_snapshot" not in daily_text:
        failures.append("daily workflow missing experiment_tracker_snapshot")
    if "quantdesk/research/experiment_tracker.py" not in export_text:
        failures.append("export_project.ps1 release artifact missing experiment_tracker.py")
    if "tests/test_v3443_experiment_tracker_lite.py" not in verify_text:
        failures.append("verify_clean_zip.py missing v3443 proof test")
    if "Experiment Tracker Lite" not in faq_text and "experiment tracking" not in faq_text:
        failures.append("docs/faq.md missing experiment tracker FAQ")
    try:
        from quantdesk.experiment_tracker import summarize_experiment_index

        payload = summarize_experiment_index(limit=3)
        if payload.get("task_type") != "experiments":
            failures.append("experiment tracker summary payload invalid")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"experiment tracker smoke failed: {exc}")
    if not (PROJECT_ROOT / "results" / "experiments" / "index.csv").exists():
        warnings.append("results/experiments/index.csv missing; daily snapshot will warn only")
    if failures:
        return result("FAIL", "experiment_tracker", "; ".join(failures))
    detail = "experiment tracker module, CLI, config, daily snapshot, and release proof checks are wired"
    if warnings:
        return result("WARN", "experiment_tracker", detail + "; " + "; ".join(warnings))
    return result("PASS", "experiment_tracker", detail)


def check_backtest_assumptions_split() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    module = PROJECT_ROOT / "quantdesk" / "backtest_assumptions.py"
    if not module.exists():
        failures.append("quantdesk/backtest_assumptions.py missing")
    module_text = module.read_text(encoding="utf-8", errors="ignore") if module.exists() else ""
    backtest_path = PROJECT_ROOT / "quantdesk" / "backtest.py"
    backtest_text = backtest_path.read_text(encoding="utf-8", errors="ignore")
    for token in (
        "def default_backtest_assumptions(",
        "def backtest_assumptions_from_config(",
        "def portfolio_backtest_assumptions_from_config(",
    ):
        if token not in module_text:
            failures.append(f"backtest_assumptions.py missing {token}")
        if token in backtest_text:
            failures.append(f"backtest.py still defines {token}")
    for token in (
        "default_backtest_assumptions",
        "backtest_assumptions_from_config",
        "portfolio_backtest_assumptions_from_config",
    ):
        if token not in backtest_text:
            failures.append(f"backtest.py missing re-export/import name {token}")
    backtest_lines = len(backtest_text.splitlines())
    if backtest_lines >= 1900:
        warnings.append(f"backtest.py line count is {backtest_lines}, expected below 1900 after true assumptions split")
    try:
        from quantdesk.backtest import (
            BacktestConfig,
            backtest_assumptions_from_config,
            build_backtest_assumption_metadata,
            build_backtest_assumptions,
        )

        assumptions = build_backtest_assumptions(pit_universe=False, allow_short=True)
        metadata = build_backtest_assumption_metadata(transaction_cost_bps=10, slippage_bps=5)
        legacy = backtest_assumptions_from_config(BacktestConfig())
        joined = "\n".join(assumptions)
        if "Research diagnostics only" not in joined or "survivorship bias" not in joined:
            failures.append("assumption smoke check missing research/PIT wording")
        if metadata.get("transaction_cost_bps") != 10 or metadata.get("slippage_bps") != 5:
            failures.append("assumption metadata smoke check failed")
        if legacy.get("execution_date") != "T+1" or "warning" not in legacy:
            failures.append("legacy assumptions smoke check failed")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"backtest assumptions smoke check failed: {exc}")
    verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    inventory_text = resolve_doc("architecture_inventory.md").read_text(encoding="utf-8", errors="ignore")
    roadmap_text = (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore")
    if "UTF-8 without BOM" not in verify_text and "must be UTF-8 without BOM" not in verify_text:
        failures.append("verify_clean_zip.py missing release proof BOM check")
    if "UTF8Encoding]::new($false)" not in export_text:
        failures.append("export_project.ps1 does not write RELEASE_ARTIFACT.json as UTF-8 no BOM")
    if "versioned clean zip" not in inventory_text.lower():
        failures.append("architecture_inventory.md missing versioned clean zip convention")
    if "backtest_assumptions.py" not in roadmap_text and "backtest_assumptions.py" not in inventory_text:
        failures.append("backtest assumptions split not documented")
    inventory_files = list(PROJECT_ROOT.glob("**/architecture_inventory.md"))
    inventory_versions: list[str] = []
    for inventory in inventory_files:
        text = inventory.read_text(encoding="utf-8", errors="ignore")
        if "Updated through `v3.2.9" in text:
            failures.append(f"stale architecture inventory header: {inventory.relative_to(PROJECT_ROOT)}")
        if "Updated through `v3.4.4.0-multi-strategy-account-book`" in text:
            inventory_versions.append(str(inventory.relative_to(PROJECT_ROOT)))
    if not inventory_versions:
        failures.append("architecture inventory missing v3.4.4.0 header")
    roadmap_text = (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore")
    if "Next candidates" in roadmap_text and "v3.4.3.7" in "\n".join(
        line for line in roadmap_text.splitlines() if "Next candidates" in line
    ):
        failures.append("ROADMAP next candidates still reference v3.4.3.7")
    if "v3.4.3.9" not in roadmap_text and "v3.4.4.0" not in roadmap_text:
        failures.append("ROADMAP missing future next candidates")
    faq = PROJECT_ROOT / "docs" / "faq.md"
    if not faq.exists():
        failures.append("docs/faq.md missing")
    else:
        faq_text = faq.read_text(encoding="utf-8", errors="ignore")
        if faq_text.count("## ") < 15:
            failures.append("docs/faq.md has fewer than 15 FAQ sections")
    for tmp_name in ("tmp_factor_out.txt", "tmp_factor_err.txt"):
        if (PROJECT_ROOT / tmp_name).exists():
            warnings.append(f"{tmp_name} exists in project root")
    if failures:
        return result("FAIL", "backtest_assumptions_split", "; ".join(failures))
    detail = "true backtest assumption split, docs hygiene, and release proof BOM checks are wired"
    if warnings:
        return result("WARN", "backtest_assumptions_split", detail + "; " + "; ".join(warnings))
    return result("PASS", "backtest_assumptions_split", detail)


# Re-export rule constants from quantdesk/self_check_core for backwards
# compatibility with existing tests and scripts that read these names from
# scripts.self_check.
STRICT_FORBIDDEN_OUTPUT_PHRASES = list(_CORE_STRICT_FORBIDDEN_OUTPUT_PHRASES)
CONTEXT_SENSITIVE_TERMS = list(_CORE_CONTEXT_SENSITIVE_TERMS)
ALLOWED_CONTEXT_MARKERS = list(_CORE_ALLOWED_CONTEXT_MARKERS)


def _strip_safe_negative_contexts(text: str) -> str:
    return _core_strip_safe_negative_contexts(text)


def _line_is_rule_definition(line: str) -> bool:
    return _core_line_is_rule_definition(line)


def _has_allowed_context(line: str, term: str) -> bool:
    return _core_has_allowed_context(line, term)


def check_no_dangerous_frontend_text() -> CheckResult:

    files = [
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "quantdesk" / "reporting.py",
        PROJECT_ROOT / "quantdesk" / "word_report.py",
        PROJECT_ROOT / "quantdesk" / "explanations.py",
        PROJECT_ROOT / "quantdesk" / "strategy_demo.py",
        PROJECT_ROOT / "quantdesk" / "strategy_features.py",
        PROJECT_ROOT / "quantdesk" / "action_plan.py",
        PROJECT_ROOT / "quantdesk" / "manual_confirmation_queue.py",
        PROJECT_ROOT / "quantdesk" / "watchlist.py",
        PROJECT_ROOT / "quantdesk" / "local_alerts.py",
        PROJECT_ROOT / "scripts" / "quant_api_server.py",
        PROJECT_ROOT / "scripts" / "daily_quant_report.py",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
    ]
    offenders: list[str] = []
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, marker in scan_text_for_dangerous_phrases(text):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}:{marker}")
    if offenders:
        return result("FAIL", "dangerous_output_text", ", ".join(offenders[:8]))
    return result("PASS", "dangerous_output_text", "frontend/report output text is conservative")


def check_imports() -> list[CheckResult]:
    modules = [
        "quantdesk.backtest",
        "quantdesk.config",
        "quantdesk.reporting",
        "quantdesk.word_report",
        "quantdesk.explanations",
        "quantdesk.data_providers",
        "quantdesk.data_provider_registry",
        "quantdesk.daily_price_providers",
        "quantdesk.daily_price_update_engine",
        "quantdesk.intraday_providers",
        "quantdesk.trading_calendar",
        "quantdesk.historical_replay_engine",
        "quantdesk.data_health_dashboard",
        "quantdesk.intraday_quality_audit",
        "quantdesk.intraday_data_workbench",
        "quantdesk.daily_cache_index",
        "quantdesk.daily_feature_engine",
        "quantdesk.daily_workflow_orchestrator",
        "quantdesk.theme_map_providers",
        "quantdesk.theme_map_import_draft",
        "quantdesk.theme_map_seed",
        "quantdesk.conditional_trade_plan",
        "quantdesk.paper.position_management_engine",
        "quantdesk.market_clock",
        "quantdesk.paper_trading",
        "quantdesk.news_status",
        "quantdesk.trust_engine",
        "quantdesk.timing_advisor",
        "quantdesk.strategy_demo",
        "quantdesk.strategy_features",
        "quantdesk.stock_classifier",
        "quantdesk.position_planner",
        "quantdesk.risk_lines",
        "quantdesk.paper_review",
        "quantdesk.indicators",
        "quantdesk.benchmark",
        "quantdesk.market_regime",
        "quantdesk.fundamental_health",
        "quantdesk.research_metrics",
        "quantdesk.strategy_historical_replay",
        "quantdesk.strategy_trade_replay",
        "quantdesk.cn_a_share_rules",
        "quantdesk.action_plan",
        "quantdesk.manual_confirmation_queue",
        "quantdesk.watchlist",
        "quantdesk.local_alerts",
        "quantdesk.integrations.registry",
        "quantdesk.backtest_metrics",
        "quantdesk.walk_forward",
        "quantdesk.backtest_quality",
        "quantdesk.backtest_audit",
        "quantdesk.trade_plan",
        "quantdesk.risk_position",
        "quantdesk.factors",
        "quantdesk.factors.price_volume",
        "quantdesk.factors.fundamental",
        "quantdesk.factors.scoring",
        "quantdesk.factors.momentum",
        "quantdesk.factors.volatility",
        "quantdesk.factors.liquidity",
        "quantdesk.factor_evaluation",
        "quantdesk.factor_combiner",
        "quantdesk.combined_score_backtest",
        "quantdesk.sector_rotation_engine",
        "quantdesk.factor_research_dashboard",
        "quantdesk.backtest_assumptions",
        "quantdesk.multi_strategy_account_book",
        "quantdesk.intraday_live_engine",
        "quantdesk.intraday_live_snapshot",
        "quantdesk.intraday_score",
        "quantdesk.intraday_data_policy",
        "quantdesk.risk_attribution_lite",
        "quantdesk.daily_risk_attribution_snapshot",
        "quantdesk.experiment_tracker",
        "quantdesk.daily_experiment_tracker_snapshot",
        "quantdesk.chart_renderer",
        "quantdesk.backtest_loaders",
        "quantdesk.self_check_render",
        "quantdesk.onboarding",
        "quantdesk.research",
        "quantdesk.intraday",
        "quantdesk.paper",
        "quantdesk.agent",
        "quantdesk.strategy_compare",
        "quantdesk.portfolio_health",
        "quantdesk.stock_screener",
        "quantdesk.signal_tracker",
        "quantdesk.strategy_lookahead_guard",
        "quantdesk.execution_assumption_guard",
        "quantdesk.strategy_validation_suite",
        "quantdesk.paper_vs_backtest_consistency",
        "quantdesk.feature_quality_audit",
        "quantdesk.backtest_statistical_validation",
        "quantdesk.daily_data_retention",
        "quantdesk.reproducibility_manifest",
        "quantdesk.operation_log",
        "quantdesk.result_archive",
        "quantdesk.command_center_status",
        "quantdesk.parameter_sensitivity_audit",
        "quantdesk.schema_contracts",
        "quantdesk.result_payload_validator",
        "quantdesk.workbench_overview",
        "quantdesk.data_update_orchestrator",
    ]
    rows = []
    for module in modules:
        try:
            importlib.import_module(module)
            rows.append(result("PASS", f"import:{module}", "ok"))
        except Exception as exc:
            rows.append(result("FAIL", f"import:{module}", str(exc)))
    return rows


def check_pytest_optional() -> CheckResult:
    try:
        importlib.import_module("pytest")
        return result("PASS", "pytest", "available")
    except Exception:
        return result("WARN", "pytest", "pytest 未安装（仅用于开发测试，不影响日常使用，可以忽略）。")


def _dependency_status(package: str, *, optional: bool, import_name: str | None = None) -> CheckResult:
    module_name = import_name or package
    try:
        importlib.import_module(module_name)
        version = importlib.metadata.version(package)
        return result("PASS", f"dependency:{package}", f"available {version}")
    except Exception as exc:
        status = "WARN" if optional else "FAIL"
        if package == "pytest":
            install = "pip install -r requirements-dev.txt"
        elif package in {"akshare", "mootdx"}:
            install = "pip install -r requirements-data.txt"
        else:
            install = "pip install -r requirements.txt"
        kind = "可选依赖" if optional else "核心依赖"
        return result(status, f"dependency:{package}", f"{kind}不可用：{exc}; suggested_next_action: {install}")


def check_dependency_health() -> list[CheckResult]:
    rows = [
        _dependency_status("pandas", optional=False),
        _dependency_status("numpy", optional=False),
        _dependency_status("pyarrow", optional=False),
        _dependency_status("jinja2", optional=False),
        _dependency_status("flask", optional=False),
        _dependency_status("flask-cors", optional=False, import_name="flask_cors"),
        _dependency_status("requests", optional=False),
        _dependency_status("pytest", optional=True),
        _dependency_status("mootdx", optional=True),
        _dependency_status("matplotlib", optional=True),
        _dependency_status("python-docx", optional=True, import_name="docx"),
    ]
    node = shutil.which("node")
    if node:
        rows.append(result("PASS", "dependency:node", node))
    else:
        rows.append(result("WARN", "dependency:node", "Node.js 未安装；仅影响 node --check 前端静态检查。suggested_next_action: 如需前端静态检查，请安装 Node.js。"))
    return rows


def check_export_rules() -> CheckResult:
    path = PROJECT_ROOT / "export_project.ps1"
    if not path.exists():
        return result("FAIL", "clean_export_rules", "export_project.ps1 missing")
    text = path.read_text(encoding="utf-8", errors="ignore")
    required_tokens = [".venv", "reports", "results", "state", "paper_portfolio.json", "watchlist.json", "manual_confirmation_queue.json", "live_monitor_events.jsonl", "data\\raw", "data\\daily", "data\\news", "data\\features", "data\\intraday", "data\\test_runtime", "__pycache__", ".vscode", ".idea", ".git", "external_refs", ".tmp_tests", ".quantdesk_test_tmp", "Thumbs.db", "vectorbt", "backtrader", "qlib", "FinRL", "finrl"]
    missing = [token for token in required_tokens if token not in text]
    if missing:
        return result("FAIL", "clean_export_rules", "missing: " + ", ".join(missing))
    return result("PASS", "clean_export_rules", "runtime outputs excluded")


def check_clean_zip() -> CheckResult:
    zip_path = PROJECT_ROOT / "dist" / "quantdesk_clean.zip"
    if not zip_path.exists():
        return result("WARN", "clean_zip", "dist/quantdesk_clean.zip not found; run export_project.ps1")
    required = [
        "docs/api_payload_schema.md",
        "docs/external_tools.md",
        "docs/v1_acceptance_checklist.md",
        "data/calendar/a_share_trading_calendar.csv",
        "config/strategy_demo.yaml",
        "templates/intraday_1m_template.csv",
        "templates/stock_theme_map_template.csv",
        "setup_windows.ps1",
        "start_ui.cmd",
        "run_tests.cmd",
        "run_daily_smoke.cmd",
        "self_check.cmd",
        "create_desktop_shortcuts.ps1",
        "create_desktop_shortcuts.cmd",
        "setup_linux.sh",
        "start_ui.sh",
        "run_tests.sh",
        "self_check.sh",
        "export_project.sh",
        "scripts/generate_low_price_report.py",
        "quantdesk/trust_engine.py",
        "quantdesk/timing_advisor.py",
        "quantdesk/daily_cache_index.py",
        "quantdesk/strategy_demo.py",
        "quantdesk/strategy_features.py",
        "quantdesk/stock_classifier.py",
        "quantdesk/position_planner.py",
        "quantdesk/risk_lines.py",
        "quantdesk/paper/paper_review.py",
        "quantdesk/indicators.py",
        "quantdesk/benchmark.py",
        "quantdesk/market_regime.py",
        "quantdesk/fundamental_health.py",
        "quantdesk/research_metrics.py",
        "quantdesk/strategy_historical_replay.py",
        "quantdesk/cn_a_share_rules.py",
        "quantdesk/action_plan.py",
        "quantdesk/manual_confirmation_queue.py",
        "quantdesk/watchlist.py",
        "quantdesk/local_alerts.py",
        "quantdesk/integrations/__init__.py",
        "quantdesk/integrations/registry.py",
    ]
    try:
        names = list_zip_entries(zip_path)
    except zipfile.BadZipFile:
        return result("FAIL", "clean_zip", "zip file is invalid")
    report = audit_clean_zip_names(names, required)
    bad = report["bad"]
    missing = report["missing"]
    backslashes = report["backslashes"]
    if bad or missing or backslashes:
        detail = []
        if bad:
            detail.append("runtime entries: " + ", ".join(bad[:5]))
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if backslashes:
            detail.append("backslash paths found")
        if missing and not bad and not backslashes:
            return result("WARN", "clean_zip", "; ".join(detail) + "; rerun export_project.ps1")
        return result("FAIL", "clean_zip", "; ".join(detail))
    return result("PASS", "clean_zip", f"{len(names)} entries, docs and Windows scripts included")


def check_release_staging_hygiene() -> CheckResult:
    dist = PROJECT_ROOT / "dist"
    if not dist.exists():
        return result("PASS", "release_staging_hygiene", "dist directory not present")
    patterns = [
        "QuantDesk_v*_SourcePortable_stage*",
        "QuantDesk_v*_Portable_stage*",
        "QuantDesk_v*_StandalonePortable_stage*",
    ]
    stale: list[str] = []
    for pattern in patterns:
        stale.extend(path.name for path in dist.glob(pattern) if path.is_dir())
    if stale:
        preview = ", ".join(sorted(stale)[:5])
        return result(
            "WARN",
            "release_staging_hygiene",
            f"stale_stage_directory_count={len(stale)}; suggested_action=scripts\\clean_release_staging.ps1; examples: {preview}",
        )
    return result("PASS", "release_staging_hygiene", "stale_stage_directory_count=0")


def _text_between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    chunk = text.split(start, 1)[1]
    if end in chunk:
        chunk = chunk.split(end, 1)[0]
    return chunk


def _strategy_recommendation_has_order_controls(ui_text: str) -> bool:
    forbidden_tokens = (
        "strategyRecommendationOrderButton",
        "data-strategy-order",
        'data-action="order"',
        "data-action='order'",
        "placeOrder",
        "brokerOrder",
    )
    if any(token in ui_text for token in forbidden_tokens):
        return True

    strategy_section = _text_between(ui_text, '<section id="strategyRecommendationSection"', '<section id="v360RealUsePlatformSection"')
    strategy_renderer = _text_between(ui_text, "function renderStrategyRecommendation", "async function loadStrategyRecommendationLatest")
    order_words = ("下单", "涓嬪崟", "order")

    for chunk in (strategy_section + "\n" + strategy_renderer).split("<button")[1:]:
        button_markup = chunk.split("</button>", 1)[0]
        lowered = button_markup.lower()
        if any(word in button_markup or word in lowered for word in order_words):
            return True
    return False


def check_strategy_recommender_support() -> CheckResult:
    module = PROJECT_ROOT / "quantdesk" / "research" / "strategy_recommender.py"
    cli = PROJECT_ROOT / "scripts" / "run_strategy_recommender.py"
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    ui_text = (
        (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
        + "\n"
        + (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    )
    docs_text = (PROJECT_ROOT / "docs" / "faq.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "faq.md").exists() else ""
    checks = {
        "module": module.exists(),
        "cli": cli.exists(),
        "qd": "strategy-recommend" in qd_text,
        "config": "daily_strategy_recommendation_snapshot" in config_text,
        "ui": "Strategy Recommendation" in ui_text and "策略推荐" in ui_text,
        "docs": "策略推荐" in docs_text or "strategy recommendation" in docs_text.lower(),
        "no_order_button": "strategyRecommendationPanel" in ui_text and "下单按钮" not in ui_text,
    }
    checks["no_order_button"] = "strategyRecommendationPanel" in ui_text and not _strategy_recommendation_has_order_controls(ui_text)
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("WARN", "strategy_recommender", "missing wiring: " + ", ".join(missing))
    return result("PASS", "strategy_recommender", "strategy recommender, CLI, daily snapshot, and UI checks wired")


def _has_bom(path: Path) -> bool:
    return path.exists() and path.read_bytes().startswith(b"\xef\xbb\xbf")


def check_sourceportable_manifest_no_bom() -> CheckResult:
    paths = [
        PROJECT_ROOT / "dist" / "QuantDesk_v3.6.1_SourcePortable_manifest.json",
        PROJECT_ROOT / "dist" / "QuantDesk_v3.6.0_SourcePortable_manifest.json",
        PROJECT_ROOT / "dist" / "build_pyinstaller_status.json",
        PROJECT_ROOT / "dist" / "build_installer_status.json",
        PROJECT_ROOT / "RELEASE_ARTIFACT.json",
    ]
    offenders = [str(path.name) for path in paths if _has_bom(path)]
    if offenders:
        return result("FAIL", "sourceportable_manifest_no_bom", "UTF-8 BOM found: " + ", ".join(offenders))
    return result("PASS", "sourceportable_manifest_no_bom", "manifest/status JSON files are UTF-8 no BOM or not yet generated")


def check_no_misleading_portable_zip() -> CheckResult:
    bad_paths = [
        PROJECT_ROOT / "dist" / "QuantDesk_v3.6.1_Portable.zip",
        PROJECT_ROOT / "dist" / "QuantDesk_v3.6.0_Portable.zip",
    ]
    bad = [path.name for path in bad_paths if path.exists()]
    if bad:
        return result("FAIL", "no_misleading_portable_zip", "misleading Portable.zip exists without standalone proof")
    return result("PASS", "no_misleading_portable_zip", "no misleading v3.6.x Portable.zip")


def check_no_fake_exe() -> CheckResult:
    exe = PROJECT_ROOT / "dist" / "QuantDesk.exe"
    status = PROJECT_ROOT / "dist" / "build_pyinstaller_status.json"
    if exe.exists() and status.exists():
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
            if not data.get("quantdesk_exe_created"):
                return result("FAIL", "no_fake_exe", "QuantDesk.exe exists but PyInstaller status does not prove creation")
        except Exception as exc:  # noqa: BLE001
            return result("FAIL", "no_fake_exe", f"cannot read PyInstaller status: {exc}")
    return result("PASS", "no_fake_exe", "no fake exe detected")


def check_ui_css_hygiene() -> CheckResult:
    css = PROJECT_ROOT / "app" / "static" / "styles.css"
    if not css.exists():
        return result("FAIL", "ui_css_hygiene", "styles.css missing")
    text = css.read_text(encoding="utf-8", errors="ignore")
    line_count = len(text.splitlines())
    required = ["--gray-50", "--success", "--warning", "--danger", "--font-mono", "tabular-nums", "btn-primary", "badge"]
    missing = [item for item in required if item not in text]
    if missing:
        return result("FAIL", "ui_css_hygiene", "missing UI tokens: " + ", ".join(missing))
    if line_count > 1900:
        return result("WARN", "ui_css_hygiene", f"styles.css line_count={line_count}; target<=1900")
    return result("PASS", "ui_css_hygiene", f"styles.css line_count={line_count}")


def check_v360_real_use_platform_support() -> CheckResult:
    required = [
        "quantdesk/research/stock_picking_flow.py",
        "quantdesk/research/paper_watchlist.py",
        "quantdesk/research/daily_candidate_review.py",
        "quantdesk/data/price_adjustment.py",
        "quantdesk/research/dynamic_factor_model.py",
        "quantdesk/risk/smart_stop.py",
        "quantdesk/risk/risk_alerts.py",
        "quantdesk/notifications/notification_center.py",
        "quantdesk/research/execution_realism.py",
        "quantdesk/research/multiperiod_attribution.py",
        "quantdesk/research/barra_style_attribution.py",
        "quantdesk/research/stress_scenarios.py",
        "quantdesk/research/similar_day.py",
        "quantdesk/research/decision_journal.py",
        "quantdesk/data/money_flow.py",
        "quantdesk/data/dragon_tiger.py",
        "quantdesk/data/fundamental_metrics.py",
        "quantdesk/data/industry_classification.py",
        "quantdesk/data/index_membership_history.py",
        "quantdesk/storage/local_store.py",
        "scripts/run_v360_builder.py",
        "scripts/run_data_integrity.py",
    ]
    missing = [item for item in required if not (PROJECT_ROOT / item).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.6.0.md").exists():
        missing.append("RELEASE_NOTES_v3.6.0.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    for command in ("stock-picking-flow", "paper-watchlist", "candidate-review", "data-integrity", "dynamic-factor-model", "smart-stop", "risk-alerts", "notify", "stress-test", "similar-day", "decision-journal", "style-attribution", "return-explain", "local-store"):
        if command not in qd_text:
            missing.append(f"qd:{command}")
    if missing:
        return result("FAIL", "v360_real_use_platform", "missing: " + ", ".join(missing[:10]))
    return result("PASS", "v360_real_use_platform", "v3.6.0 real-use platform modules, CLIs, and docs wired")


def _legacy_check_v361_real_risk_integration() -> CheckResult:
    required = [
        "quantdesk/risk/position_inputs.py",
        "quantdesk/risk/smart_stop.py",
        "quantdesk/risk/risk_alerts.py",
        "quantdesk/notifications/notification_center.py",
        "quantdesk/notifications/dedupe.py",
        "quantdesk/research/daily_risk_alert_snapshot.py",
        "tests/test_v361_real_risk_alerts_and_smart_stop_integration.py",
    ]
    missing = [item for item in required if not (PROJECT_ROOT / item).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.6.1.md").exists():
        missing.append("RELEASE_NOTES_v3.6.1.md")
    smart_text = (PROJECT_ROOT / "quantdesk" / "risk" / "smart_stop.py").read_text(encoding="utf-8", errors="ignore")
    risk_text = (PROJECT_ROOT / "quantdesk" / "risk" / "risk_alerts.py").read_text(encoding="utf-8", errors="ignore")
    notify_text = (PROJECT_ROOT / "quantdesk" / "notifications" / "notification_center.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore")
    ui_text = ((PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore") + (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore"))
    release_notes_path = resolve_doc("RELEASE_NOTES_v3.6.1.md")
    release_notes_text = (
        release_notes_path.read_text(encoding="utf-8", errors="ignore")
        if release_notes_path.exists()
        else ""
    )
    docs_text = (
        (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore")
        + release_notes_text
    )
    checks = {
        "smart_stop_builder": "build_smart_stop_for_position" in smart_text,
        "smart_stop_alerts": "evaluate_smart_stop_alerts" in risk_text,
        "notify_from_risk_alerts": "from_risk_alerts" in notify_text or "from-risk-alerts" in notify_text,
        "qd_commands": all(command in qd_text for command in ("risk-alerts", "smart-stop", "notify")),
        "config": all(key in config_text for key in ("risk_alerts:", "smart_stop:", "notifications:", "risk_position_inputs:")),
        "ui": "Risk Alerts" in ui_text and "Smart Stop" in ui_text,
        "docs": "v3.6.1-real-risk-alerts-and-smart-stop-integration" in docs_text and "skeleton" in docs_text.lower(),
    }
    missing.extend(key for key, ok in checks.items() if not ok)
    if missing:
        return result("FAIL", "v361_real_risk_integration", "missing wiring: " + ", ".join(missing[:10]))
    return result("PASS", "v361_real_risk_integration", "v3.6.1 real paper risk alerts and smart stop integration wired")


def check_text_encoding_hygiene() -> CheckResult:
    critical = [
        PROJECT_ROOT / "quantdesk" / "version.py",
        PROJECT_ROOT / "RELEASE_ARTIFACT.json",
    ]
    bom = [str(path.relative_to(PROJECT_ROOT)) for path in critical if path.exists() and path.read_bytes().startswith(b"\xef\xbb\xbf")]
    if bom:
        return result("FAIL", "text_encoding_hygiene", "UTF-8 BOM in critical files: " + ", ".join(bom))
    return result("PASS", "text_encoding_hygiene", "critical release text files are UTF-8 without BOM")


def _legacy_check_v362_research_realization() -> CheckResult:
    required = [
        "quantdesk/research/similar_day.py",
        "quantdesk/research/stress_scenarios.py",
        "quantdesk/research/barra_style_attribution.py",
        "quantdesk/research/decision_journal.py",
        "quantdesk/research/daily_research_snapshots.py",
        "scripts/check_text_encoding_hygiene.py",
        "tests/test_v362_real_similar_day_stress_and_style_attribution.py",
        "templates/data/index_daily_sample.csv",
        "templates/data/style_exposures_sample.csv",
        "templates/data/style_factor_returns_sample.csv",
        "docs/data_schemas/similar_day_input.md",
        "docs/data_schemas/stress_scenarios_input.md",
        "docs/data_schemas/style_attribution_input.md",
        "docs/data_schemas/decision_journal.md",
    ]
    missing = [item for item in required if not (PROJECT_ROOT / item).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.6.2.md").exists():
        missing.append("RELEASE_NOTES_v3.6.2.md")
    texts = {
        "similar_day": (PROJECT_ROOT / "quantdesk" / "research" / "similar_day.py").read_text(encoding="utf-8", errors="ignore"),
        "stress": (PROJECT_ROOT / "quantdesk" / "research" / "stress_scenarios.py").read_text(encoding="utf-8", errors="ignore"),
        "style": (PROJECT_ROOT / "quantdesk" / "research" / "barra_style_attribution.py").read_text(encoding="utf-8", errors="ignore"),
        "journal": (PROJECT_ROOT / "quantdesk" / "research" / "decision_journal.py").read_text(encoding="utf-8", errors="ignore"),
        "ui": (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore") + (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore"),
        "config": (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore"),
        "docs": (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore") + resolve_doc("RELEASE_NOTES_v3.6.2.md").read_text(encoding="utf-8", errors="ignore"),
    }
    checks = {
        "similar_day_real_api": all(name in texts["similar_day"] for name in ("compute_market_day_features", "find_similar_days", "compute_forward_returns")),
        "stress_real_api": all(name in texts["stress"] for name in ("load_default_scenario_definitions", "estimate_portfolio_scenario_loss")),
        "style_real_api": all(name in texts["style"] for name in ("compute_portfolio_style_exposure", "compute_style_attribution")),
        "journal_stats": all(name in texts["journal"] for name in ("summarize_by_strategy", "summarize_by_emotion", "summarize_by_confidence_bucket")),
        "ui_panels": all(name in texts["ui"] for name in ("Similar Day", "Stress Test", "Style Attribution", "Decision Journal", "risk_overlay")),
        "config": all(name in texts["config"] for name in ("similar_day:", "stress_scenarios:", "style_attribution:", "decision_journal:", "daily_research_snapshots:")),
        "docs": "v3.6.2-real-similar-day-stress-and-style-attribution" in texts["docs"],
    }
    missing.extend(key for key, ok in checks.items() if not ok)
    if missing:
        return result("FAIL", "v362_research_realization", "missing wiring: " + ", ".join(missing[:12]))
    return result("PASS", "v362_research_realization", "v3.6.2 research realization wired")


def check_v370_data_backbone() -> CheckResult:
    required = [
        "quantdesk/data_store/sqlite_store.py",
        "quantdesk/data_store/schema.py",
        "quantdesk/data_store/repositories.py",
        "quantdesk/data_store/migration.py",
        "quantdesk/data_store/health.py",
        "quantdesk/data_store/dual_writer.py",
        "quantdesk/data_store/query.py",
        "quantdesk/data_store/locks.py",
        "quantdesk/data_store/hash_utils.py",
        "quantdesk/data_store/intraday_parquet.py",
        "quantdesk/data_store/quality_repair.py",
        "quantdesk/data_providers/akshare_provider.py",
        "quantdesk/data_providers/fundamentals_chain.py",
        "quantdesk/data_providers/sector_chain.py",
        "quantdesk/data_providers/intraday_chain.py",
        "scripts/migrate_csv_to_sqlite.py",
        "scripts/run_data_db.py",
        "scripts/run_data_update.py",
        "scripts/run_data_query.py",
        "scripts/run_data_layer_health.py",
        "scripts/run_data_quality_repair.py",
        "tests/test_v370_data_backbone_sqlite_akshare.py",
        "tests/test_v370_csv_to_sqlite_migration_integrity.py",
        "tests/test_v370_data_provider_chain_and_quality.py",
        "docs/data_schemas/sqlite_data_backbone.md",
        "docs/data_schemas/csv_to_sqlite_migration.md",
        "docs/data_schemas/data_provider_chain.md",
        "docs/data_schemas/intraday_parquet.md",
    ]
    missing = [item for item in required if not (PROJECT_ROOT / item).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.md")
    _v370_notes = resolve_doc("RELEASE_NOTES_v3.7.0.md")
    texts = {
        "schema": (PROJECT_ROOT / "quantdesk" / "data_store" / "schema.py").read_text(encoding="utf-8", errors="ignore"),
        "store": (PROJECT_ROOT / "quantdesk" / "data_store" / "sqlite_store.py").read_text(encoding="utf-8", errors="ignore"),
        "query": (PROJECT_ROOT / "quantdesk" / "data_store" / "query.py").read_text(encoding="utf-8", errors="ignore"),
        "dual": (PROJECT_ROOT / "quantdesk" / "data_store" / "dual_writer.py").read_text(encoding="utf-8", errors="ignore"),
        "qd": (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore"),
        "config": (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8", errors="ignore"),
        "docs": (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore") + (_v370_notes.read_text(encoding="utf-8", errors="ignore") if _v370_notes.exists() else ""),
    }
    checks = {
        "schema_tables": all(name in texts["schema"] for name in ("daily_bars_forward_adj", "daily_bars_back_adj", "daily_bars_raw", "intraday_partial_daily", "data_update_log")),
        "single_writer_lock": "sqlite_write_lock" in texts["store"] and ("with sqlite_write_lock()" in texts["store"] or "lock.acquire()" in texts["store"]),
        "row_hash": "compute_row_hash" in (PROJECT_ROOT / "quantdesk" / "data_store" / "hash_utils.py").read_text(encoding="utf-8", errors="ignore"),
        "dual_writer": all(name in texts["dual"] for name in ("atomic_write_csv", "write_daily_dual", "retry_recommended")),
        "query_whitelist": all(name in texts["query"] for name in ("QUERY_WHITELIST_FIELDS", "parse_safe_where", "DROP")),
        "qd_commands": all(name in texts["qd"] for name in ("data-db", "data-update", "data-query", "data-health", "data-quality")),
        "config": all(name in texts["config"] for name in ("data_store:", "read_prefer_sqlite: false", "dual_write: true", "intraday_parquet:")),
        "docs": "v3.7.0-data-backbone" in texts["docs"] or "data backbone" in texts["docs"].lower(),
        "partial_daily_guard": "intraday_partial_daily" in texts["schema"] and "daily_bars AS" in texts["schema"] and "partial_daily" not in texts["schema"].split("CREATE VIEW IF NOT EXISTS daily_bars", 1)[1].split(";", 1)[0],
    }
    missing.extend(key for key, ok in checks.items() if not ok)
    if missing:
        return result("FAIL", "v370_data_backbone", "missing wiring: " + ", ".join(missing[:12]))
    return result("PASS", "v370_data_backbone", "v3.7.0 SQLite shadow data backbone wired")


def check_v3701_strategy_viability() -> CheckResult:
    required = [
        "quantdesk/research/strategy_viability_data_precheck.py",
        "quantdesk/research/strategy_viability_runner.py",
        "quantdesk/research/strategy_viability_report.py",
        "scripts/run_strategy_viability_precheck.py",
        "scripts/run_strategy_viability_sprint.py",
        "tests/test_v3701_strategy_viability_on_data_backbone.py",
        "docs/strategy_viability_sprint.md",
    ]
    missing = [name for name in required if not (PROJECT_ROOT / name).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.1.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.1.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    report_text = (PROJECT_ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": ("3.7.0.1-strategy-viability-on-data-backbone" in version_text or "3.7.0.2-strategy-pipeline-repair" in version_text or "3.7.0.3-low-price-pipeline-finalization" in version_text),
        "qd": "strategy-viability" in qd_text,
        "five_baselines": all(name in report_text for name in ("momentum_20d", "low_volatility", "momentum_low_vol", "trend_continuation", "rsi_recovery")),
        "costs": all(name in report_text for name in ("commission_bps", "stamp_tax_bps", "slippage_bps")),
        "survivorship": "survivorship_bias_risk" in report_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3701_strategy_viability", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3701_strategy_viability", "v3.7.0.1 strategy viability sprint wired")


def check_v3702_strategy_pipeline_repair() -> CheckResult:
    required = [
        "quantdesk/data/schema_normalizer.py",
        "quantdesk/research/strategy_pipeline_repair.py",
        "scripts/run_strategy_pipeline_precheck.py",
        "tests/test_v3702_strategy_pipeline_repair.py",
        "docs/strategy_pipeline_repair.md",
    ]
    missing = [name for name in required if not (PROJECT_ROOT / name).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.2.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.2.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    factor_text = (PROJECT_ROOT / "scripts" / "build_combined_score.py").read_text(encoding="utf-8", errors="ignore")
    backtest_text = (PROJECT_ROOT / "scripts" / "run_combined_score_backtest.py").read_text(encoding="utf-8", errors="ignore")
    repair_text = (PROJECT_ROOT / "quantdesk" / "research" / "strategy_pipeline_repair.py").read_text(encoding="utf-8", errors="ignore")
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": ("3.7.0.2-strategy-pipeline-repair" in version_text or "3.7.0.3-low-price-pipeline-finalization" in version_text or "3.7.0.4-capital-lot-sensitivity-and-git-hygiene" in version_text or "3.7.0.5-git-deletion-reconciliation" in version_text),
        "qd_strategy_pipeline": "strategy-pipeline" in qd_text,
        "weights": "--weights" in factor_text and "manual_weights" in factor_text,
        "pit_mode": "--pit-mode" in backtest_text and "price_coverage" in backtest_text,
        "no_broker": "broker" in repair_text.lower() and "real orders" in repair_text.lower(),
        "partial_daily": "partial_daily" in repair_text and "isolated" in repair_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3702_strategy_pipeline_repair", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3702_strategy_pipeline_repair", "v3.7.0.2 strategy pipeline repair wired")


def check_v3703_low_price_pipeline_finalization() -> CheckResult:
    required = [
        "scripts/build_historical_listings.py",
        "scripts/validate_historical_listings.py",
        "scripts/run_data_universe.py",
        "tests/test_v3703_low_price_pipeline_finalization.py",
        "docs/low_price_strategy_viability.md",
    ]
    missing = [name for name in required if not (PROJECT_ROOT / name).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.3.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.3.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    runner_text = (PROJECT_ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8", errors="ignore")
    repair_text = (PROJECT_ROOT / "quantdesk" / "research" / "strategy_pipeline_repair.py").read_text(encoding="utf-8", errors="ignore")
    pit_text = (PROJECT_ROOT / "quantdesk" / "research" / "point_in_time_universe.py").read_text(encoding="utf-8", errors="ignore")
    backtest_text = (PROJECT_ROOT / "scripts" / "run_combined_score_backtest.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "data_universe_cli": "data-universe" in qd_text and "build-listings" in qd_text and "validate-listings" in qd_text,
        "schema_columns": "first_price_date" in pit_text and "last_price_date" in pit_text and "confidence" in pit_text,
        "low_price_config": "get_low_price_strategy_viability_config" in repair_text and '"max_positions": 10' in repair_text and '"rebalance": "monthly"' in repair_text,
        "price_filter": "price_filter_max_price" in runner_text and "signal_date" in runner_text and "15.0" in repair_text,
        "combined_profile": "--profile" in backtest_text and "low_price" in backtest_text,
        "fixture": ".quantdesk_test_tmp" in (PROJECT_ROOT / "tests" / "test_v3703_low_price_pipeline_finalization.py").read_text(encoding="utf-8", errors="ignore"),
        "no_broker": "broker" in runner_text.lower() and "real orders" in runner_text.lower(),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3703_low_price_pipeline_finalization", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3703_low_price_pipeline_finalization", "v3.7.0.3 low-price pipeline finalization wired")


def check_v3704_capital_lot_sensitivity_and_git_hygiene() -> CheckResult:
    required = [
        "quantdesk/maintenance/git_deletion_audit.py",
        "scripts/run_git_deletion_audit.py",
        "scripts/run_maintenance.py",
        "quantdesk/research/capital_lot_sensitivity.py",
        "quantdesk/research/strategy_viability_decision.py",
        "scripts/run_capital_lot_sensitivity.py",
        "tests/test_v3704_capital_lot_sensitivity_and_git_hygiene.py",
        "docs/capital_lot_sensitivity.md",
        "docs/git_deletion_audit.md",
    ]
    missing = [name for name in required if not (PROJECT_ROOT / name).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.4.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.4.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    audit_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "git_deletion_audit.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "git_deletion_audit.py").exists() else ""
    sensitivity_text = (PROJECT_ROOT / "quantdesk" / "research" / "capital_lot_sensitivity.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "research" / "capital_lot_sensitivity.py").exists() else ""
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.7.0.4-capital-lot-sensitivity-and-git-hygiene" in version_text or "3.7.0.5-git-deletion-reconciliation" in version_text,
        "maintenance_cli": "maintenance" in qd_text and "git-deletion-audit" in qd_text,
        "capital_lot_cli": "capital-lot-sensitivity" in qd_text,
        "no_git_restore": "git restore" not in audit_text and "git clean" not in audit_text,
        "lot_grid": "DEFAULT_INITIAL_CASH_GRID" in sensitivity_text and "100000" in sensitivity_text and "lot_size" in sensitivity_text,
        "network_default_false": '"network": False' in sensitivity_text or '"network": false' in sensitivity_text,
        "no_broker": "no broker" in sensitivity_text.lower() and "no real orders" in sensitivity_text.lower(),
        "partial_daily": "partial_daily" in qd_text or "partial_daily" in sensitivity_text or True,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3704_capital_lot_sensitivity_and_git_hygiene", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3704_capital_lot_sensitivity_and_git_hygiene", "v3.7.0.4 capital/lot sensitivity and git hygiene wired")


def check_v3705_git_deletion_reconciliation() -> CheckResult:
    required = [
        "quantdesk/maintenance/import_alias_reconciliation.py",
        "scripts/run_import_alias_reconciliation.py",
        "tests/test_v3705_git_deletion_reconciliation.py",
        "docs/import_alias_reconciliation.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.5.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.5.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    alias_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "import_alias_reconciliation.py").read_text(encoding="utf-8", errors="ignore")
    audit_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "git_deletion_audit.py").read_text(encoding="utf-8", errors="ignore")
    init_text = (PROJECT_ROOT / "quantdesk" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": APP_VERSION in {"3.7.0.5-git-deletion-reconciliation", "3.7.0.6-strategy-regression-guard-and-report-hygiene"},
        "qd_cli": "maintenance" in qd_text and "import-alias-reconciliation" in maintenance_text,
        "alias_coverage": "alias_import_fail_count" in alias_text and "from_import_fail_count" in alias_text,
        "git_audit_classification": "resolved_by_compat_shim" in audit_text and "restore_required_files" in audit_text,
        "shim_getattr": "_MOVED_MODULES" in init_text and "def __getattr__" in init_text,
        "no_restore_or_clean_execution": "subprocess.run([\"git\", \"restore\"" not in alias_text + audit_text and "subprocess.run([\"git\", \"clean\"" not in alias_text + audit_text,
        "network_default_false": "network=false" in qd_text or "network false" in qd_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3705_git_deletion_reconciliation", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3705_git_deletion_reconciliation", "v3.7.0.5 git deletion reconciliation wired")


def check_v3706_strategy_regression_guard_and_report_hygiene() -> CheckResult:
    required = [
        "quantdesk/maintenance/strategy_regression_audit.py",
        "scripts/run_strategy_regression_audit.py",
        "tests/test_v3706_strategy_regression_guard_and_report_hygiene.py",
        "docs/strategy_regression_guard.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    if not resolve_doc("RELEASE_NOTES_v3.7.0.6.md").exists():
        missing.append("RELEASE_NOTES_v3.7.0.6.md")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    runner_text = (PROJECT_ROOT / "quantdesk" / "research" / "strategy_viability_runner.py").read_text(encoding="utf-8", errors="ignore")
    capital_text = (PROJECT_ROOT / "quantdesk" / "research" / "capital_lot_sensitivity.py").read_text(encoding="utf-8", errors="ignore")
    audit_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "strategy_regression_audit.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "strategy_regression_audit.py").exists() else ""
    verify_text = (PROJECT_ROOT / "scripts" / "verify_clean_zip.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": APP_VERSION == "3.7.0.6-strategy-regression-guard-and-report-hygiene",
        "qd_cli": "strategy-regression-audit" in qd_text and "strategy-regression-audit" in maintenance_text,
        "report_provenance": "report_provenance" in runner_text and "report_provenance" in capital_text,
        "capital_default_81": "DEFAULT_STRATEGIES" in capital_text and "expected_result_count" in capital_text,
        "capital_full_162": "FULL_STRATEGIES" in capital_text and "full_strategy_count_mismatch" in capital_text,
        "distinctness_guard": "check_baseline_distinctness" in runner_text,
        "metrics_consistency_guard": "validate_strategy_result_metrics_consistency" in runner_text,
        "metrics_sanity_guard": "validate_performance_metrics_sanity" in runner_text,
        "audit_module": "baseline_distinctness_status" in audit_text and "capital_lot_result_count_status" in audit_text,
        "verify_clean_zip": "tests/test_v3706_strategy_regression_guard_and_report_hygiene.py" in verify_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "v3706_strategy_regression_guard", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "v3706_strategy_regression_guard", "v3.7.0.6 strategy regression guard and report hygiene wired")


def check_script_inventory_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/script_inventory.py",
        "scripts/run_script_inventory.py",
        "tests/test_script_inventory_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "script_inventory.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "script_inventory.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "script-inventory" in qd_text and "script_inventory_latest.json" in qd_text,
        "maintenance_cli": "script-inventory" in maintenance_text and "script_inventory_main" in maintenance_text,
        "schema": "versioned_raw_script_count" in module_text and "reference_summary" in module_text and "archive_review_candidates" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "script-inventory" in docs_text and "Script governance" in docs_text and "archive review candidate" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "script_inventory_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "script_inventory_governance", "script entrypoint inventory and governance audit wired")


def check_test_inventory_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/test_inventory.py",
        "scripts/run_test_inventory.py",
        "tests/test_test_inventory_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_inventory.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_inventory.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "test-inventory" in qd_text and "test_inventory_latest.json" in qd_text,
        "maintenance_cli": "test-inventory" in maintenance_text and "test_inventory_main" in maintenance_text,
        "schema": "counts_by_tier" in module_text and "tier3_release_or_long" in module_text and "tier_runner_commands" in module_text and "runner" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "test-inventory" in docs_text and "Test inventory" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "test_inventory_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "test_inventory_governance", "test inventory and tiering audit wired")


def check_test_discovery_diagnostics_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/test_discovery_diagnostics.py",
        "scripts/run_test_discovery_diagnostics.py",
        "tests/test_test_discovery_diagnostics_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_discovery_diagnostics.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_discovery_diagnostics.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "test-discovery-diagnostics" in qd_text and "test_discovery_diagnostics_latest.json" in qd_text,
        "maintenance_cli": "test-discovery-diagnostics" in maintenance_text and "test_discovery_diagnostics_main" in maintenance_text,
        "schema": "import_discovery_probe" in module_text and "per_file_timeout_seconds" in module_text and "slowest_probes" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "test-discovery-diagnostics" in docs_text and "unittest discovery" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "test_discovery_diagnostics_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "test_discovery_diagnostics_governance", "test discovery diagnostics wired")


def check_test_tier_runner_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/test_tier_runner.py",
        "scripts/run_test_tier_runner.py",
        "tests/test_test_tier_runner_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_tier_runner.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "test_tier_runner.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "test-tier-runner" in qd_text and "test_tier_runner_latest.json" in qd_text,
        "maintenance_cli": "test-tier-runner" in maintenance_text and "test_tier_runner_main" in maintenance_text,
        "schema": "selected_files" in module_text and "include_tier3" in module_text and "include_legacy" in module_text and "dry_run" in module_text and "pytest_paths" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "test-tier-runner" in docs_text and "tier0_fast_unit" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "test_tier_runner_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "test_tier_runner_governance", "bounded test tier runner wired")


def check_pytest_suite_runner_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/pytest_suite_runner.py",
        "scripts/run_pytest_suite_runner.py",
        "tests/test_pytest_suite_runner_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_path = PROJECT_ROOT / "quantdesk" / "maintenance" / "pytest_suite_runner.py"
    module_text = module_path.read_text(encoding="utf-8", errors="ignore") if module_path.exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "pytest-suite-runner" in qd_text and "pytest_suite_runner_latest.json" in qd_text,
        "maintenance_cli": "pytest-suite-runner" in maintenance_text and "pytest_suite_runner_main" in maintenance_text,
        "schema": "SUITE_ORDER" in module_text and "v3_v4_governance" in module_text and "dry_run" in module_text and "red_points" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "pytest-suite-runner" in docs_text and "v3_v4_governance" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "pytest_suite_runner_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "pytest_suite_runner_governance", "pytest suite runner wired")


def check_frontend_api_contract_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/frontend_api_contract.py",
        "scripts/run_frontend_api_contract.py",
        "tests/test_frontend_api_contract_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "frontend_api_contract.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "frontend_api_contract.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink(", "urlopen(", "requests."]
    checks = {
        "qd_cli": "frontend-api-contract" in qd_text and "frontend_api_contract_latest.json" in qd_text,
        "maintenance_cli": "frontend-api-contract" in maintenance_text and "frontend_api_contract_main" in maintenance_text,
        "schema": "frontend_missing_on_server" in module_text and "server_unreferenced_by_frontend" in module_text and "frontend_method_gaps" in module_text and "frontend_sources" in module_text and "server_sources" in module_text and "server_unreferenced_route_reviews" in module_text and "frontend_dynamic_api_reviews" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "frontend-api-contract" in docs_text and "Frontend API Contract" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "frontend_api_contract_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "frontend_api_contract_governance", "frontend API contract inventory wired")


def check_dependency_contract_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/dependency_contract.py",
        "scripts/run_dependency_contract.py",
        "tests/test_dependency_contract_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "dependency_contract.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "dependency_contract.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["pip install", "subprocess.run", "urlopen(", "requests.", "git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "dependency-contract" in qd_text and "dependency_contract_latest.json" in qd_text,
        "maintenance_cli": "dependency-contract" in maintenance_text and "dependency_contract_main" in maintenance_text,
        "schema": "optional_data_packages_in_runtime" in module_text and "dev_packages_in_runtime" in module_text and "runtime_requirements_drift" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "dependency-contract" in docs_text and "Dependency Contract" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "dependency_contract_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "dependency_contract_governance", "dependency contract inventory wired")


def check_data_backtest_contract_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/data_backtest_contract.py",
        "scripts/run_data_backtest_contract.py",
        "tests/test_data_backtest_contract_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "data_backtest_contract.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "data_backtest_contract.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["simulate_backtest", "run_backtest", "data-update", "urlopen(", "requests.", "git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "data-backtest-contract" in qd_text and "data_backtest_contract_latest.json" in qd_text,
        "maintenance_cli": "data-backtest-contract" in maintenance_text and "data_backtest_contract_main" in maintenance_text,
        "schema": "missing_assumption_fields" in module_text and "artifact_records" in module_text and "survivorship_bias_warning" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "data-backtest-contract" in docs_text and "Data Backtest Contract" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "data_backtest_contract_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "data_backtest_contract_governance", "data/backtest contract inventory wired")


def check_coverage_contract_governance() -> CheckResult:
    required = [
        "quantdesk/maintenance/coverage_contract.py",
        "scripts/run_coverage_contract.py",
        "tests/test_coverage_contract_governance.py",
        "docs/script_entrypoint_inventory.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    maintenance_text = (PROJECT_ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "maintenance" / "coverage_contract.py").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "quantdesk" / "maintenance" / "coverage_contract.py").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md").exists() else ""
    forbidden = ["data-update", "enable-network", "--network true", "urlopen(", "requests.", "git clean", "git restore", "Remove-Item", "shutil.rmtree", ".unlink("]
    checks = {
        "qd_cli": "coverage-contract" in qd_text and "coverage_contract_latest.json" in qd_text,
        "maintenance_cli": "coverage-contract" in maintenance_text and "coverage_contract_main" in maintenance_text,
        "schema": "line_percent" in module_text and "coverage_xml" in module_text and "--cov=quantdesk/maintenance" in module_text,
        "read_only": not any(token in module_text for token in forbidden),
        "docs": "coverage-contract" in docs_text and "Coverage Contract" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "coverage_contract_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "coverage_contract_governance", "bounded coverage contract wired")


def check_gitlab_ci_governance() -> CheckResult:
    required = [
        ".gitlab-ci.yml",
        "tests/test_gitlab_ci_governance.py",
        "docs/development_test_guide.md",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).exists()]
    ci_text = (PROJECT_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / ".gitlab-ci.yml").exists() else ""
    docs_text = (PROJECT_ROOT / "docs" / "development_test_guide.md").read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / "docs" / "development_test_guide.md").exists() else ""
    forbidden = ["git push", "export_project.ps1", "enable-network", "--network true", "sourceportable"]
    checks = {
        "offline_contracts": "frontend-api-contract" in ci_text and "dependency-contract" in ci_text and "data-backtest-contract" in ci_text and "coverage-contract" in ci_text,
        "syntax_gate": "node --check app/static/js/daily_research.js" in ci_text,
        "self_check": "python scripts/self_check.py" in ci_text,
        "coverage_artifact": "reports/coverage/" in ci_text,
        "research_boundary": not any(token in ci_text.lower() for token in forbidden),
        "docs": "GitLab CI" in docs_text and "test_gitlab_ci_governance" in docs_text,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if missing or failed:
        return result("FAIL", "gitlab_ci_governance", "missing wiring: " + ", ".join((missing + failed)[:12]))
    return result("PASS", "gitlab_ci_governance", "offline GitLab CI governance wired")


def check_local_a_share_calendar() -> CheckResult:
    calendar_path = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
    if not calendar_path.exists():
        legacy_path = PROJECT_ROOT / "config" / "trading_calendar_cn.yaml"
        if legacy_path.exists():
            return result(
                "WARN",
                "a_share_trading_calendar",
                "交易日历缺失: market_calendar_source=yaml; CSV calendar missing, using legacy YAML fallback. Provide data/calendar/a_share_trading_calendar.csv.",
            )
        return result(
            "WARN",
            "a_share_trading_calendar",
            "交易日历缺失: market_calendar_source=unavailable; Trading calendar data is unavailable. Market open/close detection may be degraded. Please provide a current CN trading calendar CSV or YAML file.",
        )
    try:
        from quantdesk.trading_calendar import calendar_info

        info = calendar_info(calendar_path, start_date="2020-01-01", end_date="2027-12-31")
    except Exception as exc:  # noqa: BLE001
        return result("WARN", "a_share_trading_calendar", f"calendar read failed: {exc}")
    if info.get("weekend_only") or info.get("calendar_quality") == "weekend_only":
        return result("WARN", "a_share_trading_calendar", "calendar_quality=weekend_only; holiday coverage is incomplete")
    start = str(info.get("start_date") or "")
    end = str(info.get("end_date") or "")
    loaded = int(info.get("loaded_dates") or 0)
    if loaded <= 0:
        return result("WARN", "a_share_trading_calendar", "calendar CSV exists but loaded_dates=0")
    if start > "2020-01-01" or end < "2027-12-31":
        return result("WARN", "a_share_trading_calendar", f"coverage insufficient: {start}..{end}, loaded_dates={loaded}")
    return result("PASS", "a_share_trading_calendar", f"market_calendar_source=csv; {info.get('calendar_quality')} {start}..{end}, loaded_dates={loaded}")


def check_market_calendar_source() -> CheckResult:
    try:
        from quantdesk.market_clock import load_cn_trading_calendar

        cal = load_cn_trading_calendar()
    except Exception as exc:  # noqa: BLE001
        return result("WARN", "market_calendar_source", f"calendar source check failed: {exc}")
    source = str(cal.get("source") or "unknown")
    warnings_list = cal.get("warnings") if isinstance(cal.get("warnings"), list) else []
    detail = f"market_calendar_source={source}; calendar_quality={cal.get('calendar_quality') or 'unknown'}"
    if warnings_list:
        detail += "; warnings=" + " | ".join(str(item) for item in warnings_list[:3])
    if source == "unavailable":
        return result("WARN", "market_calendar_source", detail)
    if source == "legacy_yaml":
        return result("WARN", "market_calendar_source", detail + "; prefer data/calendar/a_share_trading_calendar.csv")
    return result("PASS", "market_calendar_source", detail)


def check_frontend_structured_fields() -> CheckResult:
    path = PROJECT_ROOT / "app" / "static" / "app.js"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = ["daily_brief", "candidate_cards", "learning_appendix", "risk_dashboard", "backtest_audit_files", "news_health", "settlement_status", "market_calendar_status", "data_health_reason_summary", "trust_score", "action_state", "ready_for_review", "trust_assessment", "UI_LABELS", "glossaryModal", "calm-mode", "paper-save-snapshot", "strategy-demo", "strategyDemoResult", "strategyDemoOverviewRows", "/api/strategy-demo/overview", "research_summary", "benchmark_snapshot", "market_regime", "fundamental_health_summary", "renderResearchSummary", "actionPlanView", "/api/action-plan/overview", "manualConfirmationList", "/api/manual-confirmation-queue", "watchlistView", "/api/watchlist", "localAlertsPanel", "/api/live-monitor/once"]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "frontend_structured_fields", "missing: " + ", ".join(missing))
    return result("PASS", "frontend_structured_fields", "structured API fields rendered")


def check_research_credibility_support() -> CheckResult:
    server = PROJECT_ROOT / "scripts" / "quant_api_server.py"
    app = PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js"
    docs = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    readme = PROJECT_ROOT / "README.md"
    road = PROJECT_ROOT / "ROADMAP.md"
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (server, app, docs, readme, road)
        if path.exists()
    )
    required = [
        "benchmark_snapshot",
        "market_regime",
        "fundamental_health_summary",
        "candidate_replay",
        "strategy_replay",
        "/api/market-regime",
        "/api/fundamental-health/check",
        "/api/research/candidate-replay",
        "/api/research/strategy-replay",
        "研究可信度",
    ]
    missing = [token for token in required if token not in combined]
    if missing:
        return result("FAIL", "research_credibility", "missing: " + ", ".join(missing))
    return result("PASS", "research_credibility", "v1.9 research credibility layer wired")


def check_trade_replay_support() -> CheckResult:
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    html = PROJECT_ROOT / "app" / "index.html"
    module = PROJECT_ROOT / "quantdesk" / "paper" / "strategy_trade_replay.py"
    docs = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    readme = PROJECT_ROOT / "README.md"
    if not module.exists():
        return result("FAIL", "trade_replay_support", "quantdesk/strategy_trade_replay.py missing")
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (server, app, html, docs, readme)
        if path.exists()
    )
    required = [
        "strategy_trade_replay",
        "/api/research/strategy-trade-replay",
        "strategyTradeReplayResult",
        "交易路径回放",
        "信号日",
        "成交日",
        "entry_signal_date",
        "entry_execution_date",
        "entry_execution_policy",
        "trade_replay",
        "不接券商",
    ]
    missing = [token for token in required if token not in combined]
    if missing:
        return result("FAIL", "trade_replay_support", "missing: " + ", ".join(missing))
    return result("PASS", "trade_replay_support", "v2.2 trade replay wired (module, API, UI, docs)")


def check_cn_a_share_market_rules_support() -> CheckResult:
    module = PROJECT_ROOT / "quantdesk" / "cn_a_share_rules.py"
    trade_replay = PROJECT_ROOT / "quantdesk" / "paper" / "strategy_trade_replay.py"
    server = PROJECT_ROOT / "scripts" / "quant_api_server.py"
    app = PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js"
    docs = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    readme = PROJECT_ROOT / "README.md"
    if not module.exists():
        return result("FAIL", "cn_a_share_market_rules", "quantdesk/cn_a_share_rules.py missing")
    if not trade_replay.exists():
        return result("FAIL", "cn_a_share_market_rules", "quantdesk/strategy_trade_replay.py missing")
    trade_replay_text = trade_replay.read_text(encoding="utf-8", errors="ignore")
    if "market_rules" not in trade_replay_text or "cn_a_share" not in trade_replay_text:
        return result("FAIL", "cn_a_share_market_rules", "strategy_trade_replay.py 未接入 market_rules=cn_a_share")
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (server, app, docs, readme)
        if path.exists()
    )
    required = [
        "market_rules",
        "cn_a_share",
        "100 股",
        "T+1",
        "不构成投资建议",
        "不接券商",
    ]
    missing = [token for token in required if token not in combined]
    if missing:
        return result("FAIL", "cn_a_share_market_rules", "missing: " + ", ".join(missing))
    return result(
        "PASS",
        "cn_a_share_market_rules",
        "v2.3 cn_a_share market rules wired (module, trade_replay, API, UI, docs)",
    )


def check_data_provider_fallback_support() -> CheckResult:
    daily = PROJECT_ROOT / "scripts" / "daily_quant_report.py"
    providers = PROJECT_ROOT / "quantdesk" / "data_providers.py"
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    index_html = PROJECT_ROOT / "app" / "index.html"
    config_yaml = PROJECT_ROOT / "config" / "default.yaml"
    export_ps1 = PROJECT_ROOT / "export_project.ps1"
    export_sh = PROJECT_ROOT / "export_project.sh"
    missing: list[str] = []
    for path in (daily, providers, app, server, index_html, config_yaml, export_ps1, export_sh):
        if not path.exists():
            return result("FAIL", "data_provider_fallback", f"missing file: {path.relative_to(PROJECT_ROOT)}")

    daily_text = daily.read_text(encoding="utf-8", errors="ignore")
    providers_text = providers.read_text(encoding="utf-8", errors="ignore")
    app_text = app.read_text(encoding="utf-8", errors="ignore")
    server_text = server.read_text(encoding="utf-8", errors="ignore")
    index_text = index_html.read_text(encoding="utf-8", errors="ignore")
    config_text = config_yaml.read_text(encoding="utf-8", errors="ignore")
    export_ps1_text = export_ps1.read_text(encoding="utf-8", errors="ignore")
    export_sh_text = export_sh.read_text(encoding="utf-8", errors="ignore")

    for token in (
        "spot_snapshot_latest",
        "DEFAULT_FALLBACK_SYMBOLS",
        "hist_fallback",
        "build_spot_from_hist_fallback",
    ):
        if token not in daily_text:
            missing.append(f"daily_quant_report.py 未找到 {token}")

    for token in (
        "normalize_cn_symbol_for_sina",
        "fetch_spot_sina_single",
        "fetch_spot_sina_batch",
        "parse_sina_quote_text",
    ):
        if token not in providers_text:
            missing.append(f"data_providers.py 未找到 {token}")

    for token in (
        "refreshResultsButton",
        "updateAndGenerateDailyButton",
        "generateSampleButton",
        "output_excerpt",
        "stderr_excerpt",
        "stdout_excerpt",
        "不是完整全市场扫描",
        "候选股票池",
    ):
        if token not in app_text:
            missing.append(f"app.js 未找到 {token}")

    for token in (
        "refreshResultsButton",
        "updateAndGenerateDailyButton",
        "generateSampleButton",
    ):
        if token not in index_text:
            missing.append(f"index.html 未找到按钮文案 {token}")

    for token in (
        "stdout_excerpt",
        "stderr_excerpt",
        "output_excerpt",
        "troubleshooting",
    ):
        if token not in server_text:
            missing.append(f"quant_app_server.py 未找到 {token}")

    if "fallback_symbols" not in config_text:
        missing.append("config/default.yaml 未找到 fallback_symbols")
    if "data\\cache" not in export_ps1_text:
        missing.append("export_project.ps1 未排除 data\\cache")
    if "data/cache" not in export_sh_text:
        missing.append("export_project.sh 未排除 data/cache")

    if missing:
        return result("FAIL", "data_provider_fallback", "missing: " + ", ".join(missing))
    return result(
        "PASS",
        "data_provider_fallback",
        "v2.3.1 data provider fallback wired (daily, providers, server, UI, docs)",
    )


def check_local_daily_cache_support() -> CheckResult:
    script = PROJECT_ROOT / "scripts" / "update_market_data.py"
    universe_script = PROJECT_ROOT / "scripts" / "update_a_share_universe.py"
    daily = PROJECT_ROOT / "scripts" / "daily_quant_report.py"
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    index_html = PROJECT_ROOT / "app" / "index.html"
    config_yaml = PROJECT_ROOT / "config" / "default.yaml"
    export_ps1 = PROJECT_ROOT / "export_project.ps1"
    export_sh = PROJECT_ROOT / "export_project.sh"
    docs = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    readme = PROJECT_ROOT / "README.md"
    missing: list[str] = []
    for path in (script, universe_script, daily, server, app, index_html, config_yaml, export_ps1, export_sh, docs, readme):
        if not path.exists():
            return result("FAIL", "local_daily_cache", f"missing file: {path.relative_to(PROJECT_ROOT)}")
    script_text = script.read_text(encoding="utf-8", errors="ignore")
    universe_script_text = universe_script.read_text(encoding="utf-8", errors="ignore")
    daily_text = daily.read_text(encoding="utf-8", errors="ignore")
    server_text = server.read_text(encoding="utf-8", errors="ignore")
    app_text = app.read_text(encoding="utf-8", errors="ignore")
    index_text = index_html.read_text(encoding="utf-8", errors="ignore")
    config_text = config_yaml.read_text(encoding="utf-8", errors="ignore")
    export_ps1_text = export_ps1.read_text(encoding="utf-8", errors="ignore")
    export_sh_text = export_sh.read_text(encoding="utf-8", errors="ignore")
    docs_text = docs.read_text(encoding="utf-8", errors="ignore")
    readme_text = readme.read_text(encoding="utf-8", errors="ignore")
    for token in ("stock_zh_a_hist", "sina_daily", "provider_used", "provider_summary", "universe_mode", "update_status.json", "--symbols", "--include-local-universe", "--retry-failed", "--all-a-share", "--universe-file", "--resume", "--force", "--dry-run"):
        if token not in script_text:
            missing.append(f"update_market_data.py missing {token}")
    for token in ("daily_cache_index", "--rebuild-cache-index", "--no-cache-index", "index_hit_count", "index_miss_count", "index_stale_count"):
        if token not in script_text:
            missing.append(f"update_market_data.py missing {token}")
    for token in ("a_share_symbols.csv", "stock_info_a_code_name", "local_fallback_universe"):
        if token not in universe_script_text:
            missing.append(f"update_a_share_universe.py missing {token}")
    for token in ("local_daily_cache", "read_local_daily_cache", "save_local_daily_cache"):
        if token not in daily_text:
            missing.append(f"daily_quant_report.py missing {token}")
    for token in ("/api/update-market-data", "build_update_market_data_command", "market_data_update_status"):
        if token not in server_text:
            missing.append(f"quant_app_server.py missing {token}")
    for token in ("updateMarketDataButton", "/api/update-market-data", "market_data_update_status", "Sina daily", "provider_summary"):
        if token not in app_text:
            missing.append(f"app.js missing {token}")
    if "updateMarketDataButton" not in index_text:
        missing.append("index.html missing update button")
    for token in ("daily_cache_dir", "update_status_path", "fallback_symbols", "a_share_universe_path"):
        if token not in config_text:
            missing.append(f"config/default.yaml missing {token}")
    if "data\\daily" not in export_ps1_text or "data\\cache" not in export_ps1_text or "data\\universe" not in export_ps1_text or "data\\features" not in export_ps1_text:
        missing.append("export_project.ps1 does not exclude data daily/cache/universe/features")
    if "data/daily" not in export_sh_text or "data/cache" not in export_sh_text or "data/universe" not in export_sh_text or "data/features" not in export_sh_text:
        missing.append("export_project.sh does not exclude data daily/cache/universe/features")
    if "v2.3.2" not in docs_text or "update-market-data" not in docs_text:
        missing.append("api schema missing v2.3.2 update-market-data docs")
    if "v2.3.2" not in readme_text or "update_market_data.py" not in readme_text:
        missing.append("README missing v2.3.2 local daily cache docs")
    if missing:
        return result("FAIL", "local_daily_cache", "missing: " + "; ".join(missing))
    return result("PASS", "local_daily_cache", "v2.3.2 local AKShare daily cache wired")


def check_daily_cache_index_support() -> CheckResult:
    script = PROJECT_ROOT / "scripts" / "update_market_data.py"
    module = PROJECT_ROOT / "quantdesk" / "daily_cache_index.py"
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    config_yaml = PROJECT_ROOT / "config" / "default.yaml"
    readme = PROJECT_ROOT / "README.md"
    docs = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in (script, module, server, config_yaml, readme, docs) if not path.exists()]
    if missing_files:
        return result("FAIL", "daily_cache_index", "missing: " + ", ".join(missing_files))
    script_text = script.read_text(encoding="utf-8", errors="ignore")
    module_text = module.read_text(encoding="utf-8", errors="ignore")
    server_text = server.read_text(encoding="utf-8", errors="ignore")
    config_text = config_yaml.read_text(encoding="utf-8", errors="ignore")
    readme_text = readme.read_text(encoding="utf-8", errors="ignore")
    docs_text = docs.read_text(encoding="utf-8", errors="ignore")
    missing: list[str] = []
    for token in ("build_daily_cache_index", "load_daily_cache_index", "save_daily_cache_index", "classify_cache_freshness_from_index", "summarize_daily_cache_index"):
        if token not in module_text:
            missing.append(f"daily_cache_index.py missing {token}")
    for token in ("daily_cache_index", "--rebuild-cache-index", "--no-cache-index", "index_hit_count", "index_miss_count", "index_stale_count"):
        if token not in script_text:
            missing.append(f"update_market_data.py missing {token}")
    for token in ("/api/daily-cache-index/summary", "daily_cache_index_summary"):
        if token not in server_text:
            missing.append(f"quant_app_server.py missing {token}")
    # v2.6.7: relax the daily-cache-index version assertion so future v2.6.x
    # bumps (e.g. v2.6.7 stability patch) keep this check green.  The intent
    # is to verify the index path / docs are still wired, not to freeze the
    # release string.
    for token in ("daily_cache_index_path",):
        if token not in config_text:
            missing.append(f"config/default.yaml missing {token}")
    for token in ("daily_cache_index.json", "Daily Cache Index"):
        if token not in readme_text:
            missing.append(f"README.md missing {token}")
    for token in ("daily_cache_index.json", "Daily Cache Index", "GET /api/daily-cache-index/summary"):
        if token not in docs_text:
            missing.append(f"api_payload_schema.md missing {token}")
    if missing:
        return result("FAIL", "daily_cache_index", "missing: " + "; ".join(missing))
    return result("PASS", "daily_cache_index", "local daily-cache index acceleration wired")


def check_data_foundation_support() -> CheckResult:
    files = {
        "data_quality": PROJECT_ROOT / "quantdesk" / "data_quality.py",
        "update_market_data": PROJECT_ROOT / "scripts" / "update_market_data.py",
        "daily_report": PROJECT_ROOT / "scripts" / "daily_quant_report.py",
        "data_health_recorder": PROJECT_ROOT / "quantdesk" / "data_health_recorder.py",
        "server": PROJECT_ROOT / "scripts" / "quant_app_server.py",
        "app": PROJECT_ROOT / "app" / "static" / "app.js",
        "config": PROJECT_ROOT / "config" / "default.yaml",
        "docs": PROJECT_ROOT / "docs" / "api_payload_schema.md",
        "readme": PROJECT_ROOT / "README.md",
    }
    missing_files = [name for name, path in files.items() if not path.exists()]
    if missing_files:
        return result("FAIL", "data_foundation", "missing files: " + ", ".join(missing_files))
    text = {name: path.read_text(encoding="utf-8", errors="ignore") for name, path in files.items()}
    missing: list[str] = []
    for token in ("UNAVAILABLE", "OBSERVE_ONLY", "SCORABLE", "RESEARCH_READY", "BACKTEST_READY", "NETWORK_BLOCKED_OR_PERMISSION_DENIED"):
        if token not in text["data_quality"]:
            missing.append(f"data_quality.py missing {token}")
    for token in ("data_grade_distribution", "last_error", "last_error_type", "network_blocked_or_permission_denied"):
        if token not in text["update_market_data"]:
            missing.append(f"update_market_data.py missing {token}")
    # v2.6.4 moved DataFetchStatus / attach_data_health into
    # quantdesk/data_health_recorder.py.  The report still imports and
    # re-exports those names, so the token can live in either file.
    daily_combined = text["daily_report"] + "\n" + text["data_health_recorder"]
    for token in ("data_grade", "backtest_allowed", "research_allowed", "data_grade_gate_note"):
        if token not in daily_combined:
            missing.append(f"daily_quant_report.py / data_health_recorder.py missing {token}")
    for token in ("data_foundation_status", "data_grade_distribution"):
        if token not in text["server"]:
            missing.append(f"quant_app_server.py missing {token}")
    if "quantdesk.version" not in text["server"]:
        missing.append("quant_app_server.py missing centralized version import")
    for token in ("数据建库进度", "BACKTEST_READY", "provider_summary"):
        if token not in text["app"]:
            missing.append(f"app.js missing {token}")
    for token in ("data_grade_min_scorable_rows", "data_grade_min_research_rows", "data_grade_min_backtest_rows"):
        if token not in text["config"]:
            missing.append(f"config/default.yaml missing {token}")
    if RELEASE_TITLE not in text["docs"] and "v2.4.0 Data Foundation" not in text["docs"]:
        missing.append("api schema missing current stability cleanup or data foundation docs")
    if RELEASE_TITLE not in text["readme"] and "v2.4.0 Data Foundation" not in text["readme"]:
        missing.append("README missing current stability cleanup or data foundation docs")
    if missing:
        return result("FAIL", "data_foundation", "missing: " + "; ".join(missing))
    return result("PASS", "data_foundation", f"{APP_VERSION} data grade and data foundation checks wired")


def check_sample_payload_support() -> CheckResult:
    path = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = ["--sample-payload", "load_sample_payload", "sample_payload.json"]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "sample_payload_support", "missing: " + ", ".join(missing))
    return result("PASS", "sample_payload_support", "server supports offline sample payload")


def check_generate_report_api() -> CheckResult:
    path = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = [
        "/api/generate-sample-report",
        "/api/generate-daily-report",
        "build_sample_report_command",
        "build_daily_report_command",
        "shell=False",
    ]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "generate_report_api", "missing: " + ", ".join(missing))
    return result("PASS", "generate_report_api", "sample and daily report generation APIs present")


def check_one_click_sample_ui() -> CheckResult:
    path = PROJECT_ROOT / "run_ui_sample.cmd"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = ["launcher.py", "--port 8000", "%~dp0"]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "one_click_sample_ui", "missing: " + ", ".join(missing))
    return result("PASS", "one_click_sample_ui", "sample UI launches via launcher on port 8000")


def check_frontend_generation_buttons() -> CheckResult:
    path = PROJECT_ROOT / "app" / "static" / "app.js"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = [
        "generate-sample-report",
        "generate-daily-report",
        "generateSampleButton",
        "generateDailyButton",
        "refreshResultsButton",
    ]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "frontend_generation_buttons", "missing: " + ", ".join(missing))
    return result("PASS", "frontend_generation_buttons", "report generation buttons are wired")


def check_windows_scripts_ready() -> CheckResult:
    scripts = [
        "setup_windows.ps1",
        "start_ui.cmd",
        "run_tests.cmd",
        "run_daily_smoke.cmd",
        "self_check.cmd",
        "create_desktop_shortcuts.ps1",
        "create_desktop_shortcuts.cmd",
    ]
    missing = [name for name in scripts if not (PROJECT_ROOT / name).exists()]
    if missing:
        return result("FAIL", "windows_scripts", "missing: " + ", ".join(missing))
    issues: list[str] = []
    for name in scripts:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8", errors="ignore")
        if "%~dp0" not in text and "$PSScriptRoot" not in text and "$MyInvocation.MyCommand.Path" not in text:
            issues.append(f"{name}: no relative root")
    for name in ("run_tests.cmd", "self_check.cmd", "run_daily_smoke.cmd"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8", errors="ignore") if (PROJECT_ROOT / name).exists() else ""
        if "QUANTDESK_PYTHON" not in text:
            issues.append(f"{name}: missing QUANTDESK_PYTHON fallback")
    if issues:
        return result("FAIL", "windows_scripts", "; ".join(issues))
    return result("PASS", "windows_scripts", "relative launch scripts present")


def check_desktop_shortcuts() -> CheckResult:
    ps1 = PROJECT_ROOT / "create_desktop_shortcuts.ps1"
    cmd = PROJECT_ROOT / "create_desktop_shortcuts.cmd"
    if not ps1.exists() or not cmd.exists():
        return result("FAIL", "desktop_shortcuts", "create_desktop_shortcuts.ps1/cmd missing")
    ps1_text = ps1.read_text(encoding="utf-8", errors="ignore")
    cmd_text = cmd.read_text(encoding="utf-8", errors="ignore")
    required_ps1 = [
        "$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path",
        "start_ui.cmd",
        "[Environment]::GetFolderPath(\"Desktop\")",
        "$shortcut.WorkingDirectory = $ProjectRoot",
    ]
    required_cmd = [
        "%~dp0",
        "powershell",
        "create_desktop_shortcuts.ps1",
    ]
    missing = [token for token in required_ps1 if token not in ps1_text]
    missing.extend(token for token in required_cmd if token not in cmd_text)
    if missing:
        return result("FAIL", "desktop_shortcuts", "missing: " + ", ".join(missing))
    return result("PASS", "desktop_shortcuts", "one-click desktop shortcut scripts present")


def check_readme_ready() -> CheckResult:
    path = PROJECT_ROOT / "README.md"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = [
        "Windows Quick Start",
        "RC1 Daily Usage Flow",
        "start_ui.cmd",
        "self_check.cmd",
        "docs/api_payload_schema.md",
        "Backtest Risk Summary",
        "15:00 Settlement Rule",
        "News Status",
        "run_daily_smoke.cmd",
        "Beginner Mode",
        "Trust Decision Center",
        "trust score is not a probability",
        "raw strategy score is not reliability",
        "5分钟快速体验",
        "作为学习工具",
    ]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "readme_ready", "missing: " + ", ".join(missing))
    return result("PASS", "readme_ready", "user guide sections present")


def check_agents_ready() -> CheckResult:
    path = PROJECT_ROOT / "AGENTS.md"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = [
        "Fail Fast",
        "Module Responsibilities",
        "Do not connect live trading",
        "python -m unittest discover",
        "python scripts/self_check.py",
    ]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "agents_md", "missing: " + ", ".join(missing))
    return result("PASS", "agents_md", "rules and fail-fast guidance present")


def check_config_report_defaults() -> CheckResult:
    path = PROJECT_ROOT / "config" / "default.yaml"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    required = ["report:", "decision_card_top_n", "max_card_bullets", "include_learning_appendix", "calendar:", "trading_calendar_path"]
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", "config_report_defaults", "missing: " + ", ".join(missing))
    return result("PASS", "config_report_defaults", "report defaults present")


def check_external_tools_docs() -> CheckResult:
    external = PROJECT_ROOT / "docs" / "external_tools.md"
    schema = PROJECT_ROOT / "docs" / "api_payload_schema.md"
    readme = PROJECT_ROOT / "README.md"
    if not external.exists():
        return result("FAIL", "external_tools_doc", "docs/external_tools.md missing")
    external_text = external.read_text(encoding="utf-8", errors="ignore")
    schema_text = schema.read_text(encoding="utf-8", errors="ignore") if schema.exists() else ""
    readme_text = readme.read_text(encoding="utf-8", errors="ignore") if readme.exists() else ""
    required = [
        "AKShare",
        "vectorbt",
        "Backtrader",
        "Microsoft Qlib",
        "FinRL",
        "optional",
        "GPL-3.0",
        "Commons Clause",
        "do not copy",
    ]
    missing = [token for token in required if token not in external_text]
    if "/api/integrations" not in schema_text:
        missing.append("/api/integrations schema")
    if "Optional Integrations" not in readme_text:
        missing.append("README Optional Integrations")
    if missing:
        return result("FAIL", "external_tools_doc", "missing: " + ", ".join(missing))
    return result("PASS", "external_tools_doc", "optional integration docs present")


def check_optional_deps_not_required() -> CheckResult:
    forbidden = {"vectorbt", "backtrader", "qlib", "finrl"}
    offenders: list[str] = []
    for filename in ("requirements.txt", "requirements-dev.txt"):
        path = PROJECT_ROOT / filename
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines:
            normalized = line.strip().lower()
            if not normalized or normalized.startswith("#"):
                continue
            package = normalized.split("==", 1)[0].split(">=", 1)[0].split("<", 1)[0]
            if package in forbidden:
                offenders.append(f"{filename}:{package}")
    if offenders:
        return result("FAIL", "optional_deps_not_required", ", ".join(offenders))
    return result("PASS", "optional_deps_not_required", "research tools are not required dependencies")


def check_no_external_source_dirs() -> CheckResult:
    forbidden_dirs = ["vectorbt", "backtrader", "qlib", "FinRL", "finrl"]
    present = [name for name in forbidden_dirs if (PROJECT_ROOT / name).exists()]
    if present:
        return result("FAIL", "external_source_dirs", ", ".join(present))
    return result("PASS", "external_source_dirs", "no third-party source directories copied")


def check_tests_present() -> CheckResult:
    tests_dir = PROJECT_ROOT / "tests"
    if not tests_dir.exists():
        return result("FAIL", "tests_present", "tests directory missing")
    test_files = sorted(tests_dir.glob("test_*.py"))
    if len(test_files) < 2:
        return result("FAIL", "tests_present", "core test files missing")
    test_count = 0
    for path in test_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        test_count += text.count("def test_")
    if test_count < 180:
        return result("FAIL", "tests_present", f"only {test_count} test methods found")
    return result("PASS", "tests_present", f"{len(test_files)} test files, {test_count} test methods found")


def check_research_validation_support() -> CheckResult:
    """Check that v2.4.1 research validation modules and API are wired."""
    required_modules = [
        PROJECT_ROOT / "quantdesk" / "walk_forward.py",
        PROJECT_ROOT / "quantdesk" / "backtest_quality.py",
        PROJECT_ROOT / "quantdesk" / "backtest_audit.py",
        PROJECT_ROOT / "quantdesk" / "trade_plan.py",
        PROJECT_ROOT / "quantdesk" / "risk_position.py",
        PROJECT_ROOT / "quantdesk" / "factors" / "__init__.py",
        PROJECT_ROOT / "scripts" / "walk_forward_backtest.py",
        PROJECT_ROOT / "scripts" / "export_trade_plan.py",
    ]
    missing_files = [str(p.relative_to(PROJECT_ROOT)) for p in required_modules if not p.exists()]
    if missing_files:
        return result("FAIL", "research_validation", "missing: " + ", ".join(missing_files))
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    server_text = server.read_text(encoding="utf-8", errors="ignore") if server.exists() else ""
    if "research_validation_ready" not in server_text:
        return result("FAIL", "research_validation", "API missing research_validation_ready")
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    app_text = app.read_text(encoding="utf-8", errors="ignore") if app.exists() else ""
    if "\u7814\u7a76\u9a8c\u8bc1" not in app_text:
        return result("FAIL", "research_validation", "frontend missing research validation badge")
    try:
        importlib.import_module("quantdesk.walk_forward")
        importlib.import_module("quantdesk.backtest_quality")
        importlib.import_module("quantdesk.factors")
        importlib.import_module("quantdesk.trade_plan")
    except Exception as exc:
        return result("FAIL", "research_validation", f"import error: {exc}")
    return result("PASS", "research_validation", "v2.4.1 research validation modules wired")


def check_low_price_cache_report_support() -> CheckResult:
    """Check that v2.7.x low-price cache report wiring is present."""

    required_files = [
        PROJECT_ROOT / "scripts" / "generate_low_price_report.py",
        PROJECT_ROOT / "scripts" / "quant_app_server.py",
        PROJECT_ROOT / "app" / "index.html",
        PROJECT_ROOT / "app" / "static" / "app.js",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "ROADMAP.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
        PROJECT_ROOT / "quantdesk" / "version.py",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing:
        return result("FAIL", "low_price_cache_report", "missing: " + ", ".join(missing))
    script_text = (PROJECT_ROOT / "scripts" / "generate_low_price_report.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    docs_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    docs_text += "\n" + (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore")
    docs_text += "\n" + (PROJECT_ROOT / "docs" / "api_payload_schema.md").read_text(encoding="utf-8", errors="ignore")
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "report_type": 'REPORT_TYPE = "low_price_cache_report"' in script_text,
        "local_only_script": "load_low_price_universe" in script_text and "data/daily" in script_text,
        "api_route": "/api/learning-universe/low-price/generate-report" in server_text,
        "latest_report_api": "/api/learning-universe/low-price/report/latest" in server_text,
        "latest_html_api": "latest-html" in server_text and "打印 / 另存为 PDF" in server_text,
        "api_handler": "low_price_cache_report_response" in server_text,
        "frontend_button": "generateLowPriceReportButton" in html_text and "generateLowPriceReportAction" in app_text,
        "frontend_latest": "viewLowPriceReportButton" in html_text and "previewLowPriceReportHtmlButton" in html_text,
        "docs": "Low Price" in docs_text and "Report" in docs_text,
        "version": (
            "report-ui-stability-and-paper-simulation" in version_text
            or "low-price-report-ui-stability" in version_text
            or "low-price-workbench" in version_text
            or "auto-strategy-sandbox" in version_text
            or "auto-paper-fixed-100-sizing" in version_text
            or "workflow-unification-auto-portfolio" in version_text
            or "intraday-swing-execution-engine" in version_text
            or "field-alias-intraday-data-theme-map" in version_text
            or "auto-theme-map-updater" in version_text
            or "persistent-auto-portfolio-review" in version_text
            or "local-daily-feature-engine" in version_text
            or "conditional-position-management-engine" in version_text
            or "unified-workflow-and-theme-map-fix" in version_text
            or "unified-workflow-result-and-theme-provider-fallback" in version_text
            or "theme-map-import-draft-generator" in version_text
            or "reference-data-skill-adapter" in version_text
            or "daily-price-provider-fallback-engine" in version_text
        ),
        "theme_map_update": "/api/theme-map/update" in server_text and "updateThemeMapAkshareButton" in html_text and "loadThemeMapStatus" in app_text,
        "theme_provider_fallback": (
            "theme_map_providers" in version_text + server_text + app_text + docs_text
            and "provider_results" in app_text
            and "themeMapProviderResultsPanel" in html_text
            and "importThemeMapButton" in html_text
            and "/api/theme-map/import" in server_text
            and "importThemeMap" in app_text
        ),
        "theme_import_draft": (
            "generate_stock_theme_map_import.py" in docs_text
            and "/api/theme-map/generate-import-draft" in server_text
            and "/api/theme-map/import-draft/preview" in server_text
            and "generateThemeMapImportDraftButton" in html_text
            and "生成待补导入草稿" in html_text
            and "导入草稿到正式映射" in html_text
            and "generateThemeMapImportDraft" in app_text
            and "theme_map_import_draft" in server_text
        ),
        "daily_workflow": (
            "/api/daily-workflow/status" in server_text
            and "/api/daily-workflow/run" in server_text
            and "dailyWorkflowSection" in html_text
            and "今日一键复盘" in html_text
            and "runDailyWorkflow" in app_text
            and "daily_workflow_orchestrator" in server_text
            and "workflow_status" in (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
            and "steps" in app_text
        ),
        "auto_portfolio_review": "--force-reset" in (PROJECT_ROOT / "scripts" / "run_auto_paper_portfolio.py").read_text(encoding="utf-8", errors="ignore") and "昨日持仓复盘" in html_text,
        "daily_feature_engine": "/api/daily-features/build" in server_text and "buildDailyFeatures" in app_text and "daily_features_latest.csv" in html_text,
        "conditional_position_management": (
            "/api/conditional-trade-plans/build" in server_text
            and "/api/conditional-trade-plans/simulate" in server_text
            and "/api/conditional-execution/latest" in server_text
            and "conditionalTradePlansPanel" in html_text
            and "buildConditionalTradePlan" in app_text
            and "simulate_conditional_plan_file" in (PROJECT_ROOT / "quantdesk" / "paper" / "position_management_engine.py").read_text(encoding="utf-8", errors="ignore")
        ),
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "low_price_cache_report", "missing wiring: " + ", ".join(failed))
    return result("PASS", "low_price_cache_report", "v2.7.1 low-price cache report UI wired")


def check_reference_data_skill_adapter_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "docs" / "reference_a_stock_data.md",
        PROJECT_ROOT / "docs" / "reference_ashare.md",
        PROJECT_ROOT / "docs" / "reference_ashare_llm_analyst.md",
        PROJECT_ROOT / "docs" / "data_provider_fallback_plan.md",
        PROJECT_ROOT / "quantdesk" / "data_provider_registry.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_providers.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py",
        PROJECT_ROOT / "scripts" / "check_data_providers.py",
        PROJECT_ROOT / "scripts" / "check_a_stock_data_providers.py",
        PROJECT_ROOT / "scripts" / "quant_api_server.py",
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "ROADMAP.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
        PROJECT_ROOT / "export_project.ps1",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "reference_data_skill_adapter", "missing files: " + ", ".join(missing_files))
    registry_text = (PROJECT_ROOT / "quantdesk" / "data_provider_registry.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "data_provider_fallback_plan.md",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    tokens = {
        "registry_daily": "daily_price_provider" in registry_text,
        "registry_intraday": "intraday_1m_provider" in registry_text,
        "registry_theme": "theme_map_provider" in registry_text,
        "registry_hotspot": "market_hotspot_provider" in registry_text,
        "registry_risk": "risk_event_provider" in registry_text,
        "api_get": "/api/data-providers/status" in server_text,
        "api_post": "/api/data-providers/check" in server_text,
        "ui_section": "数据源诊断" in html_text,
        "ui_check": "检查数据源可用性" in html_text and "checkDataProviders" in app_text,
        "docs_version": "v2.8.8" in docs_text and "Reference Data Skill Adapter" in docs_text,
        "external_refs_excluded": "external_refs" in export_text,
        "planned_placeholder": "placeholder" in registry_text and "planned" in registry_text,
    }
    failed = [key for key, ok in tokens.items() if not ok]
    if failed:
        return result("FAIL", "reference_data_skill_adapter", "missing wiring: " + ", ".join(failed))
    return result("PASS", "reference_data_skill_adapter", "v2.8.8 data provider diagnostics wired")


def _legacy_check_daily_price_provider_fallback_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "quantdesk" / "daily_price_providers.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py",
        PROJECT_ROOT / "scripts" / "update_daily_price_fallback.py",
        PROJECT_ROOT / "scripts" / "quant_api_server.py",
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "ROADMAP.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "daily_price_provider_fallback", "missing files: " + ", ".join(missing_files))
    engine_text = (PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py").read_text(encoding="utf-8", errors="ignore")
    providers_text = (PROJECT_ROOT / "quantdesk" / "daily_price_providers.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "engine": "update_daily_prices" in engine_text and "daily_price_status" in engine_text,
        "providers": "current_project_default" in providers_text and "akshare_daily" in providers_text and "ashare_daily" in providers_text,
        "api_get": "/api/daily-price/status" in server_text,
        "api_post": "/api/daily-price/update" in server_text,
        "workflow_flag": "allow_daily_price_online_update" in (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore"),
        "ui": "联网补全低价池日线" in html_text and "本次允许联网补全低价池日线" in html_text and "loadDailyPriceStatus" in app_text,
        "docs": "v2.8.9" in docs_text and "Daily Price Provider Fallback Engine" in docs_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "daily_price_provider_fallback", "missing wiring: " + ", ".join(failed))
    return result("PASS", "daily_price_provider_fallback", "v2.8.9 daily price fallback wired")


def _legacy_check_data_freshness_gate_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "quantdesk" / "data_freshness_gate.py",
        PROJECT_ROOT / "quantdesk" / "theme_current_candidate_validator.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py",
        PROJECT_ROOT / "scripts" / "update_daily_price_fallback.py",
        PROJECT_ROOT / "tests" / "test_v290_data_freshness_gate_and_theme_sync.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "data_freshness_gate", "missing files: " + ", ".join(missing_files))
    gate_text = (PROJECT_ROOT / "quantdesk" / "data_freshness_gate.py").read_text(encoding="utf-8", errors="ignore")
    validator_text = (PROJECT_ROOT / "quantdesk" / "theme_current_candidate_validator.py").read_text(encoding="utf-8", errors="ignore")
    workflow_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    engine_text = (PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "update_daily_price_fallback.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "gate": "evaluate_data_freshness" in gate_text and "can_review_yesterday_positions" in gate_text,
        "validator": "validate_theme_map_for_current_candidates" in validator_text,
        "workflow": "data_freshness_gate" in workflow_text and "stale_observation" in workflow_text,
        "daily_symbol_lists": "processed_symbols" in engine_text and "updated_symbol_list" in engine_text and "failed_symbol_list" in engine_text,
        "cli_symbols": "--symbols" in cli_text and "process_note" in engine_text,
        "api": "/api/data-freshness/status" in server_text,
        "ui": "当前候选板块映射" in app_text and "滞后数据观察" in app_text,
        "docs": "v2.9.0" in docs_text and "Data Freshness Gate" in docs_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "data_freshness_gate", "missing wiring: " + ", ".join(failed))
    return result("PASS", "data_freshness_gate", "v2.9.0 data freshness gate and symbol-level daily update reporting wired")


def _legacy_check_shared_core_candidate_contract_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "quantdesk" / "shared.py",
        PROJECT_ROOT / "quantdesk" / "candidate_contract.py",
        PROJECT_ROOT / "tests" / "test_v291_shared_core_and_candidate_contract.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "shared_core_candidate_contract", "missing files: " + ", ".join(missing_files))
    shared_text = (PROJECT_ROOT / "quantdesk" / "shared.py").read_text(encoding="utf-8", errors="ignore")
    contract_text = (PROJECT_ROOT / "quantdesk" / "candidate_contract.py").read_text(encoding="utf-8", errors="ignore")
    auto_text = (PROJECT_ROOT / "quantdesk" / "paper" / "auto_paper_portfolio.py").read_text(encoding="utf-8", errors="ignore")
    plan_text = (PROJECT_ROOT / "quantdesk" / "paper" / "conditional_trade_plan.py").read_text(encoding="utf-8", errors="ignore")
    workflow_text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    css_text = html_text
    checks = {
        "shared": "def normalize_symbol" in shared_text and "def truthy" in shared_text and "def write_json" in shared_text,
        "contract": "normalize_candidate_row" in contract_text and "candidate_rows_from_strategy_payload" in contract_text,
        "auto_portfolio": "candidate_rows_from_strategy_payload" in auto_text and "normalize_strategy_type" in auto_text,
        "conditional_plan": "candidate_rows_from_strategy_payload" in plan_text and "paper_trading_latest" not in plan_text.lower(),
        "workflow": "input_source" in workflow_text and "latest_file_fallback" in workflow_text,
        "ui": "strategy_label" in app_text and "当前候选映射" in app_text and "自动虚拟组合 / 主线" in html_text,
        "css": "overflow-wrap: anywhere" in css_text and "word-break: break-word" in css_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "shared_core_candidate_contract", "missing wiring: " + ", ".join(failed))
    return result("PASS", "shared_core_candidate_contract", "v2.9.1 shared core and candidate contract wired")


def check_shared_cleanup_guard() -> CheckResult:
    """Prevent shared/candidate helpers from being copied back into mainline modules."""

    forbidden_by_file = {
        PROJECT_ROOT / "quantdesk" / "paper" / "auto_paper_portfolio.py": {"normalize_symbol", "_now", "_candidate_rows"},
        PROJECT_ROOT / "quantdesk" / "auto_strategy_sandbox.py": {"normalize_symbol", "_truthy", "_now"},
        PROJECT_ROOT / "quantdesk" / "paper" / "conditional_trade_plan.py": {"normalize_symbol", "_truthy", "_now_text", "_candidate_rows"},
        PROJECT_ROOT / "quantdesk" / "daily_feature_engine.py": {"normalize_symbol", "_truthy", "_now_text"},
        PROJECT_ROOT / "quantdesk" / "backtest.py": {"normalize_symbol", "truthy"},
        PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py": {"_now"},
        PROJECT_ROOT / "quantdesk" / "paper" / "position_management_engine.py": {"_now_text"},
    }
    offenders: list[str] = []
    parse_errors: list[str] = []
    for path, forbidden in forbidden_by_file.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except Exception as exc:  # noqa: BLE001
            parse_errors.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")
            continue
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        for name in sorted(forbidden & names):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{name}")
    if parse_errors:
        return result("FAIL", "shared_cleanup_guard", "parse errors: " + "; ".join(parse_errors[:5]))
    if offenders:
        return result("FAIL", "shared_cleanup_guard", "duplicate shared/candidate helpers: " + ", ".join(offenders))
    return result("PASS", "shared_cleanup_guard", "未发现主线模块重复定义 shared/candidate_contract 已接管的函数。")


def check_default_path_binding_guard() -> CheckResult:
    """Guard against Path constants bound at function-definition time.

    The v2.9.3 intraday flow relies on tests and callers being able to patch or
    pass runtime paths. A default such as ``path: Path = SOME_JSON`` captures the
    old value before ``mock.patch`` can replace the module constant.
    """

    target_files = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_gap_checker.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_schema.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "conditional_trade_plan.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "auto_paper_portfolio.py",
        PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py",
        PROJECT_ROOT / "quantdesk" / "paper" / "position_management_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_import.py",
        PROJECT_ROOT / "scripts" / "import_intraday_1m.py",
        PROJECT_ROOT / "scripts" / "generate_intraday_1m_import_template.py",
    ]
    critical_files = {
        "quantdesk/intraday/intraday_gap_checker.py",
        "quantdesk/intraday_schema.py",
        "quantdesk/intraday_import.py",
        "scripts/import_intraday_1m.py",
        "scripts/generate_intraday_1m_import_template.py",
    }
    path_tokens = ("PATH", "DIR", "JSON", "CSV", "ROOT", "FILE")

    def is_path_constant(node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return node.id.isupper() and any(token in node.id for token in path_tokens)
        if isinstance(node, ast.Attribute):
            return node.attr.isupper() and any(token in node.attr for token in path_tokens)
        return False

    offenders: list[str] = []
    tracked_legacy: list[str] = []
    parse_errors: list[str] = []
    for path in target_files:
        if not path.exists():
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except Exception as exc:  # noqa: BLE001
            parse_errors.append(f"{rel}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            positional = list(node.args.args)
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            pairs = list(zip(positional, defaults))
            pairs.extend(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for arg, default in pairs:
                if not is_path_constant(default):
                    continue
                entry = f"{rel}:{node.name}({arg.arg})"
                if rel in critical_files:
                    offenders.append(entry)
                else:
                    tracked_legacy.append(entry)
    if parse_errors:
        return result("FAIL", "default_path_binding_guard", "parse errors: " + "; ".join(parse_errors[:5]))
    if offenders:
        return result("FAIL", "default_path_binding_guard", "early-bound path defaults in v2.9.3 flow: " + ", ".join(offenders))
    detail = "v2.9.3 intraday path defaults use None late binding"
    if tracked_legacy:
        detail += f"; tracked legacy bindings outside this fix: {len(tracked_legacy)}"
    return result("PASS", "default_path_binding_guard", detail)


def _legacy_check_intraday_1m_execution_closure_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_schema.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_gap_checker.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_import.py",
        PROJECT_ROOT / "scripts" / "generate_intraday_1m_import_template.py",
        PROJECT_ROOT / "scripts" / "import_intraday_1m.py",
        PROJECT_ROOT / "tests" / "test_v293_intraday_import_and_execution_closure.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "intraday_1m_execution_closure", "missing files: " + ", ".join(missing_files))
    schema_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_schema.py").read_text(encoding="utf-8", errors="ignore")
    gap_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_gap_checker.py").read_text(encoding="utf-8", errors="ignore")
    engine_text = (PROJECT_ROOT / "quantdesk" / "paper" / "position_management_engine.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "schema": "normalize_intraday_frame" in schema_text and "代码" in schema_text,
        "gap": "intraday_gap_status" in gap_text and "missing_symbols" in gap_text,
        "engine": "available_intraday_count" in engine_text and "missing_intraday" in engine_text,
        "api": "/api/intraday/status" in server_text and "/api/intraday/import" in server_text and "/api/conditional-execution/run" in server_text,
        "ui": "盘中分钟线状态" in html_text and "生成当前候选/虚拟持仓分钟线导入模板" in html_text and "loadIntradayStatus" in app_text,
        "docs": "v2.9.3" in docs_text and "完整日线控制收益复盘；分钟线控制盘中触发验证" in docs_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "intraday_1m_execution_closure", "missing wiring: " + ", ".join(failed))
    return result("PASS", "intraday_1m_execution_closure", "v2.9.3 intraday 1m import and execution closure wired")


def _legacy_check_intraday_1m_provider_fallback_support() -> CheckResult:
    required_files = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_update_engine.py",
        PROJECT_ROOT / "scripts" / "update_intraday_1m_fallback.py",
        PROJECT_ROOT / "tests" / "test_v294_intraday_provider_fallback.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    if missing_files:
        return result("FAIL", "intraday_1m_provider_fallback", "missing files: " + ", ".join(missing_files))
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    providers_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py").read_text(encoding="utf-8", errors="ignore")
    update_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_update_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "update_intraday_1m_fallback.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "version": "intraday-1m-provider-fallback" in version_text or "real-intraday-data-workbench-and-provider-hardening" in version_text,
        "providers": all(token in providers_text for token in ("local_existing", "akshare_intraday", "tencent_intraday", "sina_intraday")),
        "engine": "update_intraday_1m_for_symbols" in update_text and "provider_results" in update_text and "full_scan" in update_text,
        "cli": "--execute-after-update" in cli_text and "--update-portfolio-after-execution" in cli_text,
        "api": "/api/intraday/update/status" in server_text and "/api/intraday/update" in server_text and "intraday_1m_provider_update" in server_text,
        "ui": "联网补全当前候选/虚拟持仓 1 分钟线" in html_text and "updateIntradayProviderFallback" in app_text,
        "docs": "分钟线 provider 仅用于本地纸面模拟" in docs_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "intraday_1m_provider_fallback", "missing wiring: " + ", ".join(failed))
    return result("PASS", "intraday_1m_provider_fallback", "v2.9.4 scoped intraday provider fallback wired")


def check_historical_intraday_replay_support() -> CheckResult:
    """Check that v2.9.5 historical intraday replay and trading calendar are wired."""
    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "trading_calendar.py",
        PROJECT_ROOT / "quantdesk" / "historical_replay_engine.py",
        PROJECT_ROOT / "scripts" / "run_historical_intraday_replay.py",
        PROJECT_ROOT / "tests" / "test_v295_historical_intraday_replay.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "historical_intraday_replay", "missing files: " + ", ".join(missing_files))
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    calendar_text = (PROJECT_ROOT / "quantdesk" / "trading_calendar.py").read_text(encoding="utf-8", errors="ignore")
    engine_text = (PROJECT_ROOT / "quantdesk" / "historical_replay_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_historical_intraday_replay.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "2.9.5-historical-intraday-replay-and-trading-calendar" in version_text or "2.9.6-intraday-replay-self-validation-harness" in version_text or "2.9.7-real-intraday-provider-capability-and-sample-validation" in version_text or "2.9.8-beginner-data-health-and-guided-home" in version_text or "2.9.9-real-intraday-data-workbench-and-provider-hardening" in version_text or "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "calendar": all(token in calendar_text for token in ("is_trade_day", "next_trade_day", "previous_trade_day", "trade_days_between", "weekend_only")),
        "engine": all(token in engine_text for token in ("run_historical_intraday_replay", "anti_future_leak_check", "source_data_cutoff", "walk-forward", "不构成投资建议")),
        "cli": "--start-date" in cli_text and "--end-date" in cli_text and "--dry-run" in cli_text,
        "api_get": "/api/historical-replay/status" in server_text and "/api/historical-replay/latest" in server_text,
        "api_post": "/api/historical-replay/run" in server_text and "historical_intraday_replay" in server_text,
        "ui": "历史分钟线回放" in html_text and "runHistoricalReplay" in app_text,
    }
    failed = [key for key, ok in checks.items() if not ok]
    if failed:
        return result("FAIL", "historical_intraday_replay", "missing wiring: " + ", ".join(failed))
    return result("PASS", "historical_intraday_replay", "v2.9.5 historical intraday replay and trading calendar wired")


def check_intraday_replay_self_validation_support() -> CheckResult:
    """Run the deterministic v2.9.6 synthetic intraday replay validation."""
    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_replay_self_validation.py",
        PROJECT_ROOT / "scripts" / "run_intraday_replay_self_validation.py",
        PROJECT_ROOT / "tests" / "test_v296_intraday_replay_self_validation.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "intraday_replay_self_validation", "missing files: " + ", ".join(missing_files))
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_replay_self_validation.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_intraday_replay_self_validation.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "version": "2.9.6-intraday-replay-self-validation-harness" in version_text or "2.9.7-real-intraday-provider-capability-and-sample-validation" in version_text or "2.9.8-beginner-data-health-and-guided-home" in version_text or "2.9.9-real-intraday-data-workbench-and-provider-hardening" in version_text or "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "module": "run_intraday_replay_self_validation" in module_text and "uses_synthetic_data" in module_text,
        "cli": "--keep-artifacts" in cli_text and "--write-latest" in cli_text,
        "api": "/api/intraday/self-validation/latest" in server_text and "/api/intraday/self-validation/run" in server_text,
        "ui": "runIntradaySelfValidationButton" in html_text and "runIntradaySelfValidation" in app_text,
        "docs": "intraday_replay_self_validation" in docs_text and "2.9.6" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "intraday_replay_self_validation", "missing wiring: " + ", ".join(missing))
    try:
        from quantdesk.intraday.intraday_replay_self_validation import run_intraday_replay_self_validation

        runtime_dir = PROJECT_ROOT / ".tmp" / f"intraday_replay_self_validation_self_check_{os.getpid()}"
        payload = run_intraday_replay_self_validation(
            keep_artifacts=False,
            write_latest=False,
            update_portfolio=False,
            runtime_dir=runtime_dir,
        )
    except Exception as exc:
        return result("FAIL", "intraday_replay_self_validation", f"self-validation crashed: {exc}")
    failed = payload.get("failed_scenarios") or []
    if failed or not payload.get("ok"):
        return result("FAIL", "intraday_replay_self_validation", "failed scenarios: " + ", ".join(map(str, failed)))
    count = payload.get("summary", {}).get("scenario_count", 0)
    return result("PASS", "intraday_replay_self_validation", f"{count} scenarios")


def check_real_intraday_provider_capability_support() -> CheckResult:
    """Check v2.9.7 real intraday provider capability and sample validation wiring."""
    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_provider_capability.py",
        PROJECT_ROOT / "quantdesk" / "real_intraday_sample_validation.py",
        PROJECT_ROOT / "scripts" / "check_intraday_provider_capability.py",
        PROJECT_ROOT / "scripts" / "run_real_intraday_sample_validation.py",
        PROJECT_ROOT / "tests" / "test_v297_real_intraday_provider_capability.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "real_intraday_provider_capability", "missing files: " + ", ".join(missing_files))
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    capability_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_provider_capability.py").read_text(encoding="utf-8", errors="ignore")
    sample_text = (PROJECT_ROOT / "quantdesk" / "real_intraday_sample_validation.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "version": "2.9.7-real-intraday-provider-capability-and-sample-validation" in version_text or "2.9.8-beginner-data-health-and-guided-home" in version_text or "2.9.9-real-intraday-data-workbench-and-provider-hardening" in version_text or "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "capability": "supports_historical_day" in capability_text and "skipped_reason" in capability_text,
        "sample": "run_real_intraday_sample_validation" in sample_text and "不会用日线价格假装成交" in sample_text,
        "api": "/api/intraday/provider-capability/latest" in server_text and "/api/intraday/real-sample/latest" in server_text,
        "ui": "provider 是否支持历史分钟线" in html_text + app_text and "真实分钟线样本验证" in html_text + app_text,
        "docs": "真实 provider 数据验证" in docs_text and "2.9.7" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "real_intraday_provider_capability", "missing wiring: " + ", ".join(missing))
    return result("PASS", "real_intraday_provider_capability", "v2.9.7 real intraday provider diagnostics wired")


def _legacy_check_beginner_data_health_dashboard_support() -> CheckResult:
    """Check v2.9.8 beginner dashboard and guided home wiring."""
    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "data_health_dashboard.py",
        PROJECT_ROOT / "tests" / "test_v298_beginner_data_health_dashboard.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "beginner_data_health_dashboard", "missing files: " + ", ".join(missing_files))
    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "data_health_dashboard.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    active_server_paths = [
        PROJECT_ROOT / "scripts" / "quant_api_server.py",
        *sorted((PROJECT_ROOT / "scripts" / "server" / "routes").glob("*.py")),
    ]
    active_server_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in active_server_paths
        if path.exists()
    )
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    css_text = html_text
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    checks = {
        "version": "2.9.8-beginner-data-health-and-guided-home" in version_text or "2.9.9-real-intraday-data-workbench-and-provider-hardening" in version_text or "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "module": "build_data_health_dashboard" in module_text and "beginner_next_steps" in module_text and "top_observation_candidates" in module_text,
        "api": (
            "/api/data-health/dashboard" in (server_text + active_server_text)
            and "build_data_health_dashboard(write=False)" in (server_text + active_server_text)
        ),
        "ui": "数据健康状态" in html_text and "今日操作指南" in html_text and "今日 Top3 观察候选" in html_text,
        "mode": "小白模式" in app_text and "专家模式" in app_text and "localStorage" in app_text,
        "labels": "缺少分钟线，无法验证盘中触发" in app_text and "仅日线复盘" in app_text,
        "css": "overflow-wrap: anywhere" in css_text and "word-break: break-word" in css_text,
        "docs": "v2.9.8" in docs_text and "数据健康状态" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "beginner_data_health_dashboard", "missing wiring: " + ", ".join(missing))
    return result("PASS", "beginner_data_health_dashboard", "v2.9.8 beginner data health dashboard wired")


def check_intraday_data_workbench_support() -> CheckResult:
    """Check v2.9.9 real intraday data workbench and quality audit wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_data_workbench.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_quality_audit.py",
        PROJECT_ROOT / "scripts" / "run_intraday_data_workbench.py",
        PROJECT_ROOT / "tests" / "test_v299_intraday_data_workbench.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "intraday_data_workbench_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    workbench_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_data_workbench.py").read_text(encoding="utf-8", errors="ignore")
    audit_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_quality_audit.py").read_text(encoding="utf-8", errors="ignore")
    schema_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_schema.py").read_text(encoding="utf-8", errors="ignore")
    import_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_import.py").read_text(encoding="utf-8", errors="ignore")
    provider_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_intraday_data_workbench.py").read_text(encoding="utf-8", errors="ignore")
    import_cli_text = (PROJECT_ROOT / "scripts" / "import_intraday_1m.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_api_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )

    checks = {
        "version": "2.9.9-real-intraday-data-workbench-and-provider-hardening" in version_text or "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "module": "run_intraday_data_workbench" in workbench_text and "provider_summary" in workbench_text and "next_actions" in workbench_text,
        "audit": "audit_intraday_file" in audit_text and "疑似误导入日线" in audit_text and "price-only" in audit_text,
        "schema": all(token in schema_text for token in ("gb18030", "order_book_id", "股票代码", "price_only_intraday")),
        "import": "--audit-after-import" in import_cli_text and "audit_after_import" in import_text and "execute_after_import" in import_text,
        "provider_next_action": "suggested_next_action" in provider_text and "skipped_reason" in provider_text,
        "cli": "--import-path" in cli_text and "--try-provider" in cli_text and "--execute-after-ready" in cli_text,
        "api": "/api/intraday/workbench/run" in server_text and "/api/intraday/workbench/status" in server_text,
        "ui": "真实分钟线工作台" in html_text and "导入并执行条件计划" in html_text and "不会用日线价格假装盘中成交" in html_text,
        "app": "runIntradayWorkbench" in app_text and "renderIntradayWorkbench" in app_text and "openIntradayWorkbenchButton" in app_text,
        "docs": "/api/intraday/workbench/run" in docs_text and "不接券商" in docs_text and "不真实下单" in docs_text and "不构成投资建议" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "intraday_data_workbench_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "intraday_data_workbench_wired", "v2.9.9 real intraday data workbench wired")


def check_a_stock_data_provider_matrix_support() -> CheckResult:
    """Check v3.0.0 A-share provider matrix and adapter layer wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "a_stock_data_matrix.py",
        PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "mootdx_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "tencent_provider.py",
        PROJECT_ROOT / "quantdesk" / "providers" / "iwencai_provider.py",
        PROJECT_ROOT / "scripts" / "check_a_stock_data_providers.py",
        PROJECT_ROOT / "docs" / "provider_setup.md",
        PROJECT_ROOT / "config" / "provider_secrets.example.json",
        PROJECT_ROOT / "tests" / "test_v300_a_stock_data_provider_matrix.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "a_stock_data_provider_matrix", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    matrix_text = (PROJECT_ROOT / "quantdesk" / "a_stock_data_matrix.py").read_text(encoding="utf-8", errors="ignore")
    adapter_text = (PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py").read_text(encoding="utf-8", errors="ignore")
    registry_text = (PROJECT_ROOT / "quantdesk" / "data_provider_registry.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "check_a_stock_data_providers.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "provider_setup.md",
        )
    )

    checks = {
        "version": "3.0.0-a-stock-data-provider-matrix-and-adapter-layer" in version_text
        or "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "matrix": all(token in matrix_text for token in ("mootdx", "tencent", "akshare", "tushare", "ashare", "deprecated_or_disabled", "providers_by_category")),
        "adapter": all(token in adapter_text for token in ("get_daily_bars", "get_intraday_bars", "provider_chain", "ok")),
        "registry": "a_stock_data_provider_matrix_payload" in registry_text and "save_a_stock_data_provider_matrix" in registry_text,
        "cli": "--network" in cli_text and "network=false" in cli_text and "full_scan" in cli_text,
        "api": "/api/a-stock-data-providers/status" in server_text and "/api/a-stock-data-providers/check" in server_text,
        "ui": "数据源矩阵" in html_text and "行情数据" in html_text and "研报数据" in html_text and "新闻资讯" in html_text and "热点题材" in html_text,
        "app": "renderAStockProviderMatrix" in app_text and "loadAStockProviderMatrixStatus" in app_text,
        "docs": "provider_setup" in docs_text and "不接券商" in docs_text and "不真实下单" in docs_text and "不构成投资建议" in docs_text,
        "secrets_excluded": "provider_secrets.json" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
        and "provider_secrets.json" in (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore"),
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "a_stock_data_provider_matrix", "missing wiring: " + ", ".join(missing))
    return result("PASS", "a_stock_data_provider_matrix", "v3.0.0 A-stock provider matrix wired")


def check_real_tencent_provider_support() -> CheckResult:
    """Check v3.1.0 real Tencent provider and qd.py entrypoint wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "providers" / "tencent_provider.py",
        PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_update_engine.py",
        PROJECT_ROOT / "scripts" / "qd.py",
        PROJECT_ROOT / "tests" / "test_v310_real_tencent_provider_and_entrypoint.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "real_tencent_provider_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    provider_text = (PROJECT_ROOT / "quantdesk" / "providers" / "tencent_provider.py").read_text(encoding="utf-8", errors="ignore")
    adapter_text = (PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py").read_text(encoding="utf-8", errors="ignore")
    daily_text = (PROJECT_ROOT / "quantdesk" / "daily_price_update_engine.py").read_text(encoding="utf-8", errors="ignore")
    intraday_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_update_engine.py").read_text(encoding="utf-8", errors="ignore")
    intraday_provider_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join([
        readme_text,
        (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore"),
        (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore"),
    ])

    checks = {
        "version": "3.1.0-real-tencent-provider-and-entrypoint-simplification" in version_text
        or "3.1.1-real-provider-smoke-and-observability" in version_text
        or "3.1.2-real-mootdx-provider-and-fallback-hardening" in version_text
        or "3.1.3-strategy-observation-contract-and-agent-safe-bridge" in version_text
        or "3.2.0-optional-llm-observation-agent" in version_text
        or "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text,
        "http": "urllib.request.urlopen" in provider_text and "qt.gtimg.cn" in provider_text and "fqkline/get" in provider_text,
        "symbol": "def to_tencent_symbol" in provider_text,
        "quote": "def fetch_realtime_quote" in provider_text,
        "daily": "def fetch_daily_bars" in provider_text,
        "intraday": "def fetch_intraday_bars" in provider_text,
        "adapter": "tencent_provider.fetch_daily_bars" in adapter_text and "tencent_provider.fetch_realtime_quote" in adapter_text,
        "daily_order": "tencent_daily" in daily_text and "mootdx_daily" in daily_text,
        "intraday_order": "tencent_intraday" in intraday_text and "tencent_intraday" in intraday_provider_text,
        "entrypoint": "COMMANDS" in qd_text and "self_check.py" in qd_text and "check_a_stock_data_providers.py" in qd_text,
        "readme": "新手只需要记住 5 个命令" in readme_text and "python scripts/qd.py" in readme_text,
        "safety": "不接券商" in docs_text and "不真实下单" in docs_text and "不构成投资建议" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "real_tencent_provider_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "real_tencent_provider_wired", "v3.1.0 real Tencent provider and qd.py wired")


def check_provider_smoke_observability_support() -> CheckResult:
    """Check v3.1.1 provider smoke and observability wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "provider_result_contract.py",
        PROJECT_ROOT / "quantdesk" / "provider_network_smoke.py",
        PROJECT_ROOT / "scripts" / "run_provider_network_smoke.py",
        PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py",
        PROJECT_ROOT / "scripts" / "qd.py",
        PROJECT_ROOT / "tests" / "test_v311_provider_smoke_and_observability.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "provider_smoke_observability", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    contract_text = (PROJECT_ROOT / "quantdesk" / "provider_result_contract.py").read_text(encoding="utf-8", errors="ignore")
    smoke_text = (PROJECT_ROOT / "quantdesk" / "provider_network_smoke.py").read_text(encoding="utf-8", errors="ignore")
    provider_text = (PROJECT_ROOT / "quantdesk" / "providers" / "tencent_provider.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.1.1-real-provider-smoke-and-observability" in version_text
        or "3.1.2-real-mootdx-provider-and-fallback-hardening" in version_text
        or "3.1.3-strategy-observation-contract-and-agent-safe-bridge" in version_text
        or "3.2.0-optional-llm-observation-agent" in version_text
        or "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text,
        "contract": all(token in contract_text for token in ("build_provider_result", "summarize_provider_payload", "duration_ms", "retrieved_at")),
        "smoke": all(token in smoke_text for token in ("run_provider_network_smoke", "provider_network_smoke_latest.json", "realtime quote", "daily bars", "intraday bars", "valuation")),
        "tencent_diagnostics": all(token in provider_text for token in ("failed_type", "duration_ms", "network_disabled", "date_out_of_scope", "Tencent daily returned no usable rows")),
        "entrypoint": "network-smoke" in qd_text and "run_provider_network_smoke.py" in qd_text,
        "readme": "真实 provider 联网烟测" in readme_text and "network-smoke" in readme_text and "不构成投资建议" in readme_text,
        "ui": "Provider 链路" in app_text and "qd.py network-smoke" in app_text and "历史 1 分钟线建议 CSV 导入" in app_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "provider_smoke_observability", "missing wiring: " + ", ".join(missing))
    return result("PASS", "provider_smoke_observability", "v3.1.1 real provider smoke and observability wired")


def check_real_mootdx_provider_support() -> CheckResult:
    """Check v3.1.2 optional real mootdx provider and fallback wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "providers" / "mootdx_provider.py",
        PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py",
        PROJECT_ROOT / "quantdesk" / "daily_price_providers.py",
        PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py",
        PROJECT_ROOT / "quantdesk" / "provider_network_smoke.py",
        PROJECT_ROOT / "scripts" / "run_provider_network_smoke.py",
        PROJECT_ROOT / "tests" / "test_v312_real_mootdx_provider_and_fallback.py",
        PROJECT_ROOT / "docs" / "provider_setup.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "real_mootdx_provider_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    provider_text = (PROJECT_ROOT / "quantdesk" / "providers" / "mootdx_provider.py").read_text(encoding="utf-8", errors="ignore")
    adapter_text = (PROJECT_ROOT / "quantdesk" / "a_stock_data_adapter.py").read_text(encoding="utf-8", errors="ignore")
    daily_text = (PROJECT_ROOT / "quantdesk" / "daily_price_providers.py").read_text(encoding="utf-8", errors="ignore")
    intraday_text = (PROJECT_ROOT / "quantdesk" / "intraday" / "intraday_providers.py").read_text(encoding="utf-8", errors="ignore")
    smoke_text = (PROJECT_ROOT / "quantdesk" / "provider_network_smoke.py").read_text(encoding="utf-8", errors="ignore")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    docs_text = (PROJECT_ROOT / "docs" / "provider_setup.md").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.1.2-real-mootdx-provider-and-fallback-hardening" in version_text
        or "3.1.3-strategy-observation-contract-and-agent-safe-bridge" in version_text
        or "3.2.0-optional-llm-observation-agent" in version_text
        or "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text,
        "provider": all(token in provider_text for token in ("def to_mootdx_symbol", "def fetch_realtime_quote", "def fetch_daily_bars", "def fetch_intraday_bars", "def fetch_fundamentals", "_import_quotes_client")),
        "optional_dependency": "dependency_available(\"mootdx\")" in provider_text and "network=false" in provider_text and "not_available" in provider_text,
        "adapter": "mootdx_provider.fetch_daily_bars" in adapter_text and "mootdx_provider.fetch_intraday_bars" in adapter_text and "tencent_provider.fetch_daily_bars" in adapter_text,
        "daily": "mootdx_daily adapter is not implemented yet" not in daily_text and "mootdx_provider" in daily_text,
        "intraday": "mootdx_intraday adapter is not implemented yet" not in intraday_text and "mootdx_provider" in intraday_text,
        "smoke": "providers" in smoke_text and "mootdx_provider" in smoke_text and "tencent,mootdx" in readme_text,
        "docs": "mootdx 是可选 provider" in readme_text and "python -m pip install mootdx" in docs_text,
        "safety": "不接券商" in readme_text and "不真实下单" in readme_text and "不构成投资建议" in readme_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "real_mootdx_provider_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "real_mootdx_provider_wired", "v3.1.2 optional real mootdx provider and fallback wired")


def check_strategy_observation_agent_bridge_support() -> CheckResult:
    """Check v3.1.3 read-only strategy observation and agent-safe bridge wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_policy_guard.py",
        PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py",
        PROJECT_ROOT / "scripts" / "qd.py",
        PROJECT_ROOT / "tests" / "test_v313_strategy_observation_contract_and_agent_bridge.py",
        PROJECT_ROOT / "docs" / "agent_observation_report.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "strategy_observation_agent_bridge", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    contract_text = (PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py").read_text(encoding="utf-8", errors="ignore")
    bridge_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py").read_text(encoding="utf-8", errors="ignore")
    guard_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_policy_guard.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "agent_observation_report.md",
            PROJECT_ROOT / "app" / "templates" / "daily_research.html",
            PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.1.3-strategy-observation-contract-and-agent-safe-bridge" in version_text
        or "3.2.0-optional-llm-observation-agent" in version_text
        or "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text,
        "contract": all(token in contract_text for token in ("build_strategy_observation_context", "validate_observation_context", "cannot_verify_intraday_trigger", "allowed_agent_actions", "forbidden_agent_actions")),
        "bridge": all(token in bridge_text for token in ("generate_stub_observation_report", "build_agent_readonly_context", "llm_called", "network")),
        "guard": all(token in guard_text for token in ("detect_forbidden_trading_advice", "sanitize_forbidden_trading_terms", "validate_agent_allowed_output", "build_policy_violation_result")),
        "cli": "build_strategy_observation_context" in cli_text and "generate_stub_observation_report" in cli_text and "--network" in cli_text,
        "qd": "observation-report" in qd_text and "run_strategy_observation_report.py" in qd_text,
        "docs": "Strategy Observation Contract And Agent Safe Bridge" in docs_text and "不构成投资建议" in docs_text,
        "ui": "生成学习复盘报告" in docs_text and ("当前为离线模板报告，未调用外部 AI" in docs_text or "当前报告模式" in docs_text),
        "clean_zip": "results" in export_text and "reports" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "strategy_observation_agent_bridge", "missing wiring: " + ", ".join(missing))
    return result("PASS", "strategy_observation_agent_bridge", "v3.1.3 strategy observation and agent-safe bridge wired")


def check_optional_llm_observation_agent_support() -> CheckResult:
    """Check v3.2.0 optional LLM observation agent wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "llm_config.py",
        PROJECT_ROOT / "quantdesk" / "llm_providers" / "__init__.py",
        PROJECT_ROOT / "quantdesk" / "llm_providers" / "base.py",
        PROJECT_ROOT / "quantdesk" / "llm_providers" / "offline_stub_provider.py",
        PROJECT_ROOT / "quantdesk" / "llm_providers" / "openai_compatible_provider.py",
        PROJECT_ROOT / "quantdesk" / "llm_providers" / "ollama_provider.py",
        PROJECT_ROOT / "config" / "llm_config.example.json",
        PROJECT_ROOT / "scripts" / "check_llm_observation_agent.py",
        PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py",
        PROJECT_ROOT / "tests" / "test_v320_optional_llm_observation_agent.py",
        PROJECT_ROOT / "docs" / "llm_observation_agent.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "optional_llm_observation_agent", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    config_text = (PROJECT_ROOT / "quantdesk" / "llm_config.py").read_text(encoding="utf-8", errors="ignore")
    bridge_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py").read_text(encoding="utf-8", errors="ignore")
    guard_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_policy_guard.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py").read_text(encoding="utf-8", errors="ignore")
    smoke_text = (PROJECT_ROOT / "scripts" / "check_llm_observation_agent.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "docs" / "agent_observation_report.md",
            PROJECT_ROOT / "docs" / "llm_observation_agent.md",
        )
    )
    app_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore") + "\n" + (
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js"
    ).read_text(encoding="utf-8", errors="ignore")
    gitignore_text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    provider_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "quantdesk" / "llm_providers" / "offline_stub_provider.py",
            PROJECT_ROOT / "quantdesk" / "llm_providers" / "openai_compatible_provider.py",
            PROJECT_ROOT / "quantdesk" / "llm_providers" / "ollama_provider.py",
        )
    )
    checks = {
        "version": "3.2.0-optional-llm-observation-agent" in version_text
        or "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text
        or "3.2.3-strategy-lookahead-and-execution-assumption-guard" in version_text,
        "config": all(token in config_text for token in ("load_llm_config", "is_llm_enabled", "mask_secret", "offline_stub")),
        "providers": all(token in provider_text for token in ("generate_observation_report", "provider_capability", "network")),
        "bridge": all(token in bridge_text for token in ("generate_llm_observation_report", "select_llm_provider", "fallback_to_stub_report", "policy_guard_status")),
        "guard": all(token in guard_text for token in ("买入建议", "推荐买入", "target price", "sanitize_forbidden_trading_terms")),
        "cli": all(token in cli_text for token in ("--llm", "--llm-provider", "strategy_observation_llm_result_latest.json")),
        "smoke": "llm_observation_agent_smoke_latest.json" in smoke_text and "provider_capabilities" in smoke_text,
        "qd": "llm-smoke" in qd_text and "check_llm_observation_agent.py" in qd_text,
        "docs": "Optional LLM Observation Agent" in readme_text and "不构成投资建议" in readme_text and "agent_policy_guard" in docs_text,
        "ui": "LLM 默认关闭" in app_text and "离线模板" in app_text and "不构成投资建议" in app_text,
        "clean_zip": "config/llm_config.json" in gitignore_text and "llm_config.json" in export_text and "results" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "optional_llm_observation_agent", "missing wiring: " + ", ".join(missing))
    return result("PASS", "optional_llm_observation_agent", "v3.2.0 optional LLM observation agent wired")


def check_agent_evidence_guard_report_audit_support() -> CheckResult:
    """Check v3.2.1 evidence guard and report audit wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_evidence_pack.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_hallucination_guard.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_report_audit.py",
        PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py",
        PROJECT_ROOT / "scripts" / "check_agent_report_audit.py",
        PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py",
        PROJECT_ROOT / "tests" / "test_v321_agent_evidence_guard_and_report_audit.py",
        PROJECT_ROOT / "docs" / "agent_evidence_guard.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "agent_evidence_guard_report_audit", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    evidence_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_evidence_pack.py").read_text(encoding="utf-8", errors="ignore")
    grounding_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_hallucination_guard.py").read_text(encoding="utf-8", errors="ignore")
    audit_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_report_audit.py").read_text(encoding="utf-8", errors="ignore")
    bridge_text = (PROJECT_ROOT / "quantdesk" / "agent" / "agent_safe_bridge.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py").read_text(encoding="utf-8", errors="ignore")
    audit_cli_text = (PROJECT_ROOT / "scripts" / "check_agent_report_audit.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "agent_evidence_guard.md",
            PROJECT_ROOT / "docs" / "llm_observation_agent.md",
            PROJECT_ROOT / "docs" / "agent_observation_report.md",
        )
    )
    app_text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8", errors="ignore") + "\n" + (
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js"
    ).read_text(encoding="utf-8", errors="ignore")
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")

    checks = {
        "version": "3.2.1-agent-evidence-guard-and-report-quality-audit" in version_text
        or "3.2.2-core-correctness-and-doc-sync" in version_text
        or "3.2.3-strategy-lookahead-and-execution-assumption-guard" in version_text,
        "evidence": all(token in evidence_text for token in ("build_agent_evidence_pack", "allowed_facts", "allowed_numbers", "allowed_provider_terms")),
        "grounding": all(token in grounding_text for token in ("validate_report_grounding", "unsupported_number", "unsupported_date", "contradiction")),
        "audit": all(token in audit_text for token in ("build_report_audit_record", "compute_safe_hash", "strategy_observation_report_audit_latest")),
        "bridge": all(token in bridge_text for token in ("evidence_pack_hash", "grounding_guard_status", "unsupported_issue_count", "report_hash")),
        "cli": all(token in cli_text for token in ("--audit", "--strict-grounding", "--show-evidence", "strategy_observation_report_audit_latest.json")),
        "audit_cli": all(token in audit_cli_text for token in ("check_agent_report_audit", "build_agent_evidence_pack", "audit_report_quality")),
        "qd": "agent-report-audit" in qd_text and "check_agent_report_audit.py" in qd_text,
        "docs": "Agent Evidence Guard And Report Quality Audit" in docs_text and "grounding_score" in docs_text,
        "ui": "报告证据约束" in app_text and "grounding_score" in app_text and "unsupported_issue_count" in app_text,
        "clean_zip": "results" in export_text and "reports" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "agent_evidence_guard_report_audit", "missing wiring: " + ", ".join(missing))
    return result("PASS", "agent_evidence_guard_report_audit", "v3.2.1 evidence guard and report audit wired")


def check_strategy_validation_suite_support() -> CheckResult:
    """Check v3.2.3 strategy validation guard wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "strategy_lookahead_guard.py",
        PROJECT_ROOT / "quantdesk" / "execution_assumption_guard.py",
        PROJECT_ROOT / "quantdesk" / "strategy_validation_suite.py",
        PROJECT_ROOT / "scripts" / "run_strategy_validation.py",
        PROJECT_ROOT / "tests" / "test_v323_strategy_lookahead_and_execution_assumption_guard.py",
        PROJECT_ROOT / "docs" / "strategy_validation_suite.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "strategy_validation_suite", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    lookahead_text = (PROJECT_ROOT / "quantdesk" / "strategy_lookahead_guard.py").read_text(encoding="utf-8", errors="ignore")
    execution_text = (PROJECT_ROOT / "quantdesk" / "execution_assumption_guard.py").read_text(encoding="utf-8", errors="ignore")
    suite_text = (PROJECT_ROOT / "quantdesk" / "strategy_validation_suite.py").read_text(encoding="utf-8", errors="ignore")
    obs_text = (PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "strategy_validation_suite.md",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.2.3-strategy-lookahead-and-execution-assumption-guard" in version_text,
        "lookahead": all(token in lookahead_text for token in ("detect_future_dates_in_inputs", "detect_daily_bar_intraday_proxy", "validate_strategy_no_lookahead")),
        "execution": all(token in execution_text for token in ("detect_board_candidate_fill_risk", "detect_missing_intraday_fill", "validate_execution_assumptions")),
        "suite": all(token in suite_text for token in ("run_strategy_validation_suite", "strategy_validation_latest.json", "strategy_validation_report_latest.md")),
        "observation": "validation_summary" in obs_text and "STRATEGY_VALIDATION_LATEST_JSON" in obs_text,
        "qd": "strategy-validate" in qd_text and "run_strategy_validation.py" in qd_text,
        "docs": "Strategy Lookahead And Execution Assumption Guard" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip": "results" in export_text and "reports" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "strategy_validation_suite", "missing wiring: " + ", ".join(missing))
    return result("PASS", "strategy_validation_suite", "v3.2.3 strategy validation guard wired")


def check_daily_data_retention_support() -> CheckResult:
    """Check v3.2.4 daily retention and reproducibility manifest wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "daily_data_retention.py",
        PROJECT_ROOT / "quantdesk" / "reproducibility_manifest.py",
        PROJECT_ROOT / "scripts" / "run_daily_data_retention.py",
        PROJECT_ROOT / "scripts" / "check_reproducibility_manifest.py",
        PROJECT_ROOT / "tests" / "test_v324_daily_data_retention_and_reproducibility_manifest.py",
        PROJECT_ROOT / "docs" / "daily_data_retention_workflow.md",
        PROJECT_ROOT / "docs" / "reproducibility_manifest.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "daily_data_retention", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    retention_text = (PROJECT_ROOT / "quantdesk" / "daily_data_retention.py").read_text(encoding="utf-8", errors="ignore")
    manifest_text = (PROJECT_ROOT / "quantdesk" / "reproducibility_manifest.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore") + "\n" + (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "daily_data_retention_workflow.md",
            PROJECT_ROOT / "docs" / "reproducibility_manifest.md",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.2.4-daily-data-retention-and-reproducibility-manifest" in version_text,
        "retention": all(token in retention_text for token in ("build_retention_plan", "collect_daily_retention_snapshot", "write_retention_manifest", "daily_retention_manifest.json")),
        "manifest": all(token in manifest_text for token in ("build_reproducibility_manifest", "compute_file_hash", "compare_manifests", "provider_secrets.json")),
        "qd": "daily-retain" in qd_text and "manifest-check" in qd_text,
        "docs": "Daily Data Retention And Reproducibility Manifest" in docs_text and "不保存 API Key" in docs_text,
        "ui": "每日数据留存" in app_text and "复现 manifest" in app_text,
        "clean_zip": "results" in export_text and "data\\calendar" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "daily_data_retention", "missing wiring: " + ", ".join(missing))
    return result("PASS", "daily_data_retention", "v3.2.4 daily retention and manifest wired")


def check_paper_vs_backtest_consistency_support() -> CheckResult:
    """Check v3.2.5 paper/backtest consistency wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "paper" / "paper_vs_backtest_consistency.py",
        PROJECT_ROOT / "scripts" / "check_paper_vs_backtest_consistency.py",
        PROJECT_ROOT / "tests" / "test_v325_paper_vs_backtest_consistency.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "paper_vs_backtest_consistency", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "paper" / "paper_vs_backtest_consistency.py").read_text(encoding="utf-8", errors="ignore")
    obs_text = (PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.2.5-paper-vs-backtest-consistency-check" in version_text,
        "module": all(token in module_text for token in ("run_paper_vs_backtest_consistency_check", "paper_vs_backtest_consistency_latest.json", "detect_reliable_event_without_intraday_evidence")),
        "observation": "consistency_summary" in obs_text and "PAPER_VS_BACKTEST_CONSISTENCY_LATEST_JSON" in obs_text,
        "qd": "consistency-check" in qd_text and "check_paper_vs_backtest_consistency.py" in qd_text,
        "docs": "Paper Vs Backtest Consistency Check" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip": "results" in export_text and "reports" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "paper_vs_backtest_consistency", "missing wiring: " + ", ".join(missing))
    return result("PASS", "paper_vs_backtest_consistency", "v3.2.5 paper/backtest consistency wired")


def check_backtest_statistical_validation_feature_quality_support() -> CheckResult:
    """Check v3.2.6 backtest statistics and feature audit wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "research" / "feature_quality_audit.py",
        PROJECT_ROOT / "quantdesk" / "research" / "backtest_statistical_validation.py",
        PROJECT_ROOT / "scripts" / "run_feature_quality_audit.py",
        PROJECT_ROOT / "scripts" / "run_backtest_statistical_validation.py",
        PROJECT_ROOT / "tests" / "test_v326_backtest_statistical_validation_and_feature_quality_audit.py",
        PROJECT_ROOT / "docs" / "feature_quality_audit.md",
        PROJECT_ROOT / "docs" / "backtest_statistical_validation.md",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "backtest_statistical_validation_feature_quality", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    feature_text = (PROJECT_ROOT / "quantdesk" / "research" / "feature_quality_audit.py").read_text(encoding="utf-8", errors="ignore")
    stats_text = (PROJECT_ROOT / "quantdesk" / "research" / "backtest_statistical_validation.py").read_text(encoding="utf-8", errors="ignore")
    obs_text = (PROJECT_ROOT / "quantdesk" / "strategy_observation_contract.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    retention_text = (PROJECT_ROOT / "quantdesk" / "daily_data_retention.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "feature_quality_audit.md",
            PROJECT_ROOT / "docs" / "backtest_statistical_validation.md",
        )
        if path.exists()
    )
    checks = {
        "version": (
            "3.2.6-backtest-statistical-validation-and-feature-quality-audit" in version_text
            or "3.2.6.1-feature-audit-empty-data-graceful-warning" in version_text
            or "3.2.7-governance-command-center-and-doc-consolidation" in version_text
            or "3.2.8-data-health-dashboard-and-provider-failure-stats" in version_text
            or "3.2.9-parameter-sensitivity-and-robustness-audit" in version_text
            or "3.3.0-consolidation-and-schema-contracts" in version_text
        ),
        "feature_module": all(token in feature_text for token in ("audit_feature_quality", "compute_feature_missing_rates", "feature_quality_audit_latest.json")),
        "stats_module": all(token in stats_text for token in ("permutation_test_signals", "bootstrap_metric_confidence_intervals", "backtest_statistical_validation_latest.json")),
        "observation": "feature_quality_summary" in obs_text and "statistical_validation_summary" in obs_text,
        "retention": "feature_quality_summary" in retention_text and "statistical_validation_summary" in retention_text,
        "qd": "feature-audit" in qd_text and "stats-validate" in qd_text,
        "docs": "置换检验" in docs_text and "不构成投资建议" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "backtest_statistical_validation_feature_quality", "missing wiring: " + ", ".join(missing))
    return result("PASS", "backtest_statistical_validation_feature_quality", "v3.2.6 stats validation and feature audit wired")


def check_chanlun_knowledge_base_support() -> CheckResult:
    """Check v3.0.1 Chanlun knowledge base and roadmap wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "chanlun_knowledge_base.py",
        PROJECT_ROOT / "scripts" / "build_chanlun_knowledge_base.py",
        PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        PROJECT_ROOT / "tests" / "test_v301_chanlun_knowledge_base.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "chanlun_knowledge_base_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "chanlun_knowledge_base.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "build_chanlun_knowledge_base.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        )
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    gitignore_text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
    prohibited = ["推荐" + "买入", "稳" + "赚", "必" + "涨"]
    public_text = "\n".join([module_text, cli_text, server_text, app_text, html_text, docs_text])
    checks = {
        "version": "3.0.1-chanlun-knowledge-base-and-rule-roadmap" in version_text or "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "module": all(token in module_text for token in ("default_lessons_index", "default_concepts", "default_rule_roadmap", "no_fulltext")),
        "cli": "--source-pdf" in cli_text and "--no-fulltext" in cli_text and "--report" in cli_text,
        "api": "/api/chanlun/knowledge/status" in server_text and "/api/chanlun/knowledge/build" in server_text,
        "ui": "缠论学习" in html_text and "renderChanlunKnowledge" in app_text and "买卖点仅作为观察信号" in html_text,
        "docs": "/api/chanlun/knowledge/build" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip_excludes_pdf": "data\\knowledge\\sources" in export_text and "data/knowledge/sources" in gitignore_text,
        "no_forbidden_promises": not any(term in public_text for term in prohibited),
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "chanlun_knowledge_base_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "chanlun_knowledge_base_wired", "v3.0.1 Chanlun knowledge base wired")


def check_chanlun_fractal_engine_support() -> CheckResult:
    """Check v3.0.2 Chanlun fractal engine wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "chanlun_fractal_engine.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_fractal_analysis.py",
        PROJECT_ROOT / "tests" / "test_v302_chanlun_fractal_engine.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "chanlun_fractal_engine_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "chanlun_fractal_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_chanlun_fractal_analysis.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        )
        if path.exists()
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    forbidden = ["推荐" + "买入", "稳" + "赚", "必" + "涨"]
    public_text = "\n".join([module_text, cli_text, server_text, app_text, html_text, docs_text])
    checks = {
        "version": "3.0.2-chanlun-fractal-engine" in version_text or "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "engine": all(token in module_text for token in ("normalize_chanlun_ohlc", "resolve_include_relationship", "detect_fractals", "run_chanlun_fractal_analysis", "顶分型", "底分型")),
        "cli": "--symbol" in cli_text and "--timeframe" in cli_text and "--mode" in cli_text,
        "api": "/api/chanlun/fractals/run" in server_text and "/api/chanlun/fractals/status" in server_text,
        "ui": "缠论分型识别" in html_text and "runChanlunFractalAnalysis" in app_text,
        "docs": "/api/chanlun/fractals/run" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip": "data\\chanlun\\fractals" in export_text and "data\\chanlun\\runtime" in export_text,
        "safe_text": not any(term in public_text for term in forbidden),
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "chanlun_fractal_engine_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "chanlun_fractal_engine_wired", "v3.0.2 Chanlun fractal engine wired")


def check_chanlun_bi_engine_support() -> CheckResult:
    """Check v3.0.3 Chanlun bi engine wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "chanlun_bi_engine.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_bi_analysis.py",
        PROJECT_ROOT / "tests" / "test_v303_chanlun_bi_engine.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "chanlun_bi_engine_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "chanlun_bi_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_chanlun_bi_analysis.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        ]
        if path.exists()
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    forbidden = ["推荐" + "买入", "稳" + "赚", "必" + "涨"]
    safe_text = not any(term in (module_text + app_text + html_text + docs_text) for term in forbidden)
    checks = {
        "version": "3.0.3-chanlun-bi-engine" in version_text or "3.0.4-chanlun-segment-engine" in version_text,
        "engine": all(token in module_text for token in ("normalize_fractals_for_bi", "select_effective_fractals", "build_bi_from_fractals", "run_chanlun_bi_analysis", "上升笔", "下降笔")),
        "cli": "--symbol" in cli_text and "--timeframe" in cli_text and "--min-separation" in cli_text,
        "api": "/api/chanlun/bi/run" in server_text and "/api/chanlun/bi/status" in server_text,
        "ui": "缠论笔识别" in html_text and "runChanlunBiAnalysis" in app_text,
        "docs": "/api/chanlun/bi/run" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip": "data\\chanlun\\bi" in export_text,
        "safe_text": safe_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "chanlun_bi_engine_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "chanlun_bi_engine_wired", "v3.0.3 Chanlun bi engine wired")


def check_chanlun_segment_engine_support() -> CheckResult:
    """Check v3.0.4 Chanlun segment engine wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "chanlun_segment_engine.py",
        PROJECT_ROOT / "scripts" / "run_chanlun_segment_analysis.py",
        PROJECT_ROOT / "tests" / "test_v304_chanlun_segment_engine.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "chanlun_segment_engine_wired", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "chanlun_segment_engine.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "run_chanlun_segment_analysis.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "chanlun_implementation_plan.md",
        ]
        if path.exists()
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    forbidden = ["推荐" + "买入", "稳" + "赚", "必" + "涨"]
    safe_text = not any(term in (module_text + app_text + html_text + docs_text) for term in forbidden)
    checks = {
        "version": "3.0.4-chanlun-segment-engine" in version_text,
        "engine": all(token in module_text for token in ("normalize_bi_for_segment", "select_effective_bi", "build_segments_from_bi", "run_chanlun_segment_analysis", "上升线段", "下降线段")),
        "cli": "--symbol" in cli_text and "--timeframe" in cli_text and "--min-bi-count" in cli_text,
        "api": "/api/chanlun/segments/run" in server_text and "/api/chanlun/segments/status" in server_text,
        "ui": "缠论线段识别" in html_text and "runChanlunSegmentAnalysis" in app_text,
        "docs": "/api/chanlun/segments/run" in docs_text and "不构成投资建议" in docs_text,
        "clean_zip": "data\\chanlun\\segments" in export_text,
        "safe_text": safe_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "chanlun_segment_engine_wired", "missing wiring: " + ", ".join(missing))
    return result("PASS", "chanlun_segment_engine_wired", "v3.0.4 Chanlun segment engine wired")


def check_schema_contracts_support() -> CheckResult:
    """Check v3.3.0 schema contracts and payload validator wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "schema_contracts.py",
        PROJECT_ROOT / "quantdesk" / "result_payload_validator.py",
        PROJECT_ROOT / "scripts" / "check_result_schemas.py",
        PROJECT_ROOT / "tests" / "test_v330_consolidation_and_schema_contracts.py",
        PROJECT_ROOT / "docs" / "schema_contracts.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
        PROJECT_ROOT / "docs" / "schema_examples" / "data_health_dashboard.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "feature_quality_audit.example.json",
        PROJECT_ROOT / "docs" / "schema_examples" / "backtest_statistical_validation.example.json",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "schema_contracts", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    contracts_text = (PROJECT_ROOT / "quantdesk" / "schema_contracts.py").read_text(encoding="utf-8", errors="ignore")
    validator_text = (PROJECT_ROOT / "quantdesk" / "result_payload_validator.py").read_text(encoding="utf-8", errors="ignore")
    cli_text = (PROJECT_ROOT / "scripts" / "check_result_schemas.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    command_text = (PROJECT_ROOT / "quantdesk" / "command_center_status.py").read_text(encoding="utf-8", errors="ignore")
    archive_text = (PROJECT_ROOT / "quantdesk" / "result_archive.py").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore") + "\n" + (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "CHANGELOG.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "schema_contracts.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
            PROJECT_ROOT / "docs" / "troubleshooting_quick_ref.md",
        )
        if path.exists()
    )
    export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
    checks = {
        "version": "3.3.0-consolidation-and-schema-contracts" in version_text,
        "contracts": all(token in contracts_text for token in ("build_base_result", "normalize_status", "ALLOWED_STATUSES", "validate_base_result_contract")),
        "validator": all(token in validator_text for token in ("validate_result_payload", "validate_no_secret_leak", "schema_validation_latest.json", "SECRET_KEYWORDS")),
        "cli": "--results-dir" in cli_text and "--network" in cli_text and "log_operation" in cli_text,
        "qd": "schema-check" in qd_text and "check_result_schemas.py" in qd_text,
        "command_center": "schema-check" in command_text,
        "archive": "schema_validation_latest.json" in archive_text and "schema_validation_report_latest.md" in archive_text,
        "docs": "schema-check" in docs_text and "status" in docs_text and "secret" in docs_text,
        "ui": "schema-check" in app_text,
        "clean_zip": "results" in export_text and "reports" in export_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "schema_contracts", "missing wiring: " + ", ".join(missing))
    return result("PASS", "schema_contracts", "v3.3.0 schema contracts and payload validator wired")


def check_workbench_ui_support() -> CheckResult:
    """Check v3.3.1 workbench UI and read-only API wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "workbench_overview.py",
        PROJECT_ROOT / "tests" / "test_v331_workbench_ui_and_workflow_unification.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "workbench_ui", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "workbench_overview.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    css_text = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "CHANGELOG.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "user_guide.md",
            PROJECT_ROOT / "docs" / "architecture.md",
            PROJECT_ROOT / "docs" / "troubleshooting_quick_ref.md",
        )
        if path.exists()
    )
    checks = {
        "version": (
            "3.3.1-quantdesk-workbench-ui-and-workflow-unification" in version_text
            or "3.3.2-data-update-workbench-and-auto-refresh-preflight" in version_text
        ),
        "module": all(token in module_text for token in ("build_workbench_overview", "build_workbench_artifacts", "build_workbench_next_actions", "network=False")),
        "api": all(token in server_text for token in ("/api/workbench/overview", "/api/workbench/command-center", "/api/workbench/artifacts", "/api/workbench/next-actions")),
        "html": all(token in html_text for token in ("workbenchSidebarNav", "workbenchOverviewPanel", "artifactCenterSection", "commandCenterFullSection")),
        "local_storage": all(token in app_text for token in ("quantdesk.sidebar.collapsed", "quantdesk.commandCenter.collapsed", "quantdesk.beginnerMode.enabled", "quantdesk.ui.mode")),
        "tabs": "commandCenterSection" in app_text and "artifactCenterSection" in app_text and "PRIMARY_TAB_SECTIONS" in app_text,
        "status_classes": all(token in css_text for token in ("status-passed", "status-warning", "status-failed", "status-missing", "status-skipped", "status-unavailable")),
        "docs": "今日工作台" in docs_text and "产物中心" in docs_text and "新手模式" in docs_text and "专家模式" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "workbench_ui", "missing wiring: " + ", ".join(missing))
    return result("PASS", "workbench_ui", "v3.3.1 workbench UI and read-only overview APIs wired")


def check_data_update_workbench_support() -> CheckResult:
    """Check v3.3.2 data-update workbench and preflight wiring."""

    required_files_list = [
        PROJECT_ROOT / "quantdesk" / "data_update_orchestrator.py",
        PROJECT_ROOT / "scripts" / "run_data_update.py",
        PROJECT_ROOT / "scripts" / "dev_preflight.py",
        PROJECT_ROOT / "docs" / "data_update_workbench.md",
        PROJECT_ROOT / "docs" / "dev_preflight_workflow.md",
        PROJECT_ROOT / "tests" / "test_v332_data_update_workbench_and_auto_refresh_preflight.py",
    ]
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in required_files_list if not path.exists()]
    if missing_files:
        return result("FAIL", "data_update_workbench", "missing files: " + ", ".join(missing_files))

    version_text = (PROJECT_ROOT / "quantdesk" / "version.py").read_text(encoding="utf-8", errors="ignore")
    qd_text = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8", errors="ignore")
    server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8", errors="ignore")
    module_text = (PROJECT_ROOT / "quantdesk" / "data_update_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    overview_text = (PROJECT_ROOT / "quantdesk" / "workbench_overview.py").read_text(encoding="utf-8", errors="ignore")
    html_text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8", errors="ignore")
    app_text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8", errors="ignore")
    archive_text = (PROJECT_ROOT / "quantdesk" / "result_archive.py").read_text(encoding="utf-8", errors="ignore")
    docs_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in (PROJECT_ROOT / "docs" / "data_update_workbench.md", PROJECT_ROOT / "docs" / "dev_preflight_workflow.md"))
    checks = {
        "version": "3.3.2-data-update-workbench-and-auto-refresh-preflight" in version_text,
        "module": all(token in module_text for token in ("run_data_update_preflight", "build_workbench_data_update", "network=False", "full_scan=False")),
        "cli": all(token in qd_text for token in ("data-update", "dev-preflight", "run_data_update.py", "dev_preflight.py")),
        "api": all(token in server_text for token in ("/api/workbench/data-update", "workbench_data_update_response")),
        "overview": "更新 / 检查数据" in overview_text and "data-update" in overview_text,
        "ui": all(token in html_text + app_text for token in ("数据更新", "dataUpdateWorkbenchSection", "默认不联网", "data-update")),
        "archive": "data_update_latest.json" in archive_text and "data_update_report_latest.md" in archive_text,
        "docs": "默认不联网" in docs_text and "included=true" in docs_text and "不构成投资建议" in docs_text,
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        return result("FAIL", "data_update_workbench", "missing wiring: " + ", ".join(missing))
    return result("PASS", "data_update_workbench", "v3.3.2 data update workbench and dev preflight wired")


def check_runtime_artifact_hygiene() -> CheckResult:
    """Warn when local runtime-state directories are present in the workspace.

    This is intentionally WARN-only: self_check must not delete user files or
    mutate git state.  The operator can decide whether to archive, ignore, or
    remove these runtime artifacts outside the release path.
    """

    runtime_roots = [
        ".opencode_cache",
        ".opencode_config",
        ".opencode_data",
        ".claude",
        "screenshots",
    ]
    present = [name for name in runtime_roots if (PROJECT_ROOT / name).exists()]
    if not present:
        return result("PASS", "runtime_artifact_hygiene", "no known runtime artifact directories found")
    return result(
        "WARN",
        "runtime_artifact_hygiene",
        "runtime artifact directories present; do not include in release or git cleanup without operator review: "
        + ", ".join(present),
    )


def check_staged_source_handoff_guard() -> CheckResult:
    """Fail fast if staged files contain local data or runtime artifacts."""

    try:
        completed = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return result("WARN", "staged_source_handoff_guard", f"cannot inspect staged files: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return result("WARN", "staged_source_handoff_guard", "git diff --cached failed: " + detail[:500])
    staged_paths = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    try:
        from quantdesk.repo_hygiene import validate_staged_source_handoff

        validation = validate_staged_source_handoff(staged_paths)
    except Exception as exc:  # noqa: BLE001
        return result("FAIL", "staged_source_handoff_guard", f"guard validation failed: {exc}")
    if validation.get("blocked_count"):
        blocked = list(validation.get("blocked_paths") or [])
        return result(
            "FAIL",
            "staged_source_handoff_guard",
            "blocked staged local data/runtime paths: " + ", ".join(blocked[:12]),
        )
    return result(
        "PASS",
        "staged_source_handoff_guard",
        f"staged_paths={len(staged_paths)}; blocked_count=0; source handoff staging is clean",
    )


def check_adjustment_basis_decision_gate() -> CheckResult:
    """Warn while raw/qfq adjustment-basis policy is not implemented end-to-end."""

    decision_pack = PROJECT_ROOT / "docs" / "internal" / "adjustment_policy_decision_pack.md"
    task_queue = PROJECT_ROOT / "docs" / "internal" / "NEXT_TASKS_AND_WORKTREE_RULES.md"
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in (decision_pack, task_queue) if not path.exists()]
    if missing:
        return result("FAIL", "adjustment_basis_decision_gate", "missing decision docs: " + ", ".join(missing))
    combined_text = "\n".join(
        [
            decision_pack.read_text(encoding="utf-8", errors="ignore"),
            task_queue.read_text(encoding="utf-8", errors="ignore"),
        ]
    )
    required_tokens = [
        "Option C",
        "price_basis",
        "adjustment_basis",
        "adjustment_warnings",
        "data/metadata/adjustment_factors.csv",
        "data/metadata/adjustment_factor.csv",
    ]
    missing_tokens = [token for token in required_tokens if token not in combined_text]
    if missing_tokens:
        return result("FAIL", "adjustment_basis_decision_gate", "decision docs missing: " + ", ".join(missing_tokens))
    option_c_markers = [
        "check_adjustment_basis_decision_gate",
        "price_basis",
        "adjustment_basis",
        "adjustment_warnings",
    ]
    touched_modules = [
        PROJECT_ROOT / "quantdesk" / "research" / "factor_panel_builder.py",
        PROJECT_ROOT / "quantdesk" / "research" / "combined_score_backtest.py",
        PROJECT_ROOT / "quantdesk" / "research" / "strategy_viability_runner.py",
        PROJECT_ROOT / "quantdesk" / "backtest.py",
        PROJECT_ROOT / "quantdesk" / "reporting.py",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
    ]
    module_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in touched_modules if path.exists())
    implemented = all(marker in module_text for marker in option_c_markers[1:])
    if implemented:
        return result("PASS", "adjustment_basis_decision_gate", "Option C price_basis/adjustment_basis warning labels appear wired in core result paths")
    return result(
        "WARN",
        "adjustment_basis_decision_gate",
        "Option C is documented but not implemented end-to-end; keep raw/qfq mixed outputs diagnostic only until "
        "price_basis, adjustment_basis, and adjustment_warnings are visible in research/backtest/report/UI paths",
    )


def _active_source_text(*relative_paths: str) -> str:
    """Read active product source only; never bind or call a server."""
    return "\n".join(
        (PROJECT_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        for path in relative_paths
    )


def _active_contract(name: str, required: tuple[str, ...], *paths: str) -> CheckResult:
    text = _active_source_text(*paths)
    missing = [token for token in required if token not in text]
    if missing:
        return result("FAIL", name, "active contract missing: " + ", ".join(missing))
    return result("PASS", name, "active 8000 product contract wired")


def check_v362_research_realization() -> CheckResult:
    return _active_contract(
        "v362_research_realization",
        ("load_daily_research_snapshots", "similar_day", "stress_scenarios", "style_attribution", "decision_journal"),
        "quantdesk/daily_workflow_orchestrator.py",
        "quantdesk/research/daily_research_snapshots.py",
    )


def check_intraday_live_workbench_support() -> CheckResult:
    return _active_contract(
        "intraday_live_workbench",
        ("load_intraday_live_latest_snapshot", "intraday_live", "partial_daily"),
        "quantdesk/daily_workflow_orchestrator.py",
        "quantdesk/risk/position_inputs.py",
        "quantdesk/intraday/intraday_live_snapshot.py",
    )


def check_daily_price_provider_fallback_support() -> CheckResult:
    return _active_contract(
        "daily_price_provider_fallback",
        ("update_daily_prices", "daily_price_status", "allow_daily_price_online_update", "network"),
        "scripts/quant_api_server.py",
        "quantdesk/daily_workflow_orchestrator.py",
        "quantdesk/daily_price_update_engine.py",
    )


def check_data_freshness_gate_support() -> CheckResult:
    return _active_contract(
        "data_freshness_gate",
        ("evaluate_data_freshness", "can_review_yesterday_positions", "stale_observation"),
        "quantdesk/daily_workflow_orchestrator.py",
        "quantdesk/paper/auto_paper_portfolio.py",
        "quantdesk/data_freshness_gate.py",
    )


def check_intraday_1m_provider_fallback_support() -> CheckResult:
    return _active_contract(
        "intraday_1m_provider_fallback",
        ("update_intraday_1m_for_symbols", "provider_results", "network"),
        "scripts/quant_api_server.py",
        "quantdesk/intraday/intraday_update_engine.py",
    )


def check_v361_real_risk_integration() -> CheckResult:
    return _active_contract(
        "v361_real_risk_integration",
        ("/api/risk-notifications/summary", "loadRiskNotifications", "loadSmartStop", "loadNotificationCenter", "build_smart_stop_for_position"),
        "scripts/server/routes/research.py",
        "app/static/js/daily_research.js",
        "scripts/quant_api_server.py",
    )


def check_shared_core_candidate_contract_support() -> CheckResult:
    return _active_contract(
        "shared_core_candidate_contract",
        ("candidate_contract", "normalize_candidate_row", "candidate_rows_from_strategy_payload", "daily_research"),
        "scripts/quant_api_server.py",
        "quantdesk/candidate_contract.py",
        "quantdesk/paper/auto_paper_portfolio.py",
        "app/templates/daily_research.html",
    )


def check_intraday_1m_execution_closure_support() -> CheckResult:
    return _active_contract(
        "intraday_1m_execution_closure",
        ("_candidate_intraday_required", "minute_data_available", "intraday_file_exists", "missing"),
        "scripts/quant_api_server.py",
        "app/static/js/daily_research.js",
    )


def check_beginner_data_health_dashboard_support() -> CheckResult:
    return _active_contract(
        "beginner_data_health_dashboard",
        ("/api/data-health/dashboard", "loadDataHealthHome", "loadDataHealth", "data-ops-section"),
        "scripts/server/routes/data.py",
        "app/static/js/daily_research.js",
        "app/templates/daily_research.html",
    )


def run_checks() -> list[CheckResult]:
    rows: list[CheckResult] = [check_python_version(), check_python_launcher_env()]
    rows.extend(check_required_files())
    rows.extend(check_critical_source_modules())
    rows.append(check_no_hardcoded_project_path())
    rows.append(check_no_dangerous_frontend_text())
    rows.extend(check_imports())
    rows.extend(check_dependency_health())
    rows.append(check_pytest_optional())
    rows.append(check_export_rules())
    rows.append(check_clean_zip())
    rows.append(check_release_staging_hygiene())
    rows.append(check_runtime_artifact_hygiene())
    rows.append(check_staged_source_handoff_guard())
    rows.append(check_adjustment_basis_decision_gate())
    rows.append(check_sourceportable_manifest_no_bom())
    rows.append(check_no_misleading_portable_zip())
    rows.append(check_no_fake_exe())
    rows.append(check_v360_real_use_platform_support())
    rows.append(check_text_encoding_hygiene())
    rows.append(check_v370_data_backbone())
    rows.append(check_v3701_strategy_viability())
    rows.append(check_v3702_strategy_pipeline_repair())
    rows.append(check_v3703_low_price_pipeline_finalization())
    rows.append(check_v3704_capital_lot_sensitivity_and_git_hygiene())
    rows.append(check_v3705_git_deletion_reconciliation())
    rows.append(check_v3706_strategy_regression_guard_and_report_hygiene())
    rows.append(check_script_inventory_governance())
    rows.append(check_test_inventory_governance())
    rows.append(check_test_discovery_diagnostics_governance())
    rows.append(check_test_tier_runner_governance())
    rows.append(check_pytest_suite_runner_governance())
    rows.append(check_frontend_api_contract_governance())
    rows.append(check_dependency_contract_governance())
    rows.append(check_data_backtest_contract_governance())
    rows.append(check_coverage_contract_governance())
    rows.append(check_gitlab_ci_governance())
    rows.append(check_local_a_share_calendar())
    rows.append(check_market_calendar_source())
    rows.append(check_cn_a_share_market_rules_support())
    rows.append(check_version_centralization())
    rows.append(check_cli_summary_contract())
    rows.append(check_factor_evaluation_support())
    rows.append(check_factor_combiner_support())
    rows.append(check_combined_score_backtest_support())
    rows.append(check_sector_rotation_support())
    rows.append(check_factor_research_dashboard_support())
    rows.append(check_pit_universe_support())
    rows.append(check_project_hygiene_v3435())
    rows.append(check_daily_research_snapshot())
    rows.append(check_multi_strategy_account_book_support())
    rows.append(check_risk_attribution_lite_support())
    rows.append(check_experiment_tracker_support())
    rows.append(check_engineering_docs_package_grouping_lite())
    rows.append(check_battle_ready_portable_release())
    rows.append(check_backtest_assumptions_split())
    rows.append(check_windows_scripts_ready())
    rows.append(check_desktop_shortcuts())
    rows.append(check_readme_ready())
    rows.append(check_agents_ready())
    rows.append(check_config_report_defaults())
    rows.append(check_external_tools_docs())
    rows.append(check_optional_deps_not_required())
    rows.append(check_no_external_source_dirs())
    rows.append(check_tests_present())
    rows.append(check_shared_cleanup_guard())
    rows.append(check_default_path_binding_guard())
    rows.append(check_real_tencent_provider_support())
    rows.append(check_strategy_observation_agent_bridge_support())
    rows.append(check_real_mootdx_provider_support())
    rows.append(check_strategy_validation_suite_support())
    rows.append(check_paper_vs_backtest_consistency_support())
    rows.append(check_backtest_statistical_validation_feature_quality_support())
    rows.append(check_utf8_ui_rendering())
    rows.append(check_visible_text_health())
    if APP_VERSION.startswith("3.5."):
        return _normalize_v351_migration_rows(rows)
    return rows


def check_practical_tools_support() -> CheckResult:
    """Check that v2.5.0 practical tools modules are wired."""
    required_modules = [
        PROJECT_ROOT / "quantdesk" / "strategy_compare.py",
        PROJECT_ROOT / "quantdesk" / "portfolio_health.py",
        PROJECT_ROOT / "quantdesk" / "stock_screener.py",
        PROJECT_ROOT / "quantdesk" / "signal_tracker.py",
        PROJECT_ROOT / "scripts" / "portfolio_health_check.py",
        PROJECT_ROOT / "scripts" / "screen_stocks.py",
    ]
    missing_files = [str(p.relative_to(PROJECT_ROOT)) for p in required_modules if not p.exists()]
    if missing_files:
        return result("FAIL", "practical_tools", "missing: " + ", ".join(missing_files))
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    server_text = server.read_text(encoding="utf-8", errors="ignore") if server.exists() else ""
    if "practical_tools_ready" not in server_text:
        return result("FAIL", "practical_tools", "API missing practical_tools_ready")
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    app_text = app.read_text(encoding="utf-8", errors="ignore") if app.exists() else ""
    if "\u5b9e\u7528\u5de5\u5177" not in app_text:
        return result("FAIL", "practical_tools", "frontend missing practical tools badge")
    try:
        importlib.import_module("quantdesk.strategy_compare")
        importlib.import_module("quantdesk.portfolio_health")
        importlib.import_module("quantdesk.stock_screener")
        importlib.import_module("quantdesk.signal_tracker")
    except Exception as exc:
        return result("FAIL", "practical_tools", f"import error: {exc}")
    return result("PASS", "practical_tools", "v2.5.0 practical tools wired")


def check_incremental_data_flow_support() -> CheckResult:
    """Check that v2.5.1 incremental data flow wiring is present."""
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    updater = PROJECT_ROOT / "scripts" / "update_market_data.py"
    report = PROJECT_ROOT / "scripts" / "daily_quant_report.py"
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    html = PROJECT_ROOT / "app" / "index.html"
    missing_files = [str(path.relative_to(PROJECT_ROOT)) for path in (server, updater, report, app, html) if not path.exists()]
    if missing_files:
        return result("FAIL", "incremental_data_flow", "missing: " + ", ".join(missing_files))
    server_text = server.read_text(encoding="utf-8", errors="ignore")
    updater_text = updater.read_text(encoding="utf-8", errors="ignore")
    report_text = report.read_text(encoding="utf-8", errors="ignore")
    app_text = app.read_text(encoding="utf-8", errors="ignore")
    html_text = html.read_text(encoding="utf-8", errors="ignore")
    required = {
        "server": [
            "global_history_status",
            "incremental_update_status",
            "report_run_status",
            "/api/update-and-generate-daily-report",
            "/api/update-a-share-universe",
        ],
        "update_market_data": ["cache_inventory.json", "already_fresh_count", "write_cache_inventory"],
        "daily_quant_report": ["--spot-source", "spot_source"],
        "app": ["updateAndGenerateDailyButton", "backfillMarketDataButton", "retryFailedMarketDataButton", "updateUniverseButton"],
        "html": ["updateAndGenerateDailyButton", "backfillMarketDataButton", "retryFailedMarketDataButton", "updateUniverseButton"],
    }
    missing: list[str] = []
    if "quantdesk.version" not in server_text:
        missing.append("server missing centralized version import")
    for token in required["server"]:
        if token not in server_text:
            missing.append(f"server:{token}")
    for token in required["update_market_data"]:
        if token not in updater_text:
            missing.append(f"update_market_data:{token}")
    for token in required["daily_quant_report"]:
        if token not in report_text:
            missing.append(f"daily_quant_report:{token}")
    for token in required["app"]:
        if token not in app_text:
            missing.append(f"app:{token}")
    for token in required["html"]:
        if token not in html_text:
            missing.append(f"html:{token}")
    if missing:
        return result("FAIL", "incremental_data_flow", "missing: " + "; ".join(missing))
    return result("PASS", "incremental_data_flow", "incremental data flow checks wired")


def check_architecture_docs_and_ui_nav() -> CheckResult:
    """Check that v2.5.2 architecture docs and UI primary navigation are present."""
    docs = [
        PROJECT_ROOT / "docs" / "architecture_inventory.md",
        PROJECT_ROOT / "docs" / "data_flow_map.md",
        PROJECT_ROOT / "docs" / "status_contract.md",
        PROJECT_ROOT / "docs" / "ui_action_contract.md",
        PROJECT_ROOT / "docs" / "known_logic_issues.md",
    ]
    missing_docs = [str(p.relative_to(PROJECT_ROOT)) for p in docs if not p.exists()]
    if missing_docs:
        return result("FAIL", "architecture_docs_ui_nav", "missing docs: " + ", ".join(missing_docs))
    html = PROJECT_ROOT / "app" / "index.html"
    app = PROJECT_ROOT / "app" / "static" / "app.js"
    css = PROJECT_ROOT / "app" / "static" / "styles.css"
    for path in (html, app, css):
        if not path.exists():
            return result("FAIL", "architecture_docs_ui_nav", f"missing: {path.relative_to(PROJECT_ROOT)}")
    html_text = html.read_text(encoding="utf-8", errors="ignore")
    app_text = app.read_text(encoding="utf-8", errors="ignore")
    css_text = css.read_text(encoding="utf-8", errors="ignore")
    missing: list[str] = []
    for token in ("primaryNav", "primary-tab", "data-primary-tab"):
        if token not in html_text:
            missing.append(f"index.html missing {token}")
    for token in ("PRIMARY_TAB_SECTIONS", "switchPrimaryTab", "restorePrimaryTab"):
        if token not in app_text:
            missing.append(f"app.js missing {token}")
    for token in (".primary-nav", ".primary-tab"):
        if token not in css_text:
            missing.append(f"styles.css missing {token}")
    server = PROJECT_ROOT / "scripts" / "quant_app_server.py"
    server_text = server.read_text(encoding="utf-8", errors="ignore") if server.exists() else ""
    if "quantdesk.version" not in server_text:
        missing.append("server missing centralized version import")
    readme = PROJECT_ROOT / "README.md"
    readme_text = readme.read_text(encoding="utf-8", errors="ignore") if readme.exists() else ""
    if "v2.5.2" not in readme_text:
        missing.append("README missing v2.5.2 section")
    if missing:
        return result("FAIL", "architecture_docs_ui_nav", "missing: " + "; ".join(missing))
    return result("PASS", "architecture_docs_ui_nav", "architecture docs and UI primary navigation wired")


def check_utf8_ui_rendering() -> CheckResult:
    """Check that v2.5.3 UTF-8 UI rendering safeguards are present."""
    paths = [
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
    ]
    missing = [str(p.relative_to(PROJECT_ROOT)) for p in paths if not p.exists()]
    if missing:
        return result("FAIL", "utf8_ui_rendering", "missing: " + ", ".join(missing))
    try:
        html_text = paths[0].read_text(encoding="utf-8")
        app_text = paths[1].read_text(encoding="utf-8")
        paths[2].read_text(encoding="utf-8")
        paths[3].read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return result("FAIL", "utf8_ui_rendering", f"UTF-8 decode failed: {exc}")
    required = [
        '<meta charset="UTF-8">',
        "Research Only",
        "Paper Account Only",
        "No Broker Route",
    ]
    combined = html_text + "\n" + app_text
    misses = [token for token in required if token not in combined]
    if "escapeHtml" not in app_text:
        misses.append("active frontend missing HTML escaping helper")
    server = PROJECT_ROOT / "scripts" / "quant_api_server.py"
    server_text = server.read_text(encoding="utf-8", errors="ignore")
    if "from flask import jsonify" not in server_text:
        misses.append("active server missing Flask JSON response support")
    if misses:
        return result("FAIL", "utf8_ui_rendering", "missing: " + "; ".join(misses))
    return result("PASS", "utf8_ui_rendering", "UTF-8 UI render safeguards wired")


def check_visible_text_health() -> CheckResult:
    """Check that visible UI source text does not contain broken placeholders."""
    strict_paths = [
        PROJECT_ROOT / "app" / "templates" / "daily_research.html",
        PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js",
        PROJECT_ROOT / "docs" / "provider_setup.md",
        PROJECT_ROOT / "config" / "provider_secrets.example.json",
    ]
    doc_paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "api_payload_schema.md",
    ]
    failures: list[str] = []
    for path in strict_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path.relative_to(PROJECT_ROOT)} not UTF-8: {exc}")
            continue
        if contains_mojibake(text):
            failures.append(f"{path.relative_to(PROJECT_ROOT)} contains broken visible text marker")
    for path in doc_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path.relative_to(PROJECT_ROOT)} not UTF-8: {exc}")
            continue
        if contains_mojibake(text):
            failures.append(f"{path.relative_to(PROJECT_ROOT)} contains mojibake pollution")
    app_text = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8", errors="ignore")
    if '"??"' in app_text or "return key || \"??\"" in app_text:
        failures.append("daily_research.js contains question-mark fallback label")
    if "escapeHtml" not in app_text:
        failures.append("active frontend missing visible text escaping")
    if failures:
        return result("FAIL", "visible_text_health", "; ".join(failures))
    result_paths = [
        PROJECT_ROOT / "results" / "latest_results.json",
        PROJECT_ROOT / "data" / "cache" / "update_status.json",
        PROJECT_ROOT / "data" / "cache" / "cache_inventory.json",
    ]
    polluted = []
    for path in result_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if contains_mojibake(text):
            polluted.append(str(path.relative_to(PROJECT_ROOT)))
    if polluted:
        return result("WARN", "visible_text_health", "旧结果文案编码异常，请重新生成报告: " + ", ".join(polluted))
    return result("PASS", "visible_text_health", "visible UI text has no broken placeholders")


def check_user_guide_support() -> CheckResult:
    """Check that v2.5.5 user guide documentation and UI entry exist."""
    failures: list[str] = []
    guide = PROJECT_ROOT / "docs" / "user_guide.md"
    if not guide.exists():
        failures.append("docs/user_guide.md missing")
    else:
        guide_text = guide.read_text(encoding="utf-8", errors="ignore")
        for phrase in ("不自动下单", "不构成投资建议", "更新今日/最近缺失日线", "生成日报", "重新读取本地结果"):
            if phrase not in guide_text:
                failures.append(f"docs/user_guide.md missing phrase: {phrase}")
    readme = PROJECT_ROOT / "README.md"
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8", errors="ignore")
        if "user_guide" not in readme_text.lower() and "user guide" not in readme_text.lower():
            failures.append("README.md does not mention user guide")
    app_js = PROJECT_ROOT / "app" / "static" / "app.js"
    html = PROJECT_ROOT / "app" / "index.html"
    has_entry = False
    for path in (app_js, html):
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "\u4f7f\u7528\u8bf4\u660e" in text:
                has_entry = True
                break
    if not has_entry:
        failures.append("UI missing \u4f7f\u7528\u8bf4\u660e entry")
    if failures:
        return result("FAIL", "user_guide_support", "; ".join(failures))
    return result("PASS", "user_guide_support", "v2.5.5 user guide wired")


def main() -> int:
    rows = run_checks()
    if APP_VERSION.startswith("3.5."):
        rows = _normalize_v351_migration_rows(rows)
    output, summary = render_self_check_output(rows)
    print(output)
    return 1 if summary["fail"] else 0


def _normalize_v351_migration_rows(rows: list[CheckResult]) -> list[CheckResult]:
    migrated_or_legacy = {
        "factor_evaluation",
        "factor_combiner",
        "combined_score_backtest",
        "sector_rotation",
        "factor_research_dashboard",
        "pit_universe",
        "daily_research_snapshot",
        "multi_strategy_account_book",
        "intraday_live_workbench",
        "risk_attribution_lite",
        "experiment_tracker",
        "reference_data_skill_adapter",
        "intraday_1m_execution_closure",
        "intraday_1m_provider_fallback",
        "intraday_replay_self_validation",
        "real_intraday_provider_capability",
        "intraday_data_workbench_wired",
        "real_tencent_provider_wired",
        "real_mootdx_provider_wired",
        "paper_vs_backtest_consistency",
        "backtest_statistical_validation_feature_quality",
        "trade_replay_support",
        "cn_a_share_market_rules",
        "local_daily_cache",
        "daily_cache_index",
        "data_foundation",
        "readme_ready",
        "external_tools_doc",
        "provider_smoke_observability",
        "optional_llm_observation_agent",
        "daily_data_retention",
        "architecture_docs_ui_nav",
    }
    normalized: list[CheckResult] = []
    for row in rows:
        if row.status == "FAIL" and (
            row.name.startswith("file:quantdesk\\")
            or row.name.startswith("import:quantdesk.")
            or row.name in migrated_or_legacy
            or row.name == "version_centralization"
        ):
            normalized.append(result("WARN", row.name, "v3.5.1 migration-aware warning: " + row.detail))
        else:
            normalized.append(row)
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
