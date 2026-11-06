"""Saved content and delivery evidence must agree before claiming receipt."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk import morning_brief as brief
from quantdesk.morning_brief_view import build_morning_brief_view

NOW = datetime(2026, 11, 6, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def seed(root: Path, *, generated="2026-11-06T08:35:00+08:00"):
    payload = {"status": "warning", "as_of": "2026-11-05", "expected_as_of": "2026-11-05",
               "date_status": "current", "run_id": "fixture", "generated_at": generated,
               "reason_codes": [], "data_health": {}, "paper_account": {},
               "owner_brief": {"title": "原来的早报", "body": "昨天现金1519元；<img src=x onerror=bad>"}}
    write(root / "results/morning_brief/morning_brief_latest.json", payload)
    technical = brief.render_morning_brief_markdown(payload)
    digest = hashlib.sha256((payload["owner_brief"]["title"] + "\n" + payload["owner_brief"]["body"] + "\n" + technical.rstrip()).encode()).hexdigest()
    return payload, digest


def receipt(root: Path, digest: str, **overrides):
    row = {"status": "sent", "delivery_day": "2026-11-06", "observed_at": "2026-11-06T08:37:00+08:00",
           "summary_sha256": digest, "http_status": 200, "provider_code": 0, "reason_code": "provider_accepted"}
    row.update(overrides)
    write(root / "results/notifications/serverchan_morning_brief_20261106T083700_fixture.json", row)


def task_result(root: Path, digest: str, *, status="blocked", reason="outside_delivery_window", day="2026-11-06"):
    rows = [{"task": "QuantDesk Morning Brief", "status": "started", "date": day, "at": day+"T12:52:00+08:00"},
            {"task_type": "morning_brief_delivery", "as_of": "2026-11-05", "run_id": "fixture", "content_sha256": digest,
             "status": status, "reason_code": reason}]
    path = root / "logs/ops/scheduled_tasks/quantdesk-morning-brief-fixture.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_missing_preview_does_not_create_any_files(tmp_path):
    before = list(tmp_path.rglob("*"))
    result = build_morning_brief_view(tmp_path, now=NOW)
    assert result["available"] is False and result["read_only"] is True
    assert list(tmp_path.rglob("*")) == before


def test_matching_provider_receipt_preserves_original_body_and_inputs(tmp_path, monkeypatch):
    payload, digest = seed(tmp_path)
    receipt(tmp_path, digest)
    monkeypatch.setattr(brief, "run_morning_brief", lambda *a, **k: pytest.fail("reader called writer"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = build_morning_brief_view(tmp_path, now=NOW)
    assert result["body"] == payload["owner_brief"]["body"] and result["as_of"] == "2026-11-05"
    assert result["delivery"]["status"] == "provider_accepted"
    assert result["delivery"]["user_received"] is None
    assert {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("overrides", [{"summary_sha256":"wrong"}, {"provider_code":1}, {"provider_code":False},
                                       {"http_status":None}, {"status":"ambiguous"}, {"delivery_day":"2026-11-05"}])
def test_unproven_receipt_cannot_become_success(tmp_path, overrides):
    _, digest = seed(tmp_path)
    receipt(tmp_path, digest, **overrides)
    assert build_morning_brief_view(tmp_path, now=NOW)["delivery"]["status"] == "unknown"


def test_known_late_start_without_send_receipt_is_not_sent(tmp_path):
    _, digest = seed(tmp_path, generated="2026-11-06T12:52:00+08:00")
    task_result(tmp_path,digest)
    result = build_morning_brief_view(tmp_path, now=NOW)
    assert result["delivery"]["status"] == "not_sent"
    assert "超过发送时段" in result["delivery"]["label"]


@pytest.mark.parametrize("kind", ["wrong_day", "wrong_content", "claimed_sent", "ambiguous", "broken_receipt"])
def test_logs_do_not_turn_missing_receipt_into_proof(tmp_path, kind):
    _, digest = seed(tmp_path)
    task_result(tmp_path, "other" if kind=="wrong_content" else digest,
                status="sent" if kind=="claimed_sent" else "ambiguous" if kind=="ambiguous" else "blocked",
                reason="ambiguous_delivery" if kind=="ambiguous" else "outside_delivery_window",
                day="2026-11-05" if kind=="wrong_day" else "2026-11-06")
    if kind=="broken_receipt":
        path=tmp_path/"results/notifications/serverchan_morning_brief_20261106_bad.json"
        path.parent.mkdir(parents=True);path.write_text("{",encoding="utf-8")
    assert build_morning_brief_view(tmp_path,now=NOW)["delivery"]["status"]=="unknown"


def test_old_generation_is_not_today_even_when_date_status_was_current(tmp_path):
    seed(tmp_path,generated="2026-11-05T08:35:00+08:00")
    result=build_morning_brief_view(tmp_path,now=NOW)
    assert result["date_status_at_generation"]=="current" and result["is_today"] is False


def test_input_changed_during_read_is_not_reported_as_success(tmp_path,monkeypatch):
    _,digest=seed(tmp_path);receipt(tmp_path,digest)
    original=Path.read_bytes
    counts={}
    def changing(path):
        raw=original(path)
        if path.name=="morning_brief_latest.json":
            counts[path]=counts.get(path,0)+1
            if counts[path]>1:return raw+b" "
        return raw
    monkeypatch.setattr(Path,"read_bytes",changing)
    result=build_morning_brief_view(tmp_path,now=NOW)
    assert not result["available"] and "变化" in result["message"]


def test_get_route_is_read_only_and_post_is_unavailable(tmp_path):
    from flask import Flask
    from scripts.server.routes.product_review_routes import register_product_review_routes
    seed(tmp_path)
    app=Flask(__name__)
    register_product_review_routes(app,root=lambda:tmp_path,capabilities=lambda:{})
    with app.test_client() as client:
        response=client.get("/api/morning-brief")
        assert response.status_code==200 and response.json["read_only"] is True
        assert client.post("/api/morning-brief").status_code==405


def test_preview_path_escape_not_read(tmp_path,monkeypatch):
    seed(tmp_path)
    original=Path.resolve
    def escaped(path,*a,**kw):
        if path.name=="morning_brief_latest.json":return tmp_path.parent/"outside.json"
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,"resolve",escaped)
    result=build_morning_brief_view(tmp_path,now=NOW)
    assert not result["available"] and result["source_errors"][0]["reason"]=="path_escape"


def test_real_writer_fixture_receipt_matches_reader_without_rebuilding(tmp_path,monkeypatch):
    from tests.test_morning_brief_send_clock_20261106 import fixture_run
    outcome, calls, *_ = fixture_run(tmp_path,monkeypatch,after_build="2026-11-06T08:37:00")
    assert outcome["status"]=="sent" and len(calls)==2
    monkeypatch.setattr(brief,"build_morning_brief",lambda *a,**kw:pytest.fail("reader regenerated brief"))
    view=build_morning_brief_view(tmp_path,now=NOW)
    assert view["delivery"]["status"]=="provider_accepted"
    assert view["content_sha256"]==outcome["content_sha256"]
