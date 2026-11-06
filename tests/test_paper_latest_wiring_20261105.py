"""Latest-account queries preserve warnings without depending on execution."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quantdesk.paper.queries import portfolio as queries
from quantdesk.shared import read_json_strict


def put(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def state(amount: int = 100) -> dict:
    return {"ledger_id":"fixture-a","initial_cash":100,"cash":amount,"market_value":0,
            "total_equity":amount,"positions":[],"valuation_date":"2026-11-04"}


def populate(root: Path, amount: int = 100) -> None:
    paths=queries.PaperQueryPaths.from_root(root)
    put(paths.state,state(amount))
    put(paths.latest,{**state(amount),"ok":True})
    put(root/queries.STORE_PATH/'latest.json',{'plan_review_summary':{'status':'fixture-passed'},'plan_review_items':[]})


@pytest.mark.parametrize("raw,reason", [(None,'missing'),(b'','empty'),(b'  ','empty'),
    (b'{bad','invalid_json'),(b'[]','invalid_object'),(b'{}','invalid_object'),
    (b'null','invalid_object'),(b'\xff','unreadable'),(b'\xef\xbb\xbf{"ok":true}','')])
def test_shared_strict_reader_retains_legacy_results(tmp_path,raw,reason):
    from quantdesk.daily_candidate_refresh import _read_json_strict
    assert _read_json_strict is read_json_strict
    path=tmp_path/'record.json'
    if raw is not None:path.write_bytes(raw)
    value,error=read_json_strict(path)
    assert error==reason
    assert value==({'ok':True} if not reason else None)
    assert path.read_bytes()==raw if raw is not None else not path.exists()


def test_strict_reader_maps_io_errors_without_writing(tmp_path,monkeypatch):
    path=tmp_path/'record.json';path.write_text('{}',encoding='utf-8')
    def denied(*a,**kw):raise PermissionError('fixture')
    monkeypatch.setattr(Path,'read_text',denied)
    assert read_json_strict(path)==(None,'unreadable')


def run_child(code: str, root: Path) -> None:
    result=subprocess.run([sys.executable,'-B','-X','utf8','-c',code,str(root)],
                          capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stdout+result.stderr


def test_actual_latest_query_runs_without_execution_imports_or_effects(tmp_path):
    populate(tmp_path/'populated')
    run_child(r'''
import importlib.abc,os,sys
from pathlib import Path
blocked={'quantdesk.daily_candidate_refresh','quantdesk.paper.auto_paper_portfolio',
 'quantdesk.auto_paper_portfolio','quantdesk.paper.position_management_engine','ai_trader.trader'}
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in blocked:raise AssertionError('execution import:'+fullname)
sys.meta_path.insert(0,Block())
from quantdesk.paper.queries import portfolio as q
violations=[]
def fence(event,args):
 denied=event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system','os.mkdir','os.remove','os.rename','os.rmdir','os.utime','os.chmod'}
 if event=='open':
  _,mode,flags=args
  denied=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
 if denied:violations.append(event);raise AssertionError('effect:'+event)
sys.addaudithook(fence)
root=Path(sys.argv[1])
missing=q.auto_paper_portfolio_latest(q.PaperQueryPaths.from_root(root/'missing'),disclaimer=q.DISCLAIMER,broker_boundary=q.BROKER_BOUNDARY)
actual=q.auto_paper_portfolio_latest(q.PaperQueryPaths.from_root(root/'populated'),disclaimer=q.DISCLAIMER,broker_boundary=q.BROKER_BOUNDARY)
assert missing['exists'] is False and missing['plan_review_summary']['status']=='missing'
assert actual['ledger_health']['ok'] and actual['plan_review_summary']['status']=='fixture-passed'
assert not violations and not blocked.intersection(sys.modules)
''',tmp_path)


@pytest.fixture
def api(tmp_path,monkeypatch):
    from scripts import quant_api_server as server
    from scripts.server.routes import decision_board
    from quantdesk import evidence_freshness
    from quantdesk.ui_read_cache import UI_FILE_CACHE
    paths=queries.PaperQueryPaths.from_root(tmp_path)
    server.UI_READ_CACHE.clear();UI_FILE_CACHE.clear()
    for name,value in {'project_root':tmp_path,'AUTO_PAPER_PORTFOLIO_LATEST_JSON':paths.latest,
        'AUTO_PAPER_PORTFOLIO_STATE_JSON':paths.state,'AUTO_PAPER_PORTFOLIO_HISTORY_JSON':paths.history,
        'PAPER_REVIEW_RESULTS_DIR':None,'PAPER_WATCHLIST_JSON':tmp_path/'state/watchlist.json'}.items():
        monkeypatch.setattr(server,name,value)
    monkeypatch.setattr(server,'load_paper_simulation_watchlist',lambda:{})
    monkeypatch.setattr(server,'latest_research_evidence_context',lambda:{})
    monkeypatch.setattr(server,'load_benchmark_series',lambda *a,**kw:{})
    monkeypatch.setattr(server,'ui_calendar_days',lambda:[])
    monkeypatch.setattr(server,'expected_settlement_date',lambda *a:date(2026,11,4))
    monkeypatch.setattr(decision_board,'evaluate_data_chain_consumer_gate',lambda:{'allowed':True})
    monkeypatch.setattr(evidence_freshness,'build_evidence_freshness',lambda *a:{'metrics_trustworthy':False})
    monkeypatch.setattr(decision_board,'build_decision_board_snapshot',lambda **kw:{'ok':True,'received':kw['portfolio_payload']})
    monkeypatch.setitem(server.app.config,'TESTING',True)
    monkeypatch.setitem(server.app.config,'UI_READ_CACHE_ENABLED',True)
    yield server
    server.UI_READ_CACHE.clear();UI_FILE_CACHE.clear()


def board(api):
    response=api.app.test_client().get('/api/decision-board?summary=true')
    assert response.status_code==200,response.get_json()
    return response.get_json()


def test_missing_latest_still_tracks_review_and_routes_share_callback(api,tmp_path,monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy
    monkeypatch.setattr(legacy,'auto_paper_portfolio_latest',lambda:pytest.fail('legacy accessor called'))
    first=board(api);assert first['received']['exists'] is False
    review=tmp_path/queries.STORE_PATH/'latest.json'
    put(review,{'plan_review_summary':{'status':'missing-fixture','run_id':'a'},'plan_review_items':[]})
    second=board(api)
    assert second['received']['plan_review_summary']['run_id']=='a'
    assert first['read_identity']['sha256']!=second['read_identity']['sha256']
    stat=review.stat();put(review,{'plan_review_summary':{'status':'missing-fixture','run_id':'b'},'plan_review_items':[]})
    os.utime(review,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    third=board(api);assert third['received']['plan_review_summary']['run_id']=='b'
    assert third['read_identity']['sha256']!=second['read_identity']['sha256']
    performance=api.app.test_client().get('/api/performance/summary')
    assert performance.status_code==200
    assert performance.get_json()['paper_portfolio']==api.clean_payload(api.auto_paper_portfolio_latest())
    monkeypatch.setattr(api,'auto_paper_portfolio_latest',lambda:{'exists':False,'fixture_marker':'replacement'})
    assert board(api)['received']['fixture_marker']=='replacement'
    assert api.app.test_client().get('/api/performance/summary').get_json()['paper_portfolio']['fixture_marker']=='replacement'


def test_separate_latest_state_and_review_paths_drive_cache(api,tmp_path,monkeypatch):
    populate(tmp_path,900)
    latest=tmp_path/'display/latest.json';ledger=tmp_path/'account/book.json';reviewroot=tmp_path/'review'
    put(latest,{**state(100),'ok':True});put(ledger,state(100))
    put(reviewroot/queries.STORE_PATH/'latest.json',{'plan_review_summary':{'status':'custom'},'plan_review_items':[]})
    monkeypatch.setattr(api,'AUTO_PAPER_PORTFOLIO_LATEST_JSON',latest)
    monkeypatch.setattr(api,'AUTO_PAPER_PORTFOLIO_STATE_JSON',ledger)
    monkeypatch.setattr(api,'PAPER_REVIEW_RESULTS_DIR',reviewroot/'results')
    first=board(api)
    assert first['received']['cash']==100 and first['received']['ledger_health']['ok']
    assert first['received']['plan_review_summary']['status']=='custom'
    with api.app.test_request_context('/api/decision-board'):
        files={p['path'] for p in api._ui_request_identity()['inputs']}
        assert str(latest) in files and str(ledger) in files
        assert str(reviewroot/queries.STORE_PATH/'latest.json') in files
    with api.app.test_request_context('/api/data-health/dashboard'):
        assert tmp_path/'state/auto_paper_portfolio.json' in api._ui_dependency_paths()
    stat=ledger.stat();put(ledger,state(200));os.utime(ledger,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=board(api)
    assert not second['received']['ledger_health']['ok']
    assert 'strategy_latest_cash_mismatch' in second['received']['ledger_health']['reason_codes']
    assert second['read_identity']['sha256']!=first['read_identity']['sha256']
    assert ledger.read_text(encoding='utf-8')==json.dumps(state(200))


def test_recovery_order_identity_and_backup_changes_invalidate_cache(api,tmp_path):
    paths=queries.PaperQueryPaths.from_root(tmp_path)
    put(paths.latest,{**state(),'ok':True})
    marker=paths.state.with_name(paths.state.stem+'.identity.json')
    a=paths.state.with_name(paths.state.stem+'.backup_a.json')
    b=paths.state.with_name(paths.state.stem+'.backup_b.json')
    put(marker,{'schema':'paper_state_identity_v1','identity':{'ledger_id':'fixture-a'}})
    put(a,state(100));put(b,state(200))
    os.utime(a,ns=(1000000000,1000000000));os.utime(b,ns=(2000000000,2000000000))
    first=board(api)
    assert first['received']['ledger_health']['recovery_source']==str(b)
    os.utime(a,ns=(3000000000,3000000000))
    second=board(api)
    assert second['received']['ledger_health']['recovery_source']==str(a)
    assert first['read_identity']['sha256']!=second['read_identity']['sha256']
    stamp=a.stat();put(a,state(300));os.utime(a,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    third=board(api)
    assert third['received']['ledger_health']['recovery_sha256']!=second['received']['ledger_health']['recovery_sha256']
    assert third['read_identity']['sha256']!=second['read_identity']['sha256']
    a.write_text('{bad',encoding='utf-8')
    assert board(api)['received']['ledger_health']['recovery_source']==str(b)
    b.unlink()
    assert board(api)['received']['ledger_health']['read_status']=='invalid'
    put(b,state(200))
    assert board(api)['received']['ledger_health']['recovery_source']==str(b)
    put(marker,{'schema':'paper_state_identity_v1','identity':{'ledger_id':'another'}})
    assert board(api)['received']['ledger_health']['read_status']=='invalid'
    assert not paths.state.exists()  # Queries never apply the available backup.


def test_quarantine_presence_changes_missing_state_diagnostic(api,tmp_path):
    paths=queries.PaperQueryPaths.from_root(tmp_path);put(paths.latest,{**state(),'ok':True})
    first=board(api);assert first['received']['ledger_health']['read_status']=='missing'
    bad=paths.state.with_name(paths.state.stem+'.bad_fixture.json');put(bad,{})
    second=board(api);assert second['received']['ledger_health']['read_status']=='invalid'
    assert first['read_identity']['sha256']!=second['read_identity']['sha256']
    put(bad,{'not_consumed':True})
    assert board(api)['read_identity']['sha256']==second['read_identity']['sha256']
    bad.unlink();assert board(api)['received']['ledger_health']['read_status']=='missing'


def test_viewer_remaps_current_default_and_external_latest_locations(tmp_path):
    populate(tmp_path/'one',100);populate(tmp_path/'two',200);populate(tmp_path/'external',300)
    run_child(r'''
from pathlib import Path
import sys
from scripts import quant_api_server as server
from scripts.run_research_viewer import remap_runtime_inputs
root=Path(sys.argv[1]);source=server.project_root
remap_runtime_inputs(source,root/'one',app=server.app)
assert server.auto_paper_portfolio_latest()['cash']==100
assert server.auto_paper_portfolio_latest()['ledger_health']['ok']
remap_runtime_inputs(root/'one',root/'two',app=server.app)
assert server.auto_paper_portfolio_latest()['cash']==200
server.AUTO_PAPER_PORTFOLIO_LATEST_JSON=root/'external/results/auto_paper_portfolio_latest.json'
server.AUTO_PAPER_PORTFOLIO_STATE_JSON=root/'external/state/auto_paper_portfolio.json'
server.PAPER_REVIEW_RESULTS_DIR=root/'external/results'
remap_runtime_inputs(root/'two',root/'one',app=server.app)
actual=server.auto_paper_portfolio_latest()
assert actual['cash']==300 and actual['ledger_health']['ok']
assert server._paper_latest_query_paths().review_root==root/'external'
''',tmp_path)
