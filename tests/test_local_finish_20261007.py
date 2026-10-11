"""Owned closeout: same defect families through persisted and real entry paths."""
import json
from datetime import datetime

import pytest

from ai_trader import daytime_decide as decide, daytime_fill as fill, daytime_runner as runner
from ai_trader import dashboard
from test_ai_trader_daytime_fill import DAY, NOW, QUOTE_SLOT, QUOTE_TIME, _book, _intent, _paths, _row, _write_intents
from test_ai_trader_daytime_decide import FakeModel, GOOD, _screen


def _calendar(root):
    path = root / 'data/calendar/a_share_trading_calendar.csv'
    path.parent.mkdir(parents=True)
    path.write_text('date,is_open\n2026-09-22,1\n2026-09-23,1\n2026-10-07,0\n2026-10-08,1\n', encoding='utf-8')


def _quotes(rows=None):
    return fill.quote_index(rows or [_row()], quote_slot=QUOTE_SLOT, source='synthetic')


def test_later_slot_cannot_sell_same_day_addition():
    book = _book(positions=[{'symbol':'600000','quantity':100,'avg_cost':10,'acquired_on':'2026-09-19'}])
    planned = fill.plan_fills([_intent(quantity=200)], _quotes(), book, as_of=DAY)
    bought, _ = fill.apply_to_ledger(book, planned['fills'], as_of=DAY)
    assert bought['positions'][0]['quantity'] == 300
    sale = fill.plan_fills([_intent(side='sell', quantity=200)], _quotes(), bought, as_of=DAY)
    assert sale['fills'] == [] and sale['blocks'][0]['blocked_by'] == fill.BLOCK_T_PLUS_1
    good = fill.plan_fills([_intent(side='sell', quantity=100)], _quotes(), bought, as_of=DAY)
    after, applied = fill.apply_to_ledger(bought, good['fills'], as_of=DAY)
    assert len(applied) == 1 and after['positions'][0]['quantity'] == 200


def test_direct_apply_cannot_oversell_overnight_quantity():
    book = _book(positions=[{'symbol':'600000','quantity':100,'avg_cost':10,'acquired_on':'2026-09-19'}])
    _, applied = fill.apply_to_ledger(book, [{'symbol':'600000','quantity':200,'price':10,'side':'sell'}], as_of=DAY)
    assert applied == []


def test_cash_chronology_keeps_legitimate_earlier_sale():
    book = _book(cash=15000,equity=100000,positions=[{'symbol':'600000','quantity':200,'avg_cost':10,'acquired_on':'2026-09-19'}])
    sale = _intent(side='sell',quantity=200)
    buy = _intent(symbol='600001',quantity=100,created_at=f'{DAY}T09:41:00+08:00',slot='0941')
    q = _quotes([_row(),_row(symbol='600001')])
    for sequence in [[sale,buy],[buy,sale]]:
        plan = fill.plan_fills(sequence,q,book,as_of=DAY)
        assert [x['side'] for x in plan['fills']] == ['sell','buy']


def test_later_sale_quote_cannot_fund_earlier_buy_quote():
    book = _book(cash=15000,equity=100000,positions=[{'symbol':'600000','quantity':200,'avg_cost':10,'acquired_on':'2026-09-19'}])
    sale = _intent(side='sell',quantity=200)
    buy = _intent(symbol='600001',quantity=100,created_at=f'{DAY}T09:41:00+08:00',slot='0941')
    q = _quotes([_row(quote_time=f'{DAY}T09:45:05+08:00'),
                 _row(symbol='600001',quote_time=f'{DAY}T09:44:05+08:00')])
    for sequence in [[sale,buy],[buy,sale]]:
        plan = fill.plan_fills(sequence,q,book,as_of=DAY)
        assert [x['side'] for x in plan['fills']] == ['sell']


def test_restore_torn_receipt_retains_original_evidence_and_ledger(tmp_path, monkeypatch):
    paths = _paths(tmp_path); _write_intents(paths, [_intent()])
    append = fill._append_jsonl
    def torn(path, records):
        if path.name == fill.FILLS_NAME and records:
            path.write_text('{"broken":',encoding='utf-8')
            raise OSError('partial_write')
        return append(path, records)
    monkeypatch.setattr(fill,'_append_jsonl',torn)
    first = fill.fill_slot(tmp_path,paths=paths,ledger=_book(),now=NOW,rows=[_row()],quote_slot=QUOTE_SLOT,write=True,dry=False)
    assert first['ledger_written'] and first['recovery_pending'] and not first['written']
    before = paths.ledger.read_bytes()
    monkeypatch.setattr(fill,'_append_jsonl',append)
    for _ in range(2):
        fill.fill_slot(tmp_path,paths=paths,now=NOW,rows=[_row(price=99)],quote_slot=QUOTE_SLOT,write=True,dry=False)
        assert paths.ledger.read_bytes() == before
    rows = fill.load_daytime_fills_for(DAY,state_dir=paths.ledger.parent)
    original = json.loads(before)['trades'][0]['daytime_fill_record']
    assert rows == [original] and rows[0]['price'] == 10 and rows[0]['quote_time'] == QUOTE_TIME


@pytest.mark.parametrize('fields', [
    {'quote_time':'','fetched_at':QUOTE_TIME},
    {'quote_time':'garbage','fetched_at':QUOTE_TIME},
    {'quote_time':QUOTE_TIME,'quote_date':'2026-09-21'},
])
def test_receipt_time_cannot_replace_absent_or_bad_quote(fields):
    assert fill.plan_fills([_intent()],_quotes([_row(**fields)]),_book(),as_of=DAY)['fills'] == []


def test_bad_model_completion_time_cannot_be_ignored():
    assert fill.plan_fills([_intent(model_completed_at='bad')],_quotes(),_book(),as_of=DAY)['fills'] == []


def test_after_model_completion_quote_still_fills(tmp_path,monkeypatch):
    monkeypatch.setenv(decide.DAYTIME_ENV,'1')
    clock = iter([datetime(2026,9,22,9,44),datetime(2026,9,22,9,44,30)])
    result=decide.decide_slot(tmp_path,paths=_paths(tmp_path),now=datetime(2026,9,22,9,43),clock=lambda:next(clock),llm=FakeModel(GOOD),screen=_screen(),screen_text='synthetic')
    assert result['intents'][0]['model_completed_at'].startswith(f'{DAY}T09:44:00')
    assert len(fill.plan_fills(result['intents'],_quotes(),_book(),as_of=DAY)['fills']) == 1


def test_no_model_call_has_no_completion_time(tmp_path,monkeypatch):
    monkeypatch.setenv(decide.DAYTIME_ENV,'1')
    result=decide.decide_slot(tmp_path,paths=_paths(tmp_path),now=NOW,clock=lambda:NOW,llm=FakeModel(),screen={'slot':''},screen_text='synthetic')
    assert result['calls'] == [] and result['model_completed_at'] == ''


@pytest.mark.parametrize('kind',['normal','no_quotes','future','model_failure','holiday'])
def test_first_day_requires_real_observation(tmp_path,monkeypatch,kind):
    _calendar(tmp_path);paths=_paths(tmp_path)
    monkeypatch.setenv(decide.DAYTIME_ENV,'1')
    rows = [] if kind=='no_quotes' else [_row(quote_time=f'{DAY}T15:00:00+08:00')] if kind=='future' else [_row()]
    current = datetime(2026,10,7,9,45,10) if kind=='holiday' else NOW
    model = FakeModel(error='failed') if kind=='model_failure' else FakeModel('{"orders":[],"want_minutes":[]}')
    result=runner.run_slot(tmp_path,paths=paths,now=current,clock=lambda:current,slot=QUOTE_SLOT,rows=rows,llm=model,write=True)
    if kind=='normal':
        assert result['dry_state']['observation_status']=='verified'
        assert runner.is_first_trading_day(paths,as_of=DAY,root=tmp_path)
        assert not runner.is_first_trading_day(paths,as_of='2026-09-23',root=tmp_path)
    else:
        assert result['dry_state']['observation_status']=='not_completed'
        assert runner.is_first_trading_day(paths,as_of='2026-10-08',root=tmp_path)


def test_old_unverified_dry_state_does_not_unlock_and_can_recover(tmp_path):
    _calendar(tmp_path);paths=_paths(tmp_path)
    runner.dry_state_path(paths).write_text('{"completed_dry_day":"2026-10-07"}',encoding='utf-8')
    assert runner.is_first_trading_day(paths,as_of='2026-10-08',root=tmp_path)
    state=runner.mark_dry_day_observed(paths,as_of='2026-10-08',root=tmp_path,observation={'status':'decided','written':True,'valid_quotes':1})
    assert state['completed_dry_day']=='2026-10-08'


def test_recovery_metadata_follows_chosen_model_record(tmp_path):
    state=tmp_path/'state';results=tmp_path/'results';state.mkdir();results.mkdir()
    (state/'journal_latest.json').write_text(json.dumps({'llm':{'used':True,'model':'fake','recovery_status':'original','raw_response':{'reused':True}}}),encoding='utf-8')
    result=dashboard.build_dashboard_payload(state_dir=state,results_dir=results)
    assert result['llm']['model_source']=='journal_latest'
    assert result['research_state']['llm']['recovery_status']=='original'
    assert result['llm']['raw_response_reused'] is True
    assert result['research_state']['book']['cash'] is None
    assert result['research_state']['records']['total'] is None
    assert 'not_applied' not in result['research_state']['markers']


def test_empty_decision_object_is_missing_evidence(tmp_path):
    (tmp_path/'decision_latest.json').write_text('{}',encoding='utf-8')
    result=dashboard.build_dashboard_payload(state_dir=tmp_path,results_dir=tmp_path)
    assert result['research_state']['records']['total'] is None
    assert result['research_state']['markers']['missing_data']['present']


@pytest.mark.parametrize('available', [False, True])
def test_dashboard_distinguishes_missing_from_empty_accounts(tmp_path, available):
    if available:
        for name, value in {
            'ledger.json': {'cash': 100, 'positions': [], 'trades': []},
            'plan_latest.json': {'committed': False, 'intents': []},
            'journal_latest.json': {'actions': []},
            'philosophy.json': {'revisions': [], 'style_ledger': {}},
        }.items():
            (tmp_path/name).write_text(json.dumps(value), encoding='utf-8')
    result = dashboard.build_dashboard_payload(state_dir=tmp_path, results_dir=tmp_path)
    assert result['source_availability'] == {
        'positions': available, 'trades': available, 'actions': available,
        'plan': available, 'philosophy': available,
    }
    assert result['account']['positions_count'] == (0 if available else None)
