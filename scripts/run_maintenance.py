from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_coverage_contract import main as coverage_contract_main
from run_data_backtest_contract import main as data_backtest_contract_main
from run_dependency_contract import main as dependency_contract_main
from run_git_deletion_audit import main as git_deletion_audit_main
from run_frontend_api_contract import main as frontend_api_contract_main
from run_import_alias_reconciliation import main as import_alias_reconciliation_main
from run_pytest_suite_runner import main as pytest_suite_runner_main
from run_script_inventory import main as script_inventory_main
from run_strategy_regression_audit import main as strategy_regression_audit_main
from run_test_discovery_diagnostics import main as test_discovery_diagnostics_main
from run_test_inventory import main as test_inventory_main
from run_test_tier_runner import main as test_tier_runner_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantDesk maintenance diagnostics.")
    sub = parser.add_subparsers(dest="command")
    audit = sub.add_parser("git-deletion-audit")
    audit.add_argument("--json", default="false")
    audit.add_argument("--summary", nargs="?", const="true", default="false")
    alias = sub.add_parser("import-alias-reconciliation")
    alias.add_argument("--json", default="false")
    alias.add_argument("--summary", nargs="?", const="true", default="false")
    alias.add_argument("--scan-only", default="false")
    regression = sub.add_parser("strategy-regression-audit")
    regression.add_argument("--json", default="false")
    regression.add_argument("--summary", nargs="?", const="true", default="false")
    inventory = sub.add_parser("script-inventory")
    inventory.add_argument("--json", default="false")
    inventory.add_argument("--summary", nargs="?", const="true", default="false")
    test_inventory = sub.add_parser("test-inventory")
    test_inventory.add_argument("--json", default="false")
    test_inventory.add_argument("--summary", nargs="?", const="true", default="false")
    test_discovery = sub.add_parser("test-discovery-diagnostics")
    test_discovery.add_argument("--json", default="false")
    test_discovery.add_argument("--summary", nargs="?", const="true", default="false")
    test_discovery.add_argument("--sample-limit", type=int, default=12)
    test_discovery.add_argument("--per-file-timeout-seconds", type=float, default=8.0)
    test_discovery.add_argument("--discover-timeout-seconds", type=float, default=45.0)
    test_discovery.add_argument("--timed-test-timeout-seconds", type=float, default=24.0)
    test_discovery.add_argument("--no-probes", action="store_true")
    test_discovery.add_argument("--no-timed-test-probes", action="store_true")
    test_discovery.add_argument("--include-legacy", default="false")
    test_runner = sub.add_parser("test-tier-runner")
    test_runner.add_argument("--json", default="false")
    test_runner.add_argument("--summary", nargs="?", const="true", default="false")
    test_runner.add_argument("--tier", default="tier0_fast_unit")
    test_runner.add_argument("--category", default="")
    test_runner.add_argument("--limit", type=int, default=40)
    test_runner.add_argument("--timeout-seconds", type=int, default=180)
    test_runner.add_argument("--dry-run", default="true")
    test_runner.add_argument("--include-tier3", default="false")
    test_runner.add_argument("--include-legacy", default="false")
    pytest_runner = sub.add_parser("pytest-suite-runner")
    pytest_runner.add_argument("suite_arg", nargs="?")
    pytest_runner.add_argument("--json", default="false")
    pytest_runner.add_argument("--summary", nargs="?", const="true", default="false")
    pytest_runner.add_argument("--suite", default="core")
    pytest_runner.add_argument("--limit", type=int, default=0)
    pytest_runner.add_argument("--timeout-seconds", type=int, default=180)
    pytest_runner.add_argument("--maxfail", type=int, default=10)
    pytest_runner.add_argument("--dry-run", default="true")
    pytest_runner.add_argument("--stop-on-failure", default="false")
    frontend_api = sub.add_parser("frontend-api-contract")
    frontend_api.add_argument("--json", default="false")
    frontend_api.add_argument("--summary", nargs="?", const="true", default="false")
    dependency = sub.add_parser("dependency-contract")
    dependency.add_argument("--json", default="false")
    dependency.add_argument("--summary", nargs="?", const="true", default="false")
    data_backtest = sub.add_parser("data-backtest-contract")
    data_backtest.add_argument("--json", default="false")
    data_backtest.add_argument("--summary", nargs="?", const="true", default="false")
    coverage = sub.add_parser("coverage-contract")
    coverage.add_argument("--json", default="false")
    coverage.add_argument("--summary", nargs="?", const="true", default="false")
    coverage.add_argument("--tests", default="")
    coverage.add_argument("--timeout-seconds", type=int, default=180)
    coverage.add_argument("--dry-run", default="false")
    coverage.add_argument("--min-line-percent", type=float, default=0.0)
    coverage.add_argument("--require-pytest-cov", default="false")
    args, rest = parser.parse_known_args(argv)
    if args.command == "git-deletion-audit":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return git_deletion_audit_main(forwarded)
    if args.command == "import-alias-reconciliation":
        forwarded = ["--json", args.json, "--summary", args.summary, "--scan-only", args.scan_only, *rest]
        return import_alias_reconciliation_main(forwarded)
    if args.command == "strategy-regression-audit":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return strategy_regression_audit_main(forwarded)
    if args.command == "script-inventory":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return script_inventory_main(forwarded)
    if args.command == "test-inventory":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return test_inventory_main(forwarded)
    if args.command == "test-discovery-diagnostics":
        forwarded = [
            "--json",
            args.json,
            "--summary",
            args.summary,
            "--sample-limit",
            str(args.sample_limit),
            "--per-file-timeout-seconds",
            str(args.per_file_timeout_seconds),
            "--discover-timeout-seconds",
            str(args.discover_timeout_seconds),
            "--timed-test-timeout-seconds",
            str(args.timed_test_timeout_seconds),
            "--include-legacy",
            args.include_legacy,
        ]
        if args.no_probes:
            forwarded.append("--no-probes")
        if args.no_timed_test_probes:
            forwarded.append("--no-timed-test-probes")
        forwarded.extend(rest)
        return test_discovery_diagnostics_main(forwarded)
    if args.command == "test-tier-runner":
        forwarded = [
            "--json",
            args.json,
            "--summary",
            args.summary,
            "--tier",
            args.tier,
            "--category",
            args.category,
            "--limit",
            str(args.limit),
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--dry-run",
            args.dry_run,
            "--include-tier3",
            args.include_tier3,
            "--include-legacy",
            args.include_legacy,
            *rest,
        ]
        return test_tier_runner_main(forwarded)
    if args.command == "pytest-suite-runner":
        forwarded = [
            "--json",
            args.json,
            "--summary",
            args.summary,
            "--suite",
            args.suite_arg or args.suite,
            "--limit",
            str(args.limit),
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--maxfail",
            str(args.maxfail),
            "--dry-run",
            args.dry_run,
            "--stop-on-failure",
            args.stop_on_failure,
            *rest,
        ]
        return pytest_suite_runner_main(forwarded)
    if args.command == "frontend-api-contract":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return frontend_api_contract_main(forwarded)
    if args.command == "dependency-contract":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return dependency_contract_main(forwarded)
    if args.command == "data-backtest-contract":
        forwarded = ["--json", args.json, "--summary", args.summary, *rest]
        return data_backtest_contract_main(forwarded)
    if args.command == "coverage-contract":
        forwarded = [
            "--json",
            args.json,
            "--summary",
            args.summary,
            "--tests",
            args.tests,
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--dry-run",
            args.dry_run,
            "--min-line-percent",
            str(args.min_line_percent),
            "--require-pytest-cov",
            args.require_pytest_cov,
            *rest,
        ]
        return coverage_contract_main(forwarded)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
