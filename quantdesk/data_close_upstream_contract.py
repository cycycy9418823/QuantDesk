"""Verified Stage-1/2 handoff for the consolidated post-close pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quantdesk.utils import atomic_write_json

SCHEMA_VERSION = "data_close_upstream_reuse_v1"
ACCEPTED_STAGE_STATUSES = {"completed", "completed_with_warnings"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_project_file(root: Path, value: Any, *, stage: str) -> tuple[Path | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, f"{stage}_path_missing"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None, f"{stage}_path_outside_project"
    if not resolved.is_file():
        return None, f"{stage}_artifact_missing"
    return resolved, None


def validate_upstream_reuse_manifest(
    payload: Mapping[str, Any] | None,
    *,
    target_date: str,
    project_root: str | Path,
) -> tuple[bool, tuple[str, ...], dict[str, Any]]:
    root = Path(project_root).resolve()
    doc = dict(payload or {})
    reasons: list[str] = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        reasons.append("schema_version_invalid")
    if str(doc.get("target_date") or "") != str(target_date):
        reasons.append("target_date_mismatch")
    if str(doc.get("status") or "") != "accepted":
        reasons.append("upstream_status_not_accepted")
    run_id = str(doc.get("run_id") or "").strip()
    if not run_id:
        reasons.append("run_id_missing")

    normalized_stages: dict[str, Any] = {}
    stages = doc.get("stages") if isinstance(doc.get("stages"), Mapping) else {}
    for name in ("universe", "market_refresh"):
        meta = stages.get(name) if isinstance(stages, Mapping) else None
        if not isinstance(meta, Mapping):
            reasons.append(f"{name}_stage_missing")
            continue
        status = str(meta.get("status") or "")
        if status not in ACCEPTED_STAGE_STATUSES:
            reasons.append(f"{name}_status_not_accepted")
        path, path_reason = _resolve_project_file(root, meta.get("path"), stage=name)
        if path_reason:
            reasons.append(path_reason)
            continue
        expected = str(meta.get("sha256") or "").lower()
        actual = _sha256(path)
        if expected != actual:
            reasons.append(f"{name}_sha256_mismatch")
        normalized_stages[name] = {
            "status": status,
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "sha256": actual,
        }

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "target_date": str(target_date),
        "status": "accepted" if not reasons else "blocked",
        "stages": normalized_stages,
    }
    return not reasons, tuple(reasons), normalized


def write_upstream_reuse_manifest(
    *,
    project_root: str | Path,
    run_id: str,
    target_date: str,
    universe_status: str,
    market_refresh_status: str,
    output_path: str | Path,
) -> Path:
    root = Path(project_root).resolve()
    universe = root / "data/universe/a_share_symbols.csv"
    refresh = root / "results/data_refresh/data_refresh_latest.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "target_date": str(target_date),
        "status": "accepted",
        "stages": {
            "universe": {
                "status": str(universe_status),
                "path": "data/universe/a_share_symbols.csv",
                "sha256": _sha256(universe),
            },
            "market_refresh": {
                "status": str(market_refresh_status),
                "path": "results/data_refresh/data_refresh_latest.json",
                "sha256": _sha256(refresh),
            },
        },
    }
    ok, reasons, normalized = validate_upstream_reuse_manifest(
        payload, target_date=target_date, project_root=root
    )
    if not ok:
        raise ValueError("upstream_reuse_invalid:" + ",".join(reasons))
    destination = Path(output_path)
    if not destination.is_absolute():
        destination = root / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination, normalized)
    return destination


def load_upstream_reuse_manifest(
    path: str | Path, *, project_root: str | Path, target_date: str
) -> tuple[bool, tuple[str, ...], dict[str, Any]]:
    root = Path(project_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False, ("upstream_manifest_unreadable_or_outside_project",), {}
    if not isinstance(payload, Mapping):
        return False, ("upstream_manifest_not_object",), {}
    return validate_upstream_reuse_manifest(
        payload, target_date=target_date, project_root=root
    )
