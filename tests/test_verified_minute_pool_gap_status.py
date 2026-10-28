from __future__ import annotations

import json
from pathlib import Path


def _write_minute_csv(path: Path, *, symbol: str, date: str = "2026-07-03") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "symbol,datetime,date,time,open,high,low,close,volume,amount,source,updated_at",
                f"{symbol},{date} 09:30:00,{date},09:30:00,10,10.1,9.9,10,100,1000,tushare_proxy_stk_mins,2026-07-05T00:00:00",
                f"{symbol},{date} 09:31:00,{date},09:31:00,10.1,10.2,10,10.1,100,1010,tushare_proxy_stk_mins,2026-07-05T00:00:00",
            ]
        )
        + "\n",
        encoding="utf-8-sig",
    )


def test_intraday_gap_status_marks_available_symbols_from_passed_minute_quality_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk.intraday import intraday_gap_checker as gap

    monkeypatch.setattr(gap, "PROJECT_ROOT", tmp_path)
    plan_path = tmp_path / "results" / "conditional_trade_plans_latest.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "plan_20260703",
                "trade_date": "2026-07-03",
                "symbols": [
                    {"symbol": "600000", "name": "verified"},
                    {"symbol": "000001", "name": "missing"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job = tmp_path / "data" / "vendor_staging" / "tushare_proxy_minutes_250d_phase2_top200_x"
    job.mkdir(parents=True)
    (job / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "target_end": "20260703",
                "download": [{"symbol": "600000", "status": "ok", "rows": 50000}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    quality = tmp_path / "results" / "data_health" / "tushare_minute_quality_latest.json"
    quality.parent.mkdir(parents=True)
    quality.write_text(
        json.dumps(
            {
                "task_type": "tushare_minute_quality_audit",
                "eligibility_standard": "finalized_session_evidence_v3",
                "status": "passed",
                "job": {"path": "data/vendor_staging/tushare_proxy_minutes_250d_phase2_top200_x"},
                "target_date": "2026-07-03",
                "total_symbols": 1,
                "quality_summary": {
                    "usable": 1,
                    "formal_execution_eligible": 1,
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
    intraday_dir = tmp_path / "data" / "intraday" / "1m"
    _write_minute_csv(intraday_dir / "600000.csv", symbol="600000")

    payload = gap.intraday_gap_status(
        target_trade_date="2026-07-03",
        source="plans",
        intraday_dir=intraday_dir,
        plan_path=plan_path,
    )

    by_symbol = {row["symbol"]: row for row in payload["items"]}
    assert payload["available_count"] == 1
    assert payload["missing_count"] == 1
    assert payload["verified_minute_pool"]["status"] == "passed"
    assert payload["verified_minute_pool"]["usable_count"] == 1
    assert payload["verified_minute_pool_available_count"] == 1
    assert payload["verified_minute_pool_missing_count"] == 1
    assert by_symbol["600000"]["status"] == "available"
    assert by_symbol["600000"]["minute_pool_status"] == "verified_minute_pool"
    assert by_symbol["600000"]["minute_pool_quality_status"] == "passed"
    assert by_symbol["000001"]["status"] == "missing_intraday"
    assert by_symbol["000001"]["minute_pool_status"] == "missing_from_verified_minute_pool"
