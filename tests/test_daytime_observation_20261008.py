"""An observation cannot change a book, a queue, or a trading-day upgrade."""
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from ai_trader import daytime_decide, daytime_fill, daytime_observation as observer, daytime_runner
from quantdesk.interprocess_lock import interprocess_file_lock

DAY = "2026-10-08"
NOW = datetime(2026, 10, 8, 10, 0, 20)
RAW = '{"orders":[{"symbol":"600000","side":"buy","qty":200,"reason":"synthetic"}],"note":"test"}'


def setup_data(root: Path, *, quote_time: str = "2026-10-08T10:00:00+08:00") -> dict[str, bytes]:
    for rel, text in {
        'data/calendar/a_share_trading_calendar.csv': 'date,is_open\n2026-10-08,1\n',
        'data/snapshots/2026-10-08/1000.csv': 'symbol,price,quote_time,pct_chg\n600000,10,'+quote_time+',1\n',
        'state/ai_trader/ledger.json': '{"cash":8000,"equity":10000,"positions":[],"trades":[]}',
        'state/ai_trader/plan_latest.json': '{"committed":true,"intents":[]}',
        'state/ai_trader/daytime_intents.jsonl': '',
        'state/ai_trader/daytime_dry_state.json': '{"observation_status":"not_completed"}',
        'state/auto_paper_portfolio.json': '{"cash":123}',
        'state/ai_trader_glm/ledger.json': '{"cash":456}',
    }.items():
        p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def run(root, **kwargs):
    return observer.run_observation(root, enable=True, authorized_date=DAY, now=NOW, clock=lambda:NOW, **kwargs)


def test_default_off_and_wrong_date_write_nothing(tmp_path):
    assert observer.run_observation(tmp_path)['status']=='disabled'
    assert observer.run_observation(tmp_path,enable=True,authorized_date='2026-10-07',now=NOW)['reason']=='outside_authorized_date'
    assert list(tmp_path.iterdir())==[]


def test_observation_does_not_publish_intents_or_upgrade_day(tmp_path,monkeypatch):
    before=setup_data(tmp_path);calls=[]
    def forbidden(*a,**k): raise AssertionError('execution path entered')
    monkeypatch.setattr(daytime_fill,'fill_slot',forbidden)
    monkeypatch.setattr(daytime_runner,'mark_dry_day_observed',forbidden)
    def model(prompt):
        calls.append(prompt);return daytime_decide.ModelCall(text=RAW,model='fake',ok=True)
    result=run(tmp_path,model=model)
    assert result['status']=='recorded_unverified' and result['written']
    assert result['raw_response']==RAW and result['proposals_unverified']['orders']
    assert result['ledger_written'] is False and result['execution_queue_written'] is False
    again=run(tmp_path,model=model)
    assert again['status']=='already_attempted' and len(calls)==1
    for rel,raw in before.items(): assert (tmp_path/rel).read_bytes()==raw
    assert not (tmp_path/'state/ai_trader/daytime_journal.jsonl').exists()


@pytest.mark.parametrize('stamp',['2026-10-07T10:00:00+08:00','2026-10-08T09:50:00+08:00','2026-10-08T10:01:00+08:00','bad',''])
def test_bad_stale_or_future_quotes_do_not_call_model(tmp_path,stamp):
    setup_data(tmp_path,quote_time=stamp)
    def forbidden(prompt): raise AssertionError('model called')
    result=run(tmp_path,model=forbidden)
    assert result['status']=='skipped' and result['calls_this_run']==0
    assert result['reason']=='no_fresh_quote_evidence'


def test_model_failure_consumes_slot(tmp_path):
    setup_data(tmp_path);calls=[]
    def broken(prompt): calls.append(prompt);raise TimeoutError()
    assert run(tmp_path,model=broken)['status']=='model_failed'
    assert run(tmp_path,model=broken)['status']=='already_attempted'
    assert len(calls)==1


def test_reservation_failure_prevents_call_and_completion_failure_prevents_retry(tmp_path,monkeypatch):
    setup_data(tmp_path);calls=[];write=observer.atomic_write_json
    def model(prompt): calls.append(prompt);return daytime_decide.ModelCall(text=RAW,ok=True)
    def failing(path,value): raise OSError('disk')
    monkeypatch.setattr(observer,'atomic_write_json',failing)
    assert run(tmp_path,model=model)['status']=='save_failed' and calls==[]
    def only_reserve(path,value):
        if value['status']!='reserved': raise OSError('disk')
        return write(path,value)
    monkeypatch.setattr(observer,'atomic_write_json',only_reserve)
    assert run(tmp_path,model=model)['status']=='save_failed'
    assert run(tmp_path,model=model)['previous_status']=='reserved' and len(calls)==1


def test_corrupt_receipt_is_not_reset(tmp_path):
    setup_data(tmp_path);p=tmp_path/observer.OUTPUT_RELATIVE/DAY/'1000.json';p.parent.mkdir(parents=True);p.write_text('{broken')
    assert run(tmp_path)['reason']=='existing_receipt_unreadable'
    assert p.read_text()=='{broken'


def test_busy_observer_does_not_call_model(tmp_path):
    setup_data(tmp_path)
    with interprocess_file_lock(tmp_path/observer.OUTPUT_RELATIVE/DAY/'observer.lock'):
        assert run(tmp_path)['status']=='busy'


def test_model_adapter_enforces_caps_and_reuses_existing_prompt(monkeypatch):
    sent=[]
    monkeypatch.setattr(observer,'active_profile',lambda:'')
    monkeypatch.setattr(observer.llm,'load_llm_config',lambda **kwargs:{'model':'fake','max_tokens':'24000','timeout':'300'})
    def fake(system,prompt,cfg,diagnostics): sent.append((system,prompt,cfg));return RAW
    monkeypatch.setattr(observer.llm,'_call',fake)
    assert observer.call_default_model('screen').ok
    assert sent[0][0]==daytime_decide.SYSTEM_PROMPT
    assert sent[0][2]['max_tokens']=='2000' and sent[0][2]['timeout']=='120'
    monkeypatch.setattr(observer,'active_profile',lambda:'glm')
    assert observer.call_default_model('screen').error=='default_profile_required' and len(sent)==1


def test_slots_are_bounded_and_do_not_roll_into_next_day(tmp_path):
    setup_data(tmp_path)
    for hour,minute in [(9,30),(10,5),(12,0),(15,0)]:
        result=observer.run_observation(tmp_path,enable=True,authorized_date=DAY,now=NOW.replace(hour=hour,minute=minute))
        assert result['status']=='skipped' and result['calls_this_run']==0
    assert observer.SLOTS==('1000','1100','1330','1430')


@pytest.mark.parametrize('price',['-10','0','nan','inf'])
def test_invalid_price_is_not_sent_to_model(tmp_path,price):
    setup_data(tmp_path)
    p=tmp_path/'data/snapshots/2026-10-08/1000.csv'
    p.write_text(p.read_text().replace('600000,10,','600000,'+price+','))
    assert run(tmp_path)['reason']=='no_fresh_quote_evidence'


def test_unconfigured_model_is_zero_requests(tmp_path,monkeypatch):
    setup_data(tmp_path)
    monkeypatch.setattr(observer.llm,'load_llm_config',lambda **kwargs:None)
    result=run(tmp_path)
    assert result['status']=='model_failed' and result['calls_this_run']==0
    assert result['model_completed_at']=='' and result['recorded_at']


def test_filtered_quote_coverage_stays_visible_to_model(tmp_path):
    setup_data(tmp_path)
    p=tmp_path/'data/snapshots/2026-10-08/1000.csv'
    with p.open('a') as f: f.write('600001,-10,2026-10-08T10:00:00+08:00,1\n')
    seen=[]
    def model(prompt): seen.append(prompt);return daytime_decide.ModelCall(text=RAW,ok=True)
    result=run(tmp_path,model=model)
    assert result['input_rows']==2 and result['fresh_rows']==1
    assert '有效报价 1/2' in seen[0]


def test_actual_atomic_writer_flushes_and_preserves_claim_on_replace_failure(tmp_path,monkeypatch):
    from quantdesk import shared
    setup_data(tmp_path);replaced=[];flushed=[];calls=[]
    replace=shared.os.replace;fsync=shared.os.fsync
    def observed_fsync(fd): flushed.append(fd);return fsync(fd)
    def fail_final_replace(source,target):
        replaced.append(str(target))
        if len(replaced)==2: raise OSError('final_replace_failed')
        return replace(source,target)
    monkeypatch.setattr(shared.os,'fsync',observed_fsync)
    monkeypatch.setattr(shared.os,'replace',fail_final_replace)
    def model(prompt):
        assert len(flushed)>=2  # lock initialization and flushed reservation
        path=tmp_path/observer.OUTPUT_RELATIVE/DAY/'1000.json'
        assert json.loads(path.read_bytes())['status']=='reserved'
        calls.append(prompt);return daytime_decide.ModelCall(text=RAW,ok=True)
    assert run(tmp_path,model=model)['status']=='save_failed'
    assert run(tmp_path,model=model)['previous_status']=='reserved' and len(calls)==1


def test_readable_observation_page_keeps_unknown_slots_unknown(tmp_path):
    setup_data(tmp_path)
    run(tmp_path,model=lambda prompt:daytime_decide.ModelCall(text=RAW,model='fake',ok=True))
    page=observer.write_daily_summary(tmp_path,DAY).read_text(encoding='utf-8')
    assert '已保存模型原话，未核验' in page and '不进入成交队列' in page
    assert page.count('尚无本次记录，不能认定已运行。')==3
    assert RAW in page and 'fake' in page
    p=tmp_path/observer.OUTPUT_RELATIVE/DAY/'1100.json'
    p.write_text(json.dumps({'schema_version':observer.SCHEMA,'status':'reserved','calls_this_run':0}))
    page=observer.write_daily_summary(tmp_path,DAY).read_text(encoding='utf-8')
    assert '请求尝试：未知（已预留1次，不自动重试）' in page


class InputClock:
    def __init__(self, start=NOW, on_sleep=None):
        self.current = start
        self.elapsed = 0.0
        self.on_sleep = on_sleep

    def sleep(self, seconds):
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(self)


def wait_run(root, clock, **kwargs):
    return observer.run_observation(root, enable=True, authorized_date=DAY,
        now=clock.current, clock=lambda: clock.current, monotonic=lambda: clock.elapsed,
        sleep=clock.sleep, wait_seconds=observer.INPUT_WAIT_SECONDS, **kwargs)


def test_waits_for_delayed_sync_and_rechecks_actual_quote_time(tmp_path):
    protected = setup_data(tmp_path)
    path = tmp_path/'data/snapshots/2026-10-08/1000.csv'
    raw = path.read_text().replace('10:00:00', '10:00:30')
    path.unlink()
    calls = []
    def arrive(clock):
        if clock.elapsed >= 155 and not path.exists():
            path.write_text(raw)
    clock = InputClock(on_sleep=arrive)
    def model(prompt):
        calls.append(prompt)
        assert clock.elapsed == 155
        return daytime_decide.ModelCall(text=RAW, model='fake', ok=True)
    result = wait_run(tmp_path, clock, model=model)
    assert result['status'] == 'recorded_unverified' and len(calls) == 1
    assert result['input_wait_seconds'] == 155 and result['input_read_count'] == 32
    assert result['input_checked_at'].startswith('2026-10-08T10:02:55')
    assert result['input_slot'] == '1000' and result['legacy_cache_used'] is False
    for rel, value in protected.items():
        if rel.startswith('state/'):
            assert (tmp_path/rel).read_bytes() == value
    assert wait_run(tmp_path, clock, model=model)['status'] == 'already_attempted'
    assert len(calls) == 1


def test_unknown_current_slot_can_use_previous_original_fresh_quote(tmp_path):
    setup_data(tmp_path, quote_time='10:00:00')
    previous = tmp_path/'data/snapshots/2026-10-08/0955.csv'
    previous.write_text('symbol,price,quote_date,quote_time\n600000,10,2026-10-08,09:55:30\n')
    result = run(tmp_path, model=lambda _: daytime_decide.ModelCall(text=RAW, ok=True))
    assert result['status'] == 'recorded_unverified' and result['input_slot'] == '0955'
    assert result['source_path'].endswith('0955.csv')
    assert result['quote_time_min'].startswith('2026-10-08T09:55:30')
    assert result['input_checks'][0]['fresh_rows'] == 0


def test_legacy_cache_synthesized_clock_is_not_quote_evidence(tmp_path):
    setup_data(tmp_path)
    (tmp_path/'data/snapshots/2026-10-08/1000.csv').unlink()
    cache = tmp_path/'data/cache/spot_snapshot_latest.csv'
    cache.parent.mkdir(parents=True)
    cache.write_text('symbol,price,quote_date,quote_time,source\n600000,10,2026-10-08,2026-10-08 10:00:20,sina.spot_direct\n')
    def forbidden(_): raise AssertionError('legacy cache used')
    clock = InputClock()
    result = wait_run(tmp_path, clock, model=forbidden)
    assert result['status'] == 'skipped' and result['calls_this_run'] == 0
    assert result['input_wait_seconds'] == observer.INPUT_WAIT_SECONDS
    assert result['legacy_cache_used'] is False


@pytest.mark.parametrize('quote', ['10:00:12', '2026-10-07T10:00:00+08:00',
                                   '2026-10-08T10:04:00+08:00'])
def test_wait_never_fills_quote_date_or_relaxes_freshness(tmp_path, quote):
    setup_data(tmp_path, quote_time=quote)
    path = tmp_path/'data/snapshots/2026-10-08/1000.csv'
    original = path.read_bytes()
    def forbidden(_): raise AssertionError('invalid quote admitted')
    result = wait_run(tmp_path, InputClock(), model=forbidden)
    assert result['status'] == 'skipped' and result['fresh_rows'] == 0
    assert result['input_checks'][0]['rejected_reasons']
    assert path.read_bytes() == original


def test_wait_stops_at_original_slot_boundary(tmp_path):
    setup_data(tmp_path, quote_time='')
    clock = InputClock(start=NOW.replace(minute=4, second=50))
    result = wait_run(tmp_path, clock, model=lambda _: pytest.fail('window extended'))
    assert result['status'] == 'skipped' and clock.elapsed == 10
    assert clock.current.minute == 5 and result['calls_this_run'] == 0


def test_wait_keeps_lock_and_preserves_morning_skip(tmp_path):
    setup_data(tmp_path, quote_time='')
    reentry = []
    clock = InputClock(on_sleep=lambda c: reentry.append(run(tmp_path)['status']))
    first = wait_run(tmp_path, clock, model=lambda _: pytest.fail('model called'))
    assert first['status'] == 'skipped' and set(reentry) == {'busy'}
    receipt = tmp_path/observer.OUTPUT_RELATIVE/DAY/'1000.json'
    original = receipt.read_bytes()
    (tmp_path/'data/snapshots/2026-10-08/1000.csv').write_text(
        'symbol,price,quote_time\n600000,10,2026-10-08T10:03:00+08:00\n')
    assert wait_run(tmp_path, clock)['status'] == 'already_attempted'
    assert receipt.read_bytes() == original


@pytest.mark.parametrize('at,reason', [
    (NOW.replace(minute=5, second=0), 'observation_window_elapsed'),
    (NOW.replace(second=40), 'quote_evidence_expired_before_call'),
])
def test_rechecks_window_and_age_immediately_before_request(tmp_path, at, reason):
    setup_data(tmp_path, quote_time='2026-10-08T09:55:30+08:00')
    result = observer.run_observation(tmp_path, enable=True, authorized_date=DAY,
        now=NOW, clock=lambda: at, model=lambda _: pytest.fail('late request'))
    assert result['status'] == 'skipped' and result['reason'] == reason
    assert result['calls_this_run'] == 0


HQ_NOW = NOW.replace(hour=14, minute=30, second=2)


def setup_hq(root, symbols=('600000', '000001', '920001')):
    setup_data(root)
    path = root/'data/universe/a_share_symbols.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('symbol,name\n' + ''.join(f'{symbol},合成样本\n' for symbol in symbols), encoding='utf-8')
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def hq_text(key, *, day=DAY, stamp='14:30:00', price='10'):
    parts = ['合成样本', '10', '9', price, '11', '8', '0', '0', '1000', '10000'] + ['0']*20 + [day, stamp, '00']
    return f'var hq_str_{key}="' + ','.join(parts) + '";'


def hq_run(root, clock=None, **kwargs):
    clock = clock or InputClock(start=HQ_NOW)
    return observer.run_observation(root, enable=True, authorized_date=DAY,
        now=clock.current, clock=lambda: clock.current, monotonic=lambda: clock.elapsed,
        sleep=clock.sleep, enable_local_quotes=True, **kwargs)


def test_local_quotes_unblock_observation_without_touching_formal_data(tmp_path):
    before = setup_hq(tmp_path)
    fetched = []; called = []
    def fetch(keys, *, timeout):
        claim = json.loads((tmp_path/observer.OUTPUT_RELATIVE/DAY/'1430.json').read_bytes())
        assert claim['status'] == 'input_reserved' and claim['quote_requests_reserved'] == 1
        assert claim['quote_request']['requested_symbols'] == list(keys)
        assert claim['quote_request']['timeout_seconds'] == timeout
        fetched.append((list(keys), timeout))
        return '\n'.join(hq_text(key) for key in keys)
    def model(prompt):
        called.append(prompt)
        return daytime_decide.ModelCall(text=RAW, model='fake', ok=True)
    result = hq_run(tmp_path, quote_fetcher=fetch, model=model)
    assert result['status'] == 'recorded_unverified' and len(called) == 1
    assert fetched == [(['sz000001', 'sh600000', 'bj920001'], 5.0)]
    assert result['source'] == 'sina.hq_observation' and result['fresh_rows'] == 3
    assert result['quote_time_min'].startswith('2026-10-08T14:30:00')
    evidence = json.loads(Path(result['source_path']).read_bytes())
    assert evidence['batches'][0]['response_text'] == '\n'.join(hq_text(key) for key in fetched[0][0])
    assert evidence['rows'][0]['quote_date'] == DAY and evidence['rows'][0]['quote_time'] == '14:30:00'
    assert evidence['rows'][0]['received_at'].startswith('2026-10-08T14:30:02')
    assert '有效报价 3/3' in called[0]
    assert hq_run(tmp_path, quote_fetcher=fetch, model=model)['status'] == 'already_attempted'
    assert len(fetched) == 1 and len(called) == 1
    assert '上一槽(-)→本槽 5 分钟涨幅前: 无' in called[0]
    assert '没有已核验的相邻5分钟对照' in called[0]
    for rel, raw in before.items():
        assert (tmp_path/rel).read_bytes() == raw


@pytest.mark.parametrize('day,stamp,price', [('', '14:30:00', '10'),
    ('2026-10-07', '14:30:00', '10'), (DAY, '14:31:00', '10'),
    (DAY, '14:20:00', '10'), (DAY, '', '10'), (DAY, '14:30:00', 'nan')])
def test_direct_quote_missing_old_future_or_bad_price_still_blocks(tmp_path, day, stamp, price):
    setup_hq(tmp_path, ('600000',))
    result = hq_run(tmp_path, quote_fetcher=lambda keys, **kw: hq_text(keys[0], day=day, stamp=stamp, price=price),
                    model=lambda _: pytest.fail('untrustworthy quote reached model'))
    assert result['status'] == 'skipped' and result['calls_this_run'] == 0
    assert result['fresh_rows'] == 0 and result['local_quote_input']['rejected_reasons']


def test_direct_reply_identity_and_duplicate_rows_are_not_order_dependent(tmp_path):
    setup_hq(tmp_path, ('600000', '000001'))
    reply = '\n'.join([hq_text('sh600000'), hq_text('sh600000', price='99'),
                       hq_text('bj600000'), hq_text('sz000001')])
    result = hq_run(tmp_path, quote_fetcher=lambda *a, **kw: reply,
                    model=lambda _: daytime_decide.ModelCall(text=RAW, ok=True))
    assert result['fresh_rows'] == 1 and result['input_rows'] == 2
    evidence = json.loads(Path(result['source_path']).read_bytes())
    assert [row['symbol'] for row in evidence['rows']] == ['000001']
    assert evidence['batches'][0]['rejected_reasons'] == {'duplicate_identity': 2, 'unrequested_identity': 1}


def test_direct_source_is_not_used_when_disabled_wrong_slot_or_snapshot_ready(tmp_path):
    setup_hq(tmp_path)
    def forbidden(*a, **k): pytest.fail('unnecessary quote request')
    assert observer.run_observation(tmp_path, enable=True, authorized_date=DAY, now=HQ_NOW,
        clock=lambda: HQ_NOW, quote_fetcher=forbidden)['status'] == 'skipped'
    assert observer.run_observation(tmp_path, enable=True, authorized_date=DAY, now=NOW,
        clock=lambda: NOW, enable_local_quotes=True, quote_fetcher=forbidden,
        model=lambda _: daytime_decide.ModelCall(text=RAW, ok=True))['status'] == 'recorded_unverified'


def test_direct_quote_failure_is_reserved_and_never_retried(tmp_path):
    setup_hq(tmp_path); calls = []
    def broken(keys, **kw): calls.append(keys); raise TimeoutError('synthetic')
    first = hq_run(tmp_path, quote_fetcher=broken, model=lambda _: pytest.fail('no quote'))
    assert first['status'] == 'skipped' and len(calls) == 1
    assert hq_run(tmp_path, quote_fetcher=broken)['status'] == 'already_attempted'
    assert len(calls) == 1


def test_quote_claim_must_be_durable_before_any_request(tmp_path, monkeypatch):
    setup_hq(tmp_path)
    def failed(*a, **k): raise OSError('synthetic disk failure')
    monkeypatch.setattr(observer, 'atomic_write_json', failed)
    assert hq_run(tmp_path, quote_fetcher=lambda *a, **kw: pytest.fail('request before claim'))['status'] == 'save_failed'


def test_interrupted_quote_request_consumes_slot(tmp_path):
    setup_hq(tmp_path); called = []
    def interrupted(keys, **kw): called.append(keys); raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        hq_run(tmp_path, quote_fetcher=interrupted)
    again = hq_run(tmp_path, quote_fetcher=interrupted)
    assert again['status'] == 'already_attempted' and again['previous_status'] == 'input_reserved'
    assert len(called) == 1


def test_original_quote_save_failure_prevents_next_batch_and_model(tmp_path, monkeypatch):
    setup_hq(tmp_path, tuple(f'{600000+i}' for i in range(501)))
    write = observer.atomic_write_json; calls = []
    def fail_evidence(path, value):
        if path.name.endswith('.quotes.json'):
            raise OSError('synthetic evidence save failure')
        return write(path, value)
    monkeypatch.setattr(observer, 'atomic_write_json', fail_evidence)
    def fetch(keys, **kw): calls.append(keys); return hq_text(keys[0])
    assert hq_run(tmp_path, quote_fetcher=fetch, model=lambda _: pytest.fail('unsaved input'))['status'] == 'save_failed'
    assert len(calls) == 1
    assert hq_run(tmp_path, quote_fetcher=fetch)['previous_status'] == 'input_reserved'
    assert len(calls) == 1


def test_date_changes_during_input_wait_never_enable_next_day_network(tmp_path):
    setup_hq(tmp_path)
    clock = InputClock(start=HQ_NOW)
    clock.on_sleep = lambda c: setattr(c, 'current', HQ_NOW + timedelta(days=1))
    result = hq_run(tmp_path, clock, wait_seconds=165,
        quote_fetcher=lambda *a, **kw: pytest.fail('next-day quote request'))
    assert result['status'] == 'skipped'


def test_direct_quote_budget_limits_batches_and_leaves_missing_visible(tmp_path):
    setup_hq(tmp_path, tuple(f'{600000+i}' for i in range(6000)))
    calls = []; clock = InputClock(start=HQ_NOW)
    def fetch(keys, *, timeout):
        calls.append((len(keys), timeout)); clock.sleep(5)
        return hq_text(keys[0])
    result = hq_run(tmp_path, clock, quote_fetcher=fetch,
                    model=lambda _: daytime_decide.ModelCall(text=RAW, ok=True))
    assert len(calls) == 12 and all(size == 500 and limit <= 5 for size, limit in calls)
    assert result['input_rows'] == 6000 and result['fresh_rows'] == 12
    assert result['local_quote_input']['missing_rows'] == 5988


def test_oversized_full_market_list_is_not_silently_truncated(tmp_path):
    setup_hq(tmp_path, tuple(f'{600000+i}' for i in range(6001)))
    result = hq_run(tmp_path, quote_fetcher=lambda *a, **kw: pytest.fail('scope exceeded'))
    assert result['status'] == 'skipped' and result['local_quote_input']['reason'] == 'quote_symbol_cap_exceeded'


def test_slow_direct_reply_does_not_start_model_outside_window(tmp_path):
    setup_hq(tmp_path); clock = InputClock(start=HQ_NOW)
    def fetch(keys, **kw): clock.sleep(300); return hq_text(keys[0])
    result = hq_run(tmp_path, clock, quote_fetcher=fetch, model=lambda _: pytest.fail('late model'))
    assert result['status'] == 'skipped' and result['calls_this_run'] == 0


def test_local_quote_transport_does_not_follow_redirects(monkeypatch):
    import urllib.request
    handlers = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): assert limit <= 1000001; return b'original'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://hq.sinajs.cn/list=sh600000'
            assert timeout == 5
            return Reply()
    def build(*values): handlers.extend(values); return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    assert observer._hq_read_once(['sh600000'], timeout=5) == 'original'
    assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://another.example') is None
