"""A slow render or secret read must not send using the task-start clock."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quantdesk import morning_brief as brief

TZ = ZoneInfo("Asia/Shanghai")


def fixture_run(tmp_path, monkeypatch, *, start="2026-11-06T08:35:00", after_build=None, after_key=None, existing=None):
    class Clock(datetime):
        current = datetime.fromisoformat(start).replace(tzinfo=TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz or TZ)

    monkeypatch.setattr(brief, "datetime", Clock)
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n2026-11-05,1,fixture\n2026-11-06,1,fixture\n2026-11-07,0,fixture\n", encoding="utf-8")
    from quantdesk.trading_calendar import clear_calendar_cache
    clear_calendar_cache()

    def build(*args, **kwargs):
        if after_build:
            Clock.current = datetime.fromisoformat(after_build).replace(tzinfo=TZ)
        return {"status": "warning", "as_of": "2026-11-05", "run_id": "fixture",
                "generated_at": start + "+08:00", "reason_codes": [], "data_health": {}, "paper_account": {}}

    monkeypatch.setattr(brief, "build_morning_brief", build)
    monkeypatch.setattr(brief, "_weekly_exit_replay", lambda *a: {"status": "skipped"})
    monkeypatch.setattr(brief, "_owner_brief", lambda *a: {"title": "纸面早报", "body": "纸面研究，未验证"})
    calls = []

    def key():
        calls.append("key")
        if after_key:
            Clock.current = datetime.fromisoformat(after_key).replace(tzinfo=TZ)
        return "synthetic-only"

    def sender(**kwargs):
        calls.append({"at": Clock.current.isoformat(), "recorded_at": kwargs["now"].isoformat()})
        return {"status": "sent", "reason_code": "provider_accepted", "http_status": 200, "provider_code": 0}

    if existing:
        import json
        path = tmp_path / "results/notifications/serverchan_morning_brief_20261106T083000_fixture.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(existing), encoding="utf-8")
    result = brief.run_morning_brief(tmp_path, network=True, push=True, dry_run=False, _sendkey_loader=key, sender=sender)
    return result, calls, Clock, sender, key


def test_render_crosses_window_without_loading_secret(tmp_path, monkeypatch):
    result, calls, *_ = fixture_run(tmp_path, monkeypatch, start="2026-11-06T08:49:50", after_build="2026-11-06T08:50:03")
    assert result["reason_code"] == "outside_delivery_window"
    assert result["external_sent_count"] == 0 and calls == []
    assert (tmp_path / "reports/morning_brief/morning_brief_latest.md").exists()


def test_secret_read_crosses_window_without_sending(tmp_path, monkeypatch):
    result, calls, *_ = fixture_run(tmp_path, monkeypatch, start="2026-11-06T08:49:50", after_key="2026-11-06T08:50:03")
    assert result["reason_code"] == "outside_delivery_window" and calls == ["key"]
    assert not list((tmp_path / "results/notifications").glob("*.json"))


@pytest.mark.parametrize("after,reason", [("2026-11-07T08:35:00", "send_clock_day_changed"),
                                        ("2026-11-06T08:34:59", "send_clock_moved_backwards")])
def test_clock_discontinuity_is_visible(tmp_path, monkeypatch, after, reason):
    result, calls, *_ = fixture_run(tmp_path, monkeypatch, after_build=after)
    assert result["reason_code"] == reason and calls == []


def test_late_start_keeps_original_window(tmp_path, monkeypatch):
    result, calls, *_ = fixture_run(tmp_path, monkeypatch, start="2026-11-06T09:19:00")
    assert result["reason_code"] == "outside_delivery_window" and calls == []


def test_actual_send_clock_and_duplicate_protection(tmp_path, monkeypatch):
    result, calls, clock, sender, key = fixture_run(tmp_path, monkeypatch, after_build="2026-11-06T08:37:00")
    assert result["status"] == "sent"
    assert calls[-1] == {"at": "2026-11-06T08:37:00+08:00", "recorded_at": "2026-11-06T08:37:00+08:00"}
    before = list(calls)
    again = brief.run_morning_brief(tmp_path, network=True, push=True, dry_run=False, _sendkey_loader=key, sender=sender)
    assert again["status"] == "skipped_duplicate" and calls == before


def test_ambiguous_prior_attempt_never_retries(tmp_path, monkeypatch):
    result, calls, *_ = fixture_run(tmp_path, monkeypatch, existing={
        "observed_at": "2026-11-06T08:33:00+08:00", "status": "ambiguous", "summary_sha256": "other-content"})
    assert result["reason_code"] == "uncertain_prior_attempt" and calls == []
