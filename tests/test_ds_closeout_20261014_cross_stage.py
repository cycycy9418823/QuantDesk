"""Synthetic recovery evidence through the real chain, health, Flask and main JS.

Four securities model physical bars, a verified no-session exception and a
pending gap. All provider responses and minute results are deterministic fakes;
every write is confined to the isolated runner's temporary fixture directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest
from flask import Flask

from quantdesk.full_a_daily_reconciliation import build_reconciliation_partition
from quantdesk.daily_session_evidence import build_baostock_session_evidence
from quantdesk.session_expectation import build_session_expectation
from scripts.ops import write_health_line as health
from scripts.server.routes.system import create_system_blueprint
from tests import test_ds_closeout_20261014_gap_ledger as history
from tests import test_target_daily_gap_recovery_20261013 as prices
from tests.test_daily_data_chain_universe_refresh import _install_minimal_chain_fakes
from tests.test_daily_data_chain_upstream_reuse_preflight import _valid_upstream_reuse
from tests.test_project_status_ui import _run_status_ui

DAY = history.TARGET
PRIOR = "2026-10-13"
SYMBOLS = ["000001", "000002", "000016", "688496"]
RUN_ID = "daily-data-chain-synthetic"
fake_sources = prices.fake_sources


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _file_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _api_and_ui(root: Path) -> tuple[dict, dict]:
    before = _file_hashes(root)
    app = Flask(__name__)
    app.config["QUANTDESK_PROJECT_ROOT"] = str(root)
    app.register_blueprint(create_system_blueprint(build_id="synthetic", asset_versions_func=lambda: {}))
    response = app.test_client().get("/api/project-status?path=ignored")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    script = "responses.push(" + json.dumps(payload, ensure_ascii=False) + ");" + """
        (async () => {
            await loadProjectStatus();
            console.log(JSON.stringify({
                health: nodeText("projectStatusHealth"),
                explain: nodeText("projectStatusExplain"),
                raw: nodeText("projectStatusRaw"),
                runtime: document.getElementById("projectRuntimeSummary").innerHTML,
                meta: nodeText("projectStatusMeta"),
                requests,
            }));
        })();
    """
    ui = _run_status_ui("vm.runInContext(" + json.dumps(script, ensure_ascii=False) + ", context);")
    assert ui["requests"] == [{"url": "/api/project-status", "method": "GET"}]
    assert _file_hashes(root) == before
    return payload, ui


@pytest.fixture
def recovered_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_sources) -> dict:
    calls, responses = fake_sources
    monkeypatch.setattr("quantdesk.project_status._now_iso", lambda: f"{DAY}T23:00:00+08:00")
    originals = {}
    for symbol in SYMBOLS:
        path, before = prices._old_file(tmp_path, symbol, DAY if symbol == "000001" else PRIOR)
        originals[symbol] = (path, before)
    responses[("baostock_daily", "000002")] = prices._bar("000002", DAY)
    no_session = pd.DataFrame()
    no_session.attrs["suspension_evidence"] = history._no_session("000016", DAY)
    responses[("baostock_daily", "000016")] = no_session
    reuse = _valid_upstream_reuse(tmp_path, target=DAY, run_id="synthetic-cross-stage-pipeline")
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.write_text("symbol,name,source,updated_at\n" + "".join(
        f"{symbol},SYNTHETIC,stock_zh_a_spot,{DAY}\n" for symbol in SYMBOLS), encoding="utf-8")
    reuse["stages"]["universe"]["sha256"] = hashlib.sha256(universe.read_bytes()).hexdigest()
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n" + "".join(
        f"{day},true,synthetic_test\n" for day in [PRIOR, DAY]), encoding="utf-8")
    chain = _install_minimal_chain_fakes(monkeypatch, SYMBOLS)
    monkeypatch.setattr(chain, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(chain, "update_benchmark_files", lambda *a, **kw: {"status": "completed"})

    def real_session(symbols, target_date, **kwargs):
        return build_session_expectation(symbols, target_date, **kwargs,
                                         provider=lambda date: pd.DataFrame())

    monkeypatch.setattr(chain, "build_session_expectation", real_session)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date=DAY, network=True, dry_run=False,
        rebuild_features=False, upstream_reuse=reuse, run_id=RUN_ID,
        pipeline_run_id=reuse["run_id"],
    )
    assert result["status"] != "failed", result.get("error")
    assert result["published_latest"] is True
    assert result["authoritative"] is True
    for symbol, (path, before) in originals.items():
        assert path.read_bytes().startswith(before)
        if symbol != "000002":
            assert path.read_bytes() == before
    assert {call[1] for call in calls} == {"000002", "000016", "688496"}
    return {"root": tmp_path, "chain": result}


def test_physical_bars_session_exceptions_and_pending_gaps_remain_distinct(recovered_project: dict) -> None:
    root, chain = recovered_project["root"], recovered_project["chain"]
    recovery = chain["components"]["target_day_gap_recovery"]
    assert recovery["appended_symbols"] == ["000002"]
    assert recovery["verified_no_session_symbols"] == ["000016"]
    assert recovery["remaining_symbols"] == ["688496"]
    assert recovery["evidence_source"]["run_id"] == RUN_ID
    ledger = recovery["historical_ledger"]
    assert history._row(ledger, "000002")["recovered_dates"] == [PRIOR, DAY]
    assert history._row(ledger, "000016")["verified_no_session_dates"] == [DAY]
    assert history._row(ledger, "688496")["pending_dates"] == [DAY]
    session = chain["components"]["session_expectation"]
    assert session["legitimate_no_session_symbols"] == ["000016"]
    assert session["unknown_symbols"] == ["688496"]
    before = _file_hashes(root)
    partition = build_reconciliation_partition(root, target_date=DAY)
    completeness = partition["completeness"]
    assert (completeness["with_bar_count"], completeness["verified_excluded_count"],
            completeness["gap_count"]) == (2, 1, 1)
    assert completeness["run_evidence_run_id"] == RUN_ID
    assert completeness["complete"] is False
    assert partition["pending_symbols"] == ["688496"]
    assert _file_hashes(root) == before


def test_verified_no_session_reaches_health_real_api_and_actual_js(recovered_project: dict) -> None:
    root = recovered_project["root"]
    line, explains, trading = health.build_line(root, DAY)
    assert trading is True
    assert "日线 ✗2/4(缺2)" in line
    assert "已核验当日无交易1只" in line
    assert "待核验缺口1只" in line
    assert "日线缺口原因未核验1只" in explains
    assert "日线缺口原因未核验2只" not in explains
    health.rewrite_status(root, f"# STATUS · 更新于 {DAY} 22:30 · 更新者：synthetic_test",
                          line, " · ".join(explains))
    payload, ui = _api_and_ui(root)
    assert payload["health_line"] == line
    assert payload["readable"] is True
    assert payload["health_freshness"]["status"] == "current"
    assert "非实时核验" in payload["source_label"]
    assert ui["health"] == line
    assert "已核验当日无交易1只" in ui["raw"]
    assert "日线缺口原因未核验1只" in ui["explain"]
    assert "可读≠系统健康" in ui["meta"]


def test_calendar_coverage_warning_is_preserved_by_real_api_and_js(
    recovered_project: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = recovered_project["root"]
    monkeypatch.setattr("quantdesk.project_status._now_iso", lambda: "2026-10-16T23:00:00+08:00")
    line, explains, _ = health.build_line(root, DAY)
    health.rewrite_status(root, f"# STATUS · 更新于 {DAY} 22:30 · 更新者：synthetic_test",
                          line, " · ".join(explains))
    payload, ui = _api_and_ui(root)
    assert payload["health_freshness"]["status"] == "unknown"
    warning = payload["health_freshness"]["warning"]
    assert "交易日历缺失或未覆盖当前日期" in warning
    assert ui["health"] == line + "\n" + warning
    assert "已核验当日无交易1只" in ui["health"]


def test_no_session_conflicting_with_raw_trade_is_visible_without_rewriting(recovered_project: dict) -> None:
    root = recovered_project["root"]
    path = root / "data/daily/000016.csv"
    original = path.read_bytes()
    prices._bar("000016", DAY).reindex(columns=prices.DAILY_HEADER.split(",")).to_csv(
        path, mode="a", header=False, index=False, lineterminator="\r\n")
    before = _file_hashes(root)
    partition = build_reconciliation_partition(root, target_date=DAY)
    assert partition["completeness"]["with_bar_count"] == 3
    assert partition["completeness"]["verified_excluded_count"] == 0
    assert partition["completeness"]["contradiction_symbols"] == ["000016"]
    line, explains, _ = health.build_line(root, DAY)
    assert "日线 ✗3/4(缺1)" in line
    assert "已核验当日无交易" not in line
    assert any("本轮无交易证据与当日raw成交证据冲突1只：000016" in item for item in explains)
    assert _file_hashes(root) == before
    assert path.read_bytes().startswith(original)


def test_health_standalone_cli_reads_same_evidence_without_runtime_writes(recovered_project: dict) -> None:
    root = recovered_project["root"]
    before = _file_hashes(root)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(Path(health.__file__)), "--root", str(root),
         "--target-date", DAY, "--dry-run"], cwd=root, text=True, encoding="utf-8",
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "已核验当日无交易1只" in payload["line"]
    assert payload["dry_run"] is True
    assert _file_hashes(root) == before


@pytest.mark.parametrize("change", ["stale_pointer", "binding_run", "false_authority", "symbol",
                                         "hash", "source", "not_suspended", "future_record"])
def test_rejected_evidence_stays_unknown_through_api_and_ui(recovered_project: dict, change: str) -> None:
    root = recovered_project["root"]
    pointer = root / "results/data_chain/daily_data_chain_latest.json"
    latest = copy.deepcopy(recovered_project["chain"])
    recovery = latest["components"]["target_day_gap_recovery"]
    evidence = recovery["session_evidence"]["000016"]
    if change == "stale_pointer":
        latest["target_date"] = PRIOR
    elif change == "binding_run":
        recovery["evidence_source"]["run_id"] = "some-other-run"
    elif change == "false_authority":
        latest["authoritative"] = False
    elif change == "symbol":
        evidence["symbol"] = "000002"
    elif change == "hash":
        evidence["records_sha256"] = "0" * 64
    elif change == "source":
        evidence["source"] = "synthetic_unknown_provider"
    else:
        status = "1" if change == "not_suspended" else "0"
        date = "2026-10-15" if change == "future_record" else DAY
        recovery["session_evidence"]["000016"] = build_baostock_session_evidence(
            symbol="000016", provider_code="sz.000016", query_start_date=date,
            query_end_date=date, records=[{"trade_date": date, "tradestatus": status}],
            captured_at=f"{date}T18:45:10",
        )
    _write_json(pointer, latest)
    partition = build_reconciliation_partition(root, target_date=DAY)
    assert partition["completeness"]["verified_excluded_count"] == 0
    assert set(partition["pending_symbols"]) == {"000016", "688496"}
    line, explains, _ = health.build_line(root, DAY)
    assert "已核验当日无交易" not in line
    assert "日线缺口原因未核验2只" in explains
    health.rewrite_status(root, f"# STATUS · 更新于 {DAY} 22:30 · 更新者：synthetic_test",
                          line, " · ".join(explains))
    _, ui = _api_and_ui(root)
    assert "日线 ✗2/4(缺2)" in ui["health"]
    assert "已核验当日无交易" not in ui["health"]
    assert "日线缺口原因未核验2只" in ui["explain"]
