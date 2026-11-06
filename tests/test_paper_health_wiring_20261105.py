"""Health snapshot queries preserve evidence and never initialize an account."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk import data_health_dashboard as health
from quantdesk.paper import auto_paper_portfolio as legacy
from quantdesk.paper.queries import portfolio as queries


def put(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def populate(root: Path, amount: int = 100) -> None:
    state = {"ledger_id": "health-fixture", "initial_cash": 100, "cash": amount,
             "market_value": 0, "total_equity": amount, "positions": [], "valuation_date": "2026-11-04"}
    put(root / "state/auto_paper_portfolio.json", state)
    put(root / "results/auto_paper_portfolio_latest.json", {**state, "ok": True})
    put(root / queries.STORE_PATH / "latest.json",
        {"plan_review_summary": {"status": "fixture-review"}, "plan_review_items": []})


def bind(monkeypatch, root: Path) -> None:
    paths = queries.PaperQueryPaths.from_root(root)
    for module in (health, legacy):
        monkeypatch.setattr(module, "PROJECT_ROOT", root)
        monkeypatch.setattr(module, "RESULTS_DIR", root / "results")
        monkeypatch.setattr(module, "AUTO_PAPER_PORTFOLIO_STATE_JSON", paths.state)
        monkeypatch.setattr(module, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", paths.latest)
    monkeypatch.setattr(health, "EXTENDED_DATA_GATES_LATEST_JSON", root / "results/gates.json")


def inputs() -> dict:
    return {name: {} for name in (
        "daily_price_payload", "freshness_payload", "intraday_payload", "theme_payload",
        "current_candidate_theme_payload", "provider_capability_payload", "daily_workflow_payload",
        "real_sample_payload", "self_validation_payload", "strategy_payload", "extended_gates_payload",
        "daily_data_chain_payload", "daily_scheduler_payload", "evidence_freshness_payload", "theme_map",
    )}


@pytest.mark.parametrize("case", ["missing", "malformed", "valid", "state_drift", "review_missing"])
def test_default_health_reader_equals_legacy_with_warnings(tmp_path, monkeypatch, case):
    populate(tmp_path);bind(monkeypatch, tmp_path)
    latest = tmp_path / "results/auto_paper_portfolio_latest.json"
    if case == "missing":latest.unlink()
    if case == "malformed":latest.write_text("{bad", encoding="utf-8")
    if case == "state_drift":
        state = json.loads((tmp_path / "state/auto_paper_portfolio.json").read_text())
        state["total_equity"] = 999
        put(tmp_path / "state/auto_paper_portfolio.json", state)
    if case == "review_missing":(tmp_path / queries.STORE_PATH / "latest.json").unlink()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    expected = legacy.auto_paper_portfolio_latest()
    monkeypatch.setattr(legacy, "auto_paper_portfolio_latest", lambda: pytest.fail("engine callback used"))
    actual = health.auto_paper_portfolio_latest()
    assert actual == expected
    if case in {"missing", "malformed"}:
        assert not actual["exists"] and actual["plan_review_summary"]["status"] == "fixture-review"
    elif case == "state_drift":
        assert not actual["ledger_health"]["ok"] and actual["portfolio_review_valid"] is False
        assert actual["warnings"]
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("supplied", [{}, {"exists": False, "fixture": "explicit"}])
def test_explicit_portfolio_including_empty_never_falls_back(tmp_path, monkeypatch, supplied):
    bind(monkeypatch, tmp_path)
    monkeypatch.setattr(health, "auto_paper_portfolio_latest", lambda: pytest.fail("default reader used"))
    original = deepcopy(supplied)
    result = health.build_data_health_dashboard(**inputs(), portfolio_payload=supplied)
    assert result["source_status"]["auto_paper_portfolio"] is supplied
    assert supplied == original
    assert not (tmp_path / "state").exists()


def test_builder_uses_late_callback_and_only_persists_requested_snapshot(tmp_path, monkeypatch):
    populate(tmp_path);bind(monkeypatch, tmp_path)
    monkeypatch.setattr(health, "now_text", lambda: "2026-11-05T14:00:00+08:00")
    expected = health.build_data_health_dashboard(**inputs(), portfolio_payload=legacy.auto_paper_portfolio_latest())
    actual = health.build_data_health_dashboard(**inputs())
    assert actual == expected
    monkeypatch.setattr(health, "auto_paper_portfolio_latest", lambda: {"exists": False, "fixture": "late"})
    assert health.build_data_health_dashboard(**inputs())["source_status"]["auto_paper_portfolio"]["fixture"] == "late"
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    target = tmp_path / "published/snapshot.json"
    written = health.build_data_health_dashboard(**inputs(), full_universe_observation={}, output_path=target, write=True)
    disk = json.loads(target.read_text(encoding="utf-8"))
    assert disk["source_status"]["auto_paper_portfolio"]["fixture"] == "late"
    assert written["path"] == "published/snapshot.json"
    assert before == {p: p.read_bytes() for p in before}
    assert set(p for p in tmp_path.rglob("*") if p.is_file()) == {*before, target}


def test_independent_latest_state_and_review_roots_are_resolved_at_call(tmp_path, monkeypatch):
    first, second, reviews = (tmp_path / name for name in ("first", "second", "reviews"))
    populate(first, 100);populate(second, 200);populate(reviews)
    bind(monkeypatch, first)
    assert health.auto_paper_portfolio_latest()["cash"] == 100
    monkeypatch.setattr(health, "AUTO_PAPER_PORTFOLIO_STATE_JSON", second / "state/auto_paper_portfolio.json")
    monkeypatch.setattr(health, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", second / "results/auto_paper_portfolio_latest.json")
    monkeypatch.setattr(health, "RESULTS_DIR", reviews / "results")
    put(reviews / queries.STORE_PATH / "latest.json",
        {"plan_review_summary": {"status": "separate-root"}, "plan_review_items": []})
    actual = health.auto_paper_portfolio_latest()
    assert actual["cash"] == 200 and actual["ledger_health"]["ok"]
    assert actual["plan_review_summary"]["status"] == "separate-root"
    assert legacy.auto_paper_portfolio_latest()["cash"] == 100


@pytest.mark.parametrize("supplied", [None, [], "invalid"])
def test_non_dict_uses_real_reader_and_preserves_latest_valuation_day(tmp_path, monkeypatch, supplied):
    populate(tmp_path);bind(monkeypatch, tmp_path)
    args = inputs()
    args["daily_price_payload"] = {"target_date": "2026-11-05"}
    args["freshness_payload"] = {"target_complete_trade_date": "2026-11-05"}
    result = health.build_data_health_dashboard(**args, portfolio_payload=supplied)
    portfolio = result["source_status"]["auto_paper_portfolio"]
    assert portfolio["ledger_health"]["ok"]
    assert portfolio["ledger_health"]["target_date"] == "2026-11-04"


def test_corrupt_state_only_reports_available_backup(tmp_path, monkeypatch):
    populate(tmp_path);bind(monkeypatch, tmp_path)
    state = tmp_path / "state/auto_paper_portfolio.json"
    backup = state.with_name("auto_paper_portfolio.backup_fixture.json")
    backup.write_bytes(state.read_bytes())
    put(state.with_name("auto_paper_portfolio.identity.json"),
        {"schema": "paper_state_identity_v1", "identity": {"ledger_id": "health-fixture"}})
    state.write_text("{broken", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    actual = health.build_data_health_dashboard(**inputs())["source_status"]["auto_paper_portfolio"]
    assert actual == legacy.auto_paper_portfolio_latest()
    assert actual["ledger_health"]["read_status"] == "recovery_available"
    assert actual["ledger_health"]["recovery_source"] == str(backup)
    assert actual["portfolio_review_valid"] is False and actual["warnings"]
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_reader_is_read_only_and_viewer_remaps_its_inputs(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    populate(first, 100);populate(second, 200)
    code = r'''
import os,sys
from pathlib import Path
from quantdesk import data_health_dashboard as health
from scripts.run_research_viewer import remap_runtime_inputs
root=Path(sys.argv[1]);source=Path(health.__file__).resolve().parents[1]
def fence(event,args):
 denied=event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system','os.mkdir','os.remove','os.rename','os.rmdir','os.utime','os.chmod'}
 if event=='open':
  _,mode,flags=args
  denied=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
 if denied:raise AssertionError('query effect:'+event)
sys.addaudithook(fence)
remap_runtime_inputs(source,root/'first')
assert health.auto_paper_portfolio_latest()['cash']==100
remap_runtime_inputs(root/'first',root/'second')
assert health.auto_paper_portfolio_latest()['cash']==200
assert health.auto_paper_portfolio_latest()['ledger_health']['ok']
'''
    cp = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(tmp_path)],
                        capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert cp.returncode == 0, cp.stdout + cp.stderr
