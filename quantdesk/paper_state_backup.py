"""Explicit, offline backup and restore audit for the two paper books.

The module deliberately has no project-path defaults.  Callers choose both the
source checkout and an external destination.  A restore is represented by a
read-only plan; applying it is an owner action outside this module.
"""

from __future__ import annotations

import hashlib
import base64
import io
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 128 * 1024 * 1024
_EVIDENCE_NAMES = frozenset({"ledger.json", "plan_latest.json", "review_latest.json", "journal.jsonl",
    "journal_latest.json", "decision_log.jsonl", "decision_latest.json", "hypotheses.json", "watchlist.json",
    "philosophy.json", "daytime_notes.jsonl", "daytime_session.json", "daytime_intents.jsonl",
    "llm_response_evidence.jsonl", "cycle_latest.json", "snapshot_latest.json", "decision_run_manifest.json",
    "decision_page.html", "decision_page.md", "chat.jsonl"})


def backup_transport_config(path: Path | str) -> dict[str, Any]:
    """Read explicit transport locations without reading keys or permitting a call."""
    config_path = Path(path).resolve()
    if config_path.stat().st_size > 64 * 1024:
        raise ValueError("backup_config_size_limit")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    allowed = {"project_root", "activation_plan", "ssh_key_path", "known_hosts_path", "remote",
        "remote_backup_root", "remote_snapshot_root", "temp_root", "push_log", "pull_log", "keep",
        "protected_snapshot", "network", "snapshot_cutoff"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("backup_config_unknown_or_secret_fields")
    for key in ("project_root", "activation_plan", "ssh_key_path", "known_hosts_path", "temp_root", "push_log", "pull_log"):
        if key in config:
            target = Path(config[key])
            config[key] = str(target if target.is_absolute() else (config_path.parent / target).resolve())
    for key in ("remote_backup_root", "remote_snapshot_root"):
        if key in config and not re.fullmatch(r"/[A-Za-z0-9_./-]+", config[key]):
            raise ValueError("backup_remote_path_invalid")
        if key in config and (".." in Path(config[key]).parts or config[key] == "/"):
            raise ValueError("backup_remote_path_invalid")
    if "remote" in config and not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", config["remote"]):
        raise ValueError("backup_remote_invalid")
    if "keep" in config and (isinstance(config["keep"], bool) or not 1 <= int(config["keep"]) <= 1000):
        raise ValueError("backup_retention_invalid")
    if "network" in config and not isinstance(config["network"], bool):
        raise ValueError("backup_network_flag_invalid")
    return config


def _allow_evidence_path(relative: Path) -> bool:
    parts = relative.parts
    if relative.is_absolute() or ".." in parts or not parts or any(":" in p for p in parts):
        return False
    if relative.as_posix() in {"state/auto_paper_portfolio.json", "docs/learning_log.md", "docs/owner_learning_log.md",
        "results/strategy_decision_log_latest.json", "results/strategy_decision_review_latest.json",
        "results/auto_paper_portfolio_latest.json", "results/auto_paper_portfolio_history.json"}:
        return True
    name = relative.as_posix()
    if name in {"results/strategy_evidence/latest.json", "data/metadata/adjustment_evidence/index.json"}:
        return True
    if re.fullmatch(r"results/(?:strategy_evidence|daily_evidence)/\d{4}-\d{2}-\d{2}/(?:[0-9a-f]{64}|latest)\.json", name):
        return True
    if re.fullmatch(r"results/strategy_evidence/blobs/[0-9a-f]{64}\.(?:json|csv|bin)", name):
        return True
    if re.fullmatch(r"data/metadata/adjustment_evidence/(?:blobs/[0-9a-f]{64}\.(?:json|csv|bin)|\d{4}-\d{2}-\d{2}/[0-9a-f]{64}\.json)", name):
        return True
    if len(parts) >= 3 and parts[0] in {"state", "results"} and parts[1] in {"ai_trader", "ai_trader_glm"}:
        return ((len(parts) == 3 and parts[-1] in _EVIDENCE_NAMES)
            or (len(parts) == 4 and parts[2] == "llm_requests" and parts[-1].endswith(".json"))
            or (len(parts) == 4 and parts[0] == "state" and parts[2] == "execution_evidence"
                and re.fullmatch(r"[0-9a-f]{64}\.json", parts[-1]) is not None))
    return False


def _read_evidence_bytes(root: Path, relative: Path) -> bytes:
    if not _allow_evidence_path(relative):
        raise ValueError("backup_path_not_allowlisted:" + relative.as_posix())
    path = root / relative
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents) if p != root.parent):
        raise ValueError("backup_symlink_or_junction:" + relative.as_posix())
    if not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
        raise ValueError("backup_path_or_size_invalid:" + relative.as_posix())
    return path.read_bytes()


def _evidence_objects(relative: Path, raw: bytes) -> list[Any]:
    if relative.suffix == ".jsonl":
        return [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    return [json.loads(raw.decode("utf-8-sig"))] if relative.suffix == ".json" else []


def _check_evidence_object(obj: Any, relative: Path, *, expected_account: str | None) -> list[dict[str, str]]:
    """Validate semantic references; absence is reported, never reconstructed."""
    refs = []
    def visit(value: Any, location: tuple[str, ...] = ()) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, location)
        elif isinstance(value, dict):
            # The old cycle's top-level mandate is a shared configuration
            # template. Its default ledger_id is not the recorded account.
            # Keep its original bytes; all booked ledger/plan/receipt IDs still
            # require the exact account identity.
            template = location == ("mandate",) and relative.name in {"cycle_latest.json", "snapshot_latest.json"}
            if (expected_account and not template and value.get("ledger_id") and value["ledger_id"] != expected_account):
                raise ValueError("backup_account_identity_mismatch:" + relative.as_posix())
            for key, item in value.items():
                if re.fullmatch(r"(?:api[_-]?key|authorization|password|private[_-]?key|access[_-]?token)", str(key), re.I) and item:
                    raise ValueError("backup_secret_content_excluded:" + relative.as_posix())
                reference_key = key in {"capture_path", "evidence_path", "body_path", "manifest_path", "archive_path"}
                reference_key |= relative.name == "decision_run_manifest.json" and key in {"page_html", "page_md", "log"}
                if reference_key and isinstance(item, str) and item:
                    expected_sha = value.get("capture_sha256") or value.get("file_sha256") or value.get("sha256") or value.get("manifest_sha256")
                    reference = {"path": item, "sha256": str(expected_sha or "")}
                    if key == "archive_path":
                        from ai_trader.execution_evidence import verify_execution_receipt

                        receipt = value.get("receipt")
                        evidence_id = value.get("evidence_id")
                        if (not verify_execution_receipt(receipt) or evidence_id != receipt.get("evidence_id")
                                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence_id))):
                            raise ValueError("backup_execution_receipt_invalid:" + relative.as_posix())
                        reference["execution_evidence_id"] = str(evidence_id)
                        reference["sha256"] = str(value.get("archive_sha256") or "")
                        reference["receipt_sha256"] = hashlib.sha256(json.dumps(receipt, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                    refs.append(reference)
                visit(item, (*location, str(key)))
            if relative.name == "ledger.json" and expected_account and value is obj and value.get("ledger_id") != expected_account:
                raise ValueError("backup_ledger_identity_missing:" + relative.as_posix())
            body = value.get("http_body")
            if isinstance(body, dict) and body.get("body_base64") is not None:
                retained = base64.b64decode(body["body_base64"], validate=True)
                if len(retained) != body.get("retained_bytes"):
                    raise ValueError("backup_response_body_length_mismatch")
                if body.get("byte_exact") is True and hashlib.sha256(retained).hexdigest() != body.get("received_sha256"):
                    raise ValueError("backup_response_body_hash_mismatch")
                if body.get("byte_exact") is True:
                    try:
                        body_object = json.loads(retained)
                    except (ValueError, UnicodeError):
                        if b"-----BEGIN PRIVATE KEY" in retained or re.search(rb'(?i)(authorization|api[_-]?key|access[_-]?token)\s*[=:]', retained):
                            raise ValueError("backup_response_secret_content_excluded")
                    else:
                        visit(body_object, (*location, "http_body_decoded"))
    visit(obj)
    return refs


def _verify_execution_reference(relative: Path, raw: bytes, reference: Mapping[str, str]) -> None:
    """Bind an independent archive to the unchanged receipt embedded in a trade."""
    if not reference.get("execution_evidence_id"):
        return
    from ai_trader.execution_evidence import verify_execution_receipt

    receipt = json.loads(raw.decode("utf-8-sig"))
    digest = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if (relative.name != reference["execution_evidence_id"] + ".json"
            or not verify_execution_receipt(receipt)
            or receipt.get("evidence_id") != reference["execution_evidence_id"]
            or digest != reference["receipt_sha256"]):
        raise ValueError("backup_execution_archive_binding_mismatch:" + relative.as_posix())


def _research_evidence_seeds(root: Path) -> list[str]:
    """Only immutable manifests and their declared dependencies, not all daily CSVs."""
    paths = []
    for base in (root / "results/strategy_evidence", root / "results/daily_evidence"):
        if base.is_dir():
            paths.extend(path.relative_to(root).as_posix() for path in base.glob("????-??-??/latest.json"))
    for relative in (Path("results/strategy_evidence/latest.json"), Path("data/metadata/adjustment_evidence/index.json")):
        if (root / relative).is_file():
            paths.append(relative.as_posix())
    return sorted(set(paths))


def _research_business_scope(contents: Mapping[str, bytes], target_date: str | None = None) -> dict[str, Any]:
    """Describe frozen business dates separately from byte-copy integrity."""
    assessments = []
    for name, raw in contents.items():
        if name.startswith(("results/daily_evidence/", "results/strategy_evidence/")) and name.endswith(".json"):
            try:
                value = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("schema") == "daily_evidence.v1" and value.get("kind") == "assessment":
                assessments.append(value)
    target = target_date or max((row.get("target_date", "") for row in assessments), default="") or None
    try:
        raw_book = contents["state/auto_paper_portfolio.json"]
        book = json.loads(raw_book.decode("utf-8-sig"))
        valuation = book.get("valuation_date")
        book_sha = hashlib.sha256(raw_book).hexdigest()
    except (KeyError, ValueError, AttributeError, UnicodeError):
        valuation, book_sha = None, None

    def captured_object(reference: Mapping[str, Any]) -> dict[str, Any]:
        raw = contents[reference["capture_path"]]
        if hashlib.sha256(raw).hexdigest() != reference["capture_sha256"]:
            raise ValueError("backup_research_reference_hash_mismatch")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("backup_research_reference_object_invalid")
        return value

    current: dict[str, Any] = {}
    archive: dict[str, Any] = {}
    bound = False
    try:
        # A retained earlier success must never override the latest receipt.
        review_pointer = json.loads(contents[f"results/daily_evidence/{target}/latest.json"].decode("utf-8-sig"))
        archive_pointer = json.loads(contents[f"results/strategy_evidence/{target}/latest.json"].decode("utf-8-sig"))
        current = captured_object(review_pointer)
        archive = captured_object(archive_pointer)
        same_requirements = captured_object(archive["requirements_source"]) == current["requirements"]
        bound = bool(
            review_pointer.get("schema") == "daily_evidence.v1"
            and archive_pointer.get("schema") == "strategy_evidence.v1"
            and review_pointer.get("target_date") == archive_pointer.get("target_date") == target
            and current.get("schema") == "daily_evidence.v1" and current.get("kind") == "assessment"
            and archive.get("schema") == "strategy_evidence.v1"
            and current.get("target_date") == archive.get("target_date") == target
            and current.get("stage") == "evening_review"
            and current.get("run_id") == archive.get("run_id")
            and (archive.get("after_state") or {}).get("capture_sha256") == book_sha
            and current["current"]["archive"]["manifest_reference"]["capture_sha256"] == archive_pointer["capture_sha256"]
            and same_requirements
        )
    except (KeyError, ValueError, TypeError, AttributeError, UnicodeError):
        bound = False
    ready = bool(target and valuation == target and bound
                 and current.get("current", {}).get("status") == "ready"
                 and archive.get("status") == "ready")
    return {"target_date": target, "strategy_valuation_date": valuation,
        "ledger_current": valuation == target if target else None,
        "dated_review_and_book_bound": bound, "material_restore_ready": ready,
        "status": "ready" if ready else "incomplete" if target else "not_assessed",
        "historical_trade_evidence_complete": False,
        "reason_codes": [] if ready else ["backup_copied_bytes_do_not_prove_dated_research_materials"],
        "note": "byte verification and an actually restored directory are reported separately; no old trades reconstructed"}


def build_backup_evidence_closure(source_root: Path | str, *, seed_paths: list[str] | None = None,
                                 reference_origin_root: Path | str | None = None,
                                 target_date: str | None = None) -> dict[str, Any]:
    """Bounded allowlisted dependency closure for three formal paper ledgers."""
    root = Path(source_root).resolve()
    origin = Path(reference_origin_root).resolve() if reference_origin_root is not None else root
    required = ["state/auto_paper_portfolio.json", "state/ai_trader/ledger.json", "state/ai_trader_glm/ledger.json"]
    pending = list(seed_paths if seed_paths is not None else required)
    if seed_paths is None:
        for bucket in ("state", "results"):
            for account in ("ai_trader", "ai_trader_glm"):
                pending.extend((Path(bucket) / account / name).as_posix() for name in _EVIDENCE_NAMES
                    if (root / bucket / account / name).is_file())
        pending.extend(p.as_posix() for p in (Path("docs/learning_log.md"),
            Path("results/auto_paper_portfolio_latest.json"), Path("results/auto_paper_portfolio_history.json"),
            Path("results/strategy_decision_log_latest.json"), Path("results/strategy_decision_review_latest.json")) if (root / p).is_file())
        pending.extend(_research_evidence_seeds(root))
    content: dict[str, bytes] = {}
    references = []
    errors: list[str] = []
    total = 0
    while pending and len(content) < 1000:
        rel = Path(pending.pop(0))
        if rel.as_posix() in content:
            continue
        try:
            raw = _read_evidence_bytes(root, rel)
            total += len(raw)
            if total > MAX_EVIDENCE_TOTAL_BYTES:
                raise ValueError("backup_total_size_limit")
            account = "ai_trader_100k_glm" if "ai_trader_glm" in rel.parts else "ai_trader_100k" if "ai_trader" in rel.parts else None
            for obj in _evidence_objects(rel, raw):
                for ref in _check_evidence_object(obj, rel, expected_account=account):
                    target = Path(ref["path"])
                    target = target.relative_to(origin) if target.is_absolute() and target.is_relative_to(origin) else target
                    target = target.relative_to(root) if target.is_absolute() and target.is_relative_to(root) else target
                    if not _allow_evidence_path(target):
                        raise ValueError("backup_reference_path_not_allowlisted:" + ref["path"])
                    if account and "ai_trader" in target.parts and account != "ai_trader_100k":
                        raise ValueError("backup_cross_account_reference")
                    if account and "ai_trader_glm" in target.parts and account != "ai_trader_100k_glm":
                        raise ValueError("backup_cross_account_reference")
                    target_raw = _read_evidence_bytes(root, target)
                    _verify_execution_reference(target, target_raw, ref)
                    digest = hashlib.sha256(target_raw).hexdigest()
                    if ref["sha256"] and ref["sha256"] != digest:
                        raise ValueError("backup_reference_hash_mismatch:" + target.as_posix())
                    references.append({"from": rel.as_posix(), "path": target.as_posix(), "sha256": digest,
                        "reference_declared_hash": ref["sha256"],
                        "binding_status": "embedded_execution_receipt_match" if ref.get("execution_evidence_id") else
                            "declared_hash_match" if ref["sha256"] else "frozen_backup_bytes_only"})
                    pending.append(target.as_posix())
            content[rel.as_posix()] = raw
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            errors.append(type(exc).__name__ + ":" + str(exc))
    if pending:
        errors.append("backup_file_count_limit")
    files = [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in sorted(content.items())]
    return {"ok": not errors, "status": "ready" if not errors else "blocked", "files": files,
        "research_business_scope": _research_business_scope(content, target_date),
        "contents": content, "references": references, "errors": errors, "bytes": total,
        "network_used": False, "runtime_written": False, "source_root": str(root), "reference_origin_root": str(origin)}


def pack_backup_evidence(source_root: Path | str, archive_path: Path | str, *, seed_paths: list[str] | None = None,
                         reference_origin_root: Path | str | None = None, target_date: str | None = None) -> dict[str, Any]:
    source, target = Path(source_root).resolve(), Path(archive_path).resolve()
    _assert_external_destination(source, target)
    closure = build_backup_evidence_closure(source, seed_paths=seed_paths, reference_origin_root=reference_origin_root, target_date=target_date)
    if not closure["ok"]:
        return {k: v for k, v in closure.items() if k != "contents"}
    if target.exists():
        raise FileExistsError("backup_archive_already_exists")
    manifest = {"schema": "paper_evidence_closure.v1", "source_root": str(source), "files": closure["files"],
        "research_business_scope": closure["research_business_scope"],
        "reference_origin_root": closure["reference_origin_root"],
        "references": closure["references"], "network_used": False, "restore_apply": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as archive:
        for name, raw in closure["contents"].items():
            item = tarfile.TarInfo(name); item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        item = tarfile.TarInfo("manifest.json"); item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))
    verification = verify_backup_archive(target)
    return {**verification, "archive": str(target), "archive_sha256": _sha256(target)[1], "runtime_written": False}


def verify_backup_archive(archive_path: Path | str, *, isolated_restore_root: Path | str | None = None,
                          target_date: str | None = None) -> dict[str, Any]:
    """Verify bytes and closure before optionally restoring into a NEW empty folder."""
    path = Path(archive_path).resolve()
    errors: list[str] = []
    blobs: dict[str, bytes] = {}
    manifest: dict[str, Any] = {}
    derived_refs: list[tuple[str, str, str]] = []
    try:
        if path.stat().st_size > MAX_EVIDENCE_TOTAL_BYTES:
            raise ValueError("backup_archive_size_limit")
        with tarfile.open(path, "r:gz") as archive:
            total = 0
            for item in archive:
                relative = Path(item.name)
                if (not item.isfile() or item.name in blobs or relative.as_posix() != item.name
                        or (item.name != "manifest.json" and not _allow_evidence_path(relative))
                        or item.size < 0 or item.size > MAX_EVIDENCE_FILE_BYTES):
                    raise ValueError("backup_archive_member_invalid")
                total += item.size
                if total > MAX_EVIDENCE_TOTAL_BYTES or len(blobs) >= 1000:
                    raise ValueError("backup_archive_total_limit")
                handle = archive.extractfile(item)
                if handle is None:
                    raise ValueError("backup_archive_member_unreadable")
                blobs[item.name] = handle.read(item.size + 1)
        decoded_manifest = json.loads(blobs.pop("manifest.json").decode("utf-8"))
        if not isinstance(decoded_manifest, dict) or decoded_manifest.get("schema") != "paper_evidence_closure.v1":
            raise ValueError("backup_archive_schema_invalid")
        manifest = decoded_manifest
        rows = manifest["files"]
        if not isinstance(rows, list) or len(rows) != len(blobs) or {r["path"] for r in rows} != set(blobs):
            raise ValueError("backup_archive_file_set_mismatch")
        for row in rows:
            raw = blobs[row["path"]]
            if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("backup_archive_hash_mismatch:" + row["path"])
            rel = Path(row["path"])
            account = "ai_trader_100k_glm" if "ai_trader_glm" in rel.parts else "ai_trader_100k" if "ai_trader" in rel.parts else None
            for obj in _evidence_objects(rel, raw):
                for reference in _check_evidence_object(obj, rel, expected_account=account):
                    destination = Path(reference["path"])
                    original_root = Path(manifest.get("reference_origin_root") or manifest["source_root"]).resolve()
                    if destination.is_absolute() and destination.is_relative_to(original_root):
                        destination = destination.relative_to(original_root)
                    frozen_root = Path(manifest["source_root"]).resolve()
                    if destination.is_absolute() and destination.is_relative_to(frozen_root):
                        destination = destination.relative_to(frozen_root)
                    if not _allow_evidence_path(destination) or destination.as_posix() not in blobs:
                        raise ValueError("backup_archive_reference_missing")
                    if account and (("ai_trader" in destination.parts and account != "ai_trader_100k")
                            or ("ai_trader_glm" in destination.parts and account != "ai_trader_100k_glm")):
                        raise ValueError("backup_archive_cross_account_reference")
                    target_sha = hashlib.sha256(blobs[destination.as_posix()]).hexdigest()
                    _verify_execution_reference(destination, blobs[destination.as_posix()], reference)
                    if reference["sha256"] and reference["sha256"] != target_sha:
                        raise ValueError("backup_archive_reference_hash_mismatch")
                    derived_refs.append((rel.as_posix(), destination.as_posix(), target_sha))
        if not isinstance(manifest.get("references"), list):
            raise ValueError("backup_archive_references_invalid")
        for ref in manifest["references"]:
            if not isinstance(ref, dict):
                raise ValueError("backup_archive_reference_invalid")
            if ref["path"] not in blobs or hashlib.sha256(blobs[ref["path"]]).hexdigest() != ref["sha256"]:
                raise ValueError("backup_archive_reference_incomplete")
        if sorted(derived_refs) != sorted((r["from"], r["path"], r["sha256"]) for r in manifest["references"]):
            raise ValueError("backup_archive_dependency_closure_mismatch")
        if "research_business_scope" in manifest:
            actual = _research_business_scope(blobs, manifest["research_business_scope"].get("target_date"))
            if actual != manifest["research_business_scope"]:
                raise ValueError("backup_archive_business_scope_mismatch")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, tarfile.TarError) as exc:
        errors.append(type(exc).__name__ + ":" + str(exc))
    restored = False
    if not errors and isolated_restore_root is not None:
        root = Path(isolated_restore_root).resolve()
        source = Path(str(manifest["source_root"])).resolve()
        if root.exists() or root == source or root.is_relative_to(source) or path.is_relative_to(root):
            errors.append("isolated_restore_requires_new_external_directory")
        else:
            root.mkdir(parents=True, exist_ok=False)
            for name, raw in blobs.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            restored = all(_sha256(root / name)[1] == hashlib.sha256(raw).hexdigest() for name, raw in blobs.items())
            if not restored:
                errors.append("isolated_restore_hash_mismatch")
    archive_references = manifest.get("references")
    archive_references = archive_references if isinstance(archive_references, list) else []
    execution_refs = [row for row in archive_references if isinstance(row, dict)
                      and "/execution_evidence/" in str(row.get("path") or "")]
    return {"ok": not errors, "status": "passed" if not errors else "blocked", "errors": errors,
        "research_business_scope": _research_business_scope(blobs, target_date or (manifest.get("research_business_scope") or {}).get("target_date")),
        "file_count": len(blobs), "references": archive_references,
        "execution_archive_reference_count": len(execution_refs),
        "historical_trade_evidence_complete": None,
        "verification_scope": "archived bytes and declared dependencies; no reconstruction of missing historical evidence",
        "isolated_restore_verified": restored, "network_used": False, "canonical_restore": False}

BACKUP_SCHEMA = "paper_state_backup_v1"
DISCLAIMER = "Paper-only research state backup; not a live trading account."


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    relative_path: Path


@dataclass
class BackupResult:
    """Outcome and manifest produced by :func:`backup_paper_state`."""

    ok: bool
    status: str
    destination: Path
    backup_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class VerificationResult:
    """Read-only hash audit for a previously written backup manifest."""

    ok: bool
    status: str
    manifest_path: Path | None = None
    mismatches: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


PAPER_ARTIFACTS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec("strategy_ledger", Path("state/auto_paper_portfolio.json")),
    ArtifactSpec("ai_ledger", Path("state/ai_trader/ledger.json")),
    ArtifactSpec("decision_log", Path("state/ai_trader/decision_log.jsonl")),
)

# These are useful decision/review companions when present.  They are not
# silently invented when a run did not produce them.
OPTIONAL_ARTIFACTS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec("decision_latest", Path("state/ai_trader/decision_latest.json")),
    ArtifactSpec("journal_log", Path("state/ai_trader/journal.jsonl")),
    ArtifactSpec("journal_latest", Path("state/ai_trader/journal_latest.json")),
    ArtifactSpec("strategy_decision_log", Path("results/strategy_decision_log_latest.json")),
    ArtifactSpec("strategy_decision_review", Path("results/strategy_decision_review_latest.json")),
)

LEARNING_LOG_CANDIDATES: tuple[Path, ...] = (
    Path("docs/learning_log.md"),
    Path("docs/owner_learning_log.md"),
    Path("state/owner_learning_log.md"),
    Path("state/learning_log.md"),
    Path("learning_log.md"),
)


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _assert_external_destination(source_root: Path, destination: Path) -> None:
    """Reject a destination that could write into the source checkout."""

    if destination == source_root or source_root in destination.parents:
        raise ValueError("backup destination must be outside source root")


def _timestamp_label(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        stamp = datetime.now(timezone.utc)
    elif isinstance(timestamp, datetime):
        stamp = timestamp
    else:
        stamp = datetime.fromisoformat(str(timestamp).strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with source.open("rb") as input_file, temporary.open("wb") as handle:
            shutil.copyfileobj(input_file, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _learning_spec(source_root: Path, owner_learning_log: Path | str | None) -> ArtifactSpec:
    if owner_learning_log is not None:
        requested = Path(owner_learning_log)
        relative = requested.relative_to(source_root) if requested.is_absolute() else requested
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("owner learning log must be inside source root")
        return ArtifactSpec("learning_log", relative)
    for candidate in LEARNING_LOG_CANDIDATES:
        if (source_root / candidate).is_file():
            return ArtifactSpec("learning_log", candidate)
    # Preserve one stable source path in the missing-artifact audit.
    return ArtifactSpec("learning_log", LEARNING_LOG_CANDIDATES[0])


def _specs(source_root: Path, owner_learning_log: Path | str | None) -> tuple[ArtifactSpec, ...]:
    optional = tuple(spec for spec in OPTIONAL_ARTIFACTS if (source_root / spec.relative_path).is_file())
    return PAPER_ARTIFACTS + optional + (_learning_spec(source_root, owner_learning_log),)


def _missing_required_specs(source_root: Path, owner_learning_log: Path | str | None) -> list[ArtifactSpec]:
    specs = list(PAPER_ARTIFACTS) + [_learning_spec(source_root, owner_learning_log)]
    return [spec for spec in specs if not (source_root / spec.relative_path).is_file()]


def _artifact_row(
    *,
    name: str,
    source: Path,
    relative: Path,
    target: Path,
    status: str,
    size: int | None = None,
    sha256: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "relative_path": relative.as_posix(),
        "source_path": str(source),
        "backup_path": str(target) if status == "copied" else None,
        "status": status,
        "bytes": size,
        "sha256": sha256,
    }
    if reason:
        row["reason"] = reason
    return row


def backup_paper_state(
    source_root: Path | str,
    destination: Path | str,
    *,
    timestamp: str | datetime | None = None,
    dry_run: bool = False,
    owner_learning_log: Path | str | None = None,
) -> BackupResult:
    """Copy formal paper state to an explicitly selected external directory.

    The source is read as bytes so the backup hash represents exactly what was
    present.  Missing artifacts remain visible in the manifest and make the
    operation fail closed.  ``dry_run`` performs discovery only and writes no
    destination files.
    """

    source = _resolved(source_root)
    target_root = _resolved(destination)
    if not source.is_dir():
        raise ValueError(f"source root does not exist: {source}")
    _assert_external_destination(source, target_root)
    label = _timestamp_label(timestamp)
    backup_dir = target_root / f"backup_{label}"
    manifest_path = backup_dir / "manifest.json"
    if not dry_run and backup_dir.exists():
        raise FileExistsError(f"backup destination already exists: {backup_dir}")

    specs = _specs(source, owner_learning_log)
    research_seeds = _research_evidence_seeds(source)
    research_closure = build_backup_evidence_closure(source, seed_paths=research_seeds) if research_seeds else None
    if research_closure is not None:
        existing_paths = {spec.relative_path.as_posix() for spec in specs}
        specs += tuple(ArtifactSpec("evidence:" + row["path"], Path(row["path"]))
                       for row in research_closure["files"] if row["path"] not in existing_paths)
    for spec in specs:
        resolved = (source / spec.relative_path).resolve()
        if source not in resolved.parents:
            raise ValueError(f"source artifact outside root: {spec.name}")
    if not dry_run:
        backup_dir.mkdir(parents=True, exist_ok=False)
    required_missing = set(spec.name for spec in _missing_required_specs(source, owner_learning_log))
    artifacts: list[dict[str, Any]] = []
    warnings: list[str] = []
    if research_closure is not None:
        warnings.extend(research_closure["errors"])
    before: dict[str, tuple[int, str]] = {}
    for spec in specs:
        path = source / spec.relative_path
        if path.is_file():
            try:
                before[spec.name] = _sha256(path)
            except OSError:
                warnings.append(f"source_unreadable:{spec.name}")
    for spec in specs:
        source_path = source / spec.relative_path
        target_path = backup_dir / "files" / spec.relative_path
        if not source_path.is_file():
            artifacts.append(
                _artifact_row(
                    name=spec.name,
                    source=source_path,
                    relative=spec.relative_path,
                    target=target_path,
                    status="missing",
                    reason="source file is absent",
                )
            )
            if spec.name in required_missing:
                warnings.append(spec.name)
            continue
        try:
            size, digest = before[spec.name]
            if not dry_run:
                _atomic_copy(source_path, target_path)
                if _sha256(target_path) != (size, digest):
                    warnings.append(f"backup_copy_mismatch:{spec.name}")
            artifacts.append(
                _artifact_row(
                    name=spec.name,
                    source=source_path,
                    relative=spec.relative_path,
                    target=target_path,
                    status="copied" if not dry_run else "planned",
                    size=size,
                    sha256=digest,
                )
            )
        except (OSError, KeyError) as exc:
            artifacts.append(
                _artifact_row(
                    name=spec.name,
                    source=source_path,
                    relative=spec.relative_path,
                    target=target_path,
                    status="error",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            warnings.append(spec.name)

    for spec in specs:
        if spec.name not in before:
            continue
        try:
            stable = _sha256(source / spec.relative_path) == before[spec.name]
        except OSError:
            stable = False
        if not stable:
            warnings.append(f"source_changed_during_backup:{spec.name}")
    status = "failed" if warnings else "planned" if dry_run else "passed"
    manifest: dict[str, Any] = {
        "schema_version": BACKUP_SCHEMA,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_root": str(source),
        "destination": str(target_root),
        "backup_dir": str(backup_dir),
        "status": status,
        "artifacts": artifacts,
        "missing": sorted(required_missing),
        "warnings": sorted(set(warnings)),
        "disclaimer": DISCLAIMER,
        "restore_requires_owner": True,
        "restore_apply": False,
        "network_used": False,
        "canonical_runtime_written": False,
        "research_business_scope": (_research_business_scope({**research_closure["contents"],
            **({"state/auto_paper_portfolio.json": (source / "state/auto_paper_portfolio.json").read_bytes()}
               if (source / "state/auto_paper_portfolio.json").is_file() else {})})
            if research_closure is not None else {"status": "not_assessed"}),
    }
    if not dry_run:
        _atomic_json(manifest_path, manifest)
    else:
        warnings.append("dry_run_no_files_written")
    return BackupResult(
        ok=status in {"passed", "planned"},
        status=status,
        destination=target_root,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        manifest=manifest,
        missing=tuple(sorted(required_missing)),
        warnings=tuple(sorted(set(warnings))),
    )


def _load_manifest(manifest: Path | str | Mapping[str, Any]) -> tuple[dict[str, Any], Path | None]:
    if isinstance(manifest, Mapping):
        return dict(manifest), None
    path = _resolved(manifest)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("backup manifest must be a JSON object")
    return payload, path


def _backup_artifact_path(payload: Mapping[str, Any], row: Mapping[str, Any], manifest_path: Path | None) -> Path:
    relative = Path(str(row.get("relative_path") or ""))
    if not relative.parts or relative.anchor or ".." in relative.parts:
        raise ValueError("artifact_relative_path_invalid")
    known = {spec.name: spec.relative_path for spec in PAPER_ARTIFACTS + OPTIONAL_ARTIFACTS}
    if row.get("name") in known and relative != known[row["name"]]:
        raise ValueError("artifact_identity_mismatch")
    backup_dir = str(payload.get("backup_dir") or "")
    if manifest_path is None and not backup_dir:
        raise ValueError("backup_directory_missing")
    base = manifest_path.parent if manifest_path else _resolved(backup_dir)
    files_root = (base / "files").resolve()
    target = (files_root / relative).resolve()
    if files_root not in target.parents:
        raise ValueError("artifact_outside_backup")
    return target


def verify_paper_state_backup(manifest: Path | str | Mapping[str, Any]) -> VerificationResult:
    """Verify every copied artifact against its manifest hash without writing."""

    try:
        payload, manifest_path = _load_manifest(manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VerificationResult(False, "failed", warnings=(f"manifest_invalid: {exc}",))
    if payload.get("schema_version") != BACKUP_SCHEMA:
        return VerificationResult(False, "failed", manifest_path, warnings=("manifest_schema_mismatch",))
    mismatches: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    if payload.get("status") != "passed":
        warnings.append("backup_not_complete")
    rows = payload.get("artifacts")
    if not isinstance(rows, list):
        return VerificationResult(False, "failed", manifest_path, warnings=("artifacts_invalid",))
    required = {spec.name for spec in PAPER_ARTIFACTS} | {"learning_log"}
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            warnings.append("artifact_row_invalid")
            continue
        name = str(row.get("name") or "unknown")
        if name in names:
            warnings.append(f"duplicate_artifact:{name}")
        names.add(name)
        status = str(row.get("status") or "")
        if status == "missing":
            missing.append(name)
            continue
        if status != "copied":
            warnings.append(f"artifact_not_copied:{name}")
            continue
        try:
            path = _backup_artifact_path(payload, row, manifest_path)
        except ValueError:
            mismatches.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        try:
            size, digest = _sha256(path)
        except OSError:
            missing.append(name)
            continue
        if size != row.get("bytes") or digest != row.get("sha256"):
            mismatches.append(name)
    missing.extend(sorted(required - names))
    ok = not mismatches and not missing and not warnings
    return VerificationResult(
        ok=ok,
        status="passed" if ok else "failed",
        manifest_path=manifest_path,
        mismatches=tuple(sorted(set(mismatches))),
        missing=tuple(sorted(set(missing))),
        warnings=tuple(sorted(set(warnings))),
    )


def build_restore_plan(
    manifest: Path | str | Mapping[str, Any],
    target_root: Path | str,
) -> dict[str, Any]:
    """Return a verified, non-applying restore plan for owner review."""

    payload, manifest_path = _load_manifest(manifest)
    verification = verify_paper_state_backup(manifest_path or payload)
    target = _resolved(target_root)
    items: list[dict[str, Any]] = []
    for row in payload.get("artifacts") or [] if verification.ok else []:
        if not isinstance(row, dict) or row.get("status") != "copied":
            continue
        relative = Path(str(row.get("relative_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        items.append(
            {
                "name": row.get("name"),
                "backup_path": str(_backup_artifact_path(payload, row, manifest_path)),
                "target_path": str(target / relative),
                "sha256": row.get("sha256"),
                "bytes": row.get("bytes"),
            }
        )
    return {
        "schema_version": BACKUP_SCHEMA,
        "status": "ready_for_owner" if verification.ok else "verification_failed",
        "apply": False,
        "restore_requires_owner": True,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "target_root": str(target),
        "verification": {
            "status": verification.status,
            "ok": verification.ok,
            "mismatches": list(verification.mismatches),
            "missing": list(verification.missing),
            "warnings": list(verification.warnings),
        },
        "items": items,
        "disclaimer": DISCLAIMER,
    }


def restore_plan(manifest: Path | str | Mapping[str, Any], target_root: Path | str) -> dict[str, Any]:
    """Compatibility alias emphasizing that restore remains a plan only."""

    return build_restore_plan(manifest, target_root)


__all__ = [
    "BACKUP_SCHEMA",
    "DISCLAIMER",
    "BackupResult",
    "VerificationResult",
    "backup_paper_state",
    "build_restore_plan",
    "restore_plan",
    "verify_paper_state_backup",
]
