"""Bounded, provenance-checked reference updates prepared outside live metadata.

The four historical files can be gigabytes. Planning reads only bounded new
staging files and old headers/stat; preparation streams each old file once.
Neither path replaces live metadata. A successful empty suspension response is
an observation receipt, never a fabricated row asserting every stock traded.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable

from quantdesk.data_metadata_schema import TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS

PROVIDER = "tushare_compatible_proxy"
SOURCE = "third_party_tushare_proxy_export"
FAMILIES = {"adjustment_factors": "adj_factor", "pre_close_history": "daily",
            "limit_price_history": "stk_limit", "suspension_history": "suspend_d"}
VALUE_FIELDS = {"adj_factor": ("adj_factor",), "daily": ("pre_close",),
                "stk_limit": ("pre_close", "up_limit", "down_limit"),
                "suspend_d": ("suspend_timing", "suspend_type")}


@dataclass(frozen=True)
class ReferenceRefreshConfig:
    staging_dir: Path
    metadata_dir: Path
    trade_dates: tuple[str, ...]
    max_days: int = 5
    max_rows_per_response: int = 10_000
    max_response_bytes: int = 32 * 1024 * 1024
    budget_seconds: float = 300.0


def _dates(config: ReferenceRefreshConfig) -> list[str]:
    dates = sorted({date.fromisoformat(value).isoformat() for value in config.trade_dates})
    if not dates or len(dates) > config.max_days or config.max_days > 20:
        raise ValueError("reference_trade_date_budget_exceeded_or_empty")
    if dates[-1] > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
        raise ValueError("future_reference_date")
    if (not 1 <= config.max_rows_per_response <= 20_000 or not math.isfinite(config.budget_seconds)
            or config.budget_seconds <= 0 or config.max_response_bytes <= 0):
        raise ValueError("invalid_reference_budget")
    return dates


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ValueError("reference_prepare_budget_exhausted")


def _checked_header(header: list[str] | None) -> list[str]:
    if not header or len(header) != len(set(header)) or any(not name for name in header):
        raise ValueError("reference_duplicate_or_empty_column")
    return header


def _normalize(api: str, rows: list[dict[str, Any]], trade_date: str) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: dict[str, dict[str, str]] = {}
    for raw in rows:
        raw_date = str(raw.get("trade_date") or "")
        raw_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}" if re.fullmatch(r"\d{8}", raw_date) else raw_date
        if raw_date != trade_date:
            raise ValueError("reference_row_date_mismatch_or_missing")
        code = str(raw.get("ts_code") or raw.get("symbol") or "")
        if not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", code):
            raise ValueError("reference_symbol_invalid")
        if raw.get("provider") not in (None, "", PROVIDER):
            raise ValueError("reference_provider_mismatch")
        row = {"trade_date": trade_date, "symbol": code[:6],
               "source": SOURCE + (":" + api if api in {"daily", "stk_limit"} else ""), "provider": PROVIDER}
        for field in VALUE_FIELDS[api]:
            value = str(raw.get(field) if raw.get(field) is not None else "").strip()
            if api != "suspend_d":
                try:
                    number = Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError(f"reference_invalid_{field}") from exc
                if not number.is_finite() or number <= 0:
                    raise ValueError(f"reference_invalid_{field}")
            elif field == "suspend_type" and value not in {"S", "R"}:
                raise ValueError("reference_suspend_type_unknown")
            row[field] = value
        if api == "stk_limit" and Decimal(row["down_limit"]) > Decimal(row["up_limit"]):
            raise ValueError("reference_limit_order_invalid")
        if code[:6] in seen and seen[code[:6]] != row:
            raise ValueError("reference_duplicate_key_conflict")
        if code[:6] not in seen:
            normalized.append(row)
            seen[code[:6]] = row
    if not normalized and api != "suspend_d":
        raise ValueError("reference_empty_price_response")
    return normalized


def _read_staged(config: ReferenceRefreshConfig, api: str, day: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    path = config.staging_dir / api / (day.replace("-", "") + ".csv")
    receipt_path = path.with_suffix(".receipt.json")
    if not path.is_file() or not receipt_path.is_file():
        raise ValueError(f"reference_source_or_receipt_missing:{api}:{day}")
    if path.stat().st_size > config.max_response_bytes or receipt_path.stat().st_size > 64 * 1024:
        raise ValueError("reference_staging_byte_budget_exceeded")
    raw = path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("reference_receipt_must_be_object")
    if (receipt.get("schema") != "reference_fetch.v1" or receipt.get("ok") is not True
            or receipt.get("api_name") != api or receipt.get("trade_date") != day
            or receipt.get("provider") != PROVIDER
            or receipt.get("csv_sha256") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("reference_receipt_unverified")
    fetched = datetime.fromisoformat(str(receipt.get("fetched_at") or ""))
    if (fetched.utcoffset() is None or fetched > datetime.now(timezone.utc)
            or fetched.astimezone(timezone(timedelta(hours=8))).date() < date.fromisoformat(day)):
        raise ValueError("reference_receipt_time_unverified")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    _checked_header(reader.fieldnames)
    required = {"trade_date", *VALUE_FIELDS[api]}
    if not required.issubset(reader.fieldnames or []) or not {"symbol", "ts_code"}.intersection(reader.fieldnames or []):
        raise ValueError("reference_source_columns_missing")
    rows = []
    for row in reader:
        if None in row:
            raise ValueError("reference_source_row_width_mismatch")
        rows.append(row)
        if len(rows) > config.max_rows_per_response:
            raise ValueError("reference_row_budget_exceeded")
    if type(receipt.get("rows")) is not int or receipt["rows"] != len(rows):
        raise ValueError("reference_receipt_row_count_mismatch")
    return _normalize(api, rows, day), {"path": str(path), **receipt}


def plan_reference_refresh(config: ReferenceRefreshConfig, *, _deadline: float | None = None) -> dict[str, Any]:
    """Inspect bounded new evidence; never claim old-key verification from a sample."""
    dates = _dates(config)
    deadline = time.monotonic() + config.budget_seconds if _deadline is None else _deadline
    plans: dict[str, Any] = {}
    errors: list[str] = []
    batch_path = config.staging_dir / "batch.json"
    if batch_path.exists():
        try:
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            if batch.get("status") != "staged":
                errors.append("reference_staging_batch_incomplete")
            elif type(batch.get("response_count")) is not int or batch["response_count"] != len(dates) * len(FAMILIES):
                errors.append("reference_staging_batch_response_count_mismatch")
        except (OSError, ValueError, AttributeError):
            errors.append("reference_staging_batch_unreadable")
    updates: dict[str, list[dict[str, str]]] = {}
    for family, api in FAMILIES.items():
        columns = TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS[family + ".csv"]
        baseline = config.metadata_dir / (family + ".csv")
        rows: list[dict[str, str]] = []
        receipts = []
        for day in dates:
            try:
                _check_deadline(deadline)
                new, receipt = _read_staged(config, api, day)
                _check_deadline(deadline)
                rows.extend({key: item[key] for key in columns} for item in new)
                receipts.append(receipt)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{family}:{day}:{type(exc).__name__}:{exc}")
        status: dict[str, Any] = {"exists": baseline.is_file()}
        if baseline.is_file():
            stat = baseline.stat()
            with baseline.open(encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), [])
            status.update(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, columns=header)
            if len(header) != len(columns) or len(header) != len(set(header)) or set(header) != set(columns):
                errors.append(f"{family}:canonical_header_mismatch")
        plans[family] = {"baseline": status, "new_row_count": len(rows), "receipts": receipts,
            "target_dates": dates, "new_evidence": "sparse_events" if api == "suspend_d" else "provider_rows",
            "empty_response_dates": [r["trade_date"] for r in receipts if r["rows"] == 0]}
        updates[family] = rows
    return {"status": "blocked" if errors else "ready_for_isolated_preparation", "network": False,
        "canonical_write": False, "dates": dates, "provider": PROVIDER, "families": plans,
        "baseline_duplicate_verification": "not_performed_by_header_only_plan",
        "warnings": ["Historical PIT/source completeness is not established by a reference refresh.",
                     "An empty suspend_d receipt does not prove every symbol traded."],
        "errors": errors, "_updates": updates}


def _same_values(left: dict[str, str], right: dict[str, str], columns: list[str]) -> bool:
    for key in columns:
        if key in {"source", "provider"}:
            continue
        a, b = str(left.get(key) or ""), str(right.get(key) or "")
        if key in {"adj_factor", "pre_close", "up_limit", "down_limit"}:
            try:
                if Decimal(a) != Decimal(b):
                    return False
            except InvalidOperation:
                return False
        elif a != b:
            return False
    return True


def prepare_reference_refresh(config: ReferenceRefreshConfig, output_root: Path) -> dict[str, Any]:
    """Stream complete replacement candidates into a new, separate directory."""
    deadline = time.monotonic() + config.budget_seconds
    plan = plan_reference_refresh(config, _deadline=deadline)
    if plan["errors"]:
        return {key: value for key, value in plan.items() if not key.startswith("_")}
    destination, live = Path(output_root).resolve(), config.metadata_dir.resolve()
    staging = config.staging_dir.resolve()
    if destination == live or live in destination.parents or destination in live.parents or destination == staging or staging in destination.parents:
        raise ValueError("reference_candidate_must_be_disjoint_from_live_metadata_and_staging")
    destination.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="references-", dir=destination))
    files: dict[str, Any] = {}
    try:
        for family in FAMILIES:
            _check_deadline(deadline)
            columns = TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS[family + ".csv"]
            source = config.metadata_dir / (family + ".csv")
            target = candidate / (family + ".csv")
            updates = {(row["trade_date"], row["symbol"]): row for row in plan["_updates"][family]}
            seen: set[tuple[str, str]] = set()
            source_digest = hashlib.sha256()
            source_rows, reused = 0, 0
            previous_day = ""
            appended = 0
            pending_keys = {day: [key for key in updates if key[0] == day] for day in plan["dates"]}
            pending_days = sorted(pending_keys)
            planned = plan["families"][family]["baseline"]
            current = source.stat() if source.exists() else None
            if bool(current) != planned["exists"] or (current and (current.st_size, current.st_mtime_ns) != (planned["size_bytes"], planned["mtime_ns"])):
                raise ValueError("reference_baseline_changed_after_plan")
            with target.open("w", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                if source.exists():
                    with source.open(encoding="utf-8", newline="") as original:
                        def hashed_lines():
                            for line in original:
                                source_digest.update(line.encode("utf-8"))
                                yield line
                        reader = csv.DictReader(hashed_lines())
                        reader.fieldnames = [name.lstrip("\ufeff") for name in (reader.fieldnames or [])]
                        _checked_header(reader.fieldnames)
                        if set(reader.fieldnames) != set(columns):
                            raise ValueError("reference_baseline_columns_changed")
                        for row in reader:
                            source_rows += 1
                            if source_rows % 1000 == 0:
                                _check_deadline(deadline)
                            if None in row:
                                raise ValueError("reference_baseline_row_width_mismatch")
                            if row.get("provider") != PROVIDER:
                                raise ValueError("reference_provider_mismatch_in_baseline")
                            key = (str(row.get("trade_date") or ""), str(row.get("symbol") or ""))
                            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key[0]) or key[0] < previous_day:
                                raise ValueError("reference_baseline_date_order_unverified")
                            previous_day = key[0]
                            while pending_days and pending_days[0] < previous_day:
                                for earlier in sorted(pending_keys[pending_days.pop(0)]):
                                    if earlier in updates:
                                        writer.writerow(updates.pop(earlier))
                                        appended += 1
                            if key[0] in plan["dates"]:
                                if key in seen:
                                    raise ValueError("reference_baseline_duplicate_target_key")
                                seen.add(key)
                            if key in updates:
                                if not _same_values(row, updates[key], columns):
                                    raise ValueError(f"reference_existing_key_conflict:{family}:{key}")
                                reused += 1
                                del updates[key]
                            writer.writerow(row)
                    after = source.stat()
                    if current is None or (after.st_size, after.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                        raise ValueError("reference_baseline_changed_during_prepare")
                for key in sorted(updates):
                    writer.writerow(updates[key])
                    appended += 1
                out.flush()
                os.fsync(out.fileno())
                _check_deadline(deadline)
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    _check_deadline(deadline)
                    digest.update(block)
            files[family] = {"path": str(target), "sha256": digest.hexdigest(),
                "baseline_sha256": source_digest.hexdigest() if planned["exists"] else None,
                "baseline_rows": source_rows, "appended_rows": appended, "reused_rows": reused}
        _check_deadline(deadline)
        result = {key: value for key, value in plan.items() if not key.startswith("_")}
        result.update(status="prepared_not_applied", candidate_dir=str(candidate), files=files,
            baseline_duplicate_verification="requested_dates_only; other historical rows preserved",
            publication="No live files or pointers changed. All four candidates require one reviewed application.")
    except (OSError, ValueError, csv.Error) as exc:
        result = {"status": "blocked", "canonical_write": False, "candidate_dir": str(candidate),
                  "errors": [f"{type(exc).__name__}:{exc}"], "files": {}, "publication": "incomplete candidates must not be applied"}
    (candidate / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def stage_reference_batch(config: ReferenceRefreshConfig, fetcher: Callable[..., list[dict[str, Any]]],
        *, enable_network: bool = False) -> dict[str, Any]:
    """Use an explicitly supplied provider adapter, with no default network path.

    The caller supplies its already authorized Tushare-compatible adapter. This
    module never discovers a token, chooses a proxy, or retries a failed request.
    """
    dates = _dates(config)
    if not enable_network:
        return {"status": "planned_offline", "network": False, "request_count": 0, "canonical_write": False}
    staging, live = config.staging_dir.resolve(), config.metadata_dir.resolve()
    if staging == live or live in staging.parents or staging in live.parents:
        raise ValueError("reference_staging_must_be_disjoint_from_live_metadata")
    deadline = time.monotonic() + config.budget_seconds
    responses = []
    for day in dates:
        for api in FAMILIES.values():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "blocked", "error_type": "budget_exhausted", "canonical_write": False}
            try:
                rows = fetcher(api_name=api, trade_date=day.replace("-", ""), timeout_seconds=min(30.0, remaining))
                if not isinstance(rows, list) or len(rows) > config.max_rows_per_response:
                    raise ValueError("reference_row_budget_exceeded")
                _normalize(api, rows, day)
                fetched_at = datetime.now(timezone.utc).isoformat()
                if time.monotonic() > deadline:
                    raise ValueError("reference_fetch_budget_exhausted")
                content = io.StringIO(newline="")
                writer = csv.DictWriter(content, fieldnames=["ts_code", "trade_date", *VALUE_FIELDS[api]], extrasaction="ignore", lineterminator="\n")
                writer.writeheader()
                for row in rows:
                    writer.writerow({**row, "ts_code": row.get("ts_code") or row.get("symbol")})
                    if content.tell() > config.max_response_bytes:
                        raise ValueError("reference_staging_byte_budget_exceeded")
                raw = content.getvalue().encode("utf-8")
                if len(raw) > config.max_response_bytes:
                    raise ValueError("reference_staging_byte_budget_exceeded")
                _check_deadline(deadline)
                responses.append((api, day, raw, len(rows), fetched_at))
            except Exception as exc:
                return {"status": "blocked", "network": True, "error_type": type(exc).__name__,
                        "failed_api": api, "failed_date": day, "canonical_write": False, "staged": False}
    config.staging_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="reference-fetch-", dir=config.staging_dir))
    batch_path = root / "batch.json"
    batch_path.write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
    try:
        for api, day, raw, row_count, fetched_at in responses:
            _check_deadline(deadline)
            path = root / api / (day.replace("-", "") + ".csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            _check_deadline(deadline)
            receipt = {"schema": "reference_fetch.v1", "ok": True, "provider": PROVIDER,
                "api_name": api, "trade_date": day, "fetched_at": fetched_at, "rows": row_count,
                "csv_sha256": hashlib.sha256(raw).hexdigest()}
            path.with_suffix(".receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        _check_deadline(deadline)
        batch_path.write_text(json.dumps({"status": "staged", "response_count": len(responses)}), encoding="utf-8")
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "network": True, "error_type": type(exc).__name__,
                "canonical_write": False, "staged": False, "incomplete_dir": str(root)}
    return {"status": "staged", "network": True, "staging_dir": str(root),
            "request_count": len(responses), "canonical_write": False}
