"""Seal review inputs and original actions without changing the strategy book."""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantdesk.daily_evidence_contract import (
    WriteCheck, aware_stamp, day, digest, json_bytes, load_ref, price_window,
    read_bytes, read_object, safe_path, seal_bytes, symbol, write_json,
)

SCHEMA = "strategy_evidence.v1"
BASE = Path("results/strategy_evidence")
BUY_ACTIONS = {"buy", "add", "breakout_add"}
SELL_ACTIONS = {"sell", "sell_all", "stop_loss", "reduce_half", "take_profit", "reduce"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _key(row: dict[str, Any]) -> str:
    return json.dumps([symbol(row.get("symbol")), str(row.get("strategy_type") or "undeclared")], separators=(",", ":"))


def _quantities(book: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in book.get("positions", []):
        key = _key(row)
        value = row.get("quantity")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or key in result:
            raise ValueError("strategy_position_quantity_or_key_invalid")
        result[key] = value
    return result


def _load_manifest(root: Path, pointer_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer = read_object(root, pointer_path)
    if pointer.get("schema") != SCHEMA:
        raise ValueError("strategy_archive_pointer_invalid")
    doc = json.loads(load_ref(root, pointer))
    if doc.get("schema") != SCHEMA or doc.get("target_date") != pointer.get("target_date"):
        raise ValueError("strategy_archive_identity_mismatch")
    for ref in doc.get("dependencies", []):
        load_ref(root, ref)
    return doc, pointer


def inspect_strategy_archive(root: Path, target_date: str) -> dict[str, Any]:
    target = day(target_date)
    try:
        doc, ref = _load_manifest(root, BASE / target / "latest.json")
        if digest(read_bytes(root, "state/auto_paper_portfolio.json")) != doc["after_state"]["capture_sha256"]:
            raise ValueError("strategy_archive_book_version_mismatch")
        reasons = list(doc.get("current_reason_codes", []))
        return {"status": "ready" if not reasons else "incomplete", "target_date": target,
                "manifest_reference": ref, "recorded_at": doc["recorded_at"],
                "trade_count": len(doc["trade_events"]), "reason_codes": reasons,
                "legacy_history_gaps": doc.get("legacy_history_gaps", []),
                "historical_linkage_ready": not doc.get("legacy_history_gaps")}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        return {"status": "incomplete", "reason_codes": [f"strategy_archive_missing_or_unusable:{type(exc).__name__}"],
                "legacy_history_gaps": ["old_action_archives_and_original_decision_times_not_reconstructed"]}


def archived_price_bytes(root: Path, target_date: str, code: str) -> bytes:
    doc, _ = _load_manifest(root, BASE / day(target_date) / "latest.json")
    source = doc["price_sources"][symbol(code)]
    return load_ref(root, source)


def archived_calendar_bytes(root: Path, target_date: str) -> bytes:
    try:
        doc, _ = _load_manifest(root, BASE / day(target_date) / "latest.json")
    except FileNotFoundError:
        doc, _ = _load_manifest(root, BASE / "latest.json")
    return load_ref(root, doc["calendar_source"])


def archive_strategy_review(root: Path, *, target_date: str, run_id: str, before_raw: bytes | None,
                             after_raw: bytes | None, started_at: str | datetime, recorded_at: str | datetime,
                             process_status: str, requirements: dict[str, Any], assert_write: WriteCheck) -> dict[str, Any]:
    """New reviews get an actual aware capture interval, never a backdated time.

    The book and action dictionaries remain byte-for-byte originals. Linkage is
    separate metadata; unexplained quantities and missing legacy entries stay
    missing rather than being inferred from summaries or realised PnL.
    """
    target = day(target_date)
    first, last = aware_stamp(started_at), aware_stamp(recorded_at)
    first_dt, last_dt = datetime.fromisoformat(first), datetime.fromisoformat(last)
    if first_dt > last_dt:
        raise ValueError("strategy_archive_capture_order_invalid")
    session = digest(json_bytes({"target_date": target, "run_id": str(run_id)}))
    manifest_path = BASE / target / f"{session}.json"
    if safe_path(root, manifest_path).is_file():
        original = read_bytes(root, manifest_path)
        existing = json.loads(original.decode("utf-8-sig"))
        if (existing.get("schema") != SCHEMA or existing.get("target_date") != target
                or existing.get("run_id") != str(run_id)):
            raise ValueError("strategy_archive_identity_mismatch")
        if (existing.get("after_state", {}).get("capture_sha256") != (digest(after_raw) if after_raw else None)
                or existing.get("before_sha256") != (digest(before_raw) if before_raw else None)):
            raise ValueError("strategy_archive_run_version_conflict")
        for ref in existing.get("dependencies", []):
            load_ref(root, ref)
        pointer = {"schema": SCHEMA, "target_date": target, "capture_path": manifest_path.as_posix(),
                   "capture_sha256": digest(original), "bytes": len(original)}
        original_order = (target, datetime.fromisoformat(aware_stamp(existing["recorded_at"])))
        for relative in (BASE / target / "latest.json", BASE / "latest.json"):
            pointer_path = safe_path(root, relative)
            if pointer_path.exists():
                current, _ = _load_manifest(root, relative)
                current_order = (current["target_date"], datetime.fromisoformat(aware_stamp(current["recorded_at"])))
                if current_order >= original_order:
                    continue
            # Resume publication of the sealed original, preserving its time
            # and any newer run already visible through the lookup pointers.
            write_json(pointer_path, pointer, assert_write=assert_write)
        return {"status": existing["status"], "reused": True, "manifest_path": manifest_path.as_posix(),
                "manifest_sha256": digest(original), "reason_codes": existing["current_reason_codes"]}
    before = json.loads(before_raw.decode("utf-8-sig")) if before_raw else {"positions": []}
    after = json.loads(after_raw.decode("utf-8-sig")) if after_raw else {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("strategy_archive_book_object_invalid")
    reasons: list[str] = []
    legacy: list[str] = []
    prospective = (first_dt.astimezone(SHANGHAI).date().isoformat() == target
                   and last_dt.astimezone(SHANGHAI).date().isoformat() == target)
    if not prospective:
        reasons.append("archive_observed_later;not_original_decision_time")
    if process_status not in {"completed", "daily_completed"}:
        reasons.append("review_process_not_completed")
    if after.get("valuation_date") != target:
        reasons.append("strategy_archive_business_date_not_current")
    original_actions = after.get("actions")
    if not isinstance(original_actions, list):
        original_actions = []
        reasons.append("strategy_original_actions_missing")
    dependencies: list[dict[str, Any]] = []

    def seal(raw: bytes, suffix: str) -> dict[str, Any]:
        ref = seal_bytes(root, BASE / "blobs", raw, suffix=suffix, assert_write=assert_write)
        if ref not in dependencies:
            dependencies.append(ref)
        return ref

    before_ref = seal(before_raw, "json") if before_raw else None
    after_ref = seal(after_raw, "json") if after_raw else None
    requirement_ref = seal(json_bytes(requirements), "json")
    try:
        calendar_ref = seal(read_bytes(root, "data/calendar/a_share_trading_calendar.csv"), "csv")
    except (OSError, ValueError):
        calendar_ref = None
        reasons.append("strategy_calendar_original_missing")
    old_lots: dict[str, Any] = {}
    previous_bound = False
    try:
        previous, previous_ref = _load_manifest(root, BASE / "latest.json")
        legacy.extend(previous.get("legacy_history_gaps", []))
        if (before_raw and previous.get("status") == "ready"
                and previous["after_state"]["capture_sha256"] == digest(before_raw)):
            previous_bound = True
            old_lots = previous["open_lots_after"]
            dependencies.append({key: previous_ref[key] for key in ("capture_path", "capture_sha256", "bytes")})
        elif before.get("positions"):
            legacy.append("prior_archive_does_not_match_pre_review_book")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        if before.get("positions"):
            legacy.append("legacy_entries_have_no_original_open_event")
    if before.get("closed_positions") and not previous_bound:
        legacy.append("legacy_closed_positions_not_reconstructed")

    execution_codes: set[str] = {code for window in requirements.get("windows", []) if window["scope"] == "current" for code in window["symbols"]}
    for action in original_actions:
        if not isinstance(action, dict):
            reasons.append("strategy_original_action_invalid")
            continue
        try:
            execution_codes.add(symbol(action.get("symbol")))
        except ValueError:
            if action.get("action") in BUY_ACTIONS | SELL_ACTIONS:
                reasons.append("strategy_trade_symbol_invalid")
    # Registered historical materials must also survive an empty-directory
    # restore. Their basis does not enlarge the current execution-price set.
    codes = execution_codes | {code for window in requirements.get("windows", []) for code in window["symbols"]}
    price_sources: dict[str, Any] = {}
    price_rows: dict[str, dict[str, str]] = {}
    frames: dict[str, Any] = {}
    from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
    import pandas as pd

    for code in sorted(codes):
        try:
            raw = read_bytes(root, Path("data/daily") / f"{code}.csv")
            # Keep even invalid originals. Their invalid basis cannot be fixed
            # by sorting/filtering the recorded target row.
            price_sources[code] = seal(raw, "csv")
            frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
            classify_frame_price_basis(frame)
            if code in execution_codes:
                frames[code] = frame
                check = price_window(root, code, [target])
                if check["status"] == "ready":
                    price_rows[code] = frame.loc[frame["date"].eq(target)].iloc[0].to_dict()
        except (OSError, ValueError, KeyError, UnicodeError):
            if code in execution_codes:
                reasons.append(f"strategy_price_original_missing_or_invalid:{code}")
    if frames and validate_price_frame_set(frames, consumer="paper_execution").get("eligible") is not True:
        price_rows = {}
        reasons.append("strategy_price_universe_basis_invalid")
    from quantdesk.adjustment_factor_evidence import factor_window
    from quantdesk.daily_evidence_contract import calendar_dates
    for window in requirements.get("windows", []):
        for code in window["symbols"]:
            try:
                dates = calendar_dates(root, window["start_date"], window["end_date"])
                factor = factor_window(root, code, dates, policy=window["factor_policy"])
                if factor.get("source_reference") and factor["source_reference"] not in dependencies:
                    dependencies.append(factor["source_reference"])
            except (OSError, ValueError, UnicodeError):
                pass  # the shared material checklist separately reports the gap

    try:
        quantities, expected = _quantities(before), _quantities(after)
    except (ValueError, TypeError, KeyError, AttributeError):
        quantities, expected = {}, {}
        reasons.append("strategy_quantity_linkage_unverified")
    lots = dict(old_lots)
    events = []
    for index, action in enumerate(original_actions):
        if not isinstance(action, dict) or action.get("action") not in BUY_ACTIONS | SELL_ACTIONS:
            continue
        event_id = digest(json_bytes({"run_id": str(run_id), "action_index": index, "original_action": action}))
        event: dict[str, Any] = {"event_id": event_id, "action_index": index, "original_action": action,
            "recorded_at": last, "capture_started_at": first,
            "time_evidence_kind": "prospective_review_capture" if prospective else "observed_later",
            "is_pretrade_prediction": False, "binding_status": "unverified", "linkage_verified": False, "reason_codes": []}
        try:
            key, code = _key(action), symbol(action["symbol"])
            qty = action["quantity"]
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                raise ValueError("action_quantity_invalid")
            current = quantities.get(key, 0)
            lot = lots.get(key, {"quantity": current, "open_event_ids": [], "reduction_event_ids": [],
                                  "original_entry_present": current == 0})
            if action["action"] in BUY_ACTIONS:
                new_qty = current + qty
                lot = {**lot, "quantity": new_qty, "open_event_ids": [*lot["open_event_ids"], event_id]}
                event["link_kind"] = "open" if current == 0 else "add"
            else:
                if current < qty:
                    raise ValueError("action_quantity_exceeds_original_position")
                new_qty = current - qty
                event["link_kind"] = "full_exit" if new_qty == 0 else "partial_exit"
                event["linked_open_event_ids"] = list(lot["open_event_ids"])
                event["linked_reduction_event_ids"] = list(lot["reduction_event_ids"])
                lot = {**lot, "quantity": new_qty, "reduction_event_ids": [*lot["reduction_event_ids"], event_id]}
            if current > 0 and not lot["original_entry_present"]:
                event["reason_codes"].append("legacy_open_event_missing")
                legacy.append(f"legacy_open_event_missing:{code}")
            quantities[key] = new_qty
            if new_qty:
                lots[key] = lot
            else:
                lots.pop(key, None)
            event.update(lot_key=key, quantity_before=current, quantity_after=new_qty,
                         price_original=price_sources.get(code))
            quote = price_rows.get(code)
            if action.get("decision_date") != target or quote is None:
                raise ValueError("action_date_or_raw_close_unverified")
            if abs(float(action.get("price")) - float(quote["close"])) > 0.00005:
                raise ValueError("action_price_does_not_match_raw_close")
            declared = action.get("execution_price_source_sha256")
            if declared and declared != price_sources[code]["capture_sha256"]:
                raise ValueError("action_declared_price_fingerprint_mismatch")
            event["binding_status"] = "recorded_price_matches_sealed_raw_close"
            event["source_name"] = quote["source"]
            event["source_date"] = quote["date"]
            event["source_reference_kind"] = "declared_fingerprint_match" if declared else "observed_review_context"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            event["reason_codes"].append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            reasons.append(f"strategy_trade_binding_unverified:{index}")
        events.append(event)
    quantity_match = {k: v for k, v in quantities.items() if v} == {k: v for k, v in expected.items() if v}
    if not quantity_match:
        reasons.append("original_actions_do_not_explain_position_quantities")
    for event in events:
        event["linkage_verified"] = quantity_match and not event["reason_codes"] and event["binding_status"] != "unverified"
        if not quantity_match:
            event["link_kind"] = "unverified"
            event["reason_codes"].append("original_position_quantities_not_reconciled")
    old_closed = {digest(json_bytes(row)) for row in before.get("closed_positions", [])}
    for closed in after.get("closed_positions", []):
        if digest(json_bytes(closed)) in old_closed:
            continue
        matched = any(event.get("link_kind") == "full_exit" and event.get("quantity_after") == 0
                      and event.get("original_action", {}).get("symbol") == closed.get("symbol")
                      and event.get("original_action", {}).get("strategy_type") == closed.get("strategy_type")
                      and event.get("original_action", {}).get("decision_date") == closed.get("exit_date")
                      for event in events)
        if not matched:
            reasons.append("new_closed_record_without_original_exit_action")
            legacy.append("new_closed_record_exit_link_missing")
    doc = {"schema": SCHEMA, "target_date": target, "run_id": str(run_id), "recorded_at": last,
        "capture_started_at": first, "process_status": process_status,
        "status": "ready" if not reasons else "incomplete", "current_reason_codes": sorted(set(reasons)),
        "legacy_history_gaps": sorted(set(legacy)), "before_sha256": digest(before_raw) if before_raw else None,
        "before_state": before_ref, "after_state": after_ref, "requirements_source": requirement_ref,
        "calendar_source": calendar_ref, "quantity_linkage_verified": quantity_match,
        "trade_events": events, "open_lots_after": lots, "price_sources": price_sources,
        "dependencies": dependencies, "original_action_count": len(original_actions),
        "strategy_changed": False, "book_written": False, "historical_records_reconstructed": False,
        "research_only": True, "paper_only": True, "validation_label": "纸面·未验证"}
    # The per-run manifest is immutable. Only the small lookup pointers change.
    raw = json_bytes(doc)
    path = safe_path(root, manifest_path)
    assert_write()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
    pointer = {"schema": SCHEMA, "target_date": target, "capture_path": manifest_path.as_posix(),
               "capture_sha256": digest(raw), "bytes": len(raw)}
    write_json(safe_path(root, BASE / target / "latest.json"), pointer, assert_write=assert_write)
    write_json(safe_path(root, BASE / "latest.json"), pointer, assert_write=assert_write)
    return {"status": doc["status"], "reused": False, "manifest_path": manifest_path.as_posix(),
            "manifest_sha256": digest(raw), "reason_codes": doc["current_reason_codes"],
            "trade_count": len(events), "legacy_history_gaps": doc["legacy_history_gaps"]}
