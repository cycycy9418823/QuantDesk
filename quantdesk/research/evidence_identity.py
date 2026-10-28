"""Read-only admission and output identity for formal research artifacts."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping
import uuid

SCHEMA = "research_evidence.v1"


def normalized_date(value: Any) -> str:
    """Accept explicit ISO or compact calendar dates, never infer today's date."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    elif len(text) > 10 and text[10] in {"T", " "}:
        text = text[:10]
    try:
        return date.fromisoformat(text).isoformat() if len(text) == 10 else ""
    except ValueError:
        return ""


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _has_data_hashes(value: Any) -> bool:
    if valid_sha256(value):
        return True
    if isinstance(value, Mapping):
        return bool(value) and any(_has_data_hashes(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and any(_has_data_hashes(v) for v in value)
    return False


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def nonformal_mode(payload: Mapping[str, Any]) -> str:
    provenance = payload.get("report_provenance") or {}
    scopes = [payload, provenance, payload.get("assumptions") or {}]
    for obj in scopes:
        if not isinstance(obj, Mapping):
            continue
        if any(obj.get(k) is True for k in ("synthetic_test_data", "synthetic", "demo")):
            return "synthetic"
        if obj.get("smoke_run") is True or str(obj.get("run_type") or "").lower() in {"smoke", "test", "demo", "synthetic"}:
            return str(obj.get("run_type") or "smoke")
        text = " ".join(str(obj.get(k) or "").lower() for k in ("data_scope", "scope_source", "data_source", "data_mode"))
        if "synthetic" in text or "demo" in text:
            return "synthetic"
    if any("synthetic_test_data" in str(w).lower() or "smoke_run" in str(w).lower() for w in payload.get("warnings") or []):
        return "synthetic_or_smoke"
    return "formal"


def stamp_research_evidence(payload: dict[str, Any], *, target_date: str, code_identity: dict[str, Any],
                            data_identity: Any, config_identity: Any, run_type: str = "formal",
                            run_id: str | None = None) -> dict[str, Any]:
    identity = {"schema": SCHEMA, "target_date": normalized_date(target_date), "run_type": run_type,
        "data_mode": nonformal_mode(payload), "run_id": run_id or uuid.uuid4().hex,
        "code_sha256": digest(code_identity), "data_sha256": digest(data_identity),
        "config_sha256": digest(config_identity), "code_identity": code_identity,
        "data_identity": data_identity, "config_identity": config_identity}
    return {**payload, "evidence_identity": {**identity, "identity_sha256": digest(identity)}}


def assess_research_evidence(payload: Mapping[str, Any], *, target_date: str, source_sha256: str = "") -> dict[str, Any]:
    """Historical and synthetic reports may be shown but cannot prove current readiness."""
    proof = payload.get("evidence_identity")
    reasons: list[str] = []
    mode = nonformal_mode(payload)
    if mode != "formal":
        reasons.append("nonformal_data_mode:" + mode)
    if not isinstance(proof, Mapping) or proof.get("schema") != SCHEMA:
        reasons.append("research_identity_missing")
        proof = {}
    else:
        if digest({k: v for k, v in proof.items() if k != "identity_sha256"}) != proof.get("identity_sha256"):
            reasons.append("research_identity_hash_mismatch")
        if proof.get("run_type") != "formal" or proof.get("data_mode") != "formal":
            reasons.append("research_run_nonformal")
        if not proof.get("run_id"):
            reasons.append("research_run_id_missing")
        for key in ("code", "data", "config"):
            if not proof.get(key + "_identity") or proof.get(key + "_sha256") != digest(proof.get(key + "_identity")):
                reasons.append("research_" + key + "_identity_invalid")
        code = proof.get("code_identity") or {}
        if not isinstance(code, Mapping) or not valid_sha256(code.get("source_sha256")):
            reasons.append("research_source_bytes_identity_missing")
        if not _has_data_hashes(proof.get("data_identity")):
            reasons.append("research_data_bytes_identity_missing")
        if payload.get("ok") is False or str(payload.get("status") or "").lower() in {"failed", "blocked", "missing", "unreadable"}:
            reasons.append("research_run_not_successful")
    source_date = normalized_date(proof.get("target_date") or payload.get("target_date") or payload.get("end_date"))
    target_date = normalized_date(target_date)
    try:
        date.fromisoformat(source_date)
        date.fromisoformat(target_date)
        freshness = "current" if source_date == target_date else "stale" if source_date < target_date else "future"
    except ValueError:
        freshness = "unknown"
    if freshness != "current":
        reasons.append("research_date_" + freshness)
    return {"formal_eligible": not reasons, "status": "passed" if not reasons else "nonformal_or_unverified",
        "freshness": freshness, "source_date": source_date, "target_date": target_date,
        "run_id": proof.get("run_id"), "source_sha256": source_sha256, "identity": dict(proof),
        "reason_codes": reasons, "research_only": True}


def read_research_evidence(path: Path, *, target_date: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("artifact_not_object")
        return {"payload": payload, **assess_research_evidence(payload, target_date=target_date,
            source_sha256=sha256(raw).hexdigest())}
    except (OSError, ValueError, UnicodeError) as exc:
        return {"payload": {}, "formal_eligible": False, "status": "missing_or_unreadable",
            "reason_codes": ["artifact_read_failed:" + type(exc).__name__]}


def isolated_output_path(path: Path, mode: str) -> Path:
    """Keep the same filename under a separate mode directory."""
    if mode == "formal":
        return path
    for root_name in ("results", "reports"):
        if root_name in path.parts:
            i = len(path.parts) - 1 - list(reversed(path.parts)).index(root_name)
            tail = path.parts[i + 1:]
            if tail and tail[0] in {"smoke", "synthetic", "test", "demo", "nonformal"}:
                return path
            return Path(*path.parts[:i + 1]) / "nonformal" / mode / Path(*tail)
    return path.parent / "nonformal" / mode / path.name
