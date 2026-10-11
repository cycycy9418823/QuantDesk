"""T33: validate the entire requested universe before opening any CSV."""
from pathlib import Path
import json
from unittest.mock import Mock

import pandas as pd
import pytest
from flask import Flask
from scripts.server.routes import backtest as route


def write_frame(path, basis='raw', count=150):
    path.parent.mkdir(parents=True,exist_ok=True)
    frame=pd.DataFrame({'date':pd.date_range('2025-01-01',periods=count).strftime('%Y-%m-%d'),
                        'open':10.,'high':11.,'low':9.,'close':10.,'volume':10000.,'adjust':basis})
    frame.to_csv(path,index=False)
    return path


@pytest.fixture
def sandbox(monkeypatch,tmp_path):
    root=tmp_path/'project';root.mkdir()
    monkeypatch.setattr(route,'project_root',root)
    write_frame(root/'data/daily/600000.csv')
    write_frame(root/'data/benchmark/hs300.csv')
    reader=Mock(wraps=route.read_csv_frame)
    monkeypatch.setattr(route,'read_csv_frame',reader)
    def client(candidate=None):
        app=Flask(__name__);route.register_backtest_routes(app,candidate_rows_func=candidate)
        return app.test_client()
    return root,reader,client


@pytest.mark.parametrize('bad',['../benchmark/hs300',r'..\benchmark\hs300','/tmp/600000',r'C:\temp\600000','600000/../../benchmark/hs300','600000.SH','abc600000','６０００００',600000,False,None,{},[]])
@pytest.mark.parametrize('origin',['explicit','candidate','screening'])
def test_t33_untrusted_symbols_rejected_before_any_read(sandbox,bad,origin):
    root,reader,client=sandbox
    body={'max_symbols':5}
    candidate=None
    if origin=='explicit':body['symbols']=['600000',bad]
    elif origin=='candidate':candidate=lambda limit:[{'symbol':'600000'},{'symbol':bad}]
    else:
        path=root/'results/strategy_screening_funnel_latest.json';path.parent.mkdir()
        path.write_text(json.dumps({'backtest_handoff':{'status':'ready','symbols':['600000',bad]}}),encoding='utf-8')
        body['source']='strategy_screening'
    response=client(candidate).post('/api/backtest/run',json=body)
    assert response.status_code==400,response.get_json()
    assert response.get_json()['status']=='bad_backtest_symbols'
    reader.assert_not_called()
    assert not (root/'results/backtest/latest.json').exists()


@pytest.mark.parametrize('symbols',['600000',42,False,None,{}])
def test_t33_symbols_scalar_not_silently_fallback(sandbox,symbols):
    _,reader,client=sandbox
    response=client().post('/api/backtest/run',json={'symbols':symbols})
    assert response.status_code==400
    reader.assert_not_called()


@pytest.mark.parametrize('symbols',['600000',42,False,{},['600000','../benchmark/hs300']])
def test_t33_direct_loader_enforces_same_boundary(sandbox,symbols):
    _,reader,_=sandbox
    with pytest.raises(ValueError):route._load_backtest_histories(5,symbols=symbols)
    reader.assert_not_called()


@pytest.mark.parametrize('origin',['explicit','candidate','screening','padding'])
@pytest.mark.parametrize('basis',['raw','qfq'])
def test_t33_legal_histories_preserve_price_contract(sandbox,origin,basis):
    root,reader,client=sandbox
    write_frame(root/'data/daily/600000.csv',basis=basis)
    candidate=None;body={'max_symbols':5}
    if origin=='explicit':body['symbols']=['600000','600000']
    elif origin=='candidate':candidate=lambda limit:[{'symbol':'600000'},{'symbol':'600000'}]
    elif origin=='screening':
        path=root/'results/strategy_screening_funnel_latest.json';path.parent.mkdir()
        path.write_text(json.dumps({'backtest_handoff':{'status':'ready','symbols':['600000','600000']}}),encoding='utf-8')
        body['source']='strategy_screening'
    response=client(candidate).post('/api/backtest/run',json=body)
    assert response.status_code==200,response.get_json()
    payload=response.get_json()
    assert payload['ok'] is True and payload['symbols']==['600000']
    assert payload['price_policy_validation']['eligible'] is True
    assert payload['real_execution_price'] is False
    assert payload['execution_price_basis']==('raw' if basis=='raw' else 'forward_adjusted')
    assert reader.call_count==1
    assert Path(reader.call_args.args[0]).resolve()==(root/'data/daily/600000.csv').resolve()


@pytest.mark.parametrize('basis',['unknown','mixed'])
def test_t33_invalid_price_basis_still_blocks_whole_run(sandbox,basis):
    root,reader,client=sandbox
    path=write_frame(root/'data/daily/000001.csv',basis='unknown' if basis=='unknown' else 'raw')
    if basis=='mixed':
        frame=pd.read_csv(path);frame.loc[0,'adjust']='qfq';frame.to_csv(path,index=False)
    response=client().post('/api/backtest/run',json={'symbols':['600000','000001']})
    assert response.status_code==422
    payload=response.get_json();assert payload['status']=='blocked' and payload['symbol_count']==2
    assert reader.call_count==2
    assert not (root/'results/backtest/latest.json').exists()


def test_t33_missing_or_short_data_behavior_preserved(sandbox):
    root,_,client=sandbox
    write_frame(root/'data/daily/000001.csv',count=10)
    response=client().post('/api/backtest/run',json={'symbols':['000001','000002']})
    assert response.status_code==200
    assert response.get_json()['ok'] is False
    assert '本地日线不足' in response.get_json()['message_for_ui']


def test_t33_unrelated_value_error_not_reported_as_bad_symbol(sandbox,monkeypatch):
    _,_,client=sandbox
    monkeypatch.setattr(route,'_load_backtest_histories',Mock(side_effect=ValueError('unrelated failure')))
    response=client().post('/api/backtest/run',json={'symbols':['600000']})
    assert response.status_code==500
    assert 'unrelated failure' in response.get_json()['message_for_ui']


@pytest.mark.parametrize('callback_failure',[False,True])
def test_t33_empty_symbols_preserves_candidate_or_padding(sandbox,callback_failure):
    _,reader,client=sandbox
    candidate=Mock(side_effect=RuntimeError('provider unavailable')) if callback_failure else Mock(return_value=[{'symbol':'600000'}])
    response=client(candidate).post('/api/backtest/run',json={'symbols':[]})
    assert response.status_code==200 and response.get_json()['symbols']==['600000']
    assert reader.call_count==1
    candidate.assert_called_once()


@pytest.mark.parametrize('origin',['explicit','candidate','screening'])
def test_t33_symbol_limit_does_not_hide_invalid_identity(sandbox,origin):
    root,reader,client=sandbox
    symbols=['600000','../benchmark/hs300'];body={'max_symbols':1};candidate=None
    if origin=='explicit':body['symbols']=symbols
    elif origin=='candidate':candidate=lambda limit:[{'symbol':s} for s in symbols]
    else:
        path=root/'results/strategy_screening_funnel_latest.json';path.parent.mkdir()
        path.write_text(json.dumps({'backtest_handoff':{'status':'ready','symbols':symbols}}),encoding='utf-8')
        body['source']='strategy_screening'
    response=client(candidate).post('/api/backtest/run',json=body)
    assert response.status_code==400
    reader.assert_not_called()
