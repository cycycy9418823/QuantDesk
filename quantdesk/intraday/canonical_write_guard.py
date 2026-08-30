"""Resolved-path guards for protected canonical intraday data."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path


CANONICAL_WRITE_FORBIDDEN = "canonical_intraday_direct_write_forbidden"


def resolved_paths_equal(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def path_is_within(path: Path, parent: Path) -> bool:
    resolved_path = os.path.normcase(str(Path(path).resolve()))
    resolved_parent = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([resolved_path, resolved_parent]) == resolved_parent
    except ValueError:
        return False


def ensure_path_outside_canonical(
    path: Path | None,
    canonical_dir: Path,
    *,
    role: str,
) -> Path | None:
    """Reject a support artifact path inside protected canonical storage."""

    if path is None:
        return None
    candidate = Path(path)
    if path_is_within(candidate, Path(canonical_dir)):
        raise ValueError(f"{CANONICAL_WRITE_FORBIDDEN}:{role}")
    return candidate


def ensure_directory_disjoint_from_canonical(
    path: Path,
    canonical_dir: Path,
    *,
    role: str,
) -> Path:
    """Reject a destructive/support directory that overlaps canonical storage."""

    candidate = Path(path)
    canonical = Path(canonical_dir)
    if path_is_within(candidate, canonical) or path_is_within(canonical, candidate):
        raise ValueError(f"{CANONICAL_WRITE_FORBIDDEN}:{role}")
    return candidate


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_restore_from_backup(
    target: Path,
    backup: Path,
    *,
    expected_sha256: str,
) -> str:
    """Restore exact backup bytes atomically and verify the resulting hash."""

    target = Path(target)
    backup = Path(backup)
    backup_hash = sha256_path(backup)
    if expected_sha256 and backup_hash != expected_sha256:
        raise ValueError(
            f"rollback_backup_hash_mismatch:{backup_hash}:{expected_sha256}"
        )
    data = backup.read_bytes()
    temporary = target.with_name(f".restore-{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        restored_hash = sha256_path(target)
        if expected_sha256 and restored_hash != expected_sha256:
            raise ValueError(
                f"rollback_target_hash_mismatch:{restored_hash}:{expected_sha256}"
            )
        return restored_hash
    finally:
        temporary.unlink(missing_ok=True)
