"""Actual UI read-path identities, concurrency, calendar semantics and paging."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import os
from pathlib import Path
from threading import Event

from flask import jsonify
import pytest

from quantdesk.ui_read_cache import ReadCache, byte_identity, detail_page, summary_projection, UI_READ_CACHE, UI_FILE_CACHE


def test_same_stat_byte_rewrite_invalidates_identity_and_does_not_mix_accounts(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"run":"one"}', encoding="utf-8")
    before = path.stat()
    original = byte_identity([path], target_date="2026-10-19", account="one", scope="home")
    path.write_text('{"run":"two"}', encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = byte_identity([path], target_date="2026-10-19", account="one", scope="home")
    assert changed["sha256"] != original["sha256"]
    assert byte_identity([path], target_date="2026-10-19", account="two", scope="home")["sha256"] != changed["sha256"]
    assert byte_identity([path], target_date="2026-10-20", account="one", scope="home")["sha256"] != changed["sha256"]


def test_concurrent_reads_share_one_build_but_never_share_mutable_output() -> None:
    cache = ReadCache()
    entered, release = Event(), Event()
    builds = []
    def build():
        builds.append(1)
        entered.set()
        assert release.wait(2)
        return {"items":["original"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(cache.get, "same-run", build) for _ in range(4)]
        assert entered.wait(2)
        release.set()
        values = [future.result() for future in futures]
    assert builds == [1]
    values[0]["items"].append("mutation")
    assert cache.get("same-run", build) == {"items":["original"]}


def test_failure_and_expiration_cannot_become_permanent_green_cache() -> None:
    cache = ReadCache(ttl_seconds=0)
    calls = []
    def broken():
        calls.append(1)
        raise ValueError("explicit-input-failure")
    for _ in range(2):
        with pytest.raises(ValueError,match="explicit-input-failure"):
            cache.get("error",broken)
    assert len(calls)==2
    assert cache.get("ok", lambda: "old") == "old"
    assert cache.get("ok", lambda: "new") == "new"


def test_summary_keeps_unknown_warnings_and_shallow_pages_reconstruct_every_value() -> None:
    payload = {"status":"degraded","accuracy":None,"warnings":[f"gap:{i}" for i in range(21)],
        "detail":{"data":list(range(403)),"raw":"原始"*2000}}
    summary = summary_projection(payload)
    assert summary["status"] == "degraded" and summary["accuracy"] is None
    assert next(row for row in summary["summary_omissions"] if row["pointer"]=="/warnings")["total"]==21
    restored = []
    for offset in range(0,403,100):
        page = detail_page(payload,pointer="/detail/data",offset=offset,limit=100)
        restored.extend(item["value"] for item in page["items"])
    assert restored==payload["detail"]["data"]
    root = detail_page(payload)
    assert next(item for item in root["items"] if item["key"]=="detail")["deferred"]


@pytest.fixture
def cached_server(tmp_path: Path, monkeypatch):
    from scripts import quant_api_server as server
    UI_READ_CACHE.clear(); UI_FILE_CACHE.clear()
    monkeypatch.setattr(server,"project_root",tmp_path)
    monkeypatch.setattr(server,"_ui_dependency_paths",lambda:[tmp_path/"input.json"])
    monkeypatch.setattr(server,"expected_settlement_date",lambda *_a:date(2026,10,19))
    monkeypatch.setitem(server.app.config,"TESTING",True)
    monkeypatch.setitem(server.app.config,"UI_READ_CACHE_ENABLED",True)
    (tmp_path/"input.json").write_text('{"run":"one"}',encoding="utf-8")
    return server


def test_actual_flask_cache_reuses_get_and_invalidates_run_same_stat(cached_server,tmp_path:Path,monkeypatch) -> None:
    server=cached_server
    calls=[]
    def view():
        calls.append(1)
        return jsonify(json.loads((tmp_path/"input.json").read_text(encoding="utf-8")))
    monkeypatch.setitem(server.app.view_functions,"daily_api.get_daily_overview",view)
    client=server.app.test_client()
    first=client.get("/api/daily/overview?summary=true").get_json()
    again=client.get("/api/daily/overview?summary=true").get_json()
    assert first==again and len(calls)==1
    assert client.get("/api/daily/overview").get_json()=={"run":"one"}
    stamp=(tmp_path/"input.json").stat()
    (tmp_path/"input.json").write_text('{"run":"two"}',encoding="utf-8")
    os.utime(tmp_path/"input.json",ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    changed=client.get("/api/daily/overview?summary=true").get_json()
    assert changed["run"]=="two" and len(calls)==2
    assert changed["read_identity"]["sha256"] != first["read_identity"]["sha256"]
    stale=client.get("/api/daily/overview?details=true&identity="+first["read_identity"]["sha256"])
    assert stale.status_code==409 and stale.get_json()["status"]=="read_identity_changed"


def test_flask_failed_response_is_not_retained_and_input_mutation_fails_closed(cached_server,tmp_path:Path,monkeypatch) -> None:
    server=cached_server
    calls=[]
    def failed():
        calls.append(1)
        return jsonify(ok=False,status="explicit_missing"),503
    monkeypatch.setitem(server.app.view_functions,"daily_api.get_daily_overview",failed)
    client=server.app.test_client()
    assert client.get("/api/daily/overview").status_code==503
    assert client.get("/api/daily/overview").status_code==503 and len(calls)==2
    def mutation():
        (tmp_path/"input.json").write_text('{"run":"two"}',encoding="utf-8")
        return jsonify(ok=True)
    monkeypatch.setitem(server.app.view_functions,"daily_api.get_daily_overview",mutation)
    response=client.get("/api/daily/overview")
    assert response.status_code==409 and response.get_json()["status"]=="read_input_unstable"


def test_actual_health_summary_retains_scalar_contract_and_details(cached_server,monkeypatch) -> None:
    from quantdesk import data_health_dashboard as health
    payload={"ok":True,"health_level":"yellow","cards":{"daily_data":{"status":"yellow","value":"2026-10-18"}},
        "daily_data_chain":{"run_id":"r1","target_date":"2026-10-19","status":"partial",
            "universe":list(range(500))},"warnings":["missing_raw"]}
    monkeypatch.setattr(health,"build_cached_data_health_dashboard",lambda:payload)
    client=cached_server.app.test_client()
    response=client.get("/api/data-health/dashboard?summary=true")
    assert response.status_code==200 and len(response.data)<15000
    value=response.get_json()
    assert value["daily_data_chain"]["status"]=="partial" and value["warnings"]==["missing_raw"]
    pages=[]
    for offset in range(0,500,100):
        page=client.get(value["details_url"]+"&pointer=/daily_data_chain/universe&offset="+str(offset)).get_json()
        pages.extend(item["value"] for item in page["items"])
    assert pages==list(range(500))


def test_actual_decision_board_calendar_bound_matches_full_calendar(tmp_path:Path,monkeypatch) -> None:
    from flask import Flask
    from quantdesk.decision_board import build_decision_board_snapshot
    from quantdesk.trader_profile import default_research_paper_profile
    from scripts.server.routes.decision_board import DecisionBoardRouteDeps,create_decision_board_blueprint
    from datetime import timedelta
    calendar=[(date(2000,1,1)+timedelta(days=i)).isoformat() for i in range(10000)]
    watchlist={"as_of":"2026-09-10","items":[{"symbol":"600000","entry_date":"2026-09-01","latest_date":"2026-09-10"}]}
    profile=default_research_paper_profile()
    expected=build_decision_board_snapshot(watchlist_payload=watchlist,portfolio_payload={},equity_history_payload={},
        benchmark_payload={},profile=profile,calendar_days=calendar,strategy_version="fixture",source_paths={})
    deps=DecisionBoardRouteDeps(lambda:watchlist,lambda:{},lambda:{},lambda:{},lambda:profile,
        lambda:calendar,lambda:"fixture",lambda:{})
    app=Flask(__name__);app.register_blueprint(create_decision_board_blueprint(deps))
    value=app.test_client().get("/api/decision-board").get_json()
    assert value["watchlist"][0]["holding_days"]==expected["watchlist"][0]["holding_days"]
    assert value["envelope"]["manifest"]["input_hashes"]==expected["envelope"]["manifest"]["input_hashes"]


def test_directory_input_names_invalidate_empty_ledger_fallback(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    before = byte_identity([daily])
    (daily / "600000.csv").write_text("date,close\n2026-10-19,10\n", encoding="utf-8")
    after = byte_identity([daily])
    assert after["sha256"] != before["sha256"]
    assert after["inputs"][0]["entry_count"] == 1


def test_actual_project_status_cycle_same_stat_rewrite_changes_display(tmp_path: Path, monkeypatch) -> None:
    from scripts import quant_api_server as server
    UI_READ_CACHE.clear(); UI_FILE_CACHE.clear()
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setitem(server.app.config, "QUANTDESK_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setitem(server.app.config, "TESTING", True)
    monkeypatch.setitem(server.app.config, "UI_READ_CACHE_ENABLED", True)
    (tmp_path / "STATUS.md").write_text("# STATUS · 更新于 2026-10-19T22:30:00+08:00 · 更新者：health_line\n", encoding="utf-8")
    path = tmp_path / "results/ai_trader/cycle_latest.json"
    path.parent.mkdir(parents=True)
    def cycle(model: str) -> bytes:
        return json.dumps({"as_of":"2026-10-19", "plan":{"decision_date":"2026-10-19",
            "llm":{"used":True,"model":model}}}).encode()
    path.write_bytes(cycle("kimi"))
    client = server.app.test_client()
    first = client.get("/api/project-status?summary=true").get_json()
    assert first["runtime_observations"]["profiles"]["default"]["model_label"] == "Kimi"
    stamp = path.stat()
    path.write_bytes(cycle("glm4"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = client.get("/api/project-status?summary=true").get_json()
    assert second["runtime_observations"]["profiles"]["default"]["model_label"] == "GLM"
    assert first["read_identity"]["sha256"] != second["read_identity"]["sha256"]


def test_ui_identity_binds_review_freshness_and_candidate_minute_inputs(tmp_path: Path, monkeypatch) -> None:
    from scripts import quant_api_server as server
    UI_READ_CACHE.clear(); UI_FILE_CACHE.clear()
    monkeypatch.setattr(server, "project_root", tmp_path)
    monkeypatch.setattr(server, "DAILY_DIR", tmp_path / "data/daily")
    monkeypatch.setattr(server, "TRACKING_DIR", tmp_path / "data/tracking")
    monkeypatch.setattr(server, "expected_settlement_date", lambda *_a: date(2026,10,19))
    ledger = tmp_path / "data/tracking/candidate_ledger.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("ts_code,signal_date\n600000,2026-10-19\n", encoding="utf-8")
    dependencies = {
        "/api/decision-board": "results/plan_review_receipts/latest.json",
        "/api/data-health/dashboard": "data/tracking/tracking_summary.json",
        "/api/daily/candidates": "data/tracking/candidate_review_overrides.json",
        "/api/strategy-scorecard": "data/intraday/1m/600000.csv",
    }
    for route, relative in dependencies.items():
        path = tmp_path / relative
        with server.app.test_request_context(route):
            before = server._ui_request_identity()
            assert str(path) in {row["path"] for row in before["inputs"]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"gap"}', encoding="utf-8")
        with server.app.test_request_context(route):
            assert server._ui_request_identity()["sha256"] != before["sha256"]


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_dependency_symbols_reuse_extraction_and_recheck_same_stat_bytes(tmp_path: Path, monkeypatch, suffix: str) -> None:
    from scripts import quant_api_server as server
    UI_FILE_CACHE.clear()
    path = tmp_path / ("candidate_evidence" + suffix)
    def write(symbol: str) -> None:
        value = (json.dumps({"items": [{"symbol": symbol, "evidence": ["original"] * 1000}]})
                 if suffix == ".json" else "ts_code,signal_date\n" + symbol + ",2026-10-19\n")
        path.write_text(value, encoding="utf-8")
    write("600000")
    normalize = server.normalize_symbol
    calls = []
    def observed_normalize(value):
        calls.append(value)
        return normalize(value)
    monkeypatch.setattr(server, "normalize_symbol", observed_normalize)
    first = server._ui_symbols_in_files([path])
    assert first == {"600000"}
    first.add("600999")
    assert server._ui_symbols_in_files([path]) == {"600000"}
    assert calls == ["600000"]
    stamp = path.stat()
    write("600001")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert server._ui_symbols_in_files([path]) == {"600001"}
    assert calls == ["600000", "600001"]
    monkeypatch.setattr(server, "normalize_symbol", lambda _value: "600002")
    assert server._ui_symbols_in_files([path]) == {"600002"}


def test_legacy_candidate_symbol_cache_invalidates_equal_mtime(tmp_path: Path, monkeypatch) -> None:
    from scripts import quant_api_server as server
    monkeypatch.setattr(server, "TRACKING_DIR", tmp_path)
    monkeypatch.setattr(server, "DAILY_CANDIDATE_LATEST_CSV", tmp_path / "latest.csv")
    path = tmp_path / "candidate_ledger.csv"
    def write(symbol: str) -> None:
        path.write_text("ts_code,candidate_role,signal_date\n" + symbol + ",daily_recommendation,2026-10-19\n", encoding="utf-8")
    write("600000")
    payload = {"signal_date":"2026-10-19"}
    assert server.latest_generation_symbols(payload) == {"600000"}
    stamp = path.stat()
    write("600001")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert server.latest_generation_symbols(payload) == {"600001"}


def test_actual_frontend_reuses_inflight_get_and_retries_after_failure() -> None:
    from tests.test_ai_trader_review_profiles import run_ui
    run_ui(r"""
        let release;
        reply = () => new Promise(resolve => { release = resolve; });
        const first = requestJson('/api/daily/candidates');
        const second = requestJson('/api/daily/candidates');
        assert.equal(first, second);
        assert.equal(requests.length, 1);
        release({status: 'missing'});
        await Promise.all([first, second]);
        let attempts = 0;
        reply = () => { attempts++; throw new Error('visible failure'); };
        await assert.rejects(requestJson('/api/daily/candidates'));
        await assert.rejects(requestJson('/api/daily/candidates'));
        assert.equal(attempts, 2);
        reply = url => { assert.match(url, /summary=true/); return {ok: true}; };
        await requestJson('/api/data-health/dashboard');
    """)


def test_actual_frontend_obsolete_read_cannot_replace_current_health() -> None:
    from tests.test_ai_trader_review_profiles import run_ui
    run_ui(r"""
        let release;
        reply = () => new Promise(resolve => { release = resolve; });
        renderDataHealthHome = data => { node('dataHealthHomePanel').innerHTML = data.marker; };
        const oldRead = loadDataHealthHome();
        beginReadRender('health');
        node('dataHealthHomePanel').innerHTML = 'newer-state';
        release({marker: 'obsolete-state'});
        await oldRead;
        assert.equal(node('dataHealthHomePanel').innerHTML, 'newer-state');
        reply = () => ({marker: 'fresh-state'});
        await loadDataHealthHome();
        assert.equal(node('dataHealthHomePanel').innerHTML, 'fresh-state');
    """)


def test_frontend_relative_reads_need_no_url_constructor() -> None:
    from tests.test_project_status_ui import _run_status_ui
    value = _run_status_ui(r"""
        vm.runInContext(`
            (async () => {
                responses.push({status: 'degraded'});
                await requestJson('/api/project-status');
                responses.push({status: 'degraded'});
                await requestJson('/api/data-health/dashboard?summary=false&scope=home');
                responses.push({status: 'degraded'});
                await requestJson('/api/data-health/dashboard?details=true&identity=frozen');
                console.log(JSON.stringify({requests}));
            })();
        `, context);
    """)
    assert value["requests"] == [
        {"url": "/api/project-status", "method": "GET"},
        {"url": "/api/data-health/dashboard?scope=home&summary=true", "method": "GET"},
        {"url": "/api/data-health/dashboard?details=true&identity=frozen", "method": "GET"},
    ]


def test_detail_api_pages_reconstruct_complete_nested_evidence(cached_server, monkeypatch) -> None:
    from quantdesk import data_health_dashboard as health
    original = {"ok":True, "status":"degraded", "warnings":["raw_missing"] * 17,
        "nested":{"a/b":[{"text":"原始证据" * 1100}, None, False, {"n":37}]},
        "large":list(range(503))}
    monkeypatch.setattr(health, "build_cached_data_health_dashboard", lambda: original)
    client = cached_server.app.test_client()
    summary = client.get("/api/data-health/dashboard?summary=true").get_json()
    def restore(pointer: str = ""):
        parts = []
        offset = 0
        while True:
            response = client.get("/api/data-health/dashboard", query_string={
                "details":"true", "identity":summary["read_identity"]["sha256"],
                "pointer":pointer, "offset":offset, "limit":100})
            assert response.status_code == 200
            page = response.get_json()
            if page["kind"] == "scalar":
                return page["items"]
            if page["kind"] == "string":
                parts.append(page["items"])
            else:
                parts.extend(page["items"])
            offset = page["pagination"]["next_offset"]
            if offset is None:
                break
        if page["kind"] == "string":
            return "".join(parts)
        def value(item):
            return restore(item["pointer"]) if item["deferred"] else item["value"]
        return {item["key"]:value(item) for item in parts} if page["kind"] == "object" else [value(item) for item in parts]
    assert restore() == original
