"""Consumer/window readiness regressions; all values are synthetic fixtures."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from quantdesk.daily_evidence_contract import (
    EvidenceWindow, audit_daily_evidence, build_requirements, calendar_dates,
    digest, json_bytes, price_window, readiness_note,
)
from quantdesk.adjustment_factor_evidence import FactorResponse, publish_factor_document

TARGET = "2026-10-30"
CODE = "300017"


def put(root: Path, relative: str, value: dict) -> bytes:
    raw = json_bytes(value)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def fixture_project(root: Path, *, target: str = TARGET, entry: str = "2026-10-29", quantity: int = 100) -> dict:
    cal = root / "data/calendar/a_share_trading_calendar.csv"
    cal.parent.mkdir(parents=True, exist_ok=True)
    current, last = date(2026, 10, 26), date(2026, 11, 2)
    lines = ["date,is_open,source"]
    while current <= last:
        lines.append(f"{current.isoformat()},{str(current.weekday() < 5).lower()},synthetic_test_fixture")
        current += timedelta(days=1)
    cal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    prices = root / f"data/daily/{CODE}.csv"
    prices.parent.mkdir(parents=True, exist_ok=True)
    prices.write_text("date,symbol,open,high,low,close,volume,adjust,source\n" +
        "".join(f"{current},{CODE},10,10,10,10,100,raw,synthetic_test_fixture\n" for current in calendar_dates(root, "2026-10-26", target)), encoding="utf-8")
    book = {"valuation_date": target, "positions": [{"symbol": CODE, "strategy_type": "long", "entry_date": entry, "quantity": quantity}] if quantity else [],
            "actions": [], "closed_positions": [], "cash": 10000, "fill_policy": "same_day_close", "paper_only": True}
    put(root, "state/auto_paper_portfolio.json", book)
    put(root, f"results/candidate_refresh/daily/{target}.json", {"target_date": target, "candidates": [{"symbol": CODE}]})
    return book


def response(code: str = CODE, *, latest: str = "2026-08-27", factor: str = "2.1000000000", finished: str = "2026-10-30T20:00:00+08:00") -> FactorResponse:
    market = "sh" + code if code.startswith("6") else "sz" + code
    raw = (f"var {market}hfq=" + json.dumps({"total": 3, "data": [{"d": latest, "f": factor},
         {"d": "2009-10-30", "f": "1.0000000000"}, {"d": "1900-01-01", "f": "1.0000000000"}]}) + ";\n/* synthetic_test_fixture */").encode()
    return FactorResponse(raw, {"symbol": code, "source_kind": "sina_hfq_events", "url": f"https://finance.sina.com.cn/realstock/company/{market}/hfq.js",
        "http_status": 200, "complete_response": True, "raw_sha256": digest(raw), "raw_bytes": len(raw), "finished_at": finished})


def ready_factor(root: Path, dates: list[str] | None = None, code: str = CODE) -> dict:
    return publish_factor_document(root, response(code), code, dates or ["2026-10-29", TARGET], assert_write=lambda: None)


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in root.rglob("*") if path.is_file()}


def test_fresh_daily_and_stale_legacy_factors_are_not_research_ready(tmp_path):
    fixture_project(tmp_path)
    legacy = tmp_path / "data/metadata/adjustment_factors.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(f"trade_date,symbol,adj_factor,source\n2026-07-02,{CODE},2,synthetic_test_fixture\n")
    before = snapshot(tmp_path)
    value = audit_daily_evidence(tmp_path, TARGET)
    assert value["windows"][0]["prices"]["status"] == "ready"
    assert value["windows"][0]["factors"]["status"] == "incomplete"
    assert value["current"]["status"] == "incomplete"
    assert "因子缺1组" in readiness_note(value)
    assert snapshot(tmp_path) == before


def test_missing_calendar_session_is_not_guessed_as_a_weekday(tmp_path):
    fixture_project(tmp_path)
    path = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    path.write_text(path.read_text().replace("2026-10-29,true,synthetic_test_fixture\n", ""))
    value = audit_daily_evidence(tmp_path, TARGET)
    assert value["windows"][0]["prices"]["status"] == "not_assessed"
    assert value["current"]["status"] == "incomplete"


@pytest.mark.parametrize("token", ["qfq", "", "bogus"])
def test_bad_basis_outside_requested_window_cannot_be_filtered_away(tmp_path, token):
    fixture_project(tmp_path)
    path = tmp_path / f"data/daily/{CODE}.csv"
    path.write_text(path.read_text().replace("2026-10-26,300017,10,10,10,10,100,raw", f"2026-10-26,300017,10,10,10,10,100,{token}"))
    result = price_window(tmp_path, CODE, [TARGET])
    assert result["status"] == "incomplete"
    assert result["reason_codes"][0] in {"mixed_price_basis", "unknown_price_basis"}


@pytest.mark.parametrize("change", ["wrong_symbol", "missing_source", "nan", "inf", "zero_volume", "duplicate"])
def test_dated_file_is_not_enough_to_certify_valid_prices(tmp_path, change):
    fixture_project(tmp_path)
    path = tmp_path / f"data/daily/{CODE}.csv"
    raw = path.read_text()
    if change == "wrong_symbol": raw = raw.replace(CODE, "300018")
    elif change == "missing_source": raw = raw.replace("synthetic_test_fixture", "")
    elif change in {"nan", "inf"}: raw = raw.replace("10,10,10,10,100", f"{change},10,10,10,100")
    elif change == "zero_volume": raw = raw.replace("10,10,10,10,100", "10,10,10,10,0")
    else: raw += raw.splitlines()[-1] + "\n"
    path.write_text(raw)
    assert price_window(tmp_path, CODE, [TARGET])["status"] == "incomplete"


def test_current_and_old_native_only_research_requests_are_separate(tmp_path):
    from quantdesk.strategy_evidence_archive import archive_strategy_review
    book = fixture_project(tmp_path)
    ready_factor(tmp_path)
    put(tmp_path, "state/daily_evidence/requests.json", {"schema": "daily_evidence_requests.v1", "windows": [
        {"request_id": "old_comparison", "scope": "historical", "purpose": "strategy_comparison", "start_date": "2026-10-29", "end_date": TARGET,
         "symbols": [CODE], "factor_policy": "native_only"}]})
    req = build_requirements(tmp_path, TARGET)
    archive_strategy_review(tmp_path, target_date=TARGET, run_id="synthetic-review", before_raw=json_bytes(book), after_raw=json_bytes(book),
        started_at=TARGET+"T20:00:00+08:00", recorded_at=TARGET+"T20:01:00+08:00", process_status="completed", requirements=req, assert_write=lambda: None)
    value = audit_daily_evidence(tmp_path, TARGET)
    assert value["current"]["status"] == "ready"
    assert value["historical"]["status"] == "incomplete"
    assert value["historical"]["formal_comparison_ready"] is False


def test_candidate_scope_adds_the_actual_new_symbol(tmp_path):
    fixture_project(tmp_path)
    put(tmp_path, f"results/candidate_refresh/daily/{TARGET}.json", {"target_date": TARGET, "candidates": [{"symbol": CODE}, {"symbol": "920001"}]})
    req = build_requirements(tmp_path, TARGET)
    assert {code for window in req["windows"] for code in window["symbols"]} == {CODE, "920001"}


def test_missing_candidate_batch_cannot_disappear_from_the_current_scope(tmp_path):
    from quantdesk.strategy_evidence_archive import archive_strategy_review
    book = fixture_project(tmp_path, quantity=0)
    (tmp_path / f"results/candidate_refresh/daily/{TARGET}.json").unlink()
    req = build_requirements(tmp_path, TARGET)
    raw = json_bytes(book)
    archive_strategy_review(tmp_path, target_date=TARGET, run_id="no-candidate-materials",
        before_raw=raw, after_raw=raw, started_at=TARGET+"T20:00:00+08:00",
        recorded_at=TARGET+"T20:01:00+08:00", process_status="completed",
        requirements=req, assert_write=lambda: None)
    value = audit_daily_evidence(tmp_path, TARGET)
    assert value["current"]["status"] == "incomplete"
    assert "candidate_batch_missing" in value["current"]["reason_codes"]


def test_tampered_requirement_window_cannot_reuse_an_old_plan_hash(tmp_path):
    fixture_project(tmp_path)
    req = build_requirements(tmp_path, TARGET)
    req["windows"][0]["start_date"] = "2026-10-28"
    with pytest.raises(ValueError, match="fingerprint"):
        audit_daily_evidence(tmp_path, TARGET, requirements=req)


@pytest.mark.parametrize("bad", ["600000/../../x", "60000", "600000.SH"])
def test_requests_never_turn_symbols_into_paths(bad):
    with pytest.raises(ValueError):
        EvidenceWindow("id", "current", "review", TARGET, TARGET, (bad,))
