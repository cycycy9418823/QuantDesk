"""Apply a prepared daily-repair bundle with a locked compare-and-swap.

A bundle produced by ``scripts/prepare_daily_repair_candidates.py`` is only a
proposal. This module is the separate, deliberate application step it requires:
every candidate must still match its recorded hash, the live file must still
match the hash observed at preparation time, and the replacement happens under
the per-symbol daily lock via an fsync'd temporary file plus os.replace.

Dry-run is the default. A canonical write needs both ``apply=True`` and the
explicit ``confirm_no_writer=True`` acknowledgement.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from quantdesk.interprocess_lock import interprocess_file_lock

DAILY_COLUMNS: tuple[str, ...] = (
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
    "change", "pct_chg", "adjust", "source", "updated_at",
)

_SOURCE_STATUSES = {"candidate_ready", "structural_candidate_only"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contained_file(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f"candidate_path_leaves_bundle:{relative}")
    return path


def _candidate_defects(data: bytes, *, symbol: str, columns: Sequence[str]) -> list[str]:
    defects: list[str] = []
    if b"\x00" in data:
        defects.append("candidate_contains_nul")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return defects + ["candidate_unreadable"]
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return defects + ["candidate_unreadable"]
    if not rows:
        return defects + ["candidate_empty"]
    if tuple(cell.strip() for cell in rows[0]) != tuple(columns):
        defects.append("candidate_header_mismatch")
    for values in rows[1:]:
        if len(values) != len(columns):
            defects.append("candidate_column_mismatch")
            break
    if len(rows) < 2:
        defects.append("candidate_has_no_records")
    else:
        symbols = {values[1].strip() for values in rows[1:] if len(values) == len(columns)}
        if symbols - {symbol}:
            defects.append("candidate_symbol_mismatch")
    return defects


def _load_bundle_items(bundle_dir: Path) -> list[dict[str, Any]]:
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        items = manifest.get("items") if isinstance(manifest, dict) else None
        if isinstance(items, list) and items:
            return [dict(item) for item in items]
    items = []
    for path in sorted((bundle_dir / "manifests").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _atomic_replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_daily_repair_bundle(
    *,
    bundle_dir: str | Path,
    daily_dir: str | Path,
    apply: bool = False,
    confirm_no_writer: bool = False,
    allow_incomplete: bool = True,
    lock_timeout_seconds: float = 60.0,
    columns: Sequence[str] = DAILY_COLUMNS,
    lock_factory: Callable[..., Any] = interprocess_file_lock,
) -> dict[str, Any]:
    """Verify and optionally apply every usable candidate in one bundle."""

    bundle = Path(bundle_dir).expanduser().resolve(strict=True)
    if not bundle.is_dir():
        raise ValueError("bundle_dir must be a directory")
    target_dir = Path(daily_dir).expanduser().resolve()
    if not target_dir.is_dir():
        raise ValueError("daily_dir must be an existing directory")
    items = _load_bundle_items(bundle)
    if not items:
        raise ValueError("bundle_manifest_items_missing")

    will_write = bool(apply and confirm_no_writer)
    rows: list[dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "").strip()
        record: dict[str, Any] = {
            "symbol": symbol,
            "declared_status": str(item.get("status") or ""),
            "status": "refused",
            "reasons": [],
            "before_sha256": None,
            "after_sha256": None,
            "candidate_sha256": item.get("candidate_sha256"),
        }
        if not (len(symbol) == 6 and symbol.isdigit()):
            record["reasons"].append("invalid_symbol")
            rows.append(record)
            continue
        declared = str(item.get("status") or "")
        if declared not in _SOURCE_STATUSES or not item.get("candidate_path"):
            record["reasons"].append(f"source_not_usable:{declared or 'unknown'}")
            rows.append(record)
            continue
        if item.get("history_preserved") is not True:
            record["reasons"].append("history_not_preserved")
            rows.append(record)
            continue
        if item.get("input_hashes_match_at_final_check") is False:
            record["reasons"].append("input_changed_at_final_check")
            rows.append(record)
            continue
        if not allow_incomplete and not item.get("coverage_complete"):
            record["reasons"].append("coverage_incomplete")
            rows.append(record)
            continue

        try:
            candidate_path = _contained_file(bundle, str(item["candidate_path"]))
        except ValueError as exc:
            record["reasons"].append(str(exc))
            rows.append(record)
            continue
        if not candidate_path.is_file():
            record["reasons"].append("candidate_file_missing")
            rows.append(record)
            continue
        candidate = candidate_path.read_bytes()
        candidate_sha = _sha256(candidate)
        if item.get("candidate_sha256") and candidate_sha != item.get("candidate_sha256"):
            record["reasons"].append("candidate_sha256_mismatch")
            rows.append(record)
            continue
        defects = _candidate_defects(candidate, symbol=symbol, columns=columns)
        if defects:
            record["reasons"].extend(defects)
            rows.append(record)
            continue

        live_path = target_dir / f"{symbol}.csv"
        live_bytes = live_path.read_bytes() if live_path.is_file() else None
        live_sha = _sha256(live_bytes) if live_bytes is not None else None
        record["before_sha256"] = live_sha
        expected_live = item.get("observed_original_sha256")
        if expected_live is not None and live_sha != expected_live:
            record["reasons"].append("live_changed_since_prepare")
            rows.append(record)
            continue
        if live_sha == candidate_sha:
            record["status"] = "already_applied"
            rows.append(record)
            continue
        if not will_write:
            record["status"] = "would_replace"
            record["reasons"].append("dry_run" if not apply else "confirm_no_writer_missing")
            rows.append(record)
            continue

        lock_path = target_dir / ".locks" / f"{symbol}.lock"
        try:
            with lock_factory(lock_path, timeout_seconds=float(lock_timeout_seconds)):
                current = live_path.read_bytes() if live_path.is_file() else None
                if current is not None and _sha256(current) != expected_live:
                    record["reasons"].append("live_changed_under_lock")
                    rows.append(record)
                    continue
                _atomic_replace(live_path, candidate)
        except TimeoutError:
            record["reasons"].append("lock_timeout")
            rows.append(record)
            continue
        record["status"] = "applied"
        record["after_sha256"] = candidate_sha
        rows.append(record)

    summary: dict[str, int] = {}
    for record in rows:
        summary[record["status"]] = summary.get(record["status"], 0) + 1
    return {
        "schema_version": "daily_repair_apply.v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "apply" if will_write else "dry_run",
        "bundle": str(bundle),
        "daily_dir": str(target_dir),
        "total": len(rows),
        "summary": summary,
        "rows": rows,
        "canonical_write": bool(summary.get("applied")),
        "network_effective": False,
        "requires_locked_hash_recheck": True,
    }


__all__ = ["apply_daily_repair_bundle"]
