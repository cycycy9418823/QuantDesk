"""Minute freshness is a scoped observation, not complete holding/day coverage."""
from datetime import datetime
import hashlib
import json

import pytest

from quantdesk.intraday_refresh import collector, scope

NOW = datetime(2026, 10, 15, 10, 20, tzinfo=collector.SHANGHAI)


def put(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf8')
    return path


def ledgers(root):
    return [put(root, rel, {'positions': [{'symbol': symbol, 'quantity': 100}], 'valuation_date': '2026-10-14'})
            for rel, symbol in [('state/ai_trader/ledger.json', '600000'),
                                ('state/ai_trader_glm/ledger.json', '000001'),
                                ('state/auto_paper_portfolio.json', '920786')]]


def raw(symbol, stamp):
    return {'symbol': symbol, 'datetime': f'2026-10-15 {stamp}',
            'open': 10, 'high': 10, 'low': 10, 'close': 10, 'volume': 100,
            'amount': 1000, 'price_basis': 'raw', 'source': 'fixture'}


def test_holding_inventory_does_not_expand_enabled_minute_pool(tmp_path):
    files = ledgers(tmp_path)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
    result = scope.build_dynamic_scope(tmp_path, now=NOW)
    assert [row['symbol'] for row in result['symbols']] == ['600000']
    books = {row['account']: row for row in result['holding_inventory']}
    assert books['strategy']['symbols'] == ['920786']
    assert books['ai_glm']['symbols'] == ['000001']
    assert result['ai_minute_focus_read_enabled'] is False
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]


def test_held_symbols_capped_out_are_explicit(tmp_path):
    put(tmp_path, 'state/ai_trader/ledger.json',
        {'positions': [{'symbol': code, 'quantity': 100} for code in ['600000', '600001', '600002']]})
    result = scope.build_dynamic_scope(tmp_path, now=NOW, max_symbols=1)
    assert [row['symbol'] for row in result['symbols']] == ['600000']
    assert [row['symbol'] for row in result['excluded_symbols']] == ['600001', '600002']
    assert all(row['exclusion_reason'] == 'minute_scope_capped' for row in result['excluded_symbols'])


@pytest.fixture
def no_snapshot(monkeypatch):
    monkeypatch.setattr(collector, 'refresh_full_market_snapshot', lambda *args, **kwargs: {
        'ok': False, 'status': 'fixture', 'movers': [], 'reason_codes': []})


def test_one_response_window_does_not_prove_all_holdings_or_full_day(tmp_path, no_snapshot):
    files = ledgers(tmp_path)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]

    def fetch(symbol, offset, **kwargs):
        if symbol == '000001':
            return [], 'provider_failed:mootdx:struct.error', {'error_stage': 'protocol', 'error_type': 'struct.error'}
        return [raw(symbol, '10:19:00'), raw(symbol, '10:20:00')], '', {}

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, now=NOW, trading_day=True, fetcher=fetch,
        scope_builder=lambda *a, **k: {'symbols': [{'symbol': code, 'priority': 'critical'}
                                                 for code in ['600000', '000001']], 'warnings': []},
        monotonic=lambda: 0.0)
    coverage = result['holding_minute_coverage']
    rows = {(row['account'], row['symbol']): row for row in coverage['symbols']}
    assert rows[('ai_default', '600000')]['coverage_status'] == 'observed_partial'
    assert rows[('ai_default', '600000')]['last_valid_response_minute'] == '2026-10-15 10:20:00'
    assert rows[('ai_glm', '000001')]['coverage_status'] == 'provider_failed'
    assert rows[('ai_glm', '000001')]['error_stage'] == 'protocol'
    assert rows[('strategy', '920786')]['coverage_status'] == 'not_in_scope'
    window = result['provider_status'][0]['minute_window']
    assert window['valid_response_minutes'] == 2 and window['first_valid_minute'] == '2026-10-15 10:19:00'
    assert window['expected_session_minutes'] is None and window['missing_session_minutes'] is None
    assert window['continuity_status'] == 'not_assessed_provider_session_convention'
    assert coverage['full_day_verified_count'] == 0 and coverage['observed_partial_count'] == 1
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]


def test_zero_and_unverified_quantities_do_not_become_known_holdings(tmp_path):
    put(tmp_path, 'state/ai_trader/ledger.json', {'positions': {
        '600000': {'quantity': 0}, '600001': {'quantity': 100}, '600002': {'quantity': 'invalid'}}})
    book = next(row for row in scope.read_holding_inventory(tmp_path) if row['account'] == 'ai_default')
    assert book['symbols'] == ['600001'] and book['unverified_quantity_symbols'] == ['600002']


def test_unknown_quantity_or_bad_ledger_is_visible(tmp_path):
    files = ledgers(tmp_path)
    files[2].write_text('{broken', encoding='utf8')
    books = {row['account']: row for row in scope.read_holding_inventory(tmp_path)}
    assert books['strategy']['status'] == 'unreadable' and books['strategy']['symbols'] == []
    assert books['strategy']['reason']


def test_conflicting_position_key_is_not_a_verified_holding(tmp_path):
    put(tmp_path, 'state/ai_trader/ledger.json',
        {'positions': {'600000': {'symbol': '000001', 'quantity': 100}}})
    book = next(row for row in scope.read_holding_inventory(tmp_path) if row['account'] == 'ai_default')
    assert book['status'] == 'partial' and book['symbols'] == []
    assert book['reason'] == 'position_identity_unverified'


def test_offline_scoped_holding_is_unattempted(tmp_path, no_snapshot):
    ledgers(tmp_path)
    result = collector.collect_intraday_refresh(
        tmp_path, network=False, now=NOW, trading_day=True, monotonic=lambda: 0.0,
        fetcher=lambda *a, **k: pytest.fail('offline must not fetch'))
    row = next(row for row in result['holding_minute_coverage']['symbols'] if row['account'] == 'ai_default')
    assert row['coverage_status'] == 'not_attempted' and row['reason'] == 'network_not_authorized'


def test_expired_snapshot_budget_does_not_start_inventory_read(tmp_path, monkeypatch):
    ticks = [0.0]

    def snapshot(*args, **kwargs):
        ticks[0] = 2.0
        return {'ok': False, 'status': 'fixture', 'movers': [], 'reason_codes': []}

    monkeypatch.setattr(collector, 'refresh_full_market_snapshot', snapshot)
    monkeypatch.setattr(collector, 'read_holding_inventory', lambda *a: pytest.fail('budget expired'))
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, now=NOW, trading_day=True, max_round_seconds=1,
        monotonic=lambda: ticks[0], fetcher=lambda *a, **k: pytest.fail('budget expired'))
    assert result['status'] == 'timed_out'
    assert result['holding_minute_coverage']['status'] == 'not_assessed'
    assert result['holding_minute_coverage']['known_holding_count'] is None
