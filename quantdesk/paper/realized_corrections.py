"""Append-only weighted-cost evidence; no filesystem or network mutations."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Mapping

CENT = Decimal("0.01")
AUTHORITY = "CHARTER.md section 12 decision 19"
FORMULA = "net_amount - avg_cost * exit_quantity"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _aware(value: Any) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.tzinfo is not None and stamp.utcoffset() is not None
    except (ValueError, TypeError):
        return False


def record_digest(row: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _costs(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, str] | None:
    quantity_field = "exit_quantity" if row.get("exit_quantity") is not None else "quantity"
    values = [_number(row.get(key)) for key in
              ("entry_price", "avg_cost", "net_amount", "realized_pnl", quantity_field)]
    if any(value is None for value in values):
        return None
    entry, average, net, recorded, quantity = values
    if min(entry, average, quantity) <= 0:
        return None
    return entry, average, net, recorded, quantity, quantity_field


@dataclass(frozen=True)
class AdjustmentAmounts:
    legacy_base: Decimal | None
    corrections: Decimal

    def payload(self, valid: bool) -> dict[str, Any]:
        return {
            "legacy_realized_adjustment": float(_money(self.legacy_base)) if self.legacy_base is not None else None,
            "realized_corrections_total": float(_money(self.corrections)) if valid else None,
            "effective_realized_adjustment": float(_money(self.legacy_base + self.corrections))
                if valid and self.legacy_base is not None else None,
        }


def inspect_realized_adjustment(state: Mapping[str, Any], *, legacy_base: float | None = None) -> dict[str, Any]:
    """Validate original rows, arithmetic, proofs and duplicate correction IDs."""
    base = _number(legacy_base if legacy_base is not None else state.get("legacy_realized_adjustment"))
    rows = state.get("realized_corrections", [])
    errors: list[str] = []
    applied: list[dict[str, Any]] = []
    total = Decimal(0)
    if not isinstance(rows, list):
        rows = []
        errors.append("paper_realized_corrections_not_list")
    if rows and base is None:
        errors.append("paper_realized_corrections_legacy_base_missing")
    originals = state.get("closed_positions", [])
    by_digest: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    if isinstance(originals, list):
        for index, original in enumerate(originals):
            if isinstance(original, dict):
                try:
                    by_digest.setdefault(record_digest(original), []).append((index, original))
                except (ValueError, TypeError):
                    pass
    seen: set[str] = set()
    for index, row in enumerate(rows):
        code = f"paper_realized_correction_{index}_invalid"
        try:
            if not isinstance(row, dict):
                raise ValueError("not_object")
            fingerprint = row["source_record_digest"]
            if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
                raise ValueError("record_digest")
            correction_id = row["correction_id"]
            if correction_id != "weighted_cost:" + fingerprint or correction_id in seen:
                raise ValueError("id_or_duplicate")
            seen.add(correction_id)
            matches = by_digest.get(fingerprint, [])
            if row.get("record_group") != "closed_positions" or len(matches) != 1:
                raise ValueError("original_record_missing_or_ambiguous")
            source_index, original = matches[0]
            costs = _costs(original)
            if costs is None:
                raise ValueError("original_cost_unknown")
            entry, average, net, recorded, quantity, quantity_field = costs
            old, corrected, delta = (_number(row.get(key)) for key in
                                     ("original_realized_pnl", "corrected_realized_pnl", "delta"))
            if None in (old, corrected, delta) or any(_money(value) != value for value in (old, corrected, delta)):
                raise ValueError("nonfinite_or_subcent_amount")
            if (old != _money(recorded) or old != _money(net - entry * quantity)
                    or corrected != _money(net - average * quantity)
                    or delta != corrected - old or delta == 0 or row.get("quantity_field") != quantity_field):
                raise ValueError("amount_or_formula_mismatch")
            if any(row.get(key) != original.get(key) for key in ("symbol", "entry_date", "exit_date")):
                raise ValueError("original_identity_mismatch")
            if row.get("calculation") != FORMULA or row.get("authority") != AUTHORITY:
                raise ValueError("policy_mismatch")
            evidence = row["evidence"]
            if not isinstance(evidence, dict) or not _aware(evidence.get("observed_at")):
                raise ValueError("source_time_not_aware")
            if (type(evidence.get("source_record_index")) is not int
                    or evidence["source_record_index"] != source_index
                    or not isinstance(evidence.get("source_ledger_sha256"), str)
                    or not SHA256.fullmatch(evidence["source_ledger_sha256"])):
                raise ValueError("source_proof_invalid")
            raw_record = evidence["original_record_json"].encode("utf-8")
            if (hashlib.sha256(raw_record).hexdigest() != evidence.get("original_record_sha256")
                    or json.loads(raw_record) != original):
                raise ValueError("original_source_bytes_mismatch")
            total += delta
            applied.append({"correction_id": correction_id, "source_record_digest": fingerprint,
                "source_record_index": source_index, "original_realized_pnl": float(old),
                "corrected_realized_pnl": float(corrected), "delta": float(delta),
                "symbol": row["symbol"], "entry_date": row.get("entry_date"), "exit_date": row.get("exit_date")})
        except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation) as exc:
            errors.append(code + ":" + str(exc))
    expected = {"realized_corrections_total": total,
                "effective_realized_adjustment": base + total if base is not None else None}
    for key, value in expected.items():
        if rows and key not in state:
            errors.append("paper_realized_corrections_" + key + "_missing")
        elif key in state and (value is None or _number(state[key]) != _money(value)):
            errors.append("paper_realized_corrections_" + key + "_mismatch")
    return {"status": "invalid" if errors else "valid" if rows else "legacy",
        **AdjustmentAmounts(base, total).payload(not errors),
        "reason_codes": errors, "applied_corrections": applied if not errors else [],
        "historical_cashflow_verified": False, "read_only": True}


def prepare_cost_correction_state(state: Mapping[str, Any], *, observed_at: str,
                                  source_ledger_sha256: str,
                                  original_record_json: Mapping[int, str] | None = None) -> dict[str, Any]:
    """Return a proposed state using recorded raw valuations; never infer prices."""
    if not _aware(observed_at) or not SHA256.fullmatch(source_ledger_sha256):
        raise ValueError("cost_correction_source_proof_invalid")
    previous = inspect_realized_adjustment(state)
    if previous["status"] == "invalid" or previous["legacy_realized_adjustment"] is None:
        raise ValueError("cost_correction_existing_evidence_invalid:" + ",".join(previous["reason_codes"]))
    result = copy.deepcopy(dict(state))
    for key in ("positions", "closed_positions", "realized_reductions"):
        if not isinstance(result.get(key), list) or any(not isinstance(row, dict) for row in result[key]):
            raise ValueError("cost_correction_" + key + "_invalid")
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    corrections = copy.deepcopy(result.get("realized_corrections", []))
    covered = {row["source_record_digest"] for row in corrections}
    for index, row in enumerate(result["closed_positions"]):
        costs = _costs(row)
        if costs is None:
            skipped.append({"source_record_index": index, "reason": "known_cost_fields_missing"})
            continue
        entry, average, net, recorded, quantity, quantity_field = costs
        old, corrected = _money(recorded), _money(net - average * quantity)
        fingerprint = record_digest(row)
        if fingerprint in covered or corrected == old:
            continue
        if old != _money(net - entry * quantity):
            skipped.append({"source_record_index": index, "reason": "original_pnl_not_explained_by_recorded_cost"})
            continue
        raw_record = (original_record_json or {}).get(index)
        if raw_record is None:
            raw_record = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        correction = {"correction_id": "weighted_cost:" + fingerprint, "record_group": "closed_positions",
            "source_record_digest": fingerprint, "symbol": row.get("symbol"),
            "entry_date": row.get("entry_date"), "exit_date": row.get("exit_date"),
            "original_realized_pnl": float(old), "corrected_realized_pnl": float(corrected),
            "delta": float(corrected - old), "calculation": FORMULA,
            "quantity_field": quantity_field, "authority": AUTHORITY,
            "evidence": {"source_ledger_sha256": source_ledger_sha256, "source_record_index": index,
                "original_record_sha256": hashlib.sha256(raw_record.encode("utf-8")).hexdigest(),
                "original_record_json": raw_record, "observed_at": observed_at,
                "record_hash_kind": "original_json_bytes" if original_record_json is not None else "canonical_json"}}
        corrections.append(correction)
        added.append(correction)
        covered.add(fingerprint)
    result["realized_corrections"] = corrections
    delta_sum = sum((_number(row["delta"]) for row in corrections), Decimal(0))
    base = _number(result["legacy_realized_adjustment"])
    result["realized_corrections_total"] = float(_money(delta_sum))
    result["effective_realized_adjustment"] = float(_money(base + delta_sum))
    for row in result["positions"]:
        entry, average = _number(row.get("entry_price")), _number(row.get("avg_cost"))
        if entry is None or average is None or entry == average:
            continue
        quantity, value, price, pnl = (_number(row.get(key)) for key in
                                      ("quantity", "market_value", "latest_price", "unrealized_pnl"))
        adjusted = _number(row.get("adjusted_quantity_for_valuation"))
        if (None in (quantity, value, price, pnl) or min(entry, average, quantity, price) <= 0
                or row.get("valuation_price_basis") != "raw"
                or adjusted not in (None, quantity) or abs(value - price * quantity) > CENT):
            raise ValueError("cost_correction_recorded_valuation_unsupported:" + str(row.get("symbol")))
        average_pnl = _money(value - average * quantity)
        if _money(pnl) not in (_money(value - entry * quantity), average_pnl):
            raise ValueError("cost_correction_recorded_pnl_unexplained:" + str(row.get("symbol")))
        row["unrealized_pnl"] = float(average_pnl)
        percent = float(_money((price / average - 1) * 100))
        row.update(unrealized_return_pct=percent, return_pct=percent,
                   pnl_cost_basis="weighted_average_cost", pnl_unit_cost=float(average))
    def summed(rows: list[dict[str, Any]], field: str) -> Decimal:
        values = [_number(row.get(field)) for row in rows]
        if any(value is None for value in values):
            raise ValueError("cost_correction_recorded_" + field + "_missing")
        return sum(values, Decimal(0))
    market = summed(result["positions"], "market_value")
    cash, equity, initial = (_number(result.get(key)) for key in ("cash", "total_equity", "initial_cash"))
    if None in (cash, equity, initial) or abs(cash + market - equity) > CENT:
        raise ValueError("cost_correction_recorded_balance_invalid")
    realized = summed(result["closed_positions"], "realized_pnl") + summed(result["realized_reductions"], "realized_pnl") + base + delta_sum
    unrealized = summed(result["positions"], "unrealized_pnl")
    result["realized_pnl"], result["unrealized_pnl"] = float(_money(realized)), float(_money(unrealized))
    if abs(equity - (initial + _money(realized) + _money(unrealized))) > CENT:
        raise ValueError("cost_correction_remaining_identity_difference")
    inspected = inspect_realized_adjustment(result)
    if inspected["status"] == "invalid":
        raise ValueError("cost_correction_proposed_evidence_invalid:" + ",".join(inspected["reason_codes"]))
    # Reuse the shared read contract for the proposed accounting projection.
    # The local import avoids a module-initialization cycle; it performs no I/O.
    from quantdesk.paper.accounting.reconciliation import reconcile_paper_account

    refreshed = reconcile_paper_account(result)
    if refreshed["accounting_reconciliation"]["status"] != "consistent":
        raise ValueError("cost_correction_proposed_accounting_not_consistent")
    result["accounting_reconciliation"] = refreshed["accounting_reconciliation"]
    result["warnings"] = refreshed["warnings"]
    return {"state": result, "added_corrections": added, "skipped_records": skipped,
            "adjustment": inspected, "historical_cashflow_verified": False}


def _array_spans(raw: bytes, wanted: str) -> tuple[int, int, list[bytes]] | None:
    bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    text, decoder = raw[bom:].decode("utf-8"), json.JSONDecoder()
    def ws(index: int) -> int:
        while index < len(text) and text[index].isspace():
            index += 1
        return index
    cursor = ws(0)
    if text[cursor] != "{":
        raise ValueError("cost_correction_source_not_object")
    cursor = ws(cursor + 1)
    while text[cursor] != "}":
        key, cursor = decoder.raw_decode(text, cursor)
        cursor = ws(cursor)
        if text[cursor] != ":":
            raise ValueError("cost_correction_source_key_invalid")
        start = ws(cursor + 1)
        _, end = decoder.raw_decode(text, start)
        if key == wanted:
            if text[start] != "[":
                raise ValueError("cost_correction_source_array_invalid")
            records: list[bytes] = []
            index = ws(start + 1)
            while text[index] != "]":
                _, stop = decoder.raw_decode(text, index)
                records.append(text[index:stop].encode("utf-8"))
                index = ws(stop)
                if text[index] == ",":
                    index = ws(index + 1)
            return bom + len(text[:start].encode("utf-8")), bom + len(text[:end].encode("utf-8")), records
        cursor = ws(end)
        if text[cursor] == ",":
            cursor = ws(cursor + 1)
    return None


def prepare_cost_correction_bytes(raw: bytes, *, observed_at: str) -> dict[str, Any]:
    """Preserve original closed/reduction/action arrays in a proposed wire file."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("cost_correction_duplicate_json_key:" + key)
            result[key] = value
        return result

    state = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    if not isinstance(state, dict):
        raise ValueError("cost_correction_source_not_object")
    closed_span = _array_spans(raw, "closed_positions")
    if closed_span is None:
        raise ValueError("cost_correction_closed_originals_missing")
    prepared = prepare_cost_correction_state(state, observed_at=observed_at,
        source_ledger_sha256=hashlib.sha256(raw).hexdigest(),
        original_record_json={index: record.decode("utf-8") for index, record in enumerate(closed_span[2])})
    proposed = json.dumps(prepared["state"], indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
    for key in ("closed_positions", "realized_reductions", "actions", "daily_actions"):
        old, new = _array_spans(raw, key), _array_spans(proposed, key)
        if old is not None and new is not None:
            proposed = proposed[:new[0]] + raw[old[0]:old[1]] + proposed[new[1]:]
    for key in ("closed_positions", "realized_reductions", "actions", "daily_actions"):
        old, new = _array_spans(raw, key), _array_spans(proposed, key)
        if old is not None and (new is None or raw[old[0]:old[1]] != proposed[new[0]:new[1]]):
            raise ValueError("cost_correction_original_arrays_changed")
    if json.loads(proposed) != prepared["state"]:
        raise ValueError("cost_correction_wire_state_mismatch")
    return {**prepared, "proposed_bytes": proposed, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "proposed_sha256": hashlib.sha256(proposed).hexdigest(), "original_arrays_preserved": True}
