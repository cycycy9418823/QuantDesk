from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from quantdesk import daily_candidate_refresh as refresh
from quantdesk import daily_session_evidence as sessions
from quantdesk import data_freshness_gate as freshness
from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper import conditional_trade_plan as plans
from scripts import run_auto_paper_portfolio as cli
from tests.test_daily_candidate_refresh import _passed_manifest, _strategy_payload

TARGET = "2026-08-03"
NEXT = "2026-08-04"


def make_formal_batch(target: str = TARGET, *, candidate: dict | None = None) -> dict:
    strategy = _strategy_payload(target)
    strategy["standard_candidates"][0].update(close=10.0, amount_ma20=50_000_000.0)
    manifest = _passed_manifest(target)
    if candidate is not None:
        symbol = candidate["symbol"]
        strategy["standard_candidates"] = [copy.deepcopy(candidate)]
        universe = manifest["universe"]["current_analysis_pool"]
        universe.update(symbols=[symbol], symbol_count=1)
        universe["sha256"] = refresh._stable_hash({"name": "current_analysis_pool", "target_date": target, "symbols": [symbol]})
        manifest["components"]["features"]["per_symbol"] = {symbol: {"eligible": True, "price_basis": "raw", "latest_date": target}}
        manifest["components"]["session_expectation"].update(symbols=[symbol], classification_counts={"expected_trade": 1, "unknown": 0}, counts={"expected_trade": 1, "unknown": 0})
    batch = refresh.build_formal_candidate_batch(data_chain_manifest=manifest,
                                               strategy_payload=strategy, captured_at=f"{target}T18:00:00+08:00")
    assert refresh.verify_formal_candidate_batch(batch) == []
    return batch


def dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def prepare_root(root: Path, monkeypatch, *, mode: str = "valid") -> tuple[dict, Path]:
    source_root = Path(__file__).resolve().parents[1]
    for module in (paper, plans, sessions, freshness):
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and value.is_relative_to(source_root):
                monkeypatch.setattr(module, name, root / value.relative_to(source_root))
    old = {"symbol": "600009", "strategy_type": "long", "close": 10.0}
    dump(root / "results/auto_strategy_latest.json", {
        "generated_at": "2026-07-01T18:00:00+08:00", "long_candidates": [old], "board_candidates": [],
    })
    for symbol in ("600000", "600007", "600009"):
        path = root / "data/daily" / f"{symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"date": day.strftime("%Y-%m-%d"), "open": 10.0, "high": 10.5, "low": 9.5,
                       "close": 10.0, "prev_close": 10.0, "volume": 1000, "amount": 10000, "adjust": "raw"}
                      for day in pd.bdate_range("2026-06-29", TARGET)]).to_csv(path, index=False)
    batch = make_formal_batch()
    path = root / "results/candidate_refresh/daily" / f"{TARGET}.json"
    if mode != "missing":
        data = copy.deepcopy(batch)
        if mode == "wrong_date":
            data = make_formal_batch("2026-07-31")
        elif mode == "bad_hash":
            data["provenance"]["hash"] = "0" * 64
        elif mode == "bad_qualification":
            data["candidates"][0]["source_qualification"]["universe_member"] = False
            data["provenance"]["hash"] = refresh._stable_hash(refresh._candidate_hash_projection(data))
        dump(path, data)
    monkeypatch.setattr(paper, "_load_auto_strategy", lambda *_a, **_kw: pytest.fail("formal entry read legacy candidate file"))
    return batch, path


@pytest.mark.parametrize("mode", ["missing", "wrong_date", "bad_hash", "bad_qualification"])
@pytest.mark.parametrize("entry", ["cli", "direct_builder"])
def test_public_entries_reject_bad_formal_without_old_candidate_fallback(tmp_path: Path, monkeypatch, capsys, mode: str, entry: str) -> None:
    prepare_root(tmp_path, monkeypatch, mode=mode)
    if entry == "cli":
        assert cli.main(["--dry-run", "--build-conditional-plan", "--plan-date", TARGET, "--trade-date", NEXT]) == 0
        payload = json.loads(capsys.readouterr().out)
    else:
        payload = plans.build_conditional_trade_plan(project_root=tmp_path, state={}, plan_date=TARGET,
                    trade_date=NEXT, daily_dir=tmp_path / "data/daily", write=False)
    assert payload["symbols"] == []
    assert payload["formal_execution_eligible"] is False
    assert payload["candidate_intake"]["status"] in {"missing", "blocked"}
    assert payload["warnings"]


def test_cli_default_consumes_exact_formal_batch_and_ignores_low_price_filter(tmp_path: Path, monkeypatch, capsys) -> None:
    batch, path = prepare_root(tmp_path, monkeypatch)
    universe = tmp_path / "data/universe/low_price_learning_symbols.csv"
    universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_text("symbol,included\n600009,true\n", encoding="utf-8")
    cli.main(["--dry-run", "--build-conditional-plan", "--plan-date", TARGET, "--trade-date", NEXT])
    payload = json.loads(capsys.readouterr().out)
    assert payload["input_source"] == "formal_candidate_batch"
    assert [row["symbol"] for row in payload["symbols"]] == ["600000"]
    row = payload["symbols"][0]
    assert row["formal_execution_eligible"] is True and row["rules"]["entry_rules"]
    assert row["candidate_envelope"] == refresh.build_candidate_envelope(batch, batch_path=path, candidate=batch["candidates"][0])
    assert payload["candidate_intake"]["provenance_hash"] == batch["provenance"]["hash"]


def test_injected_status_cannot_promote_modified_batch(tmp_path: Path, monkeypatch) -> None:
    batch, path = prepare_root(tmp_path, monkeypatch)
    batch["candidates"][0]["close"] = 100.0
    result = paper.run_auto_paper_portfolio(dry_run=True, build_conditional_plan=True, plan_date=TARGET, trade_date=NEXT,
                    formal_candidate_batch=batch, formal_candidate_batch_path=path, formal_candidate_status="passed")
    assert result["symbols"] == []
    assert "formal_candidate_injected_batch_mismatch" in result["candidate_intake"]["reason_codes"]


def test_missing_candidates_keep_existing_position_exit_plan(tmp_path: Path, monkeypatch) -> None:
    prepare_root(tmp_path, monkeypatch, mode="missing")
    position = {"symbol": "600007", "strategy_type": "long", "quantity": 100, "entry_price": 10.0,
                "entry_date": "2026-07-01", "sellable_quantity": 100}
    payload = plans.build_conditional_trade_plan(project_root=tmp_path, state={"positions": [position]},
        plan_date=TARGET, trade_date=NEXT, daily_dir=tmp_path / "data/daily", write=False)
    assert payload["candidate_intake"]["status"] == "missing"
    assert payload["formal_execution_eligible"] is True
    row = payload["symbols"][0]
    assert row["position_state"] == "watch_exit" and row["rules"]["exit_rules"]
    assert row["rules"]["entry_rules"] == row["rules"]["add_rules"] == []


def test_legacy_diagnostic_cannot_write_or_generate_formal_entry_rules(tmp_path: Path, monkeypatch, capsys) -> None:
    prepare_root(tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    cli.main(["--candidate-mode", "legacy_diagnostic", "--force-reset", "--plan-date", TARGET, "--trade-date", NEXT])
    result = json.loads(capsys.readouterr().out)
    after = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    assert result["mode"] == "diagnostic" and result["dry_run"] is True and result["ledger_write"] is False
    assert result["formal_execution_eligible"] is False
    assert [row["symbol"] for row in result["symbols"]] == ["600009"]
    assert all(not row["rules"]["entry_rules"] for row in result["symbols"])


def test_freshness_scope_has_separate_formal_candidate_and_ledger_provenance(tmp_path: Path, monkeypatch) -> None:
    batch, _ = prepare_root(tmp_path, monkeypatch)
    ledger_path = tmp_path / "state/auto_paper_portfolio.json"
    dump(ledger_path, {"updated_at": "2026-07-31T18:00:00+08:00", "positions": [{"symbol": "600007"}]})
    dump(tmp_path / "results/auto_paper_portfolio_latest.json", {"positions": [{"symbol": "600009"}]})
    scope = freshness.collect_current_review_scope(TARGET)
    assert scope["symbols"] == ["600000", "600007"]
    assert scope["candidate_scope"]["provenance_hash"] == batch["provenance"]["hash"]
    assert scope["candidate_scope"]["candidate_count"] == 1
    assert scope["position_scope"]["source_hash"] == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    gate = freshness.evaluate_data_freshness(target_date=TARGET, daily_feature_payload={"latest_date": TARGET})
    assert gate["review_scope_symbol_count"] == 2
    assert "600009" not in gate["review_scope_latest_by_symbol"]
    assert gate["candidate_scope"]["target_date"] == gate["position_scope"]["target_date"] == TARGET


def test_position_only_freshness_never_loads_candidate_source(tmp_path: Path, monkeypatch) -> None:
    prepare_root(tmp_path, monkeypatch)
    monkeypatch.setattr(freshness, "load_formal_candidate_intake", lambda *a, **kw: pytest.fail("position review loaded candidates"))
    gate = freshness.evaluate_paper_review_freshness({"positions": []}, target_date=TARGET, daily_dir=tmp_path / "data/daily")
    assert gate["status"] == "not_applicable" and gate["can_update_portfolio_valuation"] is True


@pytest.mark.parametrize("retry", [False, True])
def test_real_freeze_builder_keeps_current_chain_binding_before_terminal_publish(tmp_path: Path, monkeypatch, retry: bool) -> None:
    from quantdesk.daily_plan_freeze import freeze_t_plus_one_plan

    batch, dated = prepare_root(tmp_path, monkeypatch)
    manifest = _passed_manifest(TARGET)
    expected_source = dated
    if retry:
        old_manifest = copy.deepcopy(manifest)
        old_manifest["run_id"] = "earlier-run"
        old_manifest["components"]["features"]["output_sha256"] = "c" * 64
        old_manifest["coverage"]["features"]["output_sha256"] = "c" * 64
        old_batch = refresh.build_formal_candidate_batch(data_chain_manifest=old_manifest,
                        strategy_payload=_strategy_payload(TARGET), captured_at=f"{TARGET}T16:00:00+08:00")
        dump(dated, old_batch)
        expected_source = tmp_path / "results/candidate_refresh/runs" / manifest["run_id"] / "formal_candidate_batch.json"
        dump(expected_source, batch)
        manifest["components"]["candidate_refresh_gate"] = refresh.candidate_refresh_gate(manifest)
        manifest["components"]["candidate_refresh"] = {
            "status": "blocked", "applied": False, "reason_codes": ["candidate_dated_snapshot_conflict"],
            "staging_path": expected_source.relative_to(tmp_path).as_posix(),
        }
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text(f"date,is_open,source,note\n{TARGET},true,fixture,\n{NEXT},true,fixture,\n", encoding="utf-8")
    monkeypatch.setattr(plans, "now_text", lambda: f"{TARGET}T18:30:00+08:00")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = freeze_t_plus_one_plan(project_root=tmp_path, data_chain_manifest=manifest,
                now=datetime.fromisoformat(f"{TARGET}T18:30:00+08:00"), calendar_path=calendar, write=False)
    assert result["status"] == "prepared", result
    assert Path(result["formal_candidate_batch_path"]) == expected_source
    row = result["prepared_plan"]["symbols"][0]
    assert row["formal_execution_eligible"] is True and row["rules"]["entry_rules"]
    assert Path(row["candidate_envelope"]["batch_path"]) == expected_source
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
