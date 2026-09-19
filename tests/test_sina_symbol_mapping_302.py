from __future__ import annotations

import pytest

from quantdesk import data_providers as providers


@pytest.mark.parametrize('symbol', ['302132', '302132.SZ', 'sz302132', 'SZ302132'])
def test_302_symbol_has_shenzhen_quote_route(symbol: str) -> None:
    assert providers.normalize_cn_symbol_for_sina(symbol) == 'sz302132'


@pytest.mark.parametrize('symbol, expected', [
    ('300001', 'sz300001'), ('301132', 'sz301132'), ('600000', 'sh600000'),
    ('920000', 'bj920000'), ('920000.BJ', 'bj920000'), ('bj920000', 'bj920000'),
    ('900901', 'sh900901'), ('unknown', None),
])
def test_preexisting_exchange_routes_are_preserved(symbol: str, expected: str | None) -> None:
    assert providers.normalize_cn_symbol_for_sina(symbol) == expected


def test_bare_302_symbol_reaches_injected_batch_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    fields = ['fixture', '10', '10', '10.2', '10.5', '9.9', '0', '0', '100', '1020']
    fields += ['0'] * 20 + ['2026-09-10', '15:00:00', '00']

    def fake_get(url: str, *, timeout: float) -> str:
        calls.append(url)
        return 'var hq_str_sz302132="' + ','.join(fields) + '";'

    monkeypatch.setattr(providers, '_sina_http_get', fake_get)
    result = providers.fetch_spot_sina_batch(['302132'])
    assert len(calls) == 1
    assert calls[0].endswith('list=sz302132')
    assert result.provider_errors == []
    assert result.warnings == []
    assert result.rows[0]['symbol'] == 'sz302132'
    assert result.rows[0]['date'] == '2026-09-10'


def test_302_request_failure_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get(url: str, *, timeout: float) -> str:
        raise TimeoutError('injected quote timeout')

    monkeypatch.setattr(providers, '_sina_http_get', fail_get)
    result = providers.fetch_spot_sina_batch(['302132.SZ'])
    assert result.rows == []
    assert result.warnings == []
    assert len(result.provider_errors) == 1
    assert 'injected quote timeout' in result.provider_errors[0]
