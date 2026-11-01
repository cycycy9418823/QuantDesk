"""The owner must see actual dated evidence, including unresolved gaps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


DAY = "2026-10-30"


def write_case(root: Path) -> dict:
    daily = root / "data/daily"
    daily.mkdir(parents=True)
    header = "date,symbol,open,high,low,close,volume,amount,adjust,source\n"
    for symbol, day in (("600621", DAY), ("301139", "2026-10-27"), ("002717", "2026-10-23")):
        (daily / f"{symbol}.csv").write_text(
            header + f"{day},{symbol},10,11,9,10.5,100,1050,raw,fixture_provider\n", encoding="utf-8")
    roster = root / "data/universe/a_share_symbols.csv"
    roster.parent.mkdir()
    roster.write_text("symbol,name\n600621,已有日线\n301139,待核实\n002717,当日无交易\n", encoding="utf-8")
    symbols = ["600621", "301139", "002717"]
    classes = dict(zip(symbols, ["valid_raw_trading_bar", "unobtained_or_unverified", "confirmed_nontrade"]))
    audit = {
        "schema_version": "full_a_daily_reconciliation_v1", "task_type": "full_a_daily_reconciliation",
        "run_id": "fixture-audit", "target_date": DAY,
        "partition": {
            "target_date": DAY, "universe_symbols": symbols, "universe_count": 3, "gap_classification": classes,
            "roster_evidence": {"sha256": hashlib.sha256(roster.read_bytes()).hexdigest()},
            "target_row_evidence": {s: {"source_sha256": hashlib.sha256((daily / f"{s}.csv").read_bytes()).hexdigest()} for s in symbols},
            "latest_by_symbol": {"600621": DAY, "301139": "2026-10-27", "002717": "2026-10-23"},
            "completeness": {"with_bar_count": 1, "verified_excluded_count": 1, "gap_count": 1,
                "universe_count": 3, "target_date": DAY, "evidence_source": "fixture_session_provider",
                "evidence_present": True, "verified_excluded_symbols": ["002717"]},
        },
    }
    path = root / "results/data_chain/full_a_daily_reconciliation_latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(audit), encoding="utf-8")
    return {"target_date": DAY, "run_id": "fixture-chain", "coverage": {
        "daily": {"stale_symbols": ["600621", "301139"], "missing_symbols": []}}}


def review(root: Path, chain: dict) -> dict:
    from quantdesk.daily_gap_review import build_daily_gap_review
    return build_daily_gap_review(root, chain=chain, target_date=DAY)


def test_existing_row_resolves_old_warning_but_never_fills_missing_days(tmp_path: Path) -> None:
    chain = write_case(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = review(tmp_path, chain)
    assert result["available"] and result["target_date"] == DAY
    assert result["observed_counts"] == {"total": 3, "with_bar": 1, "verified_no_session": 1, "unresolved": 1}
    found = result["by_symbol"]["600621"]
    assert found["status"] == "present" and found["previously_flagged"]
    assert found["close"] == 10.5 and found["price_basis"] == "raw"
    assert found["source"] == "fixture_provider" and found["data_date"] == DAY
    missing = result["by_symbol"]["301139"]
    assert missing["status"] == "unresolved" and missing["data_date"] == "2026-10-27"
    assert missing["close"] is None
    assert result["by_symbol"]["002717"]["status"] == "verified_no_session"
    assert not result["writes_data"] and not result["network"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("bad_row", [
    f"{DAY},600621,10,11,9,10.5,100,1050,qfq,fixture_provider\n",
    f"{DAY},600621,10,11,9,10.5,100,1050,,fixture_provider\n",
    f"{DAY},600621,0,0,0,0,0,0,raw,fixture_provider\n",
    f"{DAY},600621,10,11,9,10.5,100,1050,raw,fixture_provider\n" * 2,
])
def test_untrusted_or_duplicate_target_row_is_not_green(tmp_path: Path, bad_row: str) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "data/daily/600621.csv"
    header = path.read_text(encoding="utf-8").splitlines()[0] + "\n"
    path.write_text(header + bad_row, encoding="utf-8")
    row = review(tmp_path, chain)["by_symbol"]["600621"]
    assert row["status"] == "unresolved"
    assert row["close"] is None


def test_basis_is_classified_before_filtering_old_rows(tmp_path: Path) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "data/daily/600621.csv"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("2026-10-29,600621,10,11,9,10.5,100,1050,qfq,fixture_provider\n")
    assert review(tmp_path, chain)["by_symbol"]["600621"]["status"] == "unresolved"


def test_no_session_proof_cannot_override_changed_file(tmp_path: Path) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "data/daily/002717.csv"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{DAY},002717,10,11,9,10.5,100,1050,raw,fixture_provider\n")
    row = review(tmp_path, chain)["by_symbol"]["002717"]
    assert row["status"] == "present" and row["close"] == 10.5
    assert not row["matches_audit_file"]


@pytest.mark.parametrize("fault", ["wrong_date", "bad_counts", "changed_roster", "missing", "invalid_json",
                                  "array_root", "bad_proof", "inner_date", "no_session_proof"])
def test_missing_stale_or_inconsistent_audit_has_explicit_warning(tmp_path: Path, fault: str) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "results/data_chain/full_a_daily_reconciliation_latest.json"
    audit = json.loads(path.read_text())
    if fault == "wrong_date":
        audit["target_date"] = "2026-10-29"
    elif fault == "bad_counts":
        audit["partition"]["completeness"]["with_bar_count"] = 3
    elif fault == "changed_roster":
        with (tmp_path / "data/universe/a_share_symbols.csv").open("a") as stream:
            stream.write("000001,new\n")
    elif fault == "missing":
        path.unlink()
    elif fault == "array_root":
        audit = []
    elif fault == "bad_proof":
        audit["partition"]["target_row_evidence"]["002717"] = None
    elif fault == "inner_date":
        audit["partition"]["completeness"]["target_date"] = "2026-10-29"
    elif fault == "no_session_proof":
        audit["partition"]["completeness"]["evidence_present"] = False
    if fault not in {"missing", "invalid_json"}:
        path.write_text(json.dumps(audit))
    elif fault == "invalid_json":
        path.write_text("{not-json")
    result = review(tmp_path, chain)
    assert not result["available"] and result["status"] == "unavailable"
    assert result["reason_code"] and result["message"]


def test_refresh_sees_same_size_same_mtime_data_replacement(tmp_path: Path) -> None:
    import os
    chain = write_case(tmp_path)
    assert review(tmp_path, chain)["by_symbol"]["600621"]["close"] == 10.5
    path = tmp_path / "data/daily/600621.csv"
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b",10.5,", b",10.6,"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert review(tmp_path, chain)["by_symbol"]["600621"]["close"] == 10.6


@pytest.mark.parametrize("fault", ["missing_file", "wrong_symbol", "missing_symbol", "missing_source"])
def test_file_and_row_identity_fail_closed(tmp_path: Path, fault: str) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "data/daily/600621.csv"
    content = path.read_text(encoding="utf-8")
    if fault == "missing_file":
        path.unlink()
    else:
        replacements = {"wrong_symbol": (",600621,", ",600622,"),
                        "missing_symbol": (",600621,", ",,"),
                        "missing_source": (",fixture_provider", ",")}
        path.write_text(content.replace(*replacements[fault]), encoding="utf-8")
    row = review(tmp_path, chain)["by_symbol"]["600621"]
    assert row["status"] == "unresolved" and row["close"] is None and row["reason_code"]


def test_old_complete_audit_does_not_hide_a_new_gap(tmp_path: Path) -> None:
    chain = write_case(tmp_path)
    path = tmp_path / "results/data_chain/full_a_daily_reconciliation_latest.json"
    audit = json.loads(path.read_text())
    audit["partition"]["gap_classification"]["301139"] = "valid_raw_trading_bar"
    audit["partition"]["completeness"].update(with_bar_count=2, gap_count=0)
    path.write_text(json.dumps(audit))
    result = review(tmp_path, chain)
    assert result["observed_counts"]["unresolved"] == 0
    assert result["by_symbol"]["301139"]["status"] == "unresolved"
    assert result["status"] == "partial"


@pytest.fixture
def dashboard_case(tmp_path: Path, monkeypatch):
    from quantdesk import data_health_dashboard as health
    from quantdesk.ui_read_cache import UI_READ_CACHE, UI_FILE_CACHE
    chain = write_case(tmp_path)
    (tmp_path / "results/data_chain/daily_data_chain_latest.json").write_text(json.dumps(chain))
    old_card = {"title": "日线数据", "status": "yellow", "value": "910/914",
                "detail": "旧范围快照", "next_action": "保留历史证据"}
    snapshot = tmp_path / "results/data_health/data_health_dashboard_latest.json"
    snapshot.parent.mkdir()
    snapshot.write_text(json.dumps({"ok": True, "health_level": "green", "cards": {"daily_data": old_card},
        "source_status": {}, "generated_at": DAY + "T19:00:00", "summary_text": "旧快照"}))
    (tmp_path / "results/auto_paper_portfolio_latest.json").write_text(json.dumps({"ok": True, "positions": []}))
    monkeypatch.setattr(health, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(health, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(health, "DATA_HEALTH_DASHBOARD_LATEST_JSON", snapshot)
    monkeypatch.setattr(health, "infer_target_date", lambda *_a: (DAY, "fixture"))
    monkeypatch.setattr(health, "daily_data_scheduler_status", lambda **_k: {"status": "healthy"})
    monkeypatch.setattr(health, "build_formal_daily_chain_status", lambda *_a, **_k: {})
    monkeypatch.setattr(health, "latest_daily_data_chain", lambda *_a: chain)
    monkeypatch.setattr(health, "build_evidence_freshness", lambda *_a: {})
    monkeypatch.setattr(health, "_snapshot_age_seconds", lambda *_a: 0)
    monkeypatch.setattr(health, "strategy_initialization_evidence", lambda *_a, **_k: True)
    monkeypatch.setattr(health, "inspect_strategy_ledger", lambda *_a, **_k: {
        "ok": False, "reason_codes": ["accounting_difference"], "difference": 3.03})
    monkeypatch.setattr(health, "_build_scope_health_block", lambda *_a, **_k: {
        "scopes": {"research_pool": {"label": "当前研究池", "detail": "原记录滞后2只"}}})
    monkeypatch.setattr(health, "_data_alerts_block", lambda *_a, **_k: {})
    UI_READ_CACHE.clear()
    UI_FILE_CACHE.clear()
    yield health, chain, snapshot
    UI_READ_CACHE.clear()
    UI_FILE_CACHE.clear()


def test_dashboard_updates_owner_card_without_erasing_ledger_or_historical_evidence(dashboard_case, tmp_path: Path) -> None:
    health, chain, snapshot = dashboard_case
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = health.build_cached_data_health_dashboard()
    assert result["cards"]["daily_data"]["value"] == DAY + " 日线 1/3"
    assert result["source_status"]["previous_daily_card"]["value"] == "910/914"
    assert result["daily_data_chain"] == chain
    assert result["scope_health"]["scopes"]["research_pool"]["label"] == "研究链当时记录"
    assert result["cards"]["portfolio_review"]["status"] == result["health_level"] == "red"
    assert result["source_status"]["auto_paper_portfolio"]["ledger_health"]["difference"] == 3.03
    assert "accounting_difference" in result["health_degraded_reason_codes"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_actual_health_http_cache_tracks_selected_rows_and_audit_bytes(dashboard_case, tmp_path: Path, monkeypatch) -> None:
    from datetime import date
    import os
    from scripts import quant_api_server as server
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "DAILY_DIR", tmp_path / "data/daily")
    monkeypatch.setattr(server, "TRACKING_DIR", tmp_path / "data/tracking")
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date.fromisoformat(DAY))
    monkeypatch.setitem(server.app.config, "TESTING", True)
    monkeypatch.setitem(server.app.config, "UI_READ_CACHE_ENABLED", True)
    monkeypatch.setitem(server.app.config, "QUANTDESK_PROJECT_ROOT", str(tmp_path))
    client = server.app.test_client()
    first = client.get("/api/data-health/dashboard?summary=true")
    assert first.status_code == 200
    original = first.get_json()
    assert original["daily_gap_review"]["by_symbol"]["600621"]["close"] == 10.5
    path = tmp_path / "data/daily/600621.csv"
    stamp = path.stat()
    path.write_bytes(path.read_bytes().replace(b",10.5,", b",10.6,"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed = client.get("/api/data-health/dashboard?summary=true").get_json()
    assert changed["daily_gap_review"]["by_symbol"]["600621"]["close"] == 10.6
    assert changed["read_identity"]["sha256"] != original["read_identity"]["sha256"]
    assert client.get(original["details_url"]).status_code == 409
    audit_path = tmp_path / "results/data_chain/full_a_daily_reconciliation_latest.json"
    stamp = audit_path.stat()
    audit_path.write_bytes(audit_path.read_bytes().replace(b"fixture-audit", b"fixture-other"))
    os.utime(audit_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    latest = client.get("/api/data-health/dashboard?summary=true").get_json()
    assert latest["daily_gap_review"]["run_id"] == "fixture-other"
    assert latest["read_identity"]["sha256"] != changed["read_identity"]["sha256"]
