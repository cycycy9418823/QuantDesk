"""A complete roster cannot end fallback when requested quotes are unverified."""
from datetime import datetime

import pytest

from quantdesk import snapshot_history as history


DAY = '2026-10-15'
SYMBOLS = ['600000', '000001']
NOW = datetime(2026, 10, 15, 10, 0, 5, tzinfo=history.SHANGHAI)


def quote(symbol, **changes):
    row = dict(symbol=symbol, price=11.0, date=DAY, time='10:00:01', price_basis='raw')
    row.update(changes)
    return row


def capture(symbols=SYMBOLS):
    return [quote(symbol) for symbol in symbols]


def run_chain(kind, monkeypatch, primary_rows, secondary_rows, *, clock=lambda: 0.0, budget=15):
    calls = []

    def primary(**kwargs):
        calls.append(('primary', kwargs.get('budget_seconds')))
        return primary_rows, 'fixture.primary'

    def secondary(**kwargs):
        calls.append(('secondary', kwargs.get('budget_seconds')))
        return secondary_rows, 'fixture.secondary'

    monkeypatch.setattr(history, '_as_shanghai', lambda value: value or NOW)
    if kind == 'resilient':
        rows, source = history.resilient_network_fetch(
            SYMBOLS, trade_date=DAY, primary=primary, secondary=secondary, clock=clock, budget_seconds=budget)
    else:
        monkeypatch.setattr(history, '_sina_direct_fetch', primary)
        monkeypatch.setattr(history, '_sina_hq_fetch', lambda symbols, **kwargs: secondary(**kwargs))
        monkeypatch.setattr(history, '_akshare_provider', lambda *args: pytest.fail('unbounded source must stay skipped'))
        rows, source = history.default_network_fetch(SYMBOLS, trade_date=DAY, clock=clock, budget_seconds=budget)
    return rows, source, calls


@pytest.mark.parametrize('kind', ['resilient', 'default'])
@pytest.mark.parametrize('bad', [
    {'date': ''}, {'date': '2026-10-14'}, {'date': '2026-10-16'}, {'date': '2026-02-30'},
    {'time': ''}, {'time': 'invalid'}, {'time': '25:00:00'},
    {'price_basis': ''}, {'price_basis': 'qfq'}, {'price': 0}, {'price': float('inf')},
])
def test_full_roster_with_one_deficient_quote_uses_complete_fallback(kind, bad, monkeypatch):
    primary = capture()
    primary[1].update(bad)
    rows, source, calls = run_chain(kind, monkeypatch, primary, capture())
    assert [call[0] for call in calls] == ['primary', 'secondary']
    assert [row['price'] for row in rows] == [11.0, 11.0]
    assert all(row['date'] == DAY and row['time'] == '10:00:01' for row in rows)
    stage = next(item for item in rows.provider_errors if item.get('coverage_count') == 2)
    assert stage['outcome'] == 'partial' and stage['usable_quote_count'] == 1
    assert stage['quality_status'] == 'partial'
    assert 'secondary' in source or source == 'sina.hq_batch'


@pytest.mark.parametrize('kind', ['resilient', 'default'])
def test_all_failures_retain_best_real_usable_capture_without_merging(kind, monkeypatch):
    primary = capture()
    primary[1].update(date='')
    secondary = [quote('000001', price=22.0), quote('600000', price=0)]
    rows, source, calls = run_chain(kind, monkeypatch, primary, secondary)
    assert [call[0] for call in calls] == ['primary', 'secondary']
    assert list(rows) == primary  # tie keeps the first capture, including its actual missing date
    assert rows[1]['date'] == ''
    assert not any(row['price'] == 22 for row in rows)
    assert any(item['stage'] == 'skipped' for item in rows.provider_errors) or kind == 'resilient'
    assert all(item['outcome'] != 'ok' for item in rows.provider_errors if item['stage'] == 'request')
    assert rows.warnings


@pytest.mark.parametrize('kind', ['resilient', 'default'])
def test_more_usable_quotes_win_over_larger_invalid_roster(kind, monkeypatch):
    primary = [quote(symbol, price=0) for symbol in SYMBOLS]
    secondary = [quote('600000', price=22.0)]
    rows, source, calls = run_chain(kind, monkeypatch, primary, secondary)
    assert list(rows) == secondary
    assert 'secondary' in source or source == 'sina.hq_batch'
    assert any(item.get('outcome') == 'unusable' for item in rows.provider_errors)


@pytest.mark.parametrize('kind', ['resilient', 'default'])
def test_duplicate_requested_identity_cannot_prove_complete_capture(kind, monkeypatch):
    primary = [*capture(), quote('600000', price=99.0)]
    rows, source, calls = run_chain(kind, monkeypatch, primary, capture())
    assert len(calls) == 2 and len(rows) == 2
    assert rows.provider_errors[0]['duplicate_symbol_count'] == 1
    assert rows.provider_errors[0]['outcome'] != 'ok'


@pytest.mark.parametrize('kind', ['resilient', 'default'])
def test_confirmed_stale_prices_do_not_outrank_undated_partial_quotes(kind, monkeypatch):
    primary = [quote(symbol, date='2001-01-02') for symbol in SYMBOLS]
    secondary = [quote(symbol, date='') for symbol in SYMBOLS]
    rows, source, calls = run_chain(kind, monkeypatch, primary, secondary)
    assert len(calls) == 2 and list(rows) == secondary
    assert all(item.get('usable_quote_count', 0) == 0 for item in rows.provider_errors)
    assert rows.warnings and all(row['date'] == '' for row in rows)


@pytest.mark.parametrize('kind', ['resilient', 'default'])
def test_normalized_complete_quotes_skip_unneeded_fallback_with_evidence(kind, monkeypatch):
    primary = [dict(代码='000001', 最新价=11.0, 日期=DAY, 时间戳='10:00:01', price_basis='raw'),
               quote('sh600000')]
    rows, source, calls = run_chain(kind, monkeypatch, primary, [])
    assert [call[0] for call in calls] == ['primary']
    assert list(rows) == primary
    assert rows.warnings == []
    assert rows.provider_errors[0]['quality_status'] == 'complete'
    assert rows.provider_errors[0]['usable_quote_count'] == 2
    assert any(item['stage'] == 'skipped' and item['message'] == 'verified_requested_quotes_complete'
               for item in rows.provider_errors)


def test_legacy_collector_passes_session_date_to_provider_contract(monkeypatch):
    received = []

    def fetch(symbols, *, trade_date=''):
        received.append((symbols, trade_date))
        return capture(), 'fixture'

    history._call_fetcher(fetch, NOW, SYMBOLS)
    assert received == [(SYMBOLS, DAY)]


def test_quality_metadata_detects_nonempty_invalid_date_and_time():
    normalized = history.normalize_snapshot_rows(
        [quote('600000', date='2026-02-30', time='25:00:00')],
        source='fixture', fetched_at=NOW, trade_date=DAY, slot_time='1000')
    quality = history._quote_field_quality(normalized, DAY)
    assert quality['quote_date_invalid_count'] == quality['quote_time_invalid_count'] == 1
    assert quality['quality_status'] == 'partial'
