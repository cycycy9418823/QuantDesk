"""One dated, read-only material checklist for refresh, review and recovery.

Readiness is a label for a declared consumer and window, never a new pipeline
gate. Transport success, market coverage and research evidence are separate.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

SCHEMA = "daily_evidence.v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_SYMBOLS = 100
REQUIREMENTS_REGISTRY = Path("state/daily_evidence/requests.json")
EVIDENCE_ROOT = Path("results/daily_evidence")
WriteCheck = Callable[[], None]


def day(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("evidence_date_invalid")
    return date.fromisoformat(text).isoformat()


def symbol(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError("evidence_symbol_invalid")
    return text


def aware_stamp(value: str | datetime) -> str:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("evidence_timezone_missing")
    return stamp.isoformat()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_path(root: Path, relative: str | Path) -> Path:
    base = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or rel.anchor or ".." in rel.parts or any(":" in part for part in rel.parts):
        raise ValueError("evidence_path_invalid")
    path = base / rel
    for parent in (path, *path.parents):
        if parent == base.parent:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("evidence_reparse_point")
    if not path.resolve().is_relative_to(base):
        raise ValueError("evidence_path_escape")
    return path


def read_bytes(root: Path, relative: str | Path) -> bytes:
    path = safe_path(root, relative)
    with path.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("evidence_file_size_limit")
    return raw


def read_object(root: Path, relative: str | Path) -> dict[str, Any]:
    value = json.loads(read_bytes(root, relative).decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("evidence_object_invalid")
    return value


def load_ref(root: Path, ref: Mapping[str, Any]) -> bytes:
    raw = read_bytes(root, str(ref.get("capture_path") or ""))
    if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("capture_sha256") or "")) or digest(raw) != ref["capture_sha256"]:
        raise ValueError("evidence_reference_hash_mismatch")
    return raw


def write_json(path: Path, value: Any, *, assert_write: WriteCheck) -> None:
    assert_write()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        assert_write()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seal_bytes(root: Path, base: str | Path, raw: bytes, *, suffix: str, assert_write: WriteCheck) -> dict[str, Any]:
    if suffix not in {"json", "csv", "bin"} or len(raw) > MAX_FILE_BYTES:
        raise ValueError("evidence_blob_invalid")
    rel = Path(base) / f"{digest(raw)}.{suffix}"
    path = safe_path(root, rel)
    assert_write()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if read_bytes(root, rel) != raw:
            raise ValueError("evidence_immutable_blob_conflict")
    assert_write()
    return {"capture_path": rel.as_posix(), "capture_sha256": digest(raw), "bytes": len(raw)}


@dataclass(frozen=True)
class EvidenceWindow:
    request_id: str
    scope: str
    purpose: str
    start_date: str
    end_date: str
    symbols: tuple[str, ...]
    factor_policy: str = "native_or_source_verified_derived"

    def __post_init__(self) -> None:
        if self.scope not in {"current", "historical"} or not self.request_id or not self.purpose:
            raise ValueError("evidence_window_identity_invalid")
        if day(self.start_date) > day(self.end_date) or not self.symbols or len(self.symbols) > MAX_SYMBOLS:
            raise ValueError("evidence_window_invalid")
        if tuple(sorted(set(map(symbol, self.symbols)))) != self.symbols:
            raise ValueError("evidence_window_symbols_not_canonical")
        if self.factor_policy not in {"native_only", "native_or_source_verified_derived"}:
            raise ValueError("factor_policy_invalid")


def calendar_dates(root: Path, start: str, end: str) -> list[str]:
    """Use explicit local session flags; missing calendar entries are not weekdays."""
    first, last = date.fromisoformat(day(start)), date.fromisoformat(day(end))
    if first > last or (last - first).days > 36525:
        raise ValueError("calendar_window_invalid")
    if safe_path(root, "data/calendar/a_share_trading_calendar.csv").is_file():
        calendar_raw = read_bytes(root, "data/calendar/a_share_trading_calendar.csv")
    else:
        from quantdesk.strategy_evidence_archive import archived_calendar_bytes
        calendar_raw = archived_calendar_bytes(root, end)
    rows = csv.DictReader(io.StringIO(calendar_raw.decode("utf-8-sig")))
    flags: dict[str, bool] = {}
    for row in rows:
        current = str(row.get("date") or "")
        if current < start or current > end:
            continue
        current = day(current)
        token = str(row.get("is_open") or "").lower()
        if token not in {"true", "false", "1", "0"} or current in flags:
            raise ValueError("calendar_flags_invalid")
        flags[current] = token in {"true", "1"}
    current = first
    while current <= last:
        if current.isoformat() not in flags:
            raise ValueError("calendar_window_incomplete")
        current += timedelta(days=1)
    return sorted(key for key, opened in flags.items() if opened)


def build_requirements(root: Path, target_date: str) -> dict[str, Any]:
    """Register holdings, the actual candidate batch and explicit research windows."""
    target = day(target_date)
    starts: dict[str, str] = {}
    sources: list[dict[str, Any]] = []
    reasons: list[str] = []
    for rel, field in ((Path("state/auto_paper_portfolio.json"), "positions"),
                       (Path("results/candidate_refresh/daily") / f"{target}.json", "candidates")):
        if not safe_path(root, rel).is_file():
            reasons.append("strategy_book_missing" if field == "positions" else "candidate_batch_missing")
            continue
        try:
            raw = read_bytes(root, rel)
            doc = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(doc, dict) or not isinstance(doc.get(field, []), list):
                raise ValueError("scope_input_invalid")
            if field == "candidates" and doc.get("target_date") != target:
                raise ValueError("candidate_scope_date_mismatch")
            sources.append({"path": rel.as_posix(), "sha256": digest(raw), "role": field})
            for row in doc.get(field, []):
                code = symbol(row.get("symbol"))
                entry = day(row.get("entry_date")) if field == "positions" and row.get("entry_date") else target
                if entry > target:
                    raise ValueError("position_entry_after_target")
                if field == "positions" and not row.get("entry_date"):
                    reasons.append(f"position_entry_date_missing:{code}")
                starts[code] = min(starts.get(code, entry), entry)
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError) as exc:
            reasons.append(f"scope_input_unusable:{rel.as_posix()}:{type(exc).__name__}")
    windows = [asdict(EvidenceWindow(f"current:{code}", "current", "strategy_daily_review", start, target, (code,)))
               for code, start in sorted(starts.items())]
    if safe_path(root, REQUIREMENTS_REGISTRY).is_file():
        try:
            raw = read_bytes(root, REQUIREMENTS_REGISTRY)
            registry = json.loads(raw.decode("utf-8-sig"))
            if registry.get("schema") != "daily_evidence_requests.v1" or not isinstance(registry.get("windows"), list):
                raise ValueError("requirements_registry_invalid")
            for row in registry["windows"]:
                window = EvidenceWindow(**{**row, "symbols": tuple(row["symbols"])})
                if window.scope != "historical" or window.end_date > target:
                    raise ValueError("registered_research_window_invalid")
                windows.append(asdict(window))
            sources.append({"path": REQUIREMENTS_REGISTRY.as_posix(), "sha256": digest(raw), "role": "registered_research"})
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            reasons.append(f"requirements_registry_unusable:{type(exc).__name__}")
    identifiers = [row["request_id"] for row in windows]
    codes = {code for row in windows for code in row["symbols"]}
    if len(set(identifiers)) != len(identifiers) or len(codes) > MAX_SYMBOLS:
        reasons.append("evidence_scope_limit_or_duplicate")
    result = {"schema": SCHEMA, "kind": "requirements", "target_date": target, "windows": windows,
              "scope_sources": sources, "reason_codes": sorted(set(reasons)),
              "legacy_history_scope": "only_explicitly_registered_windows; other_history_not_audited",
              "research_only": True, "paper_only": True, "new_pipeline_gate": False}
    result["requirements_sha256"] = digest(json_bytes(result))
    return result


def price_window(root: Path, code: str, dates: list[str]) -> dict[str, Any]:
    """Classify the COMPLETE original frame before filtering any session."""
    from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
    import pandas as pd

    rel = Path("data/daily") / f"{symbol(code)}.csv"
    try:
        if safe_path(root, rel).is_file():
            raw = read_bytes(root, rel)
        else:
            from quantdesk.strategy_evidence_archive import archived_price_bytes
            raw = archived_price_bytes(root, dates[-1], code) if dates else b""
        frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        basis, reason = classify_frame_price_basis(frame)
        validation = validate_price_frame_set({code: frame}, consumer="paper_execution")
        if reason or validation.get("eligible") is not True or basis != "raw":
            return {"status": "incomplete", "reason_codes": [reason or "consumer_price_basis_not_allowed"], "basis": basis}
        if not {"date", "symbol", "open", "high", "low", "close", "volume", "source"}.issubset(frame.columns):
            raise ValueError("daily_fields_missing")
        if not frame["symbol"].eq(code).all():
            raise ValueError("daily_symbol_mismatch")
        selected = frame.loc[frame["date"].isin(dates)]
        if selected["date"].duplicated().any():
            raise ValueError("daily_duplicate_session")
        missing = sorted(set(dates) - set(selected["date"]))
        numeric = selected[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        import numpy as np
        valid = pd.Series(np.isfinite(numeric.to_numpy()).all(axis=1), index=numeric.index)
        valid &= numeric.notna().all(axis=1) & (numeric[["open", "high", "low", "close"]] > 0).all(axis=1) & numeric["volume"].gt(0)
        valid &= numeric["high"].ge(numeric[["open", "close", "low"]].max(axis=1)) & numeric["low"].le(numeric[["open", "close", "high"]].min(axis=1))
        if not valid.all() or not selected["source"].str.strip().ne("").all():
            raise ValueError("daily_row_invalid_or_source_missing")
        return {"status": "ready" if not missing else "incomplete", "basis": basis,
                "missing_dates": missing, "required_dates": dates, "path": rel.as_posix(), "sha256": digest(raw),
                "reason_codes": ["daily_window_missing"] if missing else []}
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        return {"status": "incomplete", "reason_codes": [f"daily_unusable:{type(exc).__name__}"]}


def audit_daily_evidence(root: Path, target_date: str, *, requirements: dict[str, Any] | None = None) -> dict[str, Any]:
    """No writes, no provider calls, no side-effecting health commands."""
    from quantdesk.adjustment_factor_evidence import factor_window
    from quantdesk.strategy_evidence_archive import inspect_strategy_archive

    target = day(target_date)
    req = requirements or build_requirements(root, target)
    if req.get("target_date") != target or req.get("schema") != SCHEMA:
        raise ValueError("requirements_target_mismatch")
    unsigned = {key: value for key, value in req.items() if key != "requirements_sha256"}
    if req.get("requirements_sha256") != digest(json_bytes(unsigned)):
        raise ValueError("requirements_fingerprint_mismatch")
    windows: list[dict[str, Any]] = []
    for item in req["windows"]:
        window = EvidenceWindow(**{**item, "symbols": tuple(item["symbols"])})
        for code in window.symbols:
            try:
                dates = calendar_dates(root, window.start_date, window.end_date)
                prices = price_window(root, code, dates)
                factors = factor_window(root, code, dates, policy=window.factor_policy)
                reasons = [*prices.get("reason_codes", []), *factors.get("reason_codes", [])]
            except (OSError, ValueError, UnicodeError) as exc:
                prices, factors = {"status": "not_assessed"}, {"status": "not_assessed"}
                reasons = [f"calendar_unusable:{type(exc).__name__}"]
            windows.append({**item, "symbol": code, "prices": prices, "factors": factors,
                            "status": "ready" if prices["status"] == factors["status"] == "ready" else "incomplete",
                            "reason_codes": reasons})
    try:
        book = read_object(root, "state/auto_paper_portfolio.json")
        valuation_date = book.get("valuation_date")
        ledger_current = valuation_date == target
    except (OSError, ValueError, UnicodeError):
        book, valuation_date, ledger_current = {}, None, False
    archive = inspect_strategy_archive(root, target)
    current = [row for row in windows if row["scope"] == "current"]
    historical = [row for row in windows if row["scope"] == "historical"]
    current_reasons = list(req.get("reason_codes", []))
    if not ledger_current:
        current_reasons.append("strategy_book_business_date_not_current")
    if archive["status"] != "ready":
        current_reasons.extend(archive["reason_codes"])
    current_reasons.extend(reason for row in current for reason in row["reason_codes"])
    return {"schema": SCHEMA, "kind": "assessment", "target_date": target,
            "requirements_sha256": req["requirements_sha256"], "requirements": req, "windows": windows,
            "current": {"status": "ready" if not current_reasons and all(r["status"] == "ready" for r in current) else "incomplete",
                        "ledger_current": ledger_current, "valuation_date": valuation_date,
                        "archive": archive, "reason_codes": sorted(set(current_reasons))},
            "historical": {"status": "not_assessed" if not historical else "incomplete" if any(r["status"] != "ready" for r in historical) or archive.get("legacy_history_gaps") else "materials_available",
                           "requested_windows": len(historical), "legacy_history_gaps": archive.get("legacy_history_gaps", []),
                           "formal_comparison_ready": False,
                           "note": "material coverage does not certify original decision times, exits or a strategy edge"},
            "pipeline_gate_added": False, "future_dual_track_bridge_active": False,
            "network_used": False, "runtime_written": False, "research_only": True}


def inspect_backup_readiness(root: Path, target_date: str) -> dict[str, Any]:
    """Inspect the actual local backup where available, not its filename date alone."""
    target = day(target_date)
    result: dict[str, Any] = {"target_date": target, "transport_current": False, "archive_available": False, "archive_verified": False,
                              "material_restore_ready": False, "status": "incomplete", "reason_codes": []}
    try:
        rows = read_bytes(root, "logs/ops/backup_push.log").decode("utf-8-sig").splitlines()
        row = json.loads(next(line for line in reversed(rows) if line.strip()))
        stamp = str(row.get("stamp") or "").removeprefix("snapshot_")
        if re.fullmatch(r"\d{8}T\d{6}Z", stamp) is None:
            raise ValueError("backup_stamp_invalid")
        from zoneinfo import ZoneInfo
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        result["transport_current"] = moment.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() == target and row.get("all_hashes_match") is True
        sessions = {}
        calendar = csv.DictReader(io.StringIO(read_bytes(root, "data/calendar/a_share_trading_calendar.csv").decode("utf-8-sig")))
        for item in calendar:
            current = day(item.get("date"))
            flag = str(item.get("is_open") or "").strip().lower()
            if flag not in {"true", "false", "1", "0"}:
                raise ValueError("backup_business_calendar_invalid")
            if current in sessions and sessions[current] != (flag in {"true", "1"}):
                raise ValueError("backup_business_calendar_conflict")
            sessions[current] = flag in {"true", "1"}
        if target not in sessions:
            raise ValueError("backup_business_calendar_date_missing")
        business = max((current for current, opened in sessions.items() if opened and current <= target), default=None)
        if business is None:
            raise ValueError("backup_previous_business_date_missing")
        result["expected_business_date"] = business
        # This is the existing local destination, computed from the project
        # parent. Do not follow arbitrary paths supplied inside a log record.
        archive = Path(root).resolve().parent / "tmp/backup" / f"quantdesk_backup_{stamp}.tar.gz"
        if archive.is_file():
            result["archive_available"] = True
            from quantdesk.paper_state_backup import verify_backup_archive
            with archive.open("rb") as handle:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            if row.get("archive_sha256") != hasher.hexdigest():
                raise ValueError("backup_log_archive_hash_mismatch")
            verified = verify_backup_archive(archive, target_date=business)
            result["archive_verified"] = verified["ok"]
            result["research_business_scope"] = verified["research_business_scope"]
            result["material_restore_ready"] = verified["ok"] and verified["research_business_scope"]["material_restore_ready"]
            result["reason_codes"].extend(verified["errors"])
        else:
            result["reason_codes"].append("local_archive_not_available_for_business_scope_verification")
        result["status"] = "ready" if result["transport_current"] and result["material_restore_ready"] else "incomplete"
    except (OSError, ValueError, KeyError, TypeError, StopIteration, UnicodeError) as exc:
        result["reason_codes"].append(f"backup_readiness_unusable:{type(exc).__name__}")
    return result


def publish_assessment(root: Path, assessment: dict[str, Any], *, stage: str, run_id: str,
                       process_status: str, recorded_at: str | datetime, assert_write: WriteCheck) -> dict[str, Any]:
    target = day(assessment["target_date"])
    value = {**assessment, "stage": stage, "run_id": str(run_id), "process_status": process_status,
             "recorded_at": aware_stamp(recorded_at), "runtime_written": True}
    ref = seal_bytes(root, EVIDENCE_ROOT / target, json_bytes(value), suffix="json", assert_write=assert_write)
    write_json(safe_path(root, EVIDENCE_ROOT / target / "latest.json"), {"schema": SCHEMA, "target_date": target, **ref}, assert_write=assert_write)
    return ref


def readiness_note(assessment: Mapping[str, Any]) -> str:
    current = assessment["current"]
    rows = [row for row in assessment["windows"] if row["scope"] == "current"]
    daily_missing = sum(row["prices"]["status"] != "ready" for row in rows)
    factor_missing = sum(row["factors"]["status"] != "ready" for row in rows)
    native = sum(row["factors"].get("kind") == "native_daily" for row in rows)
    derived = sum(row["factors"].get("kind") == "derived_sina_hfq_events" for row in rows)
    return (f"研究资料 {'✓' if current['status'] == 'ready' else '✗'} {assessment['target_date']}："
            f"行情缺{daily_missing}组，因子缺{factor_missing}组（原生{native}/派生{derived}），"
            f"账本日{current.get('valuation_date') or '缺失'}，动作原件{'已核对' if current['archive']['status'] == 'ready' else '未齐'}；"
            f"旧历史另列{assessment['historical']['status']}。纸面·未验证")
