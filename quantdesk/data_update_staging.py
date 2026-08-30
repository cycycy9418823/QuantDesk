from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def staging_dir(root: Path, provider: str, target_date: str) -> Path:
    safe_provider = provider.replace("/", "_").replace("\\", "_")
    safe_date = target_date.replace("/", "-")
    path = root / "data/update_staging" / safe_provider / safe_date
    path.mkdir(parents=True, exist_ok=True)
    return path

