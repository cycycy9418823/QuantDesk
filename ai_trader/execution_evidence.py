"""Content-addressed evidence for new AI paper fills; never reconstruct old fills."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "ai_execution_evidence_v1"


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint_file(path: Path) -> dict[str, Any]:
    """Fingerprint the bytes actually available locally, without changing them."""
    target = Path(path).resolve()
    try:
        first = target.stat()
        digest = sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        last = target.stat()
    except OSError as exc:
        return {"path": str(target), "status": "missing", "reason": type(exc).__name__}
    stable = (first.st_size, first.st_mtime_ns, first.st_ctime_ns, first.st_ino) == (
        last.st_size, last.st_mtime_ns, last.st_ctime_ns, last.st_ino
    )
    return {
        "path": str(target), "sha256": digest.hexdigest(), "size": last.st_size,
        "mtime_ns": last.st_mtime_ns, "ctime_ns": last.st_ctime_ns, "file_id": last.st_ino,
        "status": "stable" if stable else "changed",
    }


def source_reference(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a parser result to an unchanged local file version."""
    keys = ("path", "sha256", "size", "mtime_ns", "ctime_ns", "file_id")
    stable = (
        before.get("status") == after.get("status") == "stable"
        and bool(before.get("sha256"))
        and all(before.get(key) == after.get(key) for key in keys)
    )
    return {**dict(after), "status": "stable" if stable else "unverified",
            "reason": "" if stable else "source_changed_or_missing_during_read"}


def capture_execution_evidence(
    price_evidence: Mapping[str, Any],
    bar: Mapping[str, Any],
    *,
    plan_id: str,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Preserve this fill's accepted raw bar before any financial mutation.

    An archive failure raises before settlement. Existing content is checked,
    never overwritten. Orphan evidence is not proof that a fill was committed.
    """
    source = bar.get("source_evidence")
    original = bar.get("original_row")
    if not isinstance(source, dict) or source.get("status") != "stable" or not source.get("sha256"):
        raise ValueError("execution_evidence_source_unverified")
    if not isinstance(original, dict) or not original:
        raise ValueError("execution_evidence_original_row_missing")
    if price_evidence.get("price_basis") != "raw":
        raise ValueError("execution_evidence_requires_raw")
    content = {
        "plan_id": str(plan_id),
        "execution": dict(price_evidence),
        "source": dict(source),
        "original_row": dict(original),
        "original_row_sha256": sha256(_encoded(original)).hexdigest(),
    }
    evidence_id = sha256(_encoded(content)).hexdigest()
    receipt = {
        "schema": SCHEMA, "evidence_id": evidence_id, "content": content,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "capture_kind": "observed_for_new_fill",
        "proves_committed_fill": False,
    }
    archive_path = ""
    if evidence_dir is not None:
        directory = Path(evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{evidence_id}.json"
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(_encoded(receipt).decode("utf-8") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not verify_execution_receipt(existing) or existing.get("content") != content:
                raise ValueError("execution_evidence_archive_conflict")
            receipt = existing
        archive_path = str(target.resolve())
    return {
        **dict(price_evidence),
        "evidence_id": evidence_id,
        "receipt": receipt,
        "archive_path": archive_path,
        "capture_status": "archived" if archive_path else "embedded",
    }


def verify_execution_receipt(receipt: Any) -> bool:
    """Check embedded evidence without consulting today's mutable daily CSV."""
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        return False
    content = receipt.get("content")
    if not isinstance(content, dict):
        return False
    try:
        original = content["original_row"]
        source = content["source"]
        execution = content["execution"]
        if not all(isinstance(item, dict) for item in (original, source, execution)):
            return False
        return (
            receipt.get("capture_kind") == "observed_for_new_fill"
            and source.get("status") == "stable"
            and bool(source.get("sha256"))
            and execution.get("price_basis") == "raw"
            and content.get("original_row_sha256") == sha256(_encoded(original)).hexdigest()
            and receipt.get("evidence_id") == sha256(_encoded(content)).hexdigest()
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def audit_trade_evidence(trades: list[Any]) -> dict[str, Any]:
    """Legacy rows remain unverified; this read-only audit never backfills them."""
    counts = {"captured": 0, "legacy_unverified": 0, "invalid": 0}
    rows = []
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            counts["invalid"] += 1
            rows.append({"index": index, "status": "invalid"})
            continue
        evidence = trade.get("price_evidence")
        if not isinstance(evidence, dict) or not evidence.get("receipt"):
            status = "legacy_unverified"
        else:
            receipt = evidence.get("receipt")
            content = receipt.get("content") if isinstance(receipt, dict) else {}
            execution = content.get("execution") if isinstance(content, dict) else {}
            execution = execution if isinstance(execution, dict) else {}
            try:
                price_matches = round(float(execution.get("price")), 4) == float(trade.get("price"))
            except (ValueError, TypeError, OverflowError):
                price_matches = False
            status = "captured" if (
                verify_execution_receipt(receipt)
                and evidence.get("evidence_id") == receipt.get("evidence_id")
                and execution.get("symbol") == trade.get("symbol")
                and execution.get("side") == trade.get("side")
                and execution.get("trade_date") == trade.get("date")
                and price_matches
            ) else "invalid"
        counts[status] += 1
        rows.append({"index": index, "symbol": trade.get("symbol"), "date": trade.get("date"), "status": status})
    return {"read_only": True, "historical_backfill": False, "counts": counts, "rows": rows}
