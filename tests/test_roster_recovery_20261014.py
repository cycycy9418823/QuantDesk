"""Roster refresh budgets and publication must preserve observed completeness."""
from __future__ import annotations

import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from quantdesk.process_deadline import ProcessDeadlineResult
from scripts import update_a_share_universe as updater


DAY = '2026-10-14'
SOURCE = 'sina.spot_direct'


def source_payload(provider: str, count: int = 5500, *, complete: bool = True) -> dict:
    return {
        'rows': [{'symbol': str(600000 + index), 'name': 'fixture', 'source': provider} for index in range(count)],
        'response_complete': complete, 'pagination_complete': complete if provider == SOURCE else None,
        'completion_evidence': 'terminal_page' if provider == SOURCE else 'provider_return',
        'observed_pages': 55, 'expected_pages': 55 if complete else 56,
    }


def test_default_keeps_sina_import_and_pagination_in_one_meaningful_window():
    clock = [0.0]
    calls = []

    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        provider = tasks[0]
        calls.append((provider, timeout_seconds))
        required = 13.0 if provider == SOURCE else 11.0
        clock[0] += min(required, timeout_seconds)
        if timeout_seconds < required:
            return ProcessDeadlineResult({}, {0: 'fixture_cold_import_or_pagination_timeout'}, [0], True, [])
        return ProcessDeadlineResult({0: source_payload(provider)}, {}, [], False, [])

    rows, source, errors = updater.fetch_universe_from_akshare(runner=run, clock=lambda: clock[0])
    assert rows and source == SOURCE, (calls, errors)
    assert calls[0][0] == SOURCE and calls[0][1] > 12
    assert len(rows.source_snapshots) == 2 and rows.complete is True
    assert clock[0] <= 35


def test_complete_roster_survives_later_provider_timeouts():
    clock = [0.0]

    def run(worker, tasks, *, timeout_seconds, **kwargs):
        provider = tasks[0]
        if provider == SOURCE:
            clock[0] += 2
            return ProcessDeadlineResult({0: source_payload(provider)}, {}, [], False, [])
        clock[0] += timeout_seconds
        return ProcessDeadlineResult({}, {0: 'fixture_later_timeout'}, [0], True, [])

    rows, source, errors = updater.fetch_universe_from_akshare(runner=run, clock=lambda: clock[0])
    assert source == SOURCE and len(rows) == 5500 and rows.complete is True
    assert errors and len(rows.source_snapshots) == 1
    assert clock[0] <= 35


def test_larger_partial_response_does_not_replace_a_complete_result():
    def run(worker, tasks, **kwargs):
        provider = tasks[0]
        payload = source_payload(provider, 5500 if provider == SOURCE else 5000, complete=provider != SOURCE)
        return ProcessDeadlineResult({0: payload}, {}, [], False, [])

    rows, source, _ = updater.fetch_universe_from_akshare(
        providers=['stock_info_a_code_name', SOURCE], runner=run, clock=lambda: 0,
    )
    assert len(rows) == 5000 and source == 'stock_info_a_code_name' and rows.complete is True
    assert rows.source_snapshots[-1]['status'] == 'partial'


def test_large_interrupted_pagination_is_retained_only_as_partial_evidence():
    def run(worker, tasks, **kwargs):
        return ProcessDeadlineResult({0: source_payload(SOURCE, complete=False)}, {}, [], False, [])

    rows, source, errors = updater.fetch_universe_from_akshare(providers=[SOURCE], runner=run, clock=lambda: 0)
    assert len(rows) == 5500 and source == SOURCE and rows.complete is False
    assert rows.source_snapshots[0]['status'] == 'partial' and errors
    assert rows.source_snapshots[0]['pagination_complete'] is False


def test_direct_response_without_pagination_proof_cannot_be_complete():
    def run(worker, tasks, **kwargs):
        payload = source_payload(SOURCE)
        payload.pop('pagination_complete')
        return ProcessDeadlineResult({0: payload}, {}, [], False, [])

    rows, _, errors = updater.fetch_universe_from_akshare(providers=[SOURCE], runner=run, clock=lambda: 0)
    assert not rows.complete and errors


def test_contract_candidate_changes_only_the_accurate_existing_direct_source():
    root = Path(updater.__file__).resolve().parents[1]
    contract = json.loads((root / 'config/weekly_daily_wide_pool_contract.json').read_text(encoding='utf-8'))
    approved = contract['rules']['approved_market_universe_sources']
    assert approved == ['baostock.query_stock_basic', 'stock_info_a_code_name', 'stock_zh_a_spot_em', 'stock_zh_a_spot', SOURCE]


def test_import_and_preparation_reduce_the_budget_before_any_source(monkeypatch, tmp_path):
    clock = [100.0]
    captured = []
    monkeypatch.setattr(updater.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(updater, 'PROJECT_ROOT', tmp_path)

    def load(*args):
        clock[0] += 3
        return {}

    def fetch(**kwargs):
        captured.append(kwargs['budget_seconds'])
        return updater.UniverseSourceRows(), '', {'fixture': 'no live source'}

    monkeypatch.setattr(updater, 'load_config', load)
    monkeypatch.setattr(updater, 'fetch_universe_from_akshare', fetch)
    args = updater.parse_args(['--dry-run', '--output', str(tmp_path/'roster.csv'), '--status-path', str(tmp_path/'status.json')])
    updater.run_update_universe(args)
    assert 0 < captured[0] <= 30  # 35 includes preparation and publication reserve


def test_late_serialization_does_not_replace_the_original_roster(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(updater.time, 'monotonic', lambda: clock[0])
    original = b'symbol,name,source,updated_at\n600000,old,stock_info_a_code_name,2026-09-29\n'
    output = tmp_path/'roster.csv'
    output.write_bytes(original)
    original_fsync = updater.os.fsync

    def late_flush(fd):
        original_fsync(fd)
        clock[0] = 106

    monkeypatch.setattr(updater.os, 'fsync', late_flush)
    with pytest.raises(TimeoutError):
        updater.write_universe_csv(output, source_payload(SOURCE, 5)['rows'], deadline_monotonic=105)
    assert output.read_bytes() == original and not list(tmp_path.glob('*.tmp'))


WORKER_BOOTSTRAP = r'''
import builtins
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

events = Path(os.environ['QD_ROSTER_TEST_EVENTS'])
mode = os.environ['QD_ROSTER_TEST_MODE']
probing = False

def record(event, **details):
    with events.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(dict(event=event, pid=os.getpid(), at=time.monotonic(), **details)) + '\n')

def forbidden(*args, **kwargs):
    if not probing:
        record('unexpected_network')
    raise AssertionError('offline roster guard blocked networking')

for name in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo'):
    setattr(socket, name, forbidden)
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
import requests
requests.get = forbidden
requests.request = forbidden
requests.sessions.Session.request = forbidden
probing = True
verified = []
for name, call in (
    ('connect', lambda: socket.socket.connect(None, ('fixture.invalid', 443))),
    ('connect_ex', lambda: socket.socket.connect_ex(None, ('fixture.invalid', 443))),
    ('create_connection', lambda: socket.create_connection(('fixture.invalid', 443))),
    ('getaddrinfo', lambda: socket.getaddrinfo('fixture.invalid', 443)),
    ('gethostbyname', lambda: socket.gethostbyname('fixture.invalid')),
    ('gethostbyname_ex', lambda: socket.gethostbyname_ex('fixture.invalid')),
    ('gethostbyaddr', lambda: socket.gethostbyaddr('192.0.2.1')),
    ('getnameinfo', lambda: socket.getnameinfo(('192.0.2.1', 443), 0)),
    ('requests.get', lambda: requests.get('https://fixture.invalid')),
    ('requests.request', lambda: requests.request('GET', 'https://fixture.invalid')),
    ('requests.Session.request', lambda: requests.Session().request('GET', 'https://fixture.invalid')),
):
    try:
        call()
    except AssertionError:
        verified.append(name)
    else:
        raise RuntimeError('network guard failed: ' + name)
probing = False
assert not any(name == 'quantdesk' or name.startswith('quantdesk.') for name in sys.modules)
record('guard_ready', guarded_channels=verified, project_loaded=False)
if os.environ.get('QD_ROSTER_GUARD_ONLY') == '1':
    raise SystemExit(0)

def direct(url, *, params, headers, timeout):
    if url != 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData':
        return forbidden()
    page = int(params['page'])
    record('direct_page', page=page, timeout=timeout)
    if mode in {'direct_hang', 'all_hang'}:
        time.sleep(60)
    if mode == 'partial' and page == 56:
        raise TimeoutError('fixture_final_page_unavailable')
    width = int(params['num'])
    first = (page - 1) * width
    payload = [{'code': str(600000 + index), 'name': 'fixture', 'trade': '10', 'ticktime': '15:00:00'}
               for index in range(first, min(first + width, 5500))]
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

requests.get = direct
requests.sessions.Session.get = lambda self, url, **kwargs: direct(url, **kwargs)

def login():
    record('baostock_login')
    if mode == 'all_hang':
        time.sleep(60)
    if mode in {'partial', 'direct_only'}:
        raise RuntimeError('fixture_baostock_unavailable')
    return SimpleNamespace(error_code='0')

class Query:
    error_code = '0'
    fields = ['code', 'code_name', 'type', 'status']
    index = -1
    cur_page_num = '1'
    per_page_count = '2000'
    cur_row_num = 0
    data = [None] * 5500  # SDK's documented one-response, over-page-size path.

    def next(self):
        self.index += 1
        return self.index < 5500

    def get_row_data(self):
        self.cur_row_num = self.index + 1
        return ['sh.' + str(600000 + self.index), 'fixture', '1', '1']

def sdk():
    record('sdk_call')
    raise RuntimeError('fixture_optional_sdk_unavailable')

sys.modules['baostock'] = SimpleNamespace(login=login, query_stock_basic=Query, logout=lambda: record('baostock_logout'))
sys.modules['baostock.common.contants'] = SimpleNamespace(BAOSTOCK_PER_PAGE_COUNT=2000)
sys.modules['akshare'] = SimpleNamespace(stock_info_a_code_name=sdk)
original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name in {'quantdesk.universe_refresh', 'baostock', 'akshare'}:
        record('provider_import', name=name)
    if name == 'quantdesk.universe_refresh' and mode == 'direct_import_hang':
        time.sleep(60)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

script = os.environ['QD_ROSTER_TEST_SCRIPT']
original_spawn = subprocess.Popen

def offline_spawn(argv, *args, **kwargs):
    if argv[1:3] == ['-B', script]:
        bootstrap = (
            'import runpy, sys\n'
            + 'sys.path.insert(0, ' + repr(str(Path(script).parents[1])) + ')\n'
            + 'runpy.run_path(' + repr(__file__) + ', run_name="_offline_roster")\n'
            + 'sys.argv = ' + repr([script, *argv[3:]]) + '\n'
            + 'runpy.run_path(' + repr(script) + ', run_name="__main__")\n'
        )
        argv = [argv[0], '-B', '-c', bootstrap]
    return original_spawn(argv, *args, **kwargs)

subprocess.Popen = offline_spawn
'''


def process_is_running(pid: int) -> bool:
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87
        return False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        assert result in {0, 258}
        return result == 258
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture
def fake_roster_worker(tmp_path, monkeypatch):
    fixture = tmp_path/'offline_roster_transport.py'
    fixture.write_text(WORKER_BOOTSTRAP, encoding='utf-8')
    events = tmp_path/'worker_events.jsonl'
    monkeypatch.setenv('QD_ROSTER_TEST_EVENTS', str(events))
    monkeypatch.setenv('QD_ROSTER_TEST_MODE', 'complete')
    monkeypatch.delenv('QD_ROSTER_GUARD_ONLY', raising=False)
    original_spawn = subprocess.Popen
    script = str(Path(updater.__file__).resolve())
    monkeypatch.setenv('QD_ROSTER_TEST_SCRIPT', script)

    def spawn(argv, *args, **kwargs):
        if argv[1:3] == ['-B', script]:
            bootstrap = (
                'import runpy, sys\n'
                + f'sys.path.insert(0, {str(Path(script).parents[1])!r})\n'
                + f'runpy.run_path({str(fixture)!r}, run_name="_offline_roster")\n'
                + f'sys.argv = {[script, *argv[3:]]!r}\n'
                + f'runpy.run_path({script!r}, run_name="__main__")\n'
            )
            argv = [argv[0], '-B', '-c', bootstrap]
        return original_spawn(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    yield events
    rows = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()]
    guards = [row for row in rows if row['event'] == 'guard_ready']
    assert guards and all(len(row['guarded_channels']) == 11 and not row['project_loaded'] for row in guards)
    assert not any(row['event'].startswith('unexpected_') for row in rows)
    assert {row['pid'] for row in rows} == {row['pid'] for row in guards}
    proof = [{'pid': pid, 'running': process_is_running(pid)} for pid in sorted({row['pid'] for row in guards})]
    events.with_name('process_reaping.json').write_text(json.dumps(proof), encoding='utf-8')
    assert not any(row['running'] for row in proof), proof


def test_roster_bootstrap_guards_before_project_import(fake_roster_worker, monkeypatch):
    monkeypatch.setenv('QD_ROSTER_GUARD_ONLY', '1')
    fixture = fake_roster_worker.with_name('offline_roster_transport.py')
    result = subprocess.run([sys.executable, '-B', str(fixture)], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_real_worker_keeps_complete_direct_list_when_both_backups_fail(fake_roster_worker, monkeypatch):
    monkeypatch.setenv('QD_ROSTER_TEST_MODE', 'direct_only')
    started = time.monotonic()
    rows, source, errors = updater.fetch_universe_from_akshare()
    assert time.monotonic() - started < 35
    assert source == SOURCE and len(rows) == 5500 and rows.complete
    assert len(errors) == 2
    first = rows.attempts[0]['process_evidence']
    assert first['process_reaped'] and first['return_code'] == 0
    assert [event['phase'] for event in first['phase_events']] == ['provider_import', 'provider_call', 'normalize']
    assert not process_is_running(first['runtime_pid'])
    fake_roster_worker.with_name('source_receipt.json').write_text(json.dumps({'source': source, 'attempts': rows.attempts,
        'source_snapshots': rows.source_snapshots, 'errors': errors}), encoding='utf-8')


@pytest.mark.parametrize('mode,phase', [('direct_hang', 'provider_call'), ('direct_import_hang', 'provider_import')])
def test_real_stalled_direct_yields_to_baostock_with_original_deadline(fake_roster_worker, monkeypatch, mode, phase):
    monkeypatch.setenv('QD_ROSTER_TEST_MODE', mode)
    started = time.monotonic()
    rows, source, errors = updater.fetch_universe_from_akshare(budget_seconds=18)
    elapsed = time.monotonic() - started
    assert elapsed < 18 and rows.complete and source == 'baostock.query_stock_basic'
    assert SOURCE in errors
    detail = rows.attempts[0]['process_evidence']
    assert detail['last_phase'] == phase and detail['process_reaped']
    assert not process_is_running(detail['runtime_pid'])
    fake_roster_worker.with_name('deadline_receipt.json').write_text(json.dumps({'seconds': elapsed, 'attempts': rows.attempts,
        'source': source, 'row_count': len(rows)}), encoding='utf-8')


def test_actual_publisher_accepts_complete_direct_and_preserves_partial(
    tmp_path, fake_roster_worker, monkeypatch,
):
    from scripts import refresh_universe_baseline as publisher
    from scripts import run_data_close_pipeline as pipeline
    from quantdesk.weekly_daily_wide_pool import _market_symbols

    root = tmp_path/'publication'
    (root/'scripts').mkdir(parents=True)
    (root/'config').mkdir()
    (root/'scripts/update_a_share_universe.py').write_text('# injected runner uses the current module\n', encoding='utf-8')
    source_root = Path(updater.__file__).resolve().parents[1]
    contract = json.loads((source_root/'config/weekly_daily_wide_pool_contract.json').read_text(encoding='utf-8'))
    (root/'config/weekly_daily_wide_pool_contract.json').write_text(json.dumps(contract), encoding='utf-8')
    output = root/'data/universe/a_share_symbols.csv'
    output.parent.mkdir(parents=True)
    original = ('symbol,name,source,updated_at\n' + ''.join(
        f'{600000 + index},old,stock_info_a_code_name,2026-09-29\n' for index in range(5500))).encode()
    monkeypatch.setattr(updater, 'PROJECT_ROOT', root)
    monkeypatch.setattr(updater, 'load_config', lambda *_: {})
    monkeypatch.setattr(updater, 'DEFAULT_UNIVERSE_PATH', output)
    monkeypatch.setattr(updater, 'DEFAULT_STATUS_PATH', root/'data/cache/a_share_universe_status.json')
    monkeypatch.setattr(publisher, 'market_writer_subprocess_kwargs', lambda _root: {})

    def injected_command(argv, **kwargs):
        assert Path(argv[2]) == root/'scripts/update_a_share_universe.py'
        status = updater.run_update_universe(updater.parse_args(argv[3:]))
        return SimpleNamespace(returncode=0 if status['ok'] else 1, stdout=json.dumps(status), stderr='')

    monkeypatch.setattr(publisher.subprocess, 'run', injected_command)
    approved = pipeline._approved_universe_sources(root)
    assert approved == set(contract['rules']['approved_market_universe_sources']) and SOURCE in approved
    for mode in ('direct_only', 'partial'):
        monkeypatch.setenv('QD_ROSTER_TEST_MODE', mode)
        output.write_bytes(original)
        result = publisher.run_refresh(root, evidence_dir=root/'evidence'/mode)
        before = result['before']
        after = publisher.inspect_universe(output)
        if mode == 'direct_only':
            assert result['accepted'] and not result['restored_after_failure']
            assert not publisher.validate_refresh(before=before, after=after, approved_sources=approved,
                                                  today=datetime.now().date().isoformat())
            symbols, evidence = _market_symbols(output, target=datetime.now().date().isoformat(), rules=contract['rules'])
            assert len(symbols) == 5500 and evidence['status'] == 'passed'
        else:
            assert not result['accepted'] and result['restored_after_failure']
            assert 'updater_exit_nonzero' in result['problems']
            assert output.read_bytes() == original and after['updated_at_dates'] == ['2026-09-29']
            receipt = json.loads((root/'data/cache/a_share_universe_status.json').read_text(encoding='utf-8'))
            assert receipt['fetched_row_count'] == 5500 and receipt['full_roster_published'] is False
            assert receipt['source_snapshots'][0]['pagination_complete'] is False


@pytest.mark.parametrize('mode', ['direct_only', 'all_hang'])
def test_actual_cli_main_respects_budget_and_preserves_old_csv_on_timeout(
    tmp_path, fake_roster_worker, monkeypatch, mode,
):
    monkeypatch.setenv('QD_ROSTER_TEST_MODE', mode)
    root = tmp_path/'cli'
    root.mkdir()
    output = root/'roster.csv'
    status_path = root/'status.json'
    config = root/'config.json'
    config.write_text(json.dumps({'data': {'a_share_universe_path': str(output),
                                           'a_share_universe_status_path': str(status_path)}}), encoding='utf-8')
    original = ('symbol,name,source,updated_at\n' + ''.join(
        f'{600000 + index},old,stock_info_a_code_name,2026-09-29\n' for index in range(5500))).encode()
    output.write_bytes(original)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, '-B', str(Path(updater.__file__).resolve()), '--config', str(config),
         '--output', str(output), '--status-path', str(status_path), '--budget-seconds', '10'],
        cwd=str(root), capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    elapsed = time.monotonic() - started
    receipt = json.loads(status_path.read_text(encoding='utf-8'))
    assert receipt['duration_seconds'] < 10 and receipt['budget_seconds'] == 10
    assert elapsed < 10
    assert result.returncode == (0 if mode == 'direct_only' else 1), result.stdout + result.stderr
    if mode == 'all_hang':
        assert output.read_bytes() == original and receipt['full_roster_published'] is False
    else:
        assert receipt['provider_used'] == SOURCE and receipt['full_roster_published'] is True
    fake_roster_worker.with_name('cli_receipt.json').write_text(json.dumps({'wall_seconds': elapsed,
        'receipt': receipt, 'stdout': result.stdout, 'stderr': result.stderr}), encoding='utf-8')


BAO_SOURCE = 'baostock.query_stock_basic'


@pytest.fixture
def baostock_pagination(monkeypatch):
    """Exercise the optional SDK's actual iterator with an offline wire reply."""
    resultset = pytest.importorskip('baostock.data.resultset')
    constants = pytest.importorskip('baostock.common.contants')
    page_size = constants.BAOSTOCK_PER_PAGE_COUNT
    assert page_size == 2000  # The regression needs three full pages above 5000.

    def make(mode):
        result = resultset.ResultData()
        result.error_code = '0'
        result.cur_page_num = '1'
        result.per_page_count = str(page_size)
        result.fields = ['code', 'code_name', 'type', 'status']
        result.msg_type = constants.MESSAGE_TYPE_QUERYSTOCKBASIC_REQUEST
        result.msg_body = constants.MESSAGE_SPLIT.join(['query_stock_basic', 'fixture', '1', str(page_size), '', ''])
        requests = []
        logouts = []

        def records(first, count):
            return [['sh.' + str(600000 + index), 'fixture', '1', '1']
                    for index in range(first, first + count)]

        result.data = records(0, 6500 if mode == 'overfull' else page_size)

        def reply(page, data, *, error='0'):
            body = constants.MESSAGE_SPLIT.join([
                error, 'fixture', 'query_stock_basic', 'fixture', str(page), str(page_size),
                json.dumps({'record': data}, separators=(',', ':')),
            ])
            return resultset.msgheader.to_message_header(result.msg_type, len(body)) + body + '\n'

        def send_message(message):
            page = int(message[constants.MESSAGE_HEADER_LENGTH:].split(constants.MESSAGE_SPLIT)[2])
            requests.append(page)
            if page in (2, 3):
                count = 1500 if mode == 'terminal_short' and page == 3 else page_size
                response_page = 'invalid' if mode == 'invalid_page' and page == 3 else page
                return reply(response_page, records((page - 1) * page_size, count))
            assert page == 4, 'fixture must never issue an unplanned source request'
            if mode == 'transport_none':
                return None
            if mode == 'transport_blank':
                return ' \t\n'
            if mode == 'server_error':
                return reply(page, [], error='10002007')
            return reply(page - 1 if mode == 'empty_same_page' else page, [])

        monkeypatch.setattr(resultset.sock, 'send_msg', send_message)
        monkeypatch.setitem(sys.modules, 'baostock', SimpleNamespace(
            login=lambda: SimpleNamespace(error_code='0'), query_stock_basic=lambda: result,
            logout=lambda: logouts.append(True),
        ))
        return SimpleNamespace(result=result, requests=requests, logouts=logouts)

    return make


@pytest.mark.parametrize('mode,count,complete', [
    ('transport_none', 6000, False),
    ('transport_blank', 6000, False),
    ('server_error', 6000, False),
    ('invalid_page', 6000, False),
    ('empty_same_page', 6000, False),
    ('terminal_short', 5500, True),
    ('terminal_empty', 6000, True),
    ('overfull', 6500, True),
])
def test_baostock_completion_uses_sdk_terminal_evidence(baostock_pagination, mode, count, complete):
    fixture = baostock_pagination(mode)
    payload = updater._universe_source_worker(BAO_SOURCE, {})
    assert len(payload['rows']) == count
    assert payload['response_complete'] is complete
    assert fixture.logouts == [True]
    assert fixture.requests == ([] if mode == 'overfull' else
                                [2, 3] if mode in {'terminal_short', 'invalid_page'} else [2, 3, 4])
    if not complete:
        assert payload['provider_warning']
    if mode in {'transport_none', 'transport_blank'}:
        # These real SDK failure branches retain a misleading success code.
        assert fixture.result.error_code == '0'
        assert fixture.result.cur_page_num == '3'
        assert len(fixture.result.data) == fixture.result.cur_row_num == 2000


@pytest.mark.parametrize('mode', ['transport_none', 'transport_blank', 'server_error'])
def test_baostock_completion_cannot_replace_complete_direct(baostock_pagination, mode):
    baostock_pagination(mode)

    def run(worker, tasks, *, context, **kwargs):
        provider = tasks[0]
        payload = source_payload(SOURCE) if provider == SOURCE else worker(provider, context)
        return ProcessDeadlineResult({0: payload}, {}, [], False, [])

    rows, source, errors = updater.fetch_universe_from_akshare(
        providers=[SOURCE, BAO_SOURCE], runner=run, clock=lambda: 0,
    )
    assert rows.complete and source == SOURCE and len(rows) == 5500
    assert rows.source_snapshots[-1]['row_count'] == 6000
    assert rows.source_snapshots[-1]['response_complete'] is False
    assert rows.source_snapshots[-1]['status'] == 'partial'
    assert BAO_SOURCE in errors


@pytest.mark.parametrize('mode', ['transport_none', 'transport_blank', 'server_error'])
def test_baostock_completion_preserves_old_roster_and_date(baostock_pagination, monkeypatch, tmp_path, mode):
    from scripts import refresh_universe_baseline as publisher

    baostock_pagination(mode)
    output = tmp_path/'roster.csv'
    original = ('symbol,name,source,updated_at\n' + ''.join(
        f'{600000 + index},old,stock_info_a_code_name,2026-09-29\n' for index in range(5500))).encode()
    output.write_bytes(original)
    before = publisher.inspect_universe(output)
    monkeypatch.setattr(updater, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(updater, 'load_config', lambda *_: {})
    fetch = updater.fetch_universe_from_akshare

    def run(worker, tasks, *, context, **kwargs):
        return ProcessDeadlineResult({0: worker(tasks[0], context)}, {}, [], False, [])

    monkeypatch.setattr(updater, 'fetch_universe_from_akshare',
                        lambda **kwargs: fetch(providers=[BAO_SOURCE], runner=run, **kwargs))
    status = updater.run_update_universe(updater.parse_args([
        '--output', str(output), '--status-path', str(tmp_path/'status.json'),
    ]))
    assert status['ok'] is False and status['full_roster_published'] is False
    assert status['fetched_row_count'] == 6000
    assert output.read_bytes() == original
    after = publisher.inspect_universe(output)
    assert after['updated_at_dates'] == ['2026-09-29']
    assert publisher.validate_refresh(before=before, after=after,
                                      approved_sources={SOURCE, BAO_SOURCE, 'stock_info_a_code_name'},
                                      today=datetime.now().date().isoformat())
