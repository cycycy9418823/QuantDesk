"""Deterministic source fingerprint for the active QuantDesk API server."""

from __future__ import annotations

import hashlib
from pathlib import Path


_FINGERPRINT_SCHEMA = "quantdesk-api-source-v2"
_ACTIVE_SINGLE_FILES = (
    Path("scripts/quant_api_server.py"),
    Path("scripts/startup_data_update.py"),
    Path("scripts/run_strategy_screening_funnel.py"),
    Path("scripts/update_market_data.py"),
)
_ACTIVE_SOURCE_DIRS = (
    Path("quantdesk"),
    Path("scripts/server"),
)


def iter_server_source_files(project_root: Path) -> list[Path]:
    """Return active Python sources whose in-memory code requires a restart."""

    root = Path(project_root).resolve()
    paths: set[Path] = set()
    for relative in _ACTIVE_SINGLE_FILES:
        candidate = root / relative
        if candidate.is_file():
            paths.add(candidate)
    for relative in _ACTIVE_SOURCE_DIRS:
        source_dir = root / relative
        if source_dir.is_dir():
            paths.update(path for path in source_dir.rglob("*.py") if path.is_file())
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def compute_server_build_id(project_root: Path) -> str:
    """Hash active server source contents while ignoring runtime data outputs."""

    root = Path(project_root).resolve()
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_SCHEMA.encode("utf-8"))
    for path in iter_server_source_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(b"\0path\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0content\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"<unreadable:{type(exc).__name__}>".encode("utf-8"))
    return digest.hexdigest()[:20]
