"""Real file/collector counterexamples for bounded minute recovery; no network."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday_refresh import collector
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.intraday.intraday_quality_audit import audit_intraday_file
from quantdesk import data_freshness_gate, plan_review_receipts
from quantdesk.paper import position_management_engine

DAY = "2026-10-08"
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def bar(stamp=f"{DAY} 09:31:00", symbol="600000", basis="raw", close=10.0):
    return {"symbol": symbol, "datetime": stamp, "date": stamp[:10], "time": stamp[11:],
            "open": close, "high": close + 0.1, "low": close - 0.1, "close": close,
            "volume": 100.0, "amount": 1000.0, "source": "fixture", "price_basis": basis,
            "updated_at": f"{DAY}T15:00:30+08:00"}


def full_day(symbol="600000"):
    starts = [datetime(2026, 10, 8, 9, 31), datetime(2026, 10, 8, 13, 1)]
    return [bar((start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), symbol)
            for start in starts for i in range(120)]


def write_csv(path, rows, *, legacy=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [name for name in collector.MINUTE_COLUMNS if not legacy or name != "price_basis"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def scope(*args, **kwargs):
    return {"symbols": [{"symbol": "600000", "priority": "critical"}], "warnings": []}


@pytest.fixture(autouse=True)
def no_live_snapshot(monkeypatch):
    monkeypatch.setattr(collector, "refresh_full_market_snapshot",
                        lambda *a, **k: {"movers": [], "reason_codes": []})


def test_legacy_bytes_stay_and_same_timestamp_new_raw_is_readable(tmp_path, monkeypatch):
    minute_dir = tmp_path/"data/intraday/1m"
    path = minute_dir/"600000.csv"
    write_csv(path, [bar(close=999.0)], legacy=True)
    before, old_mtime = path.read_bytes(), path.stat().st_mtime_ns
    rows = [bar(), bar(f"{DAY} 09:32:00")]

    appended, reasons = collector._append_rows(path, rows, now_iso=NOW.isoformat())

    assert appended == 2, reasons
    assert path.read_bytes() == before and path.stat().st_mtime_ns == old_mtime
    payload = read_intraday_csv(path, default_symbol="600000", target_date=DAY)
    assert payload["ok"] and len(payload["frame"]) == 2
    assert set(payload["frame"]["price_basis"]) == {"raw"}
    assert set(payload["frame"]["close"]) == {10.0}
    actual = Path(payload["path"])
    assert actual != path and actual.is_file()
    assert any("legacy_price_basis_unknown" in reason for reason in payload["warnings"])
    raw_before = actual.read_bytes()
    assert collector._append_rows(path, rows, now_iso=NOW.isoformat())[0] == 0
    assert actual.read_bytes() == raw_before and path.read_bytes() == before
    engine_frame = position_management_engine._read_intraday("600000", minute_dir.parent)
    assert set(engine_frame["close"]) == {10.0}
    monkeypatch.setattr(data_freshness_gate, "INTRADAY_1M_DIR", minute_dir)
    assert data_freshness_gate._has_intraday_for_date("600000", DAY)
    audit = audit_intraday_file("600000", DAY, intraday_dir=minute_dir)
    assert Path(audit["path"]) == actual
    assert audit["formal_execution_eligible"] is False, "two real bars are still an incomplete session"


@pytest.mark.parametrize("basis", [None, "", "unknown", "qfq", "hfq"])
def test_storage_never_invents_raw_for_an_undeclared_or_adjusted_row(tmp_path, basis):
    path = tmp_path/"600000.csv"
    row = bar(basis=basis)
    if basis is None:
        row.pop("price_basis")
    count, reasons = collector._append_rows(path, [row], now_iso=NOW.isoformat())
    assert count == 0 and reasons
    assert not path.exists()


def test_round_keeps_current_day_240_in_one_request_and_reports_actual_path(tmp_path):
    minute_dir = tmp_path/"data/intraday/1m"
    legacy = minute_dir/"600000.csv"
    write_csv(legacy, [bar("2026-09-30 15:00:00")], legacy=True)
    original = legacy.read_bytes()
    calls = []

    def fetch(symbol, offset):
        calls.append((symbol, offset))
        return [bar("2026-10-07 15:00:00"), *full_day()[-offset:]], ""

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW, scope_builder=scope, fetcher=fetch,
    )
    assert calls == [("600000", 240)]
    assert result["appended_rows"] == 240
    assert result["provider_status"][0]["status"] == "ok"
    actual = Path(result["provider_status"][0]["storage_path"])
    if not actual.is_absolute():
        actual = tmp_path/actual
    assert actual != legacy
    saved = read_intraday_csv(legacy, default_symbol="600000")
    assert len(saved["frame"]) == 240 and set(saved["frame"]["date"]) == {DAY}
    assert legacy.read_bytes() == original
    assert result["provider_status"][0]["ignored_other_day_rows"] == 1
    assert result["fresh_count"] == 1


def test_existing_sina_single_response_is_not_cut_down_to_last_hour(monkeypatch):
    from quantdesk.intraday import intraday_providers

    monkeypatch.setattr(collector, "_mootdx_minute_rows", lambda *a, **k: ([], "native_failed", {}))
    payload = [{"day": row["datetime"], **{key: row[key] for key in ("open", "high", "low", "close", "volume")}}
               for row in full_day()]
    urls = []

    def http(url, **kwargs):
        urls.append(url)
        return json.dumps(payload)

    monkeypatch.setattr(intraday_providers, "_http_get_text", http)
    rows, error, diagnostics = collector._live_minute_rows(
        "600000", offset=collector.MINUTE_OFFSET, now=NOW, monotonic=lambda: 0.0,
    )
    assert error == "", diagnostics
    assert len(urls) == 1 and "datalen=240" in urls[0]
    assert len(rows) == 240
    assert {row["price_basis"] for row in rows} == {"raw"}
    assert min(row["datetime"] for row in rows).startswith(f"{DAY} 09:31")


def test_review_receipt_binds_real_files_when_one_symbol_uses_legacy_supplement(tmp_path):
    minute_dir = tmp_path/"data/intraday/1m"
    legacy = minute_dir/"600000.csv"
    write_csv(legacy, [bar("2026-09-30 15:00:00")], legacy=True)
    original = legacy.read_bytes()
    assert collector._append_rows(legacy, full_day(), now_iso=NOW.isoformat())[0] == 240
    write_csv(minute_dir/"600001.csv", full_day("600001"))
    plan = {
        "ok": True, "plan_id": "minute-recovery", "plan_date": "2026-09-30", "trade_date": DAY,
        "source_data_cutoff": "2026-09-30", "formal_execution_eligible": True,
        "symbols": [{"symbol": symbol, "rules": {"exit_rules": [{"price": 9.0}]}} for symbol in ("600000", "600001")],
        "price_data_validation": {"status": "passed", "formal_execution_eligible": True,
                                  "target_date": "2026-09-30", "blocked_symbols": []},
    }
    receipt = plan_review_receipts.build_expiry_review_receipt(
        tmp_path, plan, checked_at=f"{DAY}T18:00:00+08:00",
    )
    assert receipt["status"] == "passed", receipt["reason_codes"]
    assert plan_review_receipts.verify_plan_review_receipt(receipt, plan=plan, project_root=tmp_path) == []
    first = receipt["inputs"][0]
    actual = tmp_path/first["path"]
    assert actual != legacy and first["sha256"] == hashlib.sha256(actual.read_bytes()).hexdigest()
    assert legacy.read_bytes() == original
    actual.write_bytes(actual.read_bytes() + b"\n")
    assert "receipt_input_changed" in plan_review_receipts.verify_plan_review_receipt(
        receipt, plan=plan, project_root=tmp_path,
    )


def test_supplement_directory_name_never_supplies_price_basis(tmp_path):
    path = tmp_path/"600000.csv"
    write_csv(path, [bar()], legacy=True)
    # The helper directory indicates a schema route, never price truth.
    alternate = path.parent/"declared_basis"/path.name
    write_csv(alternate, [bar(basis="unknown")])
    payload = read_intraday_csv(path, default_symbol="600000")
    assert payload["price_basis"] == "unknown"
    assert set(payload["frame"]["price_basis"]) == {"unknown"}


def test_freshness_uses_new_source_without_touching_old_history(tmp_path, monkeypatch):
    path = tmp_path/"600000.csv"
    write_csv(path, [bar("2026-09-30 15:00:00")], legacy=True)
    old = path.read_bytes()
    assert collector._append_rows(path, [bar()], now_iso=NOW.isoformat())[0] == 1
    monkeypatch.setattr(data_freshness_gate, "INTRADAY_1M_DIR", tmp_path)
    assert data_freshness_gate._has_intraday_for_date("600000", DAY)
    assert path.read_bytes() == old


# A torn row is row-level damage (1C, owner plan 2026-10-11: isolate bad
# rows, never refuse the whole file); tests/test_fix1c_minute_quarantine.py
# proves it still cannot deduplicate or claim freshness. A non-raw segment is
# file-level and stays blocked.
@pytest.mark.parametrize("damage", ["unknown", "qfq"])
def test_invalid_stored_segment_cannot_deduplicate_or_claim_fresh(tmp_path, damage):
    legacy = tmp_path/"data/intraday/1m/600000.csv"
    write_csv(legacy, [bar("2026-09-30 15:00:00")], legacy=True)
    segment = legacy.parent/"declared_basis"/legacy.name
    stamp = f"{DAY} 14:55:00"
    if damage == "truncated":
        segment.parent.mkdir()
        segment.write_text(collector.MINUTE_HEADER + f"\n600000,{stamp}\n", encoding="utf-8")
    else:
        write_csv(segment, [bar(stamp, basis=damage)])
    before = segment.read_bytes()
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW.replace(hour=14, minute=56),
        scope_builder=scope, fetcher=lambda *_a: ([bar(stamp)], ""),
    )
    assert result["fresh_count"] == 0
    assert result["provider_status"][0]["status"] == "storage_blocked"
    assert result["provider_status"][0]["error_stage"] == "storage"
    assert result["appended_rows"] == 0 and segment.read_bytes() == before


def test_local_provider_returns_actual_supplement_path(tmp_path):
    from quantdesk.intraday.intraday_providers import _fetch_local_existing

    legacy = tmp_path/"600000.csv"
    write_csv(legacy, [bar("2026-09-30 15:00:00")], legacy=True)
    assert collector._append_rows(legacy, full_day(), now_iso=NOW.isoformat())[0] == 240
    payload = _fetch_local_existing("600000", DAY, intraday_dir=tmp_path)
    assert Path(payload["path"]) == tmp_path/"declared_basis/600000.csv"
    assert Path(payload["requested_path"]) == legacy
    empty = _fetch_local_existing("600000", "2026-10-09", intraday_dir=tmp_path)
    assert Path(empty["path"]) == Path(payload["path"])


def test_quality_reader_finds_surviving_supplement_without_legacy_file(tmp_path):
    segment = tmp_path/"declared_basis/600000.csv"
    write_csv(segment, full_day())
    result = audit_intraday_file("600000", DAY, intraday_dir=tmp_path)
    assert result["exists"] and result["formal_execution_eligible"] is True
    assert Path(result["path"]) == segment
