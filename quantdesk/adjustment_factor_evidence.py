"""Bounded factor evidence maintenance without rewriting the legacy factor table.

Sina supplies a complete *event list*, not native daily rows. Expansion is
explicit, source-local and a research proxy. It never changes paper quantities,
cost bases or the separately unimplemented adjusted-signal/raw-fill bridge.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from quantdesk.daily_evidence_contract import (
    WriteCheck, aware_stamp, calendar_dates, day, digest, json_bytes, load_ref,
    read_object, safe_path, seal_bytes, symbol, write_json,
)

SCHEMA = "adjustment_evidence.v1"
BASE = Path("data/metadata/adjustment_evidence")
INDEX = BASE / "index.json"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUESTS = 32
MAX_REFRESH_SECONDS = 60.0
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FactorResponse:
    raw: bytes
    proof: dict[str, Any]


def sina_url(code: str) -> str:
    code = symbol(code)
    prefix = "sh" if code.startswith("6") else "sz" if code[0] in "023" else ""
    if not prefix:
        raise ValueError("sina_factor_market_unsupported")
    return f"https://finance.sina.com.cn/realstock/company/{prefix}{code}/hfq.js"


def _number(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("factor_original_decimal_text_required")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("factor_not_numeric") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("factor_not_positive_finite")
    return value


def parse_sina_events(raw: bytes, code: str) -> list[dict[str, str]]:
    """Parse data, never evaluate provider JavaScript or its comment trailer."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("factor_response_size_limit")
    market = sina_url(code).split("/")[-2]
    text = raw.decode("utf-8-sig")
    match = re.match(rf"\s*var\s+{re.escape(market)}hfq\s*=\s*", text)
    if not match:
        raise ValueError("sina_factor_symbol_or_wrapper_mismatch")
    value, end = json.JSONDecoder().raw_decode(text, match.end())
    trailer = text[end:].strip().removeprefix(";").strip()
    if trailer and re.fullmatch(r"/\*[\s\S]*\*/", trailer) is None:
        raise ValueError("sina_factor_unexpected_trailer")
    if not isinstance(value, dict) or set(value) != {"total", "data"}:
        raise ValueError("sina_factor_schema_invalid")
    events = value["data"]
    total = value["total"]
    if (isinstance(total, bool) or not isinstance(total, int) or not isinstance(events, list)
            or total != len(events) or not 2 <= total <= 10000):
        raise ValueError("sina_factor_event_count_incomplete")
    dates: list[str] = []
    parsed = []
    for item in events:
        if not isinstance(item, dict) or set(item) != {"d", "f"}:
            raise ValueError("sina_factor_event_schema_invalid")
        current = day(item["d"])
        dates.append(current)
        parsed.append({"date": current, "factor": _number(item["f"])})
    if len(set(dates)) != len(dates) or dates != sorted(dates, reverse=True):
        raise ValueError("sina_factor_event_order_or_duplicate")
    if dates[-1] != "1900-01-01":
        raise ValueError("sina_factor_seed_missing")
    return parsed


def factor_document(response: FactorResponse, code: str, dates: list[str]) -> dict[str, Any]:
    """Validate full source bytes and explicit sessions before any expansion."""
    code = symbol(code)
    if not dates or dates != sorted(set(map(day, dates))):
        raise ValueError("factor_required_sessions_invalid")
    proof = response.proof
    stamp = datetime.fromisoformat(aware_stamp(proof.get("finished_at", ""))).astimezone(SHANGHAI)
    if (proof.get("symbol") != code or proof.get("raw_sha256") != digest(response.raw)
            or proof.get("raw_bytes") != len(response.raw) or proof.get("complete_response") is not True):
        raise ValueError("factor_source_proof_mismatch")
    if stamp.date().isoformat() < dates[-1] or (stamp.date().isoformat() == dates[-1] and (stamp.hour, stamp.minute) < (15, 5)):
        raise ValueError("factor_response_before_completed_target")
    kind = proof.get("source_kind")
    rows: list[dict[str, str]] = []
    if kind == "sina_hfq_events":
        if proof.get("http_status") != 200 or proof.get("url") != sina_url(code):
            raise ValueError("sina_factor_response_identity_invalid")
        events = parse_sina_events(response.raw, code)
        if events[0]["date"] > stamp.date().isoformat() or dates[0] < events[-2]["date"]:
            raise ValueError("sina_factor_event_window_invalid")
        # An event is effective on its date. The seed remains provenance; it is
        # never a fabricated market session or a fallback for a missing event.
        ordered = list(reversed(events[:-1]))
        for current in dates:
            eligible = [event for event in ordered if event["date"] <= current]
            if not eligible:
                raise ValueError("sina_factor_effective_event_missing")
            event = eligible[-1]
            rows.append({"trade_date": current, "symbol": code, "adj_factor": event["factor"],
                         "effective_event_date": event["date"], "source": "sina_hfq_event_expansion"})
        result_kind, normalization = "derived_sina_hfq_events", "sina_hfq_source_local"
        coverage = "provider_total_equals_returned_events; response_after_target; explicit_local_sessions"
    elif kind == "native_daily_csv":
        source = str(proof.get("source") or "").strip()
        if not source:
            raise ValueError("native_factor_source_missing")
        original = list(csv.DictReader(io.StringIO(response.raw.decode("utf-8-sig"))))
        found: dict[str, dict[str, str]] = {}
        for row in original:
            current = day(row.get("trade_date"))
            if current > stamp.date().isoformat():
                raise ValueError("native_factor_row_after_response")
            if symbol(row.get("symbol")) != code or row.get("source") != source or current in found:
                raise ValueError("native_factor_symbol_source_or_duplicate")
            found[current] = {"trade_date": current, "symbol": code, "adj_factor": _number(row.get("adj_factor")), "source": source}
        if set(dates) - set(found):
            raise ValueError("native_factor_daily_window_missing")
        rows = [found[current] for current in dates]
        result_kind, normalization = "native_daily", source
        coverage = "native_returned_daily_rows_cover_explicit_local_sessions"
    else:
        raise ValueError("factor_source_kind_unknown")
    return {"schema": SCHEMA, "symbol": code, "kind": result_kind, "normalization": normalization,
            "source_proof": dict(proof), "required_dates": dates, "rows": rows, "coverage_method": coverage,
            "native_daily_rows": result_kind == "native_daily", "research_proxy": result_kind != "native_daily",
            "market_completeness_independently_verified": False,
            "observed_after_window": stamp.date().isoformat() > dates[-1],
            "canonical_quantity_cost_basis_eligible": False, "cross_provider_normalization_verified": False,
            "future_dual_track_bridge_active": False, "research_only": True}


def publish_factor_document(root: Path, response: FactorResponse, code: str, dates: list[str], *,
                            assert_write: WriteCheck) -> dict[str, Any]:
    doc = factor_document(response, code, dates)
    doc["raw_source"] = seal_bytes(root, BASE / "blobs", response.raw, suffix="bin", assert_write=assert_write)
    ref = seal_bytes(root, BASE / dates[-1], json_bytes(doc), suffix="json", assert_write=assert_write)
    try:
        index = read_object(root, INDEX)
        if index.get("schema") != "adjustment_evidence_index.v1" or not isinstance(index.get("symbols"), dict):
            raise ValueError("factor_index_invalid")
    except FileNotFoundError:
        index = {"schema": "adjustment_evidence_index.v1", "symbols": {}}
    index["symbols"][code] = ref
    write_json(safe_path(root, INDEX), index, assert_write=assert_write)
    return ref


def factor_window(root: Path, code: str, dates: list[str], *, policy: str = "native_or_source_verified_derived") -> dict[str, Any]:
    if not dates:
        return {"status": "ready", "kind": "no_requested_sessions", "reason_codes": []}
    try:
        index = read_object(root, INDEX)
        if index.get("schema") != "adjustment_evidence_index.v1":
            raise ValueError("factor_index_invalid")
        ref = index["symbols"][symbol(code)]
        doc = json.loads(load_ref(root, ref))
        raw = load_ref(root, doc["raw_source"])
        validated = factor_document(FactorResponse(raw, doc["source_proof"]), code, doc["required_dates"])
        if any(doc.get(key) != value for key, value in validated.items()):
            raise ValueError("factor_derived_document_mismatch")
        missing = sorted(set(dates) - set(doc["required_dates"]))
        reasons = ["factor_daily_window_missing"] if missing else []
        if policy == "native_only" and doc["kind"] != "native_daily":
            reasons.append("native_daily_factor_rows_required;derived_source_is_separate")
        return {"status": "ready" if not reasons else "incomplete", "kind": doc["kind"],
                "normalization": doc["normalization"], "research_proxy": doc["research_proxy"],
                "observed_after_window": doc["observed_after_window"], "missing_dates": missing,
                "source_reference": ref, "reason_codes": reasons,
                "canonical_quantity_cost_basis_eligible": False}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        return {"status": "incomplete", "kind": "missing_or_unverified", "missing_dates": dates,
                "reason_codes": [f"factor_evidence_missing_or_unusable:{type(exc).__name__}"]}


def _sina_worker(task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """One paced request in a killable process, with no filesystem publication."""
    import requests

    if task["delay_seconds"]:
        time.sleep(task["delay_seconds"])
    code = task["symbol"]
    url = sina_url(code)
    with requests.get(url, timeout=(5, 10), stream=True, allow_redirects=False) as response:
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("factor_response_size_limit")
            chunks.append(chunk)
        raw = b"".join(chunks)
        return {"raw": raw, "proof": {"symbol": code, "url": url, "source_kind": "sina_hfq_events",
                "http_status": response.status_code, "complete_response": True,
                "raw_sha256": digest(raw), "raw_bytes": len(raw),
                "finished_at": datetime.now(SHANGHAI).isoformat(),
                "last_modified": response.headers.get("Last-Modified", "")}}


def _fetch_batch(tasks: list[dict[str, Any]], timeout: float) -> Any:
    from quantdesk.process_deadline import run_process_tasks_until_deadline
    return run_process_tasks_until_deadline(_sina_worker, tasks, context={}, max_workers=1, timeout_seconds=timeout)


def maintain_adjustment_evidence(root: Path, requirements: Mapping[str, Any], *, network: bool = False,
                                 assert_write: WriteCheck | None = None, max_requests: int = MAX_REQUESTS,
                                 timeout_seconds: float = MAX_REFRESH_SECONDS,
                                 fetch_batch: Callable[[list[dict[str, Any]], float], Any] = _fetch_batch) -> dict[str, Any]:
    """Local native import/cache first, then a bounded Sina fallback for gaps."""
    if (isinstance(max_requests, bool) or not 0 <= max_requests <= MAX_REQUESTS
            or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= MAX_REFRESH_SECONDS):
        raise ValueError("factor_maintenance_budget_invalid")
    needed: dict[str, set[str]] = {}
    policies: dict[str, str] = {}
    failures: dict[str, list[str]] = {}
    for window in requirements["windows"]:
        for code in window["symbols"]:
            try:
                dates = calendar_dates(root, window["start_date"], window["end_date"])
                needed.setdefault(symbol(code), set()).update(dates)
                if window["factor_policy"] == "native_only":
                    policies[code] = "native_only"
            except (OSError, ValueError, UnicodeError) as exc:
                failures.setdefault(str(code), []).append(f"factor_calendar_unusable:{type(exc).__name__}")
    from quantdesk.daily_evidence_contract import MAX_SYMBOLS
    if len(needed) > MAX_SYMBOLS:
        return {"ok": False, "status": "incomplete", "reason_codes": ["factor_scope_limit"], "requests_budgeted": 0}
    result: dict[str, Any] = {"ok": False, "status": "planned_offline" if not network else "incomplete",
        "target_date": requirements["target_date"], "requirements_sha256": requirements["requirements_sha256"],
        "network": network, "requests_budgeted": 0, "request_attempt_count": 0,
        "request_attempt_count_is_exact": True, "reused_symbols": [], "imported_symbols": [],
        "refreshed_symbols": [], "per_symbol": {}, "reason_codes": [], "legacy_factor_table_written": False}
    scope_reasons = sorted(set(requirements.get("reason_codes", [])))
    if scope_reasons:
        result["status"] = "incomplete"
        result["reason_codes"] = scope_reasons
        return result
    pending = []
    for code, values in needed.items():
        dates = sorted(values)
        local = factor_window(root, code, dates, policy=policies.get(code, "native_or_source_verified_derived"))
        if local["status"] == "ready":
            result["reused_symbols"].append(code)
            result["per_symbol"][code] = local
            continue
        # An explicit local native import is a second source. It must retain
        # exact response bytes and declared provenance, not just scalar factors.
        native = BASE / "imports" / f"{code}.json"
        if safe_path(root, native).is_file():
            try:
                imported = read_object(root, native)
                raw = load_ref(root, imported["raw_source"])
                response = FactorResponse(raw, imported["source_proof"])
                if response.proof.get("source_kind") != "native_daily_csv":
                    raise ValueError("native_import_kind_invalid")
                factor_document(response, code, dates)
                if network and assert_write is not None:
                    publish_factor_document(root, response, code, dates, assert_write=assert_write)
                    result["imported_symbols"].append(code)
                    result["per_symbol"][code] = factor_window(root, code, dates, policy=policies.get(code, "native_or_source_verified_derived"))
                    continue
            except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
                failures.setdefault(code, []).append(f"native_import_unusable:{type(exc).__name__}")
        pending.append(code)
        result["per_symbol"][code] = local
    if network:
        if assert_write is None:
            raise ValueError("factor_writer_lease_required")
        assert_write()
        selected = []
        for code in pending:
            try:
                sina_url(code)
            except ValueError:
                failures.setdefault(code, []).append("sina_factor_market_unsupported;native_import_required")
                continue
            if len(selected) < max_requests and timeout_seconds > 0:
                selected.append(code)
            else:
                failures.setdefault(code, []).append("factor_request_or_time_budget_exhausted")
        tasks = [{"symbol": code, "delay_seconds": 0.75 if index else 0.0} for index, code in enumerate(selected)]
        result["requests_budgeted"] = len(tasks)
        if tasks:
            batch = fetch_batch(tasks, timeout_seconds)
            assert_write()
            # A killed process cannot certify whether its unfinished request
            # started; report the bound and ambiguity rather than invent zero.
            result["request_attempt_count_is_exact"] = not batch.unfinished_indices
            result["request_attempt_count"] = len(batch.results) + len(batch.failures)
            result["unfinished_symbols"] = [selected[i] for i in batch.unfinished_indices]
            for index, code in enumerate(selected):
                if index not in batch.results:
                    failures.setdefault(code, []).append("factor_fetch_incomplete" if index in batch.unfinished_indices else "factor_fetch_failed")
                    continue
                try:
                    item = batch.results[index]
                    response = FactorResponse(item["raw"], item["proof"])
                    # Retain unsuccessful/malformed responses as source evidence
                    # too; they never enter the usable-factor index.
                    raw_ref = seal_bytes(root, BASE / "blobs", response.raw, suffix="bin", assert_write=assert_write)
                    attempt = {"schema": SCHEMA, "kind": "provider_attempt", "symbol": code, "raw_source": raw_ref,
                               "source_proof": response.proof, "target_date": requirements["target_date"]}
                    seal_bytes(root, BASE / requirements["target_date"], json_bytes(attempt), suffix="json", assert_write=assert_write)
                    publish_factor_document(root, response, code, sorted(needed[code]), assert_write=assert_write)
                    result["refreshed_symbols"].append(code)
                    result["per_symbol"][code] = factor_window(root, code, sorted(needed[code]), policy=policies.get(code, "native_or_source_verified_derived"))
                except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
                    failures.setdefault(code, []).append(f"factor_source_not_accepted:{type(exc).__name__}")
    for code, reasons in failures.items():
        result["per_symbol"].setdefault(code, {"status": "incomplete", "reason_codes": []})
        result["per_symbol"][code]["reason_codes"] = [*result["per_symbol"][code].get("reason_codes", []), *reasons]
    result["reason_codes"] = sorted({reason for value in result["per_symbol"].values() for reason in value.get("reason_codes", [])})
    result["ok"] = not failures and all(value["status"] == "ready" for value in result["per_symbol"].values())
    if network:
        result["status"] = "completed" if result["ok"] else "incomplete"
    return result
