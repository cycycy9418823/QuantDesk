"""Owner-selected health contract, separate from optional recovery packages."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
from quantdesk import owner_view

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('health_revision', ROOT / 'scripts/ops/write_health_line.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
DAY = '2026-11-06'
NOW = datetime.fromisoformat('2026-11-06T23:30:00+08:00')


def write(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def seed(root, **overrides):
    write(root, 'logs/ops/backup_push.log', {'stamp': 'snapshot_20261106T140050Z', 'exit_code': 3,
          'all_hashes_match': False, 'paper_backup_verified': True,
          'project_recovery': {'ok': False, 'error': 'recovery_working_source_changed'}, **overrides})
    write(root, 'results/offsite_backup/offsite_backup_latest.json',
          {'status': 'ok', 'finished_at': '2026-11-06T23:03:20+08:00', 'cloud_upload_confirmed': False})


def test_optional_recovery_failure_cannot_override_verified_ledger_copy(tmp_path):
    seed(tmp_path)
    checked = owner_view.backup_health_status(tmp_path, DAY, now=NOW)
    assert checked['ok'] and checked['paper']['ok'] and checked['offsite_ok']
    assert checked['cloud_upload_confirmed'] is False
    cell, detail = health.backup_cell(tmp_path, DAY)
    assert cell == '备份 ✓'
    assert 'P5项目恢复包参考（不影响备份格）：recovery_working_source_changed' in detail
    assert '云端上传尚未确认' in detail


@pytest.mark.parametrize('stamp,status', [
    ('2026-11-04T23:29:59+08:00', 'ok'), ('2026-11-07T01:00:00+08:00', 'ok'),
    ('2026-11-06T23:00:00', 'ok'), ('invalid', 'ok'), ('2026-11-06T23:00:00+08:00', 'failed')])
def test_invalid_stale_future_or_failed_offsite_is_not_green(tmp_path, stamp, status):
    seed(tmp_path)
    write(tmp_path, 'results/offsite_backup/offsite_backup_latest.json', {'status': status, 'finished_at': stamp})
    assert not owner_view.backup_health_status(tmp_path, DAY, now=NOW)['ok']


def test_exact_48_hours_is_included_and_missing_receipt_is_not(tmp_path):
    seed(tmp_path)
    write(tmp_path, 'results/offsite_backup/offsite_backup_latest.json', {'status': 'ok', 'finished_at': '2026-11-04T23:30:00+08:00'})
    assert owner_view.backup_health_status(tmp_path, DAY, now=NOW)['ok']
    (tmp_path / 'results/offsite_backup/offsite_backup_latest.json').unlink()
    assert not owner_view.backup_health_status(tmp_path, DAY, now=NOW)['ok']


def test_explicit_failed_paper_verification_beats_legacy_success(tmp_path):
    seed(tmp_path, paper_backup_verified=False, exit_code=0, all_hashes_match=True)
    assert not owner_view.backup_health_status(tmp_path, DAY, now=NOW)['ok']


def test_daily_rule_is_not_mislabeled_as_missing_minute_fallback(tmp_path):
    actions = [{'symbol': str(i), 'decision_date': DAY, 'quantity': 100, 'action': 'buy', **extra}
               for i, extra in enumerate([{'order_source': 'auto_strategy_sandbox'},
                   {'fill_model': 'intraday_1m_close'}, {'fill_model': 'daily_approx'}, {'fill_model': 'unknown'}])]
    actions += [{'action': 'block', 'quantity': 0, 'decision_date': DAY}]
    write(tmp_path, 'results/auto_paper_portfolio_latest.json', {'valuation_date': DAY, 'daily_actions': actions})
    result = owner_view.execution_basis_digest(tmp_path, DAY)
    assert [result[k] for k in ('minute', 'daily_approx', 'daily_close', 'unknown')] == [1, 1, 1, 1]
    assert len(result['rows']) == 4
    assert not owner_view.execution_basis_digest(tmp_path, '2026-11-05')['verified']


def test_empty_or_unreadable_actions_are_unknown_not_zero(tmp_path):
    assert not owner_view.execution_basis_digest(tmp_path, DAY)['verified']
    write(tmp_path, 'results/auto_paper_portfolio_latest.json', {'valuation_date': DAY, 'daily_actions': []})
    assert owner_view.execution_basis_digest(tmp_path, DAY)['minute'] == 0
