"""Keep the existing output guard and label the verified paper valuation."""

from __future__ import annotations

import json

import pytest

from scripts.self_check import check_no_dangerous_frontend_text
from tests.test_project_status_ui import _run_status_ui


def test_existing_output_guard_accepts_the_scoped_final_source() -> None:
    result = check_no_dangerous_frontend_text()
    assert result.status == "PASS", result.detail


@pytest.mark.parametrize("status", ["verified", "unknown"])
def test_paper_valuation_label_and_evidence_status_remain_visible(status: str) -> None:
    payload = {"ok": True, "account": {
        "total_equity": 98000, "total_return_pct": -2, "position_count": 1,
        "valuation_date": "2026-10-14", "daily_pnl": -125.5, "daily_pnl_status": status,
    }, "return_series": {"items": []}}
    script = "document.getElementById('dutyStatusStrip').querySelectorAll=()=>[];" + (
        "renderHomeEquity(" + json.dumps(payload) + ");"
        "console.log(JSON.stringify({sub:nodeText('kpiPaperSub')}));"
    )
    result = _run_status_ui("vm.runInContext(" + json.dumps(script) + ",context);")
    assert "估值截至 2026-10-14" in result["sub"]
    assert "累计 -2" in result["sub"]
    if status == "verified":
        assert "该日纸面盈亏 ¥-125.5" in result["sub"]
    else:
        assert "盈亏" not in result["sub"]
