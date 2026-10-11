from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quantdesk.paper.position_management_engine import simulate_conditional_plan_file
from quantdesk.intraday.minute_pool_capability import load_completed_minute_pool_capability


def _write_intraday(path: Path, symbol: str, prices: list[float], date: str = "2026-05-18") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, price in enumerate(prices):
        rows.append(
            {
                "symbol": symbol,
                "datetime": f"{date} 09:{31 + index:02d}:00",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000,
                "amount": price * 1000,
                # T08：合成分钟库是未复权原始报价，显式声明口径。
                "adjust": "raw",
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _plan_item(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "position_state": "new_candidate",
        "trade_date": "2026-05-18",
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


def _write_passed_top200_quality_snapshot(root: Path) -> None:
    job = root / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    job.mkdir(parents=True, exist_ok=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 1,
                "target_end": "20260703",
                "download": [{"symbol": "600000", "status": "ok", "rows": 60000}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot = root / "results" / "data_health" / "tushare_minute_quality_latest.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "passed",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_20260704_163110"},
                "target_date": "2026-07-03",
                "total_symbols": 1,
                "quality_summary": {
                    "usable": 1,
                    "partial": 0,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "use_completed_minute_pool_for_research",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_partial_top200_quality_snapshot(root: Path) -> None:
    job = root / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    job.mkdir(parents=True, exist_ok=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 2,
                "target_end": "20260703",
                "download": [
                    {"symbol": "600000", "status": "ok", "rows": 60000},
                    {"symbol": "000001", "status": "partial", "rows": 100},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snapshot = root / "results" / "data_health" / "tushare_minute_quality_latest.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "partial",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_20260704_163110"},
                "target_date": "2026-07-03",
                "total_symbols": 2,
                "quality_summary": {
                    "usable": 1,
                    "partial": 1,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "review_minute_quality_warnings",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_last_passed_top200_with_partial_latest(root: Path) -> None:
    top200_job = root / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_20260704_163110"
    top200_job.mkdir(parents=True, exist_ok=True)
    (top200_job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 1,
                "target_end": "20260703",
                "download": [{"symbol": "600000", "status": "ok", "rows": 60000}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    latest_job = root / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_project_real_2656_20260706"
    latest_job.mkdir(parents=True, exist_ok=True)
    (latest_job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "symbol_count": 2,
                "target_end": "20260703",
                "download": [
                    {"symbol": "600000", "status": "ok", "rows": 60000},
                    {"symbol": "000001", "status": "partial", "rows": 100},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    latest = root / "results" / "data_health" / "tushare_minute_quality_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "partial",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_project_real_2656_20260706"},
                "target_date": "2026-07-03",
                "total_symbols": 2,
                "quality_summary": {
                    "usable": 1,
                    "partial": 1,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "review_minute_quality_warnings",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    last_passed = root / "results" / "data_health" / "tushare_minute_quality_last_passed.json"
    last_passed.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "status": "passed",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_20260704_163110"},
                "target_date": "2026-07-03",
                "total_symbols": 1,
                "quality_summary": {
                    "usable": 1,
                    "partial": 0,
                    "insufficient": 0,
                    "invalid": 0,
                    "missing": 0,
                },
                "suggested_next_action": "use_completed_minute_pool_for_research",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_conditional_execution_uses_verified_top200_minute_pool_without_waiting_for_large_pool(tmp_path: Path) -> None:
    _write_passed_top200_quality_snapshot(tmp_path)
    intraday_dir = tmp_path / "data" / "intraday"
    _write_intraday(intraday_dir / "1m" / "600000.csv", "600000", [10.2])
    _write_intraday(intraday_dir / "1m" / "000001.csv", "000001", [10.2])
    plan = {
        "plan_id": "minute-pool-capability",
        "plan_date": "2026-05-17",
        "trade_date": "2026-05-18",
        "symbols": [_plan_item("600000"), _plan_item("000001"), _plan_item("300001")],
    }

    payload = simulate_conditional_plan_file(
        plan,
        intraday_dir=intraday_dir,
        write=False,
        include_risk_overlays=False,
    )

    event_by_symbol = {row["symbol"]: row for row in payload["events"]}
    no_fill_by_symbol = {row["symbol"]: row for row in payload["no_fill"]}
    assert event_by_symbol["600000"]["intraday_quality_gate"] == "verified_completed_pool"
    assert event_by_symbol["600000"]["intraday_evidence"]["verified_completed_pool"] is True
    assert event_by_symbol["000001"]["intraday_quality_gate"] == "local_available_unverified_pool"
    assert no_fill_by_symbol["300001"]["intraday_quality_gate"] == "missing_intraday"
    assert payload["summary"]["available_intraday_count"] == 2
    assert payload["summary"]["verified_completed_intraday_count"] == 1
    assert payload["summary"]["local_unverified_intraday_count"] == 1
    assert payload["summary"]["missing_intraday_count"] == 1
    assert payload["summary"]["intraday_quality_gate"]["pool_passed"] is True
    assert payload["summary"]["intraday_quality_gate"]["suggested_next_action"] == "use_verified_completed_pool_now"


def test_conditional_execution_surfaces_last_passed_minute_pool_provenance(tmp_path: Path) -> None:
    _write_last_passed_top200_with_partial_latest(tmp_path)
    intraday_dir = tmp_path / "data" / "intraday"
    _write_intraday(intraday_dir / "1m" / "600000.csv", "600000", [10.2])
    plan = {
        "plan_id": "minute-pool-last-passed-provenance",
        "plan_date": "2026-05-17",
        "trade_date": "2026-05-18",
        "symbols": [_plan_item("600000")],
    }

    payload = simulate_conditional_plan_file(
        plan,
        intraday_dir=intraday_dir,
        write=False,
        include_risk_overlays=False,
    )

    event = payload["events"][0]
    gate = payload["summary"]["intraday_quality_gate"]
    assert event["intraday_quality_gate"] == "verified_completed_pool"
    assert event["intraday_evidence"]["pool_source_snapshot"] == "last_passed"
    assert event["intraday_evidence"]["pool_latest_quality_status"] == "partial"
    assert gate["pool_source_snapshot"] == "last_passed"
    assert gate["pool_latest_quality_status"] == "partial"
    assert "verified_pool_from_last_passed_snapshot" in gate["warnings"]


def test_partial_minute_quality_snapshot_does_not_mark_verified_pool(tmp_path: Path) -> None:
    _write_partial_top200_quality_snapshot(tmp_path)

    pool = load_completed_minute_pool_capability(project_root=tmp_path)

    assert pool["status"] == "warning"
    assert pool["passed"] is False
    assert pool["symbols"] == set()
    assert pool["partial_count"] == 1
