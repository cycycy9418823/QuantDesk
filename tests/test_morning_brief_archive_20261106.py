"""Dated local briefs survive rollover even when sending cannot happen."""
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest
from quantdesk import morning_brief as brief
from quantdesk.morning_brief_view import build_morning_brief_view


def run(root, monkeypatch, stamp, *, dry_run=False):
    clock = datetime.fromisoformat(stamp)
    monkeypatch.setattr(brief, 'build_morning_brief', lambda *a, **k: {
        'status': 'warning', 'as_of': '2026-11-05', 'run_id': 'fixture', 'reason_codes': [],
        'data_health': {}, 'paper_account': {}})
    monkeypatch.setattr(brief, '_weekly_exit_replay', lambda *a: {})
    monkeypatch.setattr(brief, '_owner_brief', lambda *a: {'title': '本地日报', 'body': '当时的原文'})
    monkeypatch.setattr(brief, '_brief_business_date', lambda *a: {
        'expected_as_of': '2026-11-05', 'date_status': 'current', 'date_reason_code': ''})
    return brief.run_morning_brief(root, now=clock, network=False, push=False, dry_run=dry_run,
                                   sender=lambda **k: pytest.fail('must not send'))


def test_daily_archive_survives_next_day_without_network(tmp_path, monkeypatch):
    first = run(tmp_path, monkeypatch, '2026-11-06T12:52:42+08:00')
    md = tmp_path / 'reports/morning_brief/2026-11-06.md'
    original = md.read_bytes()
    assert first['reason_code'] == 'network_not_authorized'
    assert '晚生成' in original.decode() and '资料日期：2026-11-05' in original.decode()
    run(tmp_path, monkeypatch, '2026-11-09T08:35:00+08:00')
    assert md.read_bytes() == original
    view = build_morning_brief_view(tmp_path, requested_date='2026-11-06',
                                   now=datetime.fromisoformat('2026-11-09T10:00:00+08:00'))
    assert view['available'] and not view['is_today'] and view['late_generated']
    assert view['archive_dates'] == ['2026-11-09', '2026-11-06']
    assert view['body'] == '当时的原文'
    assert '晚生成' not in (tmp_path / 'reports/morning_brief/2026-11-09.md').read_text(encoding='utf-8')


def test_dry_run_is_not_a_formal_daily_archive(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, '2026-11-06T08:35:00+08:00', dry_run=True)
    assert not (tmp_path / 'reports/morning_brief/2026-11-06.md').exists()


def test_archived_receipt_remains_valid_after_renderer_changes(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch, '2026-11-06T08:35:00+08:00')
    path = tmp_path / 'results/notifications/serverchan_morning_brief_20261106T083600_fixture.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'status': 'sent', 'delivery_day': '2026-11-06',
        'observed_at': '2026-11-06T08:36:00+08:00', 'summary_sha256': result['content_sha256'],
        'http_status': 200, 'provider_code': 0}), encoding='utf-8')
    monkeypatch.setattr(brief, 'render_morning_brief_markdown', lambda *a: pytest.fail('must read frozen text'))
    view = build_morning_brief_view(tmp_path, requested_date='2026-11-06')
    assert view['available'] and view['delivery']['status'] == 'provider_accepted'
    assert view['delivery']['user_received'] is None


@pytest.mark.parametrize('damage', ['markdown', 'json_date', 'content_hash'])
def test_inconsistent_archive_is_not_presented_as_valid(tmp_path, monkeypatch, damage):
    run(tmp_path, monkeypatch, '2026-11-06T08:35:00+08:00')
    if damage == 'markdown':
        (tmp_path / 'reports/morning_brief/2026-11-06.md').write_text('tampered', encoding='utf-8')
    else:
        p = tmp_path / 'results/morning_brief/2026-11-06.json'
        value = json.loads(p.read_text(encoding='utf-8'))
        if damage == 'json_date':
            value['generated_at'] = '2026-11-05T08:35:00+08:00'
        else:
            value['saved_content']['body'] = 'changed'
        p.write_text(json.dumps(value), encoding='utf-8')
    assert not build_morning_brief_view(tmp_path, requested_date='2026-11-06')['available']


@pytest.mark.parametrize('value', ['../state/ledger', '2026-02-30', '2026-1-2', '/tmp/secret'])
def test_bad_date_never_opens_arbitrary_path(tmp_path, value):
    assert not build_morning_brief_view(tmp_path, requested_date=value)['ok']


def test_old_latest_payload_keeps_original_hash_rendering(tmp_path, monkeypatch):
    # Old on-disk briefs lack presentation_version and saved_content. Their
    # digest must remain identical to the installed renderer used when sent.
    import importlib.util
    p = Path(__file__).parents[1] / 'tests/test_morning_brief_view_20261106.py'
    spec = importlib.util.spec_from_file_location('view_fixture', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, digest = module.seed(tmp_path)
    module.receipt(tmp_path, digest)
    view = build_morning_brief_view(tmp_path)
    assert view['delivery']['status'] == 'provider_accepted'


def test_local_delivery_success_does_not_hide_data_or_lock_failure(tmp_path, monkeypatch):
    import importlib.util
    p = Path(__file__).parents[1] / 'scripts/run_morning_brief.py'
    spec = importlib.util.spec_from_file_location('brief_runner', p)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    result = run(tmp_path, monkeypatch, '2026-11-06T12:00:00+08:00')
    assert runner._exit_code(result) == 0
    for status in ('failed', 'ambiguous'):
        assert runner._exit_code({**result, 'status': status}) == 0
        assert runner._exit_code({'status': status}) == 5
    assert runner._exit_code({**result, 'status': 'busy'}) == 3
    assert runner._exit_code({**result, 'reason_code': 'snapshot_blocked'}) == 2
    assert runner._exit_code({**result, 'reason_code': 'brief_business_date_stale'}) == 4
    assert runner._exit_code({**result, 'reason_code': 'notification_receipt_path_escape'}) == 4
