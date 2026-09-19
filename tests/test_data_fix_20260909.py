"""Regression cases taken from 2026-09-09 provider observations."""
from datetime import datetime
import json
from pathlib import Path

import pytest

from quantdesk import data_providers as providers
from quantdesk import snapshot_history as history
from scripts import run_daily_data_refresh as refresh
from tests.test_charter_slimming_refresh import FakeLease, isolated_writer

DAY = "2026-09-09"
OBSERVED = datetime(2026, 9, 9, 16, 32, tzinfo=refresh.SHANGHAI)


def _pair():
    # Numeric matching is separately required; these times were observed live.
    item = {"open":10, "high":11, "low":9, "trade":10, "volume":1000}
    quote = {"open":10, "high":11, "low":9, "price":10, "volume":1000,
             "date":DAY, "time":"15:34:59"}
    return item, quote


def _hq_unavailable(*args, **kwargs):
    """Simulate the HQ batch source as unavailable.

    The deployed chain inserts ``sina.hq_batch`` between ``sina.spot_direct`` and
    the AkShare adapters. These legacy cases fix the ORIGINAL two-provider
    fallback, so HQ is declared unavailable explicitly (same convention as
    ``tests/test_snapshot_history.py::_legacy_hq_unavailable``) instead of being
    reached for real - which is what previously made the assertions depend on a
    live request.
    """

    raise RuntimeError("fixture_hq_unavailable")


@pytest.mark.parametrize("stamp", ["15:00:02", "15:30:01", "15:34:59", "15:36:00", "16:00:00"])
def test_after_close_update_timestamp_is_not_a_trading_cutoff(stamp):
    item, quote = _pair()
    quote["time"] = stamp
    assert refresh._close_quote_reason(item, quote, DAY, observed_at=OBSERVED) == ""


@pytest.mark.parametrize(("change", "reason"), [
    ({"time":"14:59:59"}, "quote_before_close"),
    ({"time":"16:33:00"}, "quote_timestamp_in_future"),
    ({"date":"2026-09-08"}, "quote_date_mismatch"),
    ({"time":None}, "quote_timestamp_invalid"),
    ({"price":10.01}, "quote_value_mismatch:trade"),
    ({"volume":100}, "quote_value_mismatch:volume"),
    ({"high":float("nan")}, "quote_value_nonfinite:high"),
])
def test_real_integrity_failures_remain_rejected(change, reason):
    item, quote = _pair()
    assert refresh._close_quote_reason(item, {**quote, **change}, DAY, observed_at=OBSERVED) == reason


@pytest.mark.parametrize(("code", "expected"), [
    ("920000", "bj920000"), ("920268.BJ", "bj920268"), ("BJ920000", "bj920000"),
    ("900901", "sh900901"), ("600000", "sh600000"), ("000001", "sz000001"),
])
def test_beijing_92_does_not_use_shanghai_namespace(code, expected):
    assert providers.normalize_cn_symbol_for_sina(code) == expected


def test_batch_failure_is_retained_and_retry_only_requests_missing(monkeypatch):
    calls = []
    def fetch(symbols, **kwargs):
        calls.append(list(symbols))
        if len(calls) == 1:
            return providers.SinaBatchResult(rows=[{"symbol":"sh600000", "date":DAY}],
                provider_errors=["test:timeout"], warnings=["test:partial"])
        return providers.SinaBatchResult(rows=[{"symbol":"bj920000", "date":DAY}])
    monkeypatch.setattr(providers, "fetch_spot_sina_batch", fetch)
    monkeypatch.setattr(refresh.time, "sleep", lambda _:None)
    result = refresh._dated_close_quotes(["600000", "920000"])
    assert calls == [["600000", "920000"], ["920000"]]
    assert result.batch_diagnostics[0]["provider_errors"] == ["test:timeout"]
    assert result.batch_diagnostics[0]["missing_symbols"] == ["920000"]
    assert result.batch_diagnostics[1]["missing_symbols"] == []


def test_missing_batch_retry_is_bounded(monkeypatch):
    calls=[]
    def empty(symbols, **kwargs):
        calls.append(symbols)
        return providers.SinaBatchResult(provider_errors=["fixture:unavailable"])
    monkeypatch.setattr(providers, "fetch_spot_sina_batch", empty)
    monkeypatch.setattr(refresh.time, "sleep", lambda _:None)
    result=refresh._dated_close_quotes(["600000"])
    assert not result and len(calls)==2
    assert len(result.batch_diagnostics)==2


def test_repeated_partial_pages_try_fallback_and_choose_real_complete_batch(monkeypatch):
    monkeypatch.setattr(history, "_sina_hq_fetch", _hq_unavailable)
    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: ([{"code":"600000"}], "sina.spot_direct"))
    calls=[]
    def backup():
        calls.append(True)
        return [{"代码":"600000"}, {"代码":"920000"}]
    monkeypatch.setattr(providers, "fetch_spot_eastmoney", backup)
    # Every chain source is simulated: the last one must never be reached once
    # the AkShare eastmoney batch is complete.
    def last_source_must_not_run():
        calls.append("sina.spot_ak")
        return []
    monkeypatch.setattr(providers, "fetch_spot_sina", last_source_must_not_run)
    # Unbounded mode is required so the finite-budget chain does not skip the
    # (deliberately unboundable) legacy provider.
    rows, source=history.default_network_fetch(symbols=["600000", "920000"], budget_seconds=None)
    assert calls==[True] and source=="eastmoney.spot_ak" and len(rows)==2


def test_all_fallbacks_failed_preserve_real_partial_and_reasons(monkeypatch):
    monkeypatch.setattr(history, "_sina_hq_fetch", _hq_unavailable)
    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: ([{"code":"600000"}], "sina.spot_direct"))
    def failed():
        raise TimeoutError("fixture")
    monkeypatch.setattr(providers, "fetch_spot_eastmoney", failed)
    monkeypatch.setattr(providers, "fetch_spot_sina", lambda: [])
    result, _source=history.default_network_fetch(symbols=["600000", "920000"], budget_seconds=None)
    assert result==[{"code":"600000"}]
    assert "snapshot_provider_coverage_incomplete" in result.warnings
    assert any("TimeoutError" in warning for warning in result.warnings)


def test_transient_empty_page_is_retried_before_stopping(monkeypatch):
    import requests
    calls=[]
    def get(*args, **kwargs):
        page=kwargs["params"]["page"]
        calls.append(page)
        if page==2 and calls.count(2)==1:
            values=[]
        elif page<=2:
            values=[{"code":str(600000+100*(page-1)+i)} for i in range(100)]
        else:
            values=[]
        class Response:
            def raise_for_status(self):pass
            def json(self):return values
        return Response()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(history.time_module, "sleep", lambda _:None)
    rows,_=history._sina_direct_fetch()
    assert len(rows)==200 and calls==[1,2,2,3,3]


def test_review_preserves_exact_difference_and_batch_diagnostics(tmp_path, monkeypatch, isolated_writer):
    item, quote = _pair()
    item.update(code="600000", name="fixture")
    quote["volume"] = 999
    batch=refresh._DatedQuotes()
    batch["600000"]=quote
    batch.batch_diagnostics=[{"provider_errors":["fixture:timeout"], "attempt":1}]
    monkeypatch.setattr(refresh, "_complete_live_session", lambda _:True)
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda _:[item])
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda _:batch)
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda:["600000"])
    result=refresh.family_bars_and_valuation(DAY, True, lease=FakeLease())
    saved=json.loads((tmp_path/result["review_evidence"]).read_text(encoding="utf-8"))
    rejected=saved["flagged_for_review"]["600000"]
    assert rejected["reason"]=="quote_value_mismatch:volume"
    assert rejected["compared_fields"]["volume"]=={"list":"1000","dated_quote":"999"}
    assert saved["quote_batch_diagnostics"]==batch.batch_diagnostics
    assert not list((tmp_path/"data/daily").glob("*.csv"))


def test_old_quote_validation_checkpoint_cannot_skip_repaired_refresh(tmp_path):
    from scripts import run_data_close_pipeline as pipeline
    run_id="data-close-20260909T154001"
    path=pipeline.checkpoint_path(tmp_path, run_id)
    path.parent.mkdir(parents=True)
    original=json.dumps({"schema_version":pipeline.SCHEMA_VERSION, "run_id":run_id,
        "target_date":DAY, "config_hash":"fixture", "stages":{"market_refresh":{
            "status":"completed_with_warnings", "stage_version":f"{pipeline.SCHEMA_VERSION}:market_refresh:2",
            "input_manifest":"old_input", "output_manifest":"old_output"}}})
    path.write_text(original,encoding="utf-8")
    result=pipeline.load_checkpoint(tmp_path, run_id, target=DAY, config_hash="fixture")
    assert result["stages"]["market_refresh"]["status"]=="invalid_stage_version"
    assert path.read_text(encoding="utf-8")==original
