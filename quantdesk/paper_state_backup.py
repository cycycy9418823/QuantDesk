"""Explicit, offline backup and restore audit for the two paper books.

The module deliberately has no project-path defaults.  Callers choose both the
source checkout and an external destination.  A restore is represented by a
read-only plan; applying it is an owner action outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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
    for spec in specs:
        resolved = (source / spec.relative_path).resolve()
        if source not in resolved.parents:
            raise ValueError(f"source artifact outside root: {spec.name}")
    if not dry_run:
        backup_dir.mkdir(parents=True, exist_ok=False)
    required_missing = set(spec.name for spec in _missing_required_specs(source, owner_learning_log))
    artifacts: list[dict[str, Any]] = []
    warnings: list[str] = []
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
