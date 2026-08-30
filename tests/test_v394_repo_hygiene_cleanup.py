from __future__ import annotations

import subprocess
from unittest.mock import patch

from quantdesk import repo_hygiene
from quantdesk.repo_hygiene import PROHIBITED_USAGE, GitEntry, category_for, classify_entries, parse_porcelain, risk_for


def test_parse_porcelain_and_classify_basic_entries() -> None:
    entries = parse_porcelain(" M quantdesk/example.py\n D docs/old.md\n?? scratch/tmp.txt\n")
    rows = classify_entries(entries)
    assert [row["status"] for row in rows] == ["modified", "deleted", "untracked"]
    assert rows[0]["category"] == "source_code"
    assert rows[2]["risk_level"] == "safe_auto_archive"


def test_protected_data_daily_is_never_touch() -> None:
    entry = GitEntry("modified", "data/daily/example.csv")
    assert category_for(entry.path) == "protected_daily_data"
    assert risk_for(entry) == "never_touch"


def test_prohibited_usage_boundary_complete() -> None:
    assert "live_trading" in PROHIBITED_USAGE
    assert "broker_connection" in PROHIBITED_USAGE
    assert "auto_execution" in PROHIBITED_USAGE
    assert "buy_sell_recommendation" in PROHIBITED_USAGE
    assert "real_money_allocation_instruction" in PROHIBITED_USAGE


def test_staged_source_handoff_guard_blocks_local_data_and_runtime_paths() -> None:
    result = repo_hygiene.validate_staged_source_handoff(
        [
            "quantdesk/backtest.py",
            "tests/test_backtest_module.py",
            "docs/internal/NEXT_TASKS_AND_WORKTREE_RULES.md",
            "data/metadata/adjustment_factors.csv",
            "data/benchmark/zz500.csv",
            "data/tracking/candidate_ledger.csv",
            "data/vendor_staging/tushare_proxy_minutes_x/raw/1min/000001.csv",
            "reports/daily_quant_report_latest.md",
            ".tmp/run_tushare_minute_secure.ps1",
        ]
    )

    assert result["ok"] is False
    assert result["blocked_count"] == 6
    assert result["allowed_count"] == 3
    assert "data/metadata/adjustment_factors.csv" in result["blocked_paths"]
    assert "data/benchmark/zz500.csv" in result["blocked_paths"]
    assert "data/tracking/candidate_ledger.csv" in result["blocked_paths"]
    assert "data/vendor_staging/tushare_proxy_minutes_x/raw/1min/000001.csv" in result["blocked_paths"]
    assert "reports/daily_quant_report_latest.md" in result["blocked_paths"]
    assert ".tmp/run_tushare_minute_secure.ps1" in result["blocked_paths"]


def test_staged_source_handoff_guard_allows_reviewable_source_tests_docs_and_tiny_assets() -> None:
    result = repo_hygiene.validate_staged_source_handoff(
        [
            "quantdesk/research/combined_score_backtest.py",
            "scripts/quant_api_server.py",
            "app/static/js/daily_research.js",
            "tests/test_v394_repo_hygiene_cleanup.py",
            "docs/internal/NEXT_TASKS_AND_WORKTREE_RULES.md",
            "assets/quantdesk.ico",
        ]
    )

    assert result["ok"] is True
    assert result["blocked_paths"] == []
    assert result["allowed_count"] == 6


def test_staged_source_handoff_guard_allows_explicit_root_governance_files() -> None:
    result = repo_hygiene.validate_staged_source_handoff([".gitignore", "AGENTS.md"])

    assert result["ok"] is True
    assert result["blocked_paths"] == []
    assert result["allowed_count"] == 2


def test_staged_source_handoff_guard_allows_explicit_runtime_contracts_and_launcher() -> None:
    paths = [
        "config/stage4_t0_gate_baseline_20260727.json",
        "config/stage4_t0_gate_baseline_v2_20260805.json",
        "config/weekly_daily_wide_pool_contract.json",
        "config/weekly_daily_wide_pool_exclusions.json",
        "config/weekly_minute_wide_pool_exclusions.json",
        "start_ui.cmd",
    ]

    result = repo_hygiene.validate_staged_source_handoff(paths)

    assert result["ok"] is True
    assert result["blocked_paths"] == []
    assert result["allowed_count"] == len(paths)


def test_staged_source_handoff_guard_keeps_local_and_unknown_config_blocked() -> None:
    result = repo_hygiene.validate_staged_source_handoff(
        ["config/llm_config.json", "config/unreviewed_runtime_contract.json"]
    )

    assert result["ok"] is False
    assert result["allowed_count"] == 0
    assert result["blocked_paths"] == [
        "config/llm_config.json",
        "config/unreviewed_runtime_contract.json",
    ]


def test_staged_source_handoff_guard_blocks_ui_lab_even_for_python_files() -> None:
    result = repo_hygiene.validate_staged_source_handoff(
        ["ui_lab/terminal/server.py", "ui_lab/redesign_prototype/index.html"]
    )

    assert result["ok"] is False
    assert result["allowed_count"] == 0
    assert result["blocked_paths"] == [
        "ui_lab/terminal/server.py",
        "ui_lab/redesign_prototype/index.html",
    ]


def test_self_check_exposes_staged_source_handoff_guard() -> None:
    from scripts import self_check

    assert hasattr(self_check, "check_staged_source_handoff_guard")
    row = self_check.check_staged_source_handoff_guard()
    assert row.name == "staged_source_handoff_guard"
    assert row.status in {"PASS", "FAIL"}


def test_self_check_staged_source_handoff_guard_fails_on_staged_data() -> None:
    from scripts import self_check

    staged = "data/metadata/adjustment_factors.csv\ndata/benchmark/zz500.csv\nquantdesk/backtest.py\n"
    completed = subprocess.CompletedProcess(
        ["git", "diff", "--cached", "--name-only"],
        0,
        stdout=staged,
        stderr="",
    )
    with patch("scripts.self_check.subprocess.run", return_value=completed):
        row = self_check.check_staged_source_handoff_guard()

    assert row.status == "FAIL"
    assert row.name == "staged_source_handoff_guard"
    assert "data/metadata/adjustment_factors.csv" in row.detail
    assert "data/benchmark/zz500.csv" in row.detail


def test_self_check_exposes_adjustment_basis_decision_gate() -> None:
    from scripts import self_check

    assert hasattr(self_check, "check_adjustment_basis_decision_gate")
    row = self_check.check_adjustment_basis_decision_gate()
    assert row.name == "adjustment_basis_decision_gate"
    assert row.status in {"WARN", "PASS", "FAIL"}


def test_self_check_adjustment_basis_decision_gate_passes_once_option_c_is_wired() -> None:
    from scripts import self_check

    row = self_check.check_adjustment_basis_decision_gate()

    # Option C (price_basis / adjustment_basis / adjustment_warnings) is now wired
    # end-to-end across the core research / backtest / report / UI result paths, so
    # the decision gate must report PASS instead of the earlier WARN placeholder.
    assert row.status == "PASS"
    assert "price_basis" in row.detail
    assert "adjustment_basis" in row.detail
