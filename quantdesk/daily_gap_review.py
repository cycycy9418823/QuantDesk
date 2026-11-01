"""Read-only comparison of recorded daily gaps with their actual local rows.

The full-market count belongs to the saved reconciliation. Only its gaps and
the previous chain's flagged symbols are reread here; no market sweep or repair
is performed when the owner opens the page.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any

from quantdesk.full_a_daily_coverage import inspect_target_daily_row
from quantdesk.ui_read_cache import InputChangedDuringRead, cached_file_value

AUDIT_PATH = Path("results/data_chain/full_a_daily_reconciliation_latest.json")
ROSTER_PATH = Path("data/universe/a_share_symbols.csv")
MAX_REVIEW_SYMBOLS = 50


def _symbols(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {s for s in values if isinstance(s, str) and re.fullmatch(r"[0-9]{6}", s)}


def _previous_symbols(chain: dict[str, Any]) -> set[str]:
    coverage = chain.get("coverage")
    daily = coverage.get("daily") if isinstance(coverage, dict) else None
    if not isinstance(daily, dict):
        return set()
    return _symbols(daily.get("stale_symbols")) | _symbols(daily.get("missing_symbols"))


def _audit_projection(root: Path) -> dict[str, Any]:
    path = root / AUDIT_PATH

    def read() -> dict[str, Any]:
        if path.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("daily_audit_too_large")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict) or value.get("task_type") != "full_a_daily_reconciliation":
            raise ValueError("daily_audit_type_invalid")
        part = value["partition"]
        if not isinstance(part, dict):
            raise ValueError("daily_audit_partition_invalid")
        counts = part["completeness"]
        universe = _symbols(part["universe_symbols"])
        classes = part["gap_classification"]
        proofs = part["target_row_evidence"]
        roster = part["roster_evidence"]
        if (not all(isinstance(v, dict) for v in (counts, classes, proofs, roster))
                or any(not isinstance(proof, dict) for proof in proofs.values())):
            raise ValueError("daily_audit_evidence_invalid")
        target_date = value["target_date"]
        if (part.get("target_date") != target_date or counts.get("target_date") != target_date
                or not isinstance(target_date, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date)):
            raise ValueError("daily_audit_evidence_date_mismatch")
        total, bars, excluded, gaps = (counts[k] for k in (
            "universe_count", "with_bar_count", "verified_excluded_count", "gap_count"))
        if (any(type(v) is not int or v < 0 for v in (total, bars, excluded, gaps))
                or not total or total != len(universe) or total != bars + excluded + gaps
                or set(classes) != universe
                or bars != sum(v == "valid_raw_trading_bar" for v in classes.values())
                or excluded != sum(v == "confirmed_nontrade" for v in classes.values())):
            raise ValueError("daily_audit_counts_inconsistent")
        excluded_symbols = {s for s, kind in classes.items() if kind == "confirmed_nontrade"}
        if excluded and (excluded_symbols != _symbols(counts.get("verified_excluded_symbols"))
                or not (counts.get("evidence_present") is True or counts.get("run_evidence_present") is True)
                or not str(counts.get("evidence_source") or "").strip()):
            raise ValueError("daily_audit_no_session_evidence_missing")
        return {
            "target_date": value["target_date"], "run_id": value.get("run_id", ""),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "record_updated_at": datetime.fromtimestamp(path.stat().st_mtime,
                tz=timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "roster_sha256": roster.get("sha256"),
            "observed_counts": {"total": total, "with_bar": bars,
                "verified_no_session": excluded, "unresolved": gaps},
            "classes": classes,
            "file_hashes": {s: e.get("source_sha256") for s, e in proofs.items()},
            "evidence_source": counts.get("evidence_source") or "",
        }

    return cached_file_value([path], read, scope="daily_gap_audit", code_id=str(id(read.__code__)))


def daily_gap_review_input_paths(root: Path, *, chain: dict[str, Any]) -> list[Path]:
    """Bind the UI cache to every file this bounded comparison actually reads."""
    root = Path(root)
    paths = [root / AUDIT_PATH, root / ROSTER_PATH]
    previous = _previous_symbols(chain)
    try:
        audit = _audit_projection(root)
        selected = previous | {s for s, kind in audit["classes"].items() if kind != "valid_raw_trading_bar"}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        selected = previous
    paths.extend(root / "data/daily" / f"{s}.csv" for s in sorted(selected)[:MAX_REVIEW_SYMBOLS])
    return paths


def _current_row(path: Path, target_date: str) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        proof = inspect_target_daily_row(path, target_date=target_date)
        if not path.is_file():
            return {"valid": False, "reason_code": "daily_file_missing", "data_date": "", "sha256": None}
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if proof.get("source_sha256") and proof["source_sha256"] != digest:
            raise InputChangedDuringRead("daily_gap_row_changed_during_read")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        dates = [str(r.get("date") or r.get("trade_date") or "")[:10] for r in rows]
        selected = [r for r, day in zip(rows, dates) if day == target_date]
        valid = proof.get("valid_trading_bar") is True and len(selected) == 1
        row = selected[0] if valid else {}
        symbol_matches = bool(rows) and all(r.get("symbol") == path.stem for r in rows)
        valid = valid and symbol_matches and bool(str(row.get("source") or "").strip())
        return {"valid": bool(valid), "reason_code": proof.get("reason_code") or ("" if valid else "row_identity_missing"),
            "data_date": target_date if valid else max(dates, default=""),
            "close": float(row["close"]) if valid else None,
            "source": row.get("source") if valid else None,
            "price_basis": "raw" if valid else None,
            "sha256": digest, "target_row_count": proof.get("target_row_count"),
            "basis_valid": (proof.get("price_policy_validation") or {}).get("eligible") is True,
            "symbol_matches": symbol_matches}

    return cached_file_value([path], read, scope="daily_gap_current_row", target_date=target_date,
                             code_id=str(id(inspect_target_daily_row)))


def build_daily_gap_review(root: Path, *, chain: dict[str, Any], target_date: str) -> dict[str, Any]:
    """Expose dated prices and honest unresolved rows, without altering evidence."""
    root = Path(root)
    result: dict[str, Any] = {"available": False, "status": "unavailable", "target_date": target_date,
        "by_symbol": {}, "source_path": AUDIT_PATH.as_posix(), "read_only": True,
        "network": False, "writes_data": False, "label": "纸面 · 未验证"}
    try:
        audit = _audit_projection(root)
        if audit["target_date"] != target_date or chain.get("target_date") != target_date:
            raise ValueError("daily_audit_business_date_mismatch")
        roster_bytes = (root / ROSTER_PATH).read_bytes()
        if hashlib.sha256(roster_bytes).hexdigest() != audit["roster_sha256"]:
            raise ValueError("daily_audit_roster_changed")
        names = {row.get("symbol"): row.get("name", "") for row in
                 csv.DictReader(io.StringIO(roster_bytes.decode("utf-8-sig")))}
        previous = _previous_symbols(chain)
        selected = sorted(previous | {s for s, kind in audit["classes"].items() if kind != "valid_raw_trading_bar"})
        by_symbol = {}
        for symbol in selected[:MAX_REVIEW_SYMBOLS]:
            current = _current_row(root / "data/daily" / f"{symbol}.csv", target_date)
            same_file = bool(current.get("sha256")) and current["sha256"] == audit["file_hashes"].get(symbol)
            confirmed = (same_file and audit["classes"].get(symbol) == "confirmed_nontrade"
                         and current.get("basis_valid") and current.get("symbol_matches")
                         and current.get("target_row_count") == 0)
            status = "present" if current["valid"] else "verified_no_session" if confirmed else "unresolved"
            message = {"present": "当前文件已有当日有效原始日线，可点开核对K线。",
                "verified_no_session": "已有同日无交易证据；未补写虚构K线。",
                "unresolved": "缺少当日有效日线，原因仍待核实；不能仅凭空响应判停牌或退市。"}[status]
            by_symbol[symbol] = {"symbol": symbol, "name": names.get(symbol, ""), "status": status, "message": message,
                "previously_flagged": symbol in previous, "matches_audit_file": same_file,
                "recorded_classification": audit["classes"].get(symbol),
                "data_date": current["data_date"], "close": current.get("close"),
                "source": current.get("source") or (audit["evidence_source"] if confirmed else None),
                "price_basis": current.get("price_basis"), "source_sha256": current.get("sha256"),
                "reason_code": "" if status != "unresolved" else current["reason_code"]}
        counts = audit["observed_counts"]
        changed = [s for s, row in by_symbol.items() if not row["matches_audit_file"]]
        incomplete = (counts["unresolved"] or changed or len(selected) > len(by_symbol)
                      or any(row["status"] == "unresolved" for row in by_symbol.values()))
        result.update(available=True, status="partial" if incomplete else "complete",
            observed_counts=counts, by_symbol=by_symbol, reviewed_count=len(by_symbol),
            review_total=len(selected), review_truncated=len(selected) > len(by_symbol),
            record_updated_at=audit["record_updated_at"], audit_sha256=audit["sha256"], run_id=audit["run_id"],
            files_changed_since_audit=changed,
            previously_flagged_now_present=[s for s, row in by_symbol.items() if row["previously_flagged"] and row["status"] == "present"],
            message=(f"{target_date} 全A名册 {counts['total']} 只：核对记录有当日日线 {counts['with_bar']} 只，"
                     f"已核实无交易 {counts['verified_no_session']} 只，待核实 {counts['unresolved']} 只。"
                     "逐只核对表只读复核原缺口，不生成或补写行情。"))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, csv.Error) as exc:
        result.update(reason_code=str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
            message="最新全A缺口核对记录缺失、过期或不一致；保留原警告，不能按已补齐显示。")
    return result
