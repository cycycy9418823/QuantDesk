"""Publication ordering, visible failure and legacy call-time dependency contracts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk.paper import auto_paper_portfolio as engine


def redirect(monkeypatch, root: Path) -> None:
    for name, relative in {
        "PROJECT_ROOT": ".", "RESULTS_DIR": "results", "REPORTS_DIR": "reports",
        "AUTO_PAPER_PORTFOLIO_LATEST_JSON": "results/latest.json",
        "AUTO_PAPER_PORTFOLIO_HISTORY_JSON": "results/history.json",
        "SWING_TRADE_PLANS_LATEST_JSON": "results/plans.json",
        "INTRADAY_EXECUTION_LATEST_JSON": "results/executions.json",
    }.items():
        monkeypatch.setattr(engine, name, root / relative)
    monkeypatch.setattr(engine, "_SHADOW_UNRECORDED", [])


def snapshot() -> dict:
    return {
        "valuation_date": "2026-11-05", "generated_at": "2026-11-05T20:00:00",
        "summary": {"initial_cash": 1000.0, "cash": 800.0, "market_value": 200.0,
            "total_equity": 1000.0, "realized_pnl": -3.0, "unrealized_pnl": 3.0},
        "positions": [{"symbol": "600000", "quantity": 100, "market_value": 200.0, "unrealized_pnl": 3.0}],
        "closed_positions": [{"symbol": "600001", "realized_pnl": -3.0}],
        "realized_reductions": [], "legacy_realized_adjustment": 0.0,
    }


@pytest.mark.parametrize("fail_at", [None, 1, 2, 3, 4])
def test_snapshot_publication_keeps_order_and_stops_at_each_failure(tmp_path, monkeypatch, fail_at):
    redirect(monkeypatch, tmp_path)
    events = []

    def step(name, value):
        events.append(name)
        if len(events) == fail_at:
            raise OSError("synthetic publication interruption")
        return value

    monkeypatch.setattr(engine, "_write_auto_paper_latest", lambda value: step("latest", deepcopy(value)))
    monkeypatch.setattr(engine, "_append_history", lambda value: step("history", {"items": [1, 2]}))
    monkeypatch.setattr(engine, "_write_report", lambda value: step("report", tmp_path / "reports/a.md"))
    monkeypatch.setattr(engine, "_display_path", lambda value: value.relative_to(tmp_path).as_posix())
    source = snapshot()
    before = deepcopy(source)
    if fail_at:
        with pytest.raises(OSError, match="synthetic publication interruption"):
            engine._publish_snapshot_result(source)
    else:
        result = engine._publish_snapshot_result(source)
        assert result["history_count"] == 2
        assert result["history_path"] == "results/history.json"
        assert result["report_path"] == "reports/a.md"
    assert events == ["latest", "history", "report", "latest"][:fail_at]
    assert source == before
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("kind,expected,fail_at", [
    (kind, expected, fail_at)
    for kind, expected in (
        ("swing", ["plans.json", "executions.json", "report", "executions.json"]),
        ("conditional", ["report", "plans.json", "executions.json"]),
    )
    for fail_at in [None, *range(1, len(expected) + 1)]
])
def test_compatibility_publishers_preserve_distinct_write_orders(tmp_path, monkeypatch, kind, expected, fail_at):
    redirect(monkeypatch, tmp_path)
    events = []
    writes = []

    def step(name):
        events.append(name)
        if len(events) == fail_at:
            raise OSError("synthetic output failure")

    def write_json(path, value):
        step(path.name)
        writes.append(deepcopy(value))

    def write_report(plans, executions):
        assert plans == [{"symbol": "600000"}]
        assert executions == [{"symbol": "600000", "is_reliable": False}]
        step("report")
        return tmp_path / "reports/swing.md"

    monkeypatch.setattr(engine, "atomic_write_json", write_json)
    monkeypatch.setattr(engine, "_write_swing_execution_report", write_report)
    monkeypatch.setattr(engine, "now_text", lambda: "fixed timestamp")
    monkeypatch.setattr(engine, "_display_path", lambda path: "custom display")
    plans = [{"symbol": "600000"}]
    executions = [{"symbol": "600000", "is_reliable": False}]
    if kind == "swing":
        call = lambda: engine._write_intraday_swing_artifacts(plans, executions,
            enabled=True, source="1m", allow_daily_approx=False)
    else:
        monkeypatch.setattr(engine, "_execution_rows", lambda value: executions)
        call = lambda: engine._write_conditional_compat_artifacts({"symbols": plans}, {"events": executions})
    if fail_at:
        with pytest.raises(OSError, match="synthetic output failure"):
            call()
    else:
        call()
        assert writes[-1]["unreliable_count"] == 1
        assert writes[-1]["report_path"] == (str(Path("reports") / "swing.md") if kind == "swing" else "custom display")
        assert all(row["generated_at"] == "fixed timestamp" for row in writes)
    assert events == expected[:fail_at]


def test_history_keeps_journal_deduplication_legacy_append_and_last_500(tmp_path, monkeypatch):
    redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "now_text", lambda: "fixed timestamp")
    items = [{"valuation_date": str(i), "strategy_journal_transaction": str(i)} for i in range(500)]
    path = engine.AUTO_PAPER_PORTFOLIO_HISTORY_JSON
    path.parent.mkdir()
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    before = path.read_bytes()
    engine._append_history({**snapshot(), "strategy_journal_transaction": "499"})
    assert path.read_bytes() == before
    result = engine._append_history({**snapshot(), "strategy_journal_transaction": "new"})
    assert len(result["items"]) == 500
    assert result["items"][0]["strategy_journal_transaction"] == "1"
    assert result["items"][-1]["strategy_journal_transaction"] == "new"
    for _ in range(2):
        result = engine._append_history(snapshot())
    assert [row.get("strategy_journal_transaction") for row in result["items"][-3:]] == ["new", None, None]
    assert result["updated_at"] == "fixed timestamp"


def test_legacy_dependencies_and_paths_resolve_again_on_each_call(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 5, 21, 0, 0)

    monkeypatch.setattr(engine, "datetime", Clock)
    for index in (1, 2):
        target = tmp_path / str(index)
        redirect(monkeypatch, target)
        captured = []
        monkeypatch.setattr(engine, "atomic_write_json", lambda p, value: captured.append((p, deepcopy(value))))
        monkeypatch.setattr(engine, "reconcile_paper_account", lambda value: {**value, "round": index})
        monkeypatch.setattr(engine, "strategy_pnl_identity", lambda value: {"status": "consistent", "round": index})
        monkeypatch.setattr(engine, "_SHADOW_UNRECORDED", [f"warning-{index}"])
        result = engine._write_auto_paper_latest(snapshot())
        assert captured[0][0] == target / "results/latest.json"
        assert result["round"] == result["summary"]["pnl_identity"]["round"] == index
        assert result["shadow_warnings"] == [f"warning-{index}"]
        assert engine._write_swing_execution_report([], []).name == "swing_execution_report_20261105_210000.md"
        assert (target / "reports/swing_execution_report_20261105_210000.md").exists()


def test_report_and_error_log_failure_reach_last_latest_without_changing_input(tmp_path, monkeypatch):
    redirect(monkeypatch, tmp_path)
    engine.RESULTS_DIR.mkdir()
    source = snapshot()
    before = deepcopy(source)

    def fail_render(value):
        raise ValueError("bad shadow data")

    def fail_log(**kwargs):
        assert kwargs["error_path"] == tmp_path / "shadow-errors.jsonl"
        raise OSError("log unavailable")

    monkeypatch.setattr(engine, "render_shadow_report_section", fail_render)
    monkeypatch.setattr(engine, "record_shadow_error_best_effort", fail_log)
    monkeypatch.setattr(engine, "_risk_gate_shadow_error_path", lambda: tmp_path / "shadow-errors.jsonl")
    real_report = engine._write_report
    monkeypatch.setattr(engine, "_write_report", lambda value: real_report(value, risk_gate_shadow_summary={"as_of": "2026-11-05"}))
    result = engine._publish_snapshot_result(source)
    text = (tmp_path / result["report_path"]).read_text(encoding="utf-8")
    assert "影子风险段生成失败：ValueError: bad shadow data" in text
    assert "错误日志写入也失败：OSError: log unavailable" in text
    expected = "shadow_error_log_write_failed:auto_paper_report_shadow_section:OSError"
    assert expected in result["shadow_warnings"]
    assert json.loads(engine.AUTO_PAPER_PORTFOLIO_LATEST_JSON.read_text(encoding="utf-8"))["shadow_warnings"] == result["shadow_warnings"]
    assert source == before


def test_publication_can_load_and_render_without_engine_store_or_journal(tmp_path):
    code = r'''
import importlib.abc, sys
from datetime import datetime
from pathlib import Path
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"quantdesk.paper.auto_paper_portfolio", "quantdesk.auto_paper_portfolio",
            "quantdesk.paper.state_store", "quantdesk.paper.strategy_fill_journal", "quantdesk.daily_candidate_refresh"}:
            raise AssertionError("publication imported execution: " + fullname)
sys.meta_path.insert(0, Deny())
from quantdesk.paper.publication import PaperPublicationPaths, write_swing_execution_report
root=Path(sys.argv[1])
paths=PaperPublicationPaths(root,root/'results',root/'reports',root/'latest.json',root/'history.json',root/'plans.json',root/'execution.json')
report=write_swing_execution_report([],[],paths=paths,clock=lambda:datetime(2026,11,5))
assert report.is_file()
assert not any('state' in name or 'journal' in name for name in paths.__dataclass_fields__)
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
