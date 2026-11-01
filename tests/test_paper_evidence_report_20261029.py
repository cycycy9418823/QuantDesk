from __future__ import annotations

import json
import os

import pytest

from quantdesk.paper.performance_report import (
    compare_observed_endpoints,
    observed_history,
    read_index_series,
)
from scripts.report_strategy_vs_benchmark import build_report, main, make_payload


def row(day, equity, *, valid=True):
    return {"valuation_date": day, "generated_at": f"{day}T20:00:00",
            "cash": equity - 100, "market_value": 100,
            "total_equity": equity, "portfolio_review_valid": valid}


def test_only_explicit_valuation_dates_are_accepted_and_conflicts_are_visible():
    history = {"items": [
        row("2026-10-13", 1000), row("2026-10-14", 900),
        row("2026-10-14", 901), row("2026-10-15", 850, valid=False),
        {"generated_at": "2026-10-16T20:00:00", "cash": 700,
         "market_value": 100, "total_equity": 800},
        {**row("2026-10-19", 800), "cash": 1},
    ]}
    result = observed_history(history)
    assert [point.date for point in result.points] == ["2026-10-13", "2026-10-15"]
    assert result.points[-1].review_valid is False
    assert result.undated_count == 1
    assert result.conflicting_dates == ["2026-10-14"]
    assert result.invalid_count == 1


def test_comparison_uses_identical_endpoints_and_marks_missing_index(tmp_path):
    points = observed_history({"items": [row("2026-10-13", 1000), row("2026-10-27", 900)]}).points
    good = tmp_path / "good.csv"
    good.write_text("date,close\n2026-10-13,100\n2026-10-27,105\n", encoding="utf-8")
    shortened = tmp_path / "short.csv"
    shortened.write_text("date,close\n2026-10-13,100\n2026-10-26,90\n", encoding="utf-8")
    a = compare_observed_endpoints(points, read_index_series(good))
    b = compare_observed_endpoints(points, read_index_series(shortened))
    assert a["available"] and a["start"] == "2026-10-13" and a["end"] == "2026-10-27"
    assert a["index_change_pct"] == 5.0 and a["recorded_equity_change_pct"] == -10.0
    assert b["available"] is False and b["reason"] == "endpoint_missing"


def test_conflicting_index_rows_are_never_silently_deduplicated(tmp_path):
    points = observed_history({"items": [row("2026-10-13", 1000), row("2026-10-27", 900)]}).points
    path = tmp_path / "index.csv"
    path.write_text("date,close\n2026-10-13,100\n2026-10-27,90\n2026-10-27,91\n", encoding="utf-8")
    result = compare_observed_endpoints(points, read_index_series(path))
    assert result["available"] is False and result["reason"] == "endpoint_conflict"


def test_invalid_duplicate_before_valid_row_is_still_a_conflict():
    bad = {**row("2026-10-13", 1000), "cash": 1}
    result = observed_history({"items": [bad, row("2026-10-13", 1000)]})
    assert result.points == []
    assert result.invalid_count == 1
    assert result.conflicting_dates == ["2026-10-13"]


def test_report_does_not_call_recorded_changes_verified_or_infer_missing_dates(tmp_path):
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"items": [row("2026-10-13", 1000), row("2026-10-27", 900),
        {"generated_at": "2026-10-25T20:00:00", "total_equity": 990}]}), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"valuation_date": "2026-10-27", "total_equity": 900,
        "accounting_reconciliation": {"status": "unverified", "pnl_identity": {"difference": 3.03}},
        "positions": []}), encoding="utf-8")
    path = tmp_path / "hs300.csv"
    path.write_text("date,close\n2026-10-13,100\n2026-10-27,105\n", encoding="utf-8")
    payload = make_payload(history, portfolio, tmp_path)
    report = build_report(payload)
    assert payload["undated_count"] == 1
    assert payload["portfolio"]["identity_difference"] == 3.03
    assert "不能视为经过完整对账的投资收益率" in report
    assert "生成时间不等于估值日期" in report
    assert "2026-10-13" in report and "2026-10-27" in report


def test_bad_later_history_record_cannot_silently_shorten_comparison(tmp_path):
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"items": [row("2026-10-13", 1000),
        row("2026-10-27", 900), {**row("2026-10-28", 800), "cash": 1}]}), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text('{"positions": []}', encoding="utf-8")
    (tmp_path / "hs300.csv").write_text(
        "date,close\n2026-10-13,100\n2026-10-27,105\n", encoding="utf-8")
    payload = make_payload(history, portfolio, tmp_path)
    assert payload["invalid_dates"] == ["2026-10-28"]
    assert payload["comparisons"]["hs300"]["reason"] == "history_conflicting_or_invalid"


def test_report_never_overwrites_the_source_history(tmp_path):
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"items": []}), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text('{"positions": []}', encoding="utf-8")
    original = history.read_bytes()
    assert main(["--history", str(history), "--portfolio", str(portfolio),
                 "--benchmark-dir", str(tmp_path), "--out-md", str(history)]) == 2
    assert history.read_bytes() == original


def test_report_refuses_hardlink_to_source_history(tmp_path):
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"items": []}), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text('{"positions": []}', encoding="utf-8")
    alias = tmp_path / "alias.md"
    try:
        os.link(history, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    original = history.read_bytes()
    assert main(["--history", str(history), "--portfolio", str(portfolio),
                 "--benchmark-dir", str(tmp_path), "--out-md", str(alias)]) == 2
    assert history.read_bytes() == original
