"""The history GET and decision board share an independent, read-only query."""
from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from flask import Flask
import pytest

from quantdesk.paper.queries import portfolio as queries
from scripts.server.routes.paper_simulation import create_paper_simulation_blueprint


def write_history(path: Path, count: int = 3, amount: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"valuation_date": (date(2020, 1, 1) + timedelta(days=i)).isoformat(),
             "cash": amount + i, "market_value": 0, "total_equity": amount + i,
             "portfolio_review_valid": True} for i in range(count)]
    path.write_text(json.dumps({"items": rows}), encoding="utf-8")


def client_for(root: Path):
    app = Flask(__name__)
    app.register_blueprint(create_paper_simulation_blueprint(SimpleNamespace(
        load_equity_history=lambda **kw: queries.auto_paper_portfolio_equity_history(
            queries.PaperQueryPaths.from_root(root), **kw))))
    return app.test_client()


@pytest.mark.parametrize("limit,expected", [(None, 250), ("bad", 250), ("", 250),
    ("-1", 2), ("1", 2), ("3", 3), ("9999", 2000)])
def test_real_query_limit_and_input_bytes(tmp_path, limit, expected):
    path = queries.PaperQueryPaths.from_root(tmp_path).history
    write_history(path, count=2005)
    before = path.read_bytes()
    response = client_for(tmp_path).get("/api/paper-simulation/equity-history",
                                      query_string={} if limit is None else {"limit": limit})
    assert response.status_code == 200
    value = response.get_json()
    assert len(value["items"]) == expected
    assert value["latest"]["total_equity"] == 2104
    assert value["source_path"] == str(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw,status,warning", [(None, "missing", None), ("{bad", "missing", None),
    ('{"items": "wrong"}', "degraded", "paper_history_items_invalid"),
    ('{"items":[{"valuation_date":"2026-11-04","cash":100,"market_value":0,"total_equity":100},'
     '{"valuation_date":"2026-11-04","cash":200,"market_value":0,"total_equity":200}]}',
     "degraded", "paper_history_conflicting_dates")])
def test_missing_and_bad_history_keep_existing_diagnostics(tmp_path, raw, status, warning):
    path = queries.PaperQueryPaths.from_root(tmp_path).history
    if raw is not None:
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding="utf-8")
    response = client_for(tmp_path).get("/api/paper-simulation/equity-history")
    assert response.status_code == 200
    assert response.get_json()["status"] == status
    if warning:
        assert warning in response.get_json()["warnings"]
    assert path.read_text(encoding="utf-8") == raw if raw is not None else not path.exists()


def test_loader_errors_keep_explicit_http_failure():
    def fail(**kwargs):
        raise ValueError("fixture unreadable")
    app = Flask(__name__)
    app.register_blueprint(create_paper_simulation_blueprint(SimpleNamespace(load_equity_history=fail)))
    response = app.test_client().get("/api/paper-simulation/equity-history")
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "fixture unreadable",
        "message_for_ui": "净值历史读取失败：fixture unreadable", "research_only": True,
        "paper_only": True, "no_broker": True}


def run_child(code: str, *args: Path) -> None:
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_standalone_history_get_never_imports_execution_or_mutates(tmp_path):
    write_history(queries.PaperQueryPaths.from_root(tmp_path).history)
    run_child(r'''
import importlib.abc, os, sys
from pathlib import Path
blocked = {'quantdesk.paper.auto_paper_portfolio', 'quantdesk.auto_paper_portfolio',
    'quantdesk.paper.position_management_engine', 'quantdesk.daily_candidate_refresh',
    'quantdesk.paper.snapshot_open_fill', 'ai_trader.trader'}
class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked: raise AssertionError('execution import: ' + fullname)
sys.meta_path.insert(0, NoExecution())
from flask import Flask
from types import SimpleNamespace
from quantdesk.paper.queries.portfolio import PaperQueryPaths, auto_paper_portfolio_equity_history
from scripts.server.routes.paper_simulation import create_paper_simulation_blueprint
paths=PaperQueryPaths.from_root(Path(sys.argv[1]))
app=Flask('independent_history')
app.register_blueprint(create_paper_simulation_blueprint(SimpleNamespace(
    load_equity_history=lambda **kw:auto_paper_portfolio_equity_history(paths,**kw))))
client=app.test_client()
violations=[]
def fence(event,args):
    denied=event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system',
                    'os.mkdir','os.rename','os.remove','os.rmdir','os.utime','os.chmod'}
    if event=='open':
        _,mode,flags=args
        denied=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
    if denied:
        violations.append(event);raise AssertionError('unexpected effect: '+event)
sys.addaudithook(fence)
response=client.get('/api/paper-simulation/equity-history')
assert response.status_code==200, response.get_json()
assert response.get_json()['latest']['total_equity']==102
assert client.post('/api/paper-simulation/equity-history').status_code==405
assert not violations and not blocked.intersection(sys.modules)
''', tmp_path)


def test_viewer_remaps_real_api_and_keeps_external_history_override(tmp_path):
    for name,amount in [('first',100),('second',200),('outside',300)]:
        write_history(queries.PaperQueryPaths.from_root(tmp_path/name).history,amount=amount)
    run_child(r'''
from pathlib import Path
import sys
from scripts import quant_api_server as server
from scripts.run_research_viewer import remap_runtime_inputs
from quantdesk.paper import auto_paper_portfolio as legacy
root=Path(sys.argv[1]);source=server.project_root
assert source==Path(server.__file__).resolve().parents[1]
assert server.AUTO_PAPER_PORTFOLIO_HISTORY_JSON==legacy.AUTO_PAPER_PORTFOLIO_HISTORY_JSON
assert server.AUTO_PAPER_PORTFOLIO_HISTORY_JSON==source/'results/auto_paper_portfolio_history.json'
server.app.config.update(TESTING=True,UI_READ_CACHE_ENABLED=False)
client=server.app.test_client()
remap_runtime_inputs(source,root/'first',app=server.app)
assert server.project_root==root/'first'
assert client.get('/api/paper-simulation/equity-history').get_json()['latest']['total_equity']==102
remap_runtime_inputs(root/'first',root/'second',app=server.app)
assert server.project_root==root/'second'
assert client.get('/api/paper-simulation/equity-history').get_json()['latest']['total_equity']==202
server.AUTO_PAPER_PORTFOLIO_HISTORY_JSON=root/'outside/results/auto_paper_portfolio_history.json'
remap_runtime_inputs(root/'second',root/'first',app=server.app)
result=client.get('/api/paper-simulation/equity-history').get_json()
assert result['latest']['total_equity']==302 and result['source_path']==str(server.AUTO_PAPER_PORTFOLIO_HISTORY_JSON)
''', tmp_path)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from scripts import quant_api_server as server
    from scripts.server.routes import decision_board
    from quantdesk.ui_read_cache import UI_FILE_CACHE
    server.UI_READ_CACHE.clear();UI_FILE_CACHE.clear()
    monkeypatch.setattr(server,"project_root",tmp_path)
    monkeypatch.setattr(server,"AUTO_PAPER_PORTFOLIO_HISTORY_JSON",queries.PaperQueryPaths.from_root(tmp_path).history)
    monkeypatch.setattr(server,"load_paper_simulation_watchlist",lambda:{})
    monkeypatch.setattr(server,"auto_paper_portfolio_latest",lambda:{})
    monkeypatch.setattr(server,"load_benchmark_series",lambda *a,**kw:{})
    monkeypatch.setattr(server,"ui_calendar_days",lambda:[])
    monkeypatch.setattr(server,"expected_settlement_date",lambda *a:date(2026,11,4))
    monkeypatch.setattr(decision_board,"evaluate_data_chain_consumer_gate",lambda:{"allowed":True})
    monkeypatch.setitem(server.app.config,"TESTING",True)
    monkeypatch.setitem(server.app.config,"UI_READ_CACHE_ENABLED",True)
    # Keep the real registered route, dependency injection and cache; expose the
    # exact history it received so the test does not depend on unrelated metrics.
    monkeypatch.setattr(decision_board,"build_decision_board_snapshot",
        lambda **kw:{"ok":True,"history_received":kw['equity_history_payload']})
    yield server
    server.UI_READ_CACHE.clear();UI_FILE_CACHE.clear()


def test_api_history_and_decision_board_use_live_query_and_file_identity(api,tmp_path,monkeypatch):
    from quantdesk.paper import auto_paper_portfolio as legacy
    def forbidden(*a,**kw):
        raise AssertionError('execution accessor called')
    monkeypatch.setattr(legacy,"auto_paper_portfolio_equity_history",forbidden)
    path=tmp_path/'external/history.json';write_history(path,amount=100)
    monkeypatch.setattr(api,"AUTO_PAPER_PORTFOLIO_HISTORY_JSON",path)
    with api.app.test_request_context('/api/decision-board'):
        assert path in api._ui_dependency_paths()
    with api.app.test_request_context('/api/data-health/dashboard'):
        dependencies = api._ui_dependency_paths()
        assert tmp_path/'results/auto_paper_portfolio_history.json' in dependencies
        assert path not in dependencies
    client=api.app.test_client()
    first=client.get('/api/decision-board?summary=true').get_json()
    assert first['history_received']['latest']['total_equity']==102
    assert client.get('/api/paper-simulation/equity-history').get_json()==client.get('/api/decision-board').get_json()['history_received']
    cached=client.get('/api/decision-board?summary=true').get_json()
    assert cached['read_identity']['sha256']==first['read_identity']['sha256']
    stamp=path.stat();write_history(path,amount=200)
    assert path.stat().st_size==stamp.st_size
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    changed=client.get('/api/decision-board?summary=true').get_json()
    assert changed['history_received']['latest']['total_equity']==202
    assert changed['read_identity']['sha256']!=first['read_identity']['sha256']
    other=tmp_path/'other/history.json';write_history(other,amount=300)
    monkeypatch.setattr(api,"AUTO_PAPER_PORTFOLIO_HISTORY_JSON",other)
    switched=client.get('/api/decision-board?summary=true').get_json()
    assert switched['history_received']['latest']['total_equity']==302
    assert switched['read_identity']['sha256']!=changed['read_identity']['sha256']


def test_registered_callbacks_are_late_bound_and_invalidate_decision_cache(api,monkeypatch):
    calls=[]
    def first(limit=250):
        calls.append(limit);return {'ok':True,'marker':'first'}
    monkeypatch.setattr(api,'auto_paper_portfolio_equity_history',first)
    client=api.app.test_client()
    assert client.get('/api/paper-simulation/equity-history?limit=7').get_json()['marker']=='first'
    before=client.get('/api/decision-board?summary=true').get_json()
    assert before['history_received']['marker']=='first' and calls==[7,250]
    monkeypatch.setattr(api,'auto_paper_portfolio_equity_history',lambda limit=250:{'ok':True,'marker':'second'})
    assert client.get('/api/paper-simulation/equity-history').get_json()['marker']=='second'
    after=client.get('/api/decision-board?summary=true').get_json()
    assert after['history_received']['marker']=='second'
    assert after['read_identity']['sha256']!=before['read_identity']['sha256']
