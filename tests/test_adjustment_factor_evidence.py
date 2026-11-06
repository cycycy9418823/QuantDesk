"""Sparse event semantics, source failures and request budgets stay explicit."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantdesk.adjustment_factor_evidence import (
    BASE, FactorResponse, factor_document, factor_window, maintain_adjustment_evidence,
    parse_sina_events, publish_factor_document,
)
from quantdesk.daily_evidence_contract import build_requirements, digest, json_bytes, seal_bytes
from quantdesk.process_deadline import ProcessDeadlineResult
from test_daily_evidence_contract import CODE, TARGET, fixture_project, put, ready_factor, response, snapshot


def rebody(original: FactorResponse, value: dict, wrapper: str | None = None) -> FactorResponse:
    raw = ((wrapper or f"var sz{CODE}hfq=") + json.dumps(value)).encode()
    return FactorResponse(raw, {**original.proof, "raw_sha256": digest(raw), "raw_bytes": len(raw)})


def event_body(original: FactorResponse) -> dict:
    return {"total": 3, "data": [{"d": "2026-08-27", "f": "2.1000000000"}, {"d": "2009-10-30", "f": "1.0000000000"}, {"d": "1900-01-01", "f": "1.0000000000"}]}


def test_sparse_events_expand_by_effective_date_and_remain_derived():
    doc = factor_document(response(), CODE, ["2026-10-29", TARGET])
    assert [row["adj_factor"] for row in doc["rows"]] == ["2.1000000000"] * 2
    assert doc["kind"] == "derived_sina_hfq_events"
    assert doc["native_daily_rows"] is False
    assert doc["canonical_quantity_cost_basis_eligible"] is False
    assert doc["cross_provider_normalization_verified"] is False
    assert all(row["trade_date"] != "1900-01-01" for row in doc["rows"])


def test_an_event_changes_the_factor_on_its_effective_session():
    original = response()
    value = event_body(original)
    value["data"].insert(0, {"d": TARGET, "f": "2.2000000000"})
    value["total"] = 4
    doc = factor_document(rebody(original, value), CODE, ["2026-10-29", TARGET])
    assert [row["adj_factor"] for row in doc["rows"]] == ["2.1000000000", "2.2000000000"]


@pytest.mark.parametrize("defect", ["count", "seed", "duplicate", "order", "offset", "future_event", "wrong_wrapper"])
def test_http_success_cannot_hide_incomplete_or_wrong_event_evidence(defect):
    original, wrapper = response(), None
    value = event_body(original)
    if defect == "count": value["total"] = 4
    elif defect == "seed": value["data"][-1]["d"] = "2000-01-01"
    elif defect == "duplicate": value["data"][1]["d"] = value["data"][0]["d"]
    elif defect == "order": value["data"] = list(reversed(value["data"]))
    elif defect == "offset": value["data"][0]["c"] = "1"
    elif defect == "future_event": value["data"][0]["d"] = "2026-11-03"
    else: wrapper = "var sz300018hfq="
    with pytest.raises(ValueError):
        factor_document(rebody(original, value, wrapper), CODE, [TARGET])


@pytest.mark.parametrize("number", ["0", "-1", "nan", "Infinity", "", 2.1])
def test_missing_or_invalid_factor_is_never_filled_with_one(number):
    original = response()
    value = event_body(original)
    value["data"][0]["f"] = number
    with pytest.raises(ValueError):
        factor_document(rebody(original, value), CODE, [TARGET])


@pytest.mark.parametrize("change", [{"http_status": 500}, {"raw_sha256": "0" * 64}, {"complete_response": False},
                                   {"finished_at": "2026-10-30T14:59:00+08:00"}, {"finished_at": "2026-10-29T20:00:00+08:00"},
                                   {"finished_at": "2026-10-30T20:00:00"}, {"source_kind": "unknown"}])
def test_response_time_integrity_and_source_kind_bound_the_coverage(change):
    original = response()
    with pytest.raises(ValueError):
        factor_document(replace(original, proof={**original.proof, **change}), CODE, [TARGET])


def native_response(code=CODE, *, rows: str | None = None) -> FactorResponse:
    raw = (rows or f"trade_date,symbol,adj_factor,source\n2026-10-29,{code},2.1,synthetic_native\n{TARGET},{code},2.1,synthetic_native\n").encode()
    return FactorResponse(raw, {"symbol": code, "source_kind": "native_daily_csv", "source": "synthetic_native",
        "raw_sha256": digest(raw), "raw_bytes": len(raw), "complete_response": True, "finished_at": TARGET+"T20:00:00+08:00"})


def test_native_daily_rows_must_cover_each_requested_session():
    with pytest.raises(ValueError, match="window_missing"):
        factor_document(native_response(rows=f"trade_date,symbol,adj_factor,source\n{TARGET},{CODE},2.1,synthetic_native\n"), CODE, ["2026-10-29", TARGET])


def test_derived_receipt_does_not_satisfy_native_only_consumer(tmp_path):
    fixture_project(tmp_path)
    ready_factor(tmp_path)
    value = factor_window(tmp_path, CODE, [TARGET], policy="native_only")
    assert value["status"] == "incomplete"
    assert value["kind"] == "derived_sina_hfq_events"


def test_offline_mode_does_not_call_a_fetcher_or_write_artifacts(tmp_path):
    fixture_project(tmp_path)
    before = snapshot(tmp_path)
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=False,
        fetch_batch=lambda *_args: pytest.fail("network called"))
    assert value["status"] == "planned_offline"
    assert value["requests_budgeted"] == 0
    assert snapshot(tmp_path) == before


def test_usable_source_cache_prevents_repeat_requests(tmp_path):
    fixture_project(tmp_path)
    ready_factor(tmp_path)
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=True,
        assert_write=lambda: None, fetch_batch=lambda *_args: pytest.fail("duplicate request"))
    assert value["ok"] and value["reused_symbols"] == [CODE]


def test_native_local_fallback_works_for_a_market_sina_does_not_support(tmp_path):
    fixture_project(tmp_path, quantity=0)
    code = "920001"
    put(tmp_path, f"results/candidate_refresh/daily/{TARGET}.json", {"target_date": TARGET, "candidates": [{"symbol": code}]})
    source = native_response(code)
    raw_ref = seal_bytes(tmp_path, BASE / "blobs", source.raw, suffix="bin", assert_write=lambda: None)
    put(tmp_path, f"{BASE.as_posix()}/imports/{code}.json", {"raw_source": raw_ref, "source_proof": source.proof})
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=True,
        assert_write=lambda: None, max_requests=0, fetch_batch=lambda *_args: pytest.fail("network fallback should not run"))
    assert value["ok"] and value["imported_symbols"] == [code]
    assert value["requests_budgeted"] == 0


def test_request_bound_and_unknown_inflight_attempts_are_reported(tmp_path):
    fixture_project(tmp_path)
    put(tmp_path, f"results/candidate_refresh/daily/{TARGET}.json", {"target_date": TARGET, "candidates": [{"symbol": CODE}, {"symbol": "300018"}, {"symbol": "300019"}]})
    calls = []
    def fetch(tasks, timeout):
        calls.append((tasks, timeout))
        original = response(tasks[0]["symbol"])
        return ProcessDeadlineResult({0: {"raw": original.raw, "proof": original.proof}}, {}, [1], True, [12345])
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=True,
        assert_write=lambda: None, max_requests=2, timeout_seconds=3, fetch_batch=fetch)
    assert len(calls[0][0]) == value["requests_budgeted"] == 2
    assert value["request_attempt_count"] == 1
    assert value["request_attempt_count_is_exact"] is False
    assert value["ok"] is False
    assert any("budget_exhausted" in reason for reason in value["reason_codes"])


def test_malformed_successful_response_is_retained_but_not_indexed(tmp_path):
    fixture_project(tmp_path)
    original = response()
    malformed = rebody(original, {"total": 99, "data": []})
    batch = ProcessDeadlineResult({0: {"raw": malformed.raw, "proof": malformed.proof}}, {}, [], False, [])
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=True,
        assert_write=lambda: None, fetch_batch=lambda *_: batch)
    assert not value["ok"]
    assert list((tmp_path / BASE / "blobs").glob("*.bin"))
    assert not (tmp_path / BASE / "index.json").exists()


def test_revoked_writer_cannot_publish_a_usable_factor_pointer(tmp_path):
    fixture_project(tmp_path)
    def revoked(): raise OSError("synthetic revoked lease")
    with pytest.raises(OSError):
        publish_factor_document(tmp_path, response(), CODE, [TARGET], assert_write=revoked)
    assert not (tmp_path / BASE / "index.json").exists()


def test_interruption_after_sealing_does_not_publish_success(tmp_path):
    fixture_project(tmp_path)
    checks = []
    def interrupt():
        checks.append(1)
        if len(checks) == 2: raise OSError("synthetic interruption")
    with pytest.raises(OSError):
        publish_factor_document(tmp_path, response(), CODE, [TARGET], assert_write=interrupt)
    assert factor_window(tmp_path, CODE, [TARGET])["status"] == "incomplete"


def test_source_bytes_tampering_invalidates_cached_coverage(tmp_path):
    fixture_project(tmp_path)
    ref = ready_factor(tmp_path)
    doc = json.loads((tmp_path / ref["capture_path"]).read_bytes())
    (tmp_path / doc["raw_source"]["capture_path"]).write_bytes(b"tampered")
    assert factor_window(tmp_path, CODE, [TARGET])["status"] == "incomplete"


def test_factor_phase_cannot_complete_with_an_unusable_scope(tmp_path):
    fixture_project(tmp_path, quantity=0)
    (tmp_path / f"results/candidate_refresh/daily/{TARGET}.json").unlink()
    value = maintain_adjustment_evidence(tmp_path, build_requirements(tmp_path, TARGET), network=True,
        assert_write=lambda: None, fetch_batch=lambda *_: pytest.fail("an unknown scope cannot trigger fetching"))
    assert value["ok"] is False
    assert value["status"] == "incomplete"
    assert "candidate_batch_missing" in value["reason_codes"]


def test_one_sina_task_never_expands_to_redirect_requests(monkeypatch):
    import requests
    from quantdesk.adjustment_factor_evidence import _sina_worker
    packet = response()
    attempted_urls = []

    class Reply:
        def __init__(self, status): self.status_code, self.headers = status, {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def iter_content(self, _): yield packet.raw

    def get(url, **kwargs):
        attempted_urls.append(url)
        if kwargs.get("allow_redirects", True):
            attempted_urls.append("https://other.invalid/redirected-factor")
            return Reply(200)
        return Reply(302)

    monkeypatch.setattr(requests, "get", get)
    result = _sina_worker({"symbol": CODE, "delay_seconds": 0}, {})
    assert len(attempted_urls) == 1
    assert result["proof"]["http_status"] == 302
    with pytest.raises(ValueError, match="response_identity"):
        factor_document(FactorResponse(result["raw"], result["proof"]), CODE, [TARGET])
