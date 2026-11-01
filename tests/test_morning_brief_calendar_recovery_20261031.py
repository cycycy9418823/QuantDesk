from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk import morning_brief as brief
from quantdesk.trading_calendar import clear_calendar_cache


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 30, 8, 35, tzinfo=SHANGHAI)
ROWS = [
    "2026-10-28,1,fixture",
    "2026-10-29,1,fixture",
    "2026-10-30,1,fixture",
    "2026-10-31,0,fixture",
    "2026-11-01,0,fixture",
    "2026-11-02,1,fixture",
]


def _calendar(root: Path, rows: list[str]) -> None:
    path = root / "data/calendar/a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,is_open,source\n" + "\n".join(rows) + "\n", encoding="utf-8")
    clear_calendar_cache()


@pytest.mark.parametrize("rows,observed", [
    ([row for row in ROWS if not row.startswith("2026-10-29")], "2026-10-28"),
    ([*ROWS, "2026-10-29,0,fixture"], "2026-10-28"),
    ([row.replace("2026-10-29,1", "2026-10-29,unknown") for row in ROWS], "2026-10-28"),
    ([row.replace("2026-10-29,1,fixture", "2026-10-29,1,provisional") for row in ROWS], "2026-10-29"),
    ([*ROWS, "2026-10-30,1,fixture"], "2026-10-29"),
])
def test_uncertain_consulted_calendar_cannot_validate_old_brief(tmp_path, monkeypatch, rows, observed):
    _calendar(tmp_path, rows)
    monkeypatch.setattr(brief, "build_morning_brief", lambda *a, **k: {
        "status": "warning", "as_of": observed, "run_id": "fixture-run",
        "reason_codes": [], "data_health": {}, "paper_account": {},
    })
    called = []
    result = brief.run_morning_brief(
        tmp_path, now=NOW, network=True, push=True, dry_run=False,
        _sendkey_loader=lambda: called.append("secret"),
        sender=lambda **kwargs: called.append("send"),
    )
    assert result["status"] == "blocked"
    assert result["date_status"] == "unverified"
    assert result["expected_as_of"] == ""
    assert result["external_sent_count"] == 0
    assert called == []


def test_weekend_and_unrelated_provisional_rows_do_not_break_prior_session(tmp_path):
    _calendar(tmp_path, [*ROWS, "2027-01-04,1,provisional"])
    result = brief._brief_business_date(
        tmp_path, datetime(2026, 11, 2, 8, 35, tzinfo=SHANGHAI), "2026-10-30",
    )
    assert result["expected_as_of"] == "2026-10-30"
    assert result["date_status"] == "current"


@pytest.mark.parametrize("observed", ["2026-02-30", "2026-W44-4", "2026-10-29 ", ""])
def test_invalid_business_date_is_unverified(tmp_path, observed):
    _calendar(tmp_path, ROWS)
    result = brief._brief_business_date(tmp_path, NOW, observed)
    assert result["date_status"] == "unverified"
    assert result["date_reason_code"] == "brief_business_date_missing"


def test_builder_and_visible_report_mark_stale_date_without_relabelling(tmp_path):
    _calendar(tmp_path, ROWS)
    payload = brief.build_morning_brief(
        tmp_path, now=NOW, snapshot_loader=lambda _root: {
            "status": "passed", "source_mode": "archived_terminal_run",
            "chain": {"run_id": "fixture-old-run", "target_date": "2026-10-28", "status": "completed"},
        },
    )
    assert payload["as_of"] == "2026-10-28"
    assert payload["expected_as_of"] == "2026-10-29"
    assert payload["date_status"] == "stale"
    text = brief.render_morning_brief_markdown(payload)
    assert "2026-10-28" in text and "2026-10-29" in text
    assert "旧日资料" in text


def test_no_calendar_cannot_validate_business_date(tmp_path):
    result = brief._brief_business_date(tmp_path, NOW, "2026-10-29")
    assert result["date_status"] == "unverified" and result["expected_as_of"] == ""


def test_naive_preview_clock_is_treated_as_shanghai(tmp_path):
    _calendar(tmp_path, ROWS)
    result = brief._brief_business_date(tmp_path, NOW.replace(tzinfo=None), "2026-10-29")
    assert result["date_status"] == "current"


def test_missing_brief_identity_cannot_have_passed_date_status(tmp_path):
    _calendar(tmp_path, ROWS)
    payload = brief.build_morning_brief(tmp_path, now=NOW, snapshot_loader=lambda root: {"status": "passed"})
    assert payload["status"] != "passed"
    assert payload["date_status"] == "unverified"
