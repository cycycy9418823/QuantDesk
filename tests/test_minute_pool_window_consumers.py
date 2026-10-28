from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import quantdesk.intraday.minute_pool_window_audit as window_audit
from quantdesk.intraday.minute_pool_capability import classify_minute_replay_evidence
import quantdesk.paper.position_management_engine as position_engine
from quantdesk.paper.position_management_engine import simulate_conditional_plan_file


WINDOW_START = "2026-05-18"
WINDOW_END = "2026-05-19"
PROVENANCE_KEYS = {
    "pool_source_snapshot",
    "window_start",
    "window_end",
    "decision_basis",
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _write_window_snapshot(
    path: Path,
    *,
    rows: list[dict],
    window_start: str = WINDOW_START,
    window_end: str = WINDOW_END,
    task_type: str = "minute_pool_window_audit",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": True,
        "task_type": task_type,
        "window_start": window_start,
        "window_end": window_end,
        "trading_days": [WINDOW_START, WINDOW_END],
        "trading_day_count": 2,
        "total_symbols": len(rows),
        "fully_usable_symbol_count": sum(
            all(status == "usable" for status in (row.get("day_status") or {}).values())
            for row in rows
        ),
        "symbols": rows,
        "criteria": {"usable_standard": "existing_intraday_quality_audit_v1",
                     "eligibility_standard": "finalized_session_evidence_v3"},
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _window_row(symbol: str, status: str) -> dict:
    return {
        "symbol": symbol,
        "day_status": {WINDOW_START: status, WINDOW_END: status},
        "day_evidence": {day: {"formal_execution_eligible": status == "usable",
                               "reliable_no_fill_eligible": status == "usable",
                               "session_status": "complete_session" if status == "usable" else "missing_session"}
                         for day in (WINDOW_START, WINDOW_END)},
        "usable_day_count": 2 if status == "usable" else 0,
        "all_days_usable": status == "usable",
    }


def _symbol_pool(*symbols: str) -> dict:
    return {
        "passed": True,
        "status": "passed",
        "symbols": set(symbols),
        "source_snapshot": "last_passed",
        "pool_job_path": "data/vendor_staging/top200/summary.json",
        "target_date": WINDOW_START,
    }


def _resolve(*args, **kwargs) -> dict:
    resolver = getattr(window_audit, "resolve_minute_replay_evidence", None)
    assert callable(resolver), "resolve_minute_replay_evidence is not implemented"
    return resolver(*args, **kwargs)


def test_window_hit_uses_symbol_date_classifier_and_verified_evidence(tmp_path: Path) -> None:
    snapshot = _write_window_snapshot(
        tmp_path / "window.json",
        rows=[_window_row("600000", "usable")],
    )
    window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=snapshot)
    result = _resolve(
        "600000",
        WINDOW_START,
        fill_model="intraday_1m_close",
        window_pool=window_pool,
        symbol_pool=_symbol_pool("600000"),
    )

    assert result["evidence"]["status"] == "verified_completed_pool"
    assert result["decision_basis"] == "symbol_date"
    assert result["pool_source_snapshot"] == "window_audit"
    assert result["window_start"] == WINDOW_START
    assert result["window_end"] == WINDOW_END
    assert PROVENANCE_KEYS <= set(result)


def test_window_day_status_overrides_top200_hit_for_missing_invalid_and_partial(
    tmp_path: Path,
) -> None:
    for status in ("missing", "invalid", "partial"):
        snapshot = _write_window_snapshot(
            tmp_path / f"{status}.json",
            rows=[_window_row("600000", status)],
        )
        window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=snapshot)
        result = _resolve(
            "600000",
            WINDOW_START,
            fill_model="intraday_1m_close",
            window_pool=window_pool,
            symbol_pool=_symbol_pool("600000"),
        )
        assert window_pool["snapshot_valid"] is True
        assert window_pool["passed"] is False
        assert result["decision_basis"] == "symbol_date"
        assert result["evidence"]["status"] == "local_available_unverified_pool"


def test_missing_or_corrupt_window_falls_back_to_old_evidence_bytes(tmp_path: Path) -> None:
    symbol_pool = _symbol_pool("600000")
    expected = classify_minute_replay_evidence(
        "600000",
        fill_model="intraday_1m_close",
        pool=symbol_pool,
    )
    missing_pool = window_audit.load_window_verified_minute_pool(
        snapshot_path=tmp_path / "missing.json"
    )
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_bytes(b"\xff\xfe\x00\x01")
    corrupt_pool = window_audit.load_window_verified_minute_pool(snapshot_path=corrupt_path)

    for pool in (missing_pool, corrupt_pool):
        result = _resolve(
            "600000",
            WINDOW_START,
            fill_model="intraday_1m_close",
            window_pool=pool,
            symbol_pool=symbol_pool,
        )
        assert pool["snapshot_valid"] is False
        assert result["decision_basis"] == "symbol_only_fallback"
        assert _canonical_json_bytes(result["evidence"]) == _canonical_json_bytes(expected)


def test_semantically_invalid_or_out_of_window_falls_back_byte_for_byte(tmp_path: Path) -> None:
    symbol_pool = _symbol_pool("600000")
    expected = classify_minute_replay_evidence(
        "600000",
        fill_model="intraday_1m_close",
        pool=symbol_pool,
    )
    invalid_path = _write_window_snapshot(
        tmp_path / "invalid.json",
        rows=[_window_row("600000", "usable")],
        task_type="wrong_task",
    )
    invalid_pool = window_audit.load_window_verified_minute_pool(snapshot_path=invalid_path)
    valid_path = _write_window_snapshot(
        tmp_path / "valid.json",
        rows=[_window_row("600000", "usable")],
    )
    valid_pool = window_audit.load_window_verified_minute_pool(snapshot_path=valid_path)

    for pool, trade_date in ((invalid_pool, WINDOW_START), (valid_pool, "2026-05-20")):
        result = _resolve(
            "600000",
            trade_date,
            fill_model="intraday_1m_close",
            window_pool=pool,
            symbol_pool=symbol_pool,
        )
        assert result["decision_basis"] == "symbol_only_fallback"
        assert _canonical_json_bytes(result["evidence"]) == _canonical_json_bytes(expected)


def test_non_numeric_window_counts_are_semantically_invalid_and_fail_closed(tmp_path: Path) -> None:
    symbol_pool = _symbol_pool("600000")
    expected = classify_minute_replay_evidence(
        "600000",
        fill_model="intraday_1m_close",
        pool=symbol_pool,
    )
    for field in ("total_symbols", "fully_usable_symbol_count"):
        path = _write_window_snapshot(
            tmp_path / f"bad-{field}.json",
            rows=[_window_row("600000", "usable")],
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[field] = "oops"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=path)
        except Exception as exc:  # pragma: no cover - documents the RED failure
            pytest.fail(f"malformed count must fail closed, not raise: {exc}")
        result = _resolve(
            "600000",
            WINDOW_START,
            fill_model="intraday_1m_close",
            window_pool=window_pool,
            symbol_pool=symbol_pool,
        )
        assert window_pool["snapshot_valid"] is False
        assert result["decision_basis"] == "symbol_only_fallback"
        assert _canonical_json_bytes(result["evidence"]) == _canonical_json_bytes(expected)


def test_fallback_provenance_does_not_inherit_window_source(tmp_path: Path) -> None:
    snapshot = _write_window_snapshot(
        tmp_path / "window.json",
        rows=[_window_row("600000", "usable")],
    )
    window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=snapshot)
    symbol_pool = _symbol_pool("600000")
    symbol_pool["source_snapshot"] = ""
    result = _resolve(
        "600000",
        "2026-05-20",
        fill_model="intraday_1m_close",
        window_pool=window_pool,
        symbol_pool=symbol_pool,
    )
    assert result["decision_basis"] == "symbol_only_fallback"
    assert result["evidence"]["pool_source_snapshot"] == ""
    assert result["pool_source_snapshot"] == ""


def test_window_pool_covers_requires_explicit_snapshot_valid() -> None:
    malformed_pool = {
        "passed": True,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "usable_by_symbol": {"600000": {WINDOW_START}},
    }
    assert window_audit.window_pool_covers(malformed_pool, "600000", WINDOW_START) is False


def test_window_audit_deduplicates_requested_symbols_stably(tmp_path: Path) -> None:
    calendar = tmp_path / "calendar.csv"
    calendar.write_text(f"date,is_open\n{WINDOW_START},1\n", encoding="utf-8")
    payload = window_audit.audit_minute_pool_window(
        window_start=WINDOW_START,
        window_end=WINDOW_START,
        symbols=["600000", "600000", "000001"],
        intraday_dir=tmp_path / "intraday",
        calendar_path=calendar,
        write=False,
    )
    assert payload["total_symbols"] == 2
    assert [row["symbol"] for row in payload["symbols"]] == ["600000", "000001"]


def test_valid_empty_window_still_has_symbol_date_authority_over_top200(tmp_path: Path) -> None:
    snapshot = _write_window_snapshot(tmp_path / "empty.json", rows=[])
    window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=snapshot)
    result = _resolve(
        "600000",
        WINDOW_START,
        fill_model="intraday_1m_close",
        window_pool=window_pool,
        symbol_pool=_symbol_pool("600000"),
    )

    assert window_pool["snapshot_valid"] is True
    assert window_pool["passed"] is False
    assert result["decision_basis"] == "symbol_date"
    assert result["evidence"]["status"] == "local_available_unverified_pool"
    assert result["window_start"] == WINDOW_START
    assert result["window_end"] == WINDOW_END


@pytest.mark.parametrize("fill_model", ["missing_intraday", "daily_close"])
def test_window_date_does_not_replace_old_evidence_for_non_intraday_fill_models(
    tmp_path: Path,
    fill_model: str,
) -> None:
    snapshot = _write_window_snapshot(
        tmp_path / f"{fill_model}.json",
        rows=[_window_row("600000", "usable")],
    )
    window_pool = window_audit.load_window_verified_minute_pool(snapshot_path=snapshot)
    symbol_pool = _symbol_pool("600000")
    expected = classify_minute_replay_evidence(
        "600000",
        fill_model=fill_model,
        pool=symbol_pool,
    )
    result = _resolve(
        "600000",
        WINDOW_START,
        fill_model=fill_model,
        window_pool=window_pool,
        symbol_pool=symbol_pool,
    )

    assert result["decision_basis"] == "symbol_only_fallback"
    assert _canonical_json_bytes(result["evidence"]) == _canonical_json_bytes(expected)


def _write_minute_row(path: Path, symbol: str, trade_date: str = WINDOW_START) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "symbol": symbol,
                "datetime": f"{trade_date} 09:31:00",
                "open": 10.2,
                "high": 10.2,
                "low": 10.2,
                "close": 10.2,
                "volume": 1000,
                "amount": 10200,
                # T08：合成分钟库是未复权原始报价，显式声明口径。
                "adjust": "raw",
            }
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")


def _intraday_frame(symbol: str, trade_date: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "datetime": f"{trade_date} 09:31:00",
                "open": 10.2,
                "high": 10.2,
                "low": 10.2,
                "close": 10.2,
                "volume": 1000,
                "amount": 10200,
                # T08：同上，显式声明 raw。
                "adjust": "raw",
            }
        ]
    )


def _write_top200_snapshot(root: Path, symbols: list[str], job_name: str = "top200") -> None:
    job = root / "data" / "vendor_staging" / job_name
    job.mkdir(parents=True, exist_ok=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": len(symbols),
                "target_end": WINDOW_START,
                "download": [
                    {"symbol": symbol, "status": "ok", "rows": 60000}
                    for symbol in symbols
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = root / "results" / "data_health" / "tushare_minute_quality_latest.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "eligibility_standard": "finalized_session_evidence_v3",
                "status": "passed",
                "job": {"path": f"data/vendor_staging/{job_name}"},
                "target_date": WINDOW_START,
                "total_symbols": len(symbols),
                "quality_summary": {
                    "usable": len(symbols),
                    "formal_execution_eligible": len(symbols),
                    "partial": 0,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "use_completed_minute_pool_for_research",
            }
        ),
        encoding="utf-8",
    )


def _plan(symbols: list[str], trade_date: str = WINDOW_START) -> dict:
    return {
        "plan_id": "window-consumer",
        "plan_date": "2026-07-15" if trade_date >= "2026-07-16" else "2026-05-17",
        "trade_date": trade_date,
        "symbols": [
            {
                "symbol": symbol,
                "name": symbol,
                "position_state": "new_candidate",
                "trade_date": trade_date,
                "rules": {
                    "risk_guards": [],
                    "entry_rules": [
                        {
                            "action": "buy",
                            "trigger_type": "pullback_buy",
                            "price_low": 10.0,
                            "price_high": 10.5,
                            "quantity": 100,
                            "reason": "test trigger",
                        }
                    ],
                },
            }
            for symbol in symbols
        ],
    }


def test_conditional_consumer_uses_injected_window_and_emits_independent_provenance(
    tmp_path: Path,
) -> None:
    symbols = ["600000", "000001"]
    _write_top200_snapshot(tmp_path, symbols)
    intraday_dir = tmp_path / "data" / "intraday"
    for symbol in symbols:
        _write_minute_row(intraday_dir / "1m" / f"{symbol}.csv", symbol)
    window_path = _write_window_snapshot(
        tmp_path / "results" / "data_health" / "window.json",
        rows=[_window_row("600000", "usable"), _window_row("000001", "missing")],
    )

    payload = simulate_conditional_plan_file(
        _plan(symbols),
        intraday_dir=intraday_dir,
        window_snapshot_path=window_path,
        write=False,
        include_risk_overlays=False,
    )
    events = {row["symbol"]: row for row in payload["events"]}

    assert set(events) == set(symbols)
    assert events["600000"]["intraday_quality_gate"] == "verified_completed_pool"
    assert events["000001"]["intraday_quality_gate"] == "local_available_unverified_pool"
    for event in events.values():
        assert set(event["intraday_pool_provenance"]) == PROVENANCE_KEYS
        assert event["intraday_pool_provenance"]["decision_basis"] == "symbol_date"
        assert event["intraday_pool_provenance"]["window_start"] == WINDOW_START
        assert event["intraday_pool_provenance"]["window_end"] == WINDOW_END
    assert payload["summary"]["verified_completed_intraday_count"] == 1
    assert payload["summary"]["local_unverified_intraday_count"] == 1


def test_temp_intraday_dir_does_not_inherit_formal_window_when_default_snapshot_is_unset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    formal_path = _write_window_snapshot(
        tmp_path / "formal" / "minute_pool_window_audit_latest.json",
        rows=[_window_row("600172", "usable")],
    )
    monkeypatch.setattr(window_audit, "WINDOW_AUDIT_LATEST_JSON", formal_path)
    formal_pool = window_audit.load_window_verified_minute_pool()
    assert formal_pool["snapshot_valid"] is True
    trade_date = formal_pool["window_start"]
    assert trade_date

    symbol = "600172"
    _write_top200_snapshot(tmp_path, [symbol])
    intraday_dir = tmp_path / "data" / "intraday"
    _write_minute_row(intraday_dir / "1m" / f"{symbol}.csv", symbol, trade_date)
    local_window = tmp_path / "results" / "data_health" / "minute_pool_window_audit_latest.json"
    assert not local_window.exists()

    payload = simulate_conditional_plan_file(
        _plan([symbol], trade_date=trade_date),
        intraday_dir=intraday_dir,
        write=False,
        include_risk_overlays=False,
    )
    event = payload["events"][0]
    assert event["intraday_pool_provenance"]["decision_basis"] == "symbol_only_fallback"


def test_formal_intraday_dir_default_hits_formal_window_snapshot(monkeypatch, tmp_path: Path) -> None:
    trade_date = WINDOW_START
    symbol = "600172"
    monkeypatch.setattr(
        position_engine,
        "_read_intraday",
        lambda _symbol, intraday_dir, source="1m": _intraday_frame(symbol, trade_date),
    )
    monkeypatch.setattr(
        position_engine,
        "load_pool_for_intraday_dir",
        lambda _intraday_dir: _symbol_pool(symbol),
    )
    formal_path = _write_window_snapshot(
        tmp_path / "formal_window_snapshot.json",
        rows=[_window_row(symbol, "usable")],
    )
    monkeypatch.setattr(window_audit, "WINDOW_AUDIT_LATEST_JSON", formal_path)
    payload = simulate_conditional_plan_file(
        _plan([symbol], trade_date=trade_date),
        intraday_dir=position_engine.INTRADAY_DIR,
        write=False,
        include_risk_overlays=False,
    )
    assert payload["events"][0]["intraday_pool_provenance"]["decision_basis"] == "symbol_date"


def test_temp_project_root_snapshot_is_used_by_default(monkeypatch, tmp_path: Path) -> None:
    symbol = "600172"
    trade_date = WINDOW_START
    local_path = _write_window_snapshot(
        tmp_path / "results" / "data_health" / "minute_pool_window_audit_latest.json",
        rows=[_window_row(symbol, "usable")],
    )
    monkeypatch.setattr(
        position_engine,
        "_read_intraday",
        lambda _symbol, intraday_dir, source="1m": _intraday_frame(symbol, trade_date),
    )
    monkeypatch.setattr(
        position_engine,
        "load_pool_for_intraday_dir",
        lambda _intraday_dir: _symbol_pool(symbol),
    )
    payload = simulate_conditional_plan_file(
        _plan([symbol], trade_date=trade_date),
        intraday_dir=tmp_path / "data" / "intraday",
        write=False,
        include_risk_overlays=False,
    )
    assert local_path.exists()
    assert payload["events"][0]["intraday_pool_provenance"]["decision_basis"] == "symbol_date"


def test_one_minute_dir_is_normalized_for_local_top200_fallback(tmp_path: Path) -> None:
    symbol = "999999"
    one_minute_dir = tmp_path / "data" / "intraday" / "1m"
    _write_top200_snapshot(tmp_path, [symbol], job_name="local_only_pool")
    _write_minute_row(one_minute_dir / f"{symbol}.csv", symbol, WINDOW_START)

    payload = simulate_conditional_plan_file(
        _plan([symbol]),
        intraday_dir=one_minute_dir,
        write=False,
        include_risk_overlays=False,
    )
    event = payload["events"][0]
    assert event["intraday_pool_provenance"]["decision_basis"] == "symbol_only_fallback"
    assert event["intraday_evidence"]["pool_status"] == "passed"
    assert "local_only_pool" in event["intraday_evidence"]["pool_job_path"]
