"""Queries remain independently usable and never become account writers."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk.paper.queries import portfolio as queries
from quantdesk.paper.reporting import html as reports


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def observation(day: str, equity: float) -> dict:
    return {"valuation_date": day, "cash": equity, "market_value": 0,
            "total_equity": equity, "portfolio_review_valid": True}


def test_query_import_does_not_load_execution_candidates_or_models():
    script = r'''
import importlib.abc, json, sys
blocked = {
    'quantdesk.paper.auto_paper_portfolio', 'quantdesk.auto_paper_portfolio',
    'quantdesk.paper.position_management_engine', 'quantdesk.daily_candidate_refresh',
    'quantdesk.paper.snapshot_open_fill', 'ai_trader.trader',
}
class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('query_imported_execution:' + fullname)
sys.meta_path.insert(0, NoExecution())
from quantdesk.paper.queries import portfolio
from quantdesk.paper.reporting import html
assert not blocked.intersection(sys.modules)
print(json.dumps({'execution_imports': sorted(blocked.intersection(sys.modules))}))
'''
    completed = subprocess.run([sys.executable, "-B", "-c", script],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["execution_imports"] == []


def test_history_uses_only_the_explicit_path(tmp_path, monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy

    left = queries.PaperQueryPaths.from_root(tmp_path / "left")
    right = queries.PaperQueryPaths.from_root(tmp_path / "right")
    write_json(left.history, {"items": [observation("2026-11-03", 110), observation("2026-11-04", 120)]})
    write_json(right.history, {"items": [observation("2026-11-03", 210), observation("2026-11-04", 220)]})
    before = {p: p.read_bytes() for p in [left.history, right.history]}
    # Existing callers may redirect only this constant, leaving every other root alone.
    monkeypatch.setattr(legacy, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", left.history)
    first = legacy.auto_paper_portfolio_equity_history()
    assert first == queries.auto_paper_portfolio_equity_history(left)
    assert first["latest"]["total_equity"] == 120
    monkeypatch.setattr(legacy, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", right.history)
    assert legacy.auto_paper_portfolio_equity_history()["latest"]["total_equity"] == 220
    assert all(p.read_bytes() == blob for p, blob in before.items())


def test_legacy_review_root_follows_results_even_when_latest_is_elsewhere(tmp_path, monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy

    root = tmp_path / "review-owner"
    latest = tmp_path / "separate-view" / "latest.json"
    state = tmp_path / "book" / "state.json"
    write_json(latest, {"valuation_date": "2026-11-04", "summary": {"total_equity": 123}})
    calls = []
    monkeypatch.setattr(legacy, "RESULTS_DIR", root / "results")
    monkeypatch.setattr(legacy, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", latest)
    monkeypatch.setattr(legacy, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state)
    monkeypatch.setattr(legacy, "load_plan_review_snapshot", lambda p: calls.append(p) or {"review_source": str(p)})
    def inspect(path, **kwargs):
        assert path == state
        assert kwargs["target_date"] == "2026-11-04"
        return {"ok": False, "reason_codes": ["fixture_unverified"]}
    monkeypatch.setattr(legacy, "inspect_strategy_ledger", inspect)
    value = legacy.auto_paper_portfolio_latest()
    assert calls == [root]
    assert value["review_source"] == str(root)
    assert value["portfolio_review_valid"] is False
    assert value["warnings"] == ["fixture_unverified"]


@pytest.mark.parametrize("latest", [None, [], {}, {"valuation_date": "2026-11-04"}])
def test_all_readers_and_renderers_work_with_writes_forbidden(tmp_path, monkeypatch, latest):
    paths = queries.PaperQueryPaths.from_root(tmp_path)
    if latest is not None:
        write_json(paths.latest, latest)
    write_json(paths.history, {"items": [observation("2026-11-03", 100), observation("2026-11-04", 101)]})
    write_json(paths.shared_plan, {"plan_executed": True})
    write_json(paths.swing_plan, {"generated_at": "fixture"})
    write_json(paths.intraday_execution, {"generated_at": "fixture"})
    paths.reports.mkdir()
    auto_report = paths.reports / "auto_paper_portfolio_report_fixture.md"
    swing_report = paths.reports / "swing_execution_report_fixture.md"
    auto_report.write_text("old report", encoding="utf-8")
    swing_report.write_text("old report", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    original_open = Path.open
    def read_open(path, mode="r", *args, **kwargs):
        assert not any(c in mode for c in "wax+"), (path, mode)
        return original_open(path, mode, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("read operation attempted a filesystem mutation")
    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", read_open)
        for name in ["mkdir", "unlink", "rename", "replace", "write_text", "write_bytes"]:
            guard.setattr(Path, name, forbidden)
        current = queries.auto_paper_portfolio_latest(paths, disclaimer="research", broker_boundary="no broker")
        history = queries.auto_paper_portfolio_equity_history(paths)
        plan = queries.shared_paper_plan_latest(paths, disclaimer="research", broker_boundary="no broker")
        swing = queries.swing_plans_latest(paths)
        execution = queries.intraday_execution_latest(paths)
        assert plan["plan_executed"] is False
        assert history["latest"]["total_equity"] == 101
        assert swing["exists"] is True
        assert queries.newest_auto_paper_report(paths.reports) == auto_report
        assert queries.newest_swing_execution_report(paths.reports) == swing_report
        assert len(queries.auto_paper_portfolio_artifacts(paths, auto_report=auto_report, swing_report=swing_report)) == 9
        assert "<!doctype html>" in reports.render_swing_execution_html(execution)
        assert "<!doctype html>" in reports.render_auto_paper_portfolio_html(current, disclaimer="research", broker_boundary="no broker")
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_renderers_escape_content_without_mutating_query_results():
    value = {"exists": True, "generated_at": '<script>alert("x")</script>', "message": "A&B"}
    original = deepcopy(value)
    for rendered in [reports.render_swing_execution_html(value),
                     reports.render_auto_paper_portfolio_html(value, disclaimer="<warning>", broker_boundary="A&B")]:
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert "A&amp;B" in rendered
    assert value == original


def test_legacy_html_uses_the_legacy_query_extension_point(monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy

    value = {"exists": False, "message": "custom <empty>"}
    monkeypatch.setattr(legacy, "auto_paper_portfolio_latest", lambda: value)
    monkeypatch.setattr(legacy, "intraday_execution_latest", lambda: value)
    assert legacy.auto_paper_portfolio_html_page() == reports.render_auto_paper_portfolio_html(
        value, disclaimer=legacy.DISCLAIMER, broker_boundary=legacy.BROKER_BOUNDARY)
    assert legacy.swing_execution_html_page() == reports.render_swing_execution_html(value)


def test_artifact_roots_and_conditional_overrides_still_work(tmp_path, monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy

    paths = queries.PaperQueryPaths.from_root(tmp_path)
    custom = tmp_path / "alternate" / "results"
    for name, value in {"PROJECT_ROOT": tmp_path, "RESULTS_DIR": custom,
                        "REPORTS_DIR": paths.reports, "AUTO_PAPER_PORTFOLIO_STATE_JSON": paths.state,
                        "AUTO_PAPER_PORTFOLIO_LATEST_JSON": paths.latest,
                        "AUTO_PAPER_PORTFOLIO_HISTORY_JSON": paths.history,
                        "SWING_TRADE_PLANS_LATEST_JSON": paths.swing_plan,
                        "INTRADAY_EXECUTION_LATEST_JSON": paths.intraday_execution}.items():
        monkeypatch.setattr(legacy, name, value)
    monkeypatch.setattr(legacy, "CONDITIONAL_TRADE_PLANS_LATEST_JSON", tmp_path / "old" / "plan.json")
    monkeypatch.setattr(legacy, "CONDITIONAL_EXECUTION_LATEST_JSON", tmp_path / "old" / "execution.json")
    rows = legacy.auto_paper_portfolio_artifacts()
    assert rows[4]["path"] == str(Path("alternate/results/conditional_trade_plans_latest.json"))
    assert rows[5]["path"] == str(Path("alternate/results/conditional_execution_latest.json"))
    assert all(not r["exists"] for r in rows)
