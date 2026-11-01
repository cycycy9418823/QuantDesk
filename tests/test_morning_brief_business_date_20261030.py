from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import quantdesk.morning_brief as brief

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _calendar(root: Path) -> None:
    path = root / "data/calendar/a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,is_open,source\n2026-10-26,1,fixture\n2026-10-27,1,fixture\n2026-10-28,1,fixture\n2026-10-29,1,fixture\n2026-10-30,1,fixture\n2026-10-31,0,fixture\n2026-11-01,0,fixture\n2026-11-02,1,fixture\n", encoding="utf-8")
    from quantdesk.trading_calendar import clear_calendar_cache
    clear_calendar_cache()


def _run(root: Path, monkeypatch, *, as_of="2026-10-28", dry_run=False, allow=False):
    _calendar(root)
    monkeypatch.setattr(brief, "build_morning_brief", lambda *a, **k: {
        "status": "warning", "as_of": as_of, "run_id": "old-recorded-run",
        "reason_codes": [], "data_health": {}, "paper_account": {},
    })
    calls = []
    result = brief.run_morning_brief(root, network=True, push=True, dry_run=dry_run,
        allow_outside_window=allow, now=datetime(2026, 10, 30, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "fixture-secret",
        sender=lambda **k: calls.append("sender") or {"status":"sent", "reason_code":"fixture"})
    return result, calls


def test_stale_previous_day_snapshot_cannot_be_delivered(tmp_path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch)
    assert result["status"] == "blocked"
    assert result["reason_code"] == "brief_business_date_stale"
    assert result["expected_as_of"] == "2026-10-29"
    assert calls == []
    assert result["external_sent_count"] == 0


def test_latest_complete_date_can_be_delivered(tmp_path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, as_of="2026-10-29")
    assert result["status"] == "sent"
    assert calls == ["secret", "sender"]
    assert result["expected_as_of"] == "2026-10-29"


def test_manual_dry_run_retains_old_evidence_but_marks_stale(tmp_path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, dry_run=True)
    assert result["status"] == "preview_generated"
    assert result["date_status"] == "stale"
    assert result["expected_as_of"] == "2026-10-29"
    assert calls == []
    payload = __import__("json").loads((tmp_path / "results/morning_brief/morning_brief_latest.json").read_text(encoding="utf-8"))
    assert payload["as_of"] == "2026-10-28"
    assert "brief_business_date_stale" in payload["reason_codes"]


def test_time_window_override_does_not_override_business_date(tmp_path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, allow=True)
    assert result["reason_code"] == "brief_business_date_stale"
    assert calls == []


def test_future_date_cannot_be_delivered(tmp_path, monkeypatch):
    result, calls = _run(tmp_path, monkeypatch, as_of="2026-10-30")
    assert result["reason_code"] == "brief_business_date_future"
    assert calls == []
