"""Robust JSON state store for local paper-account ledgers."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


@dataclass
class PaperStateStore:
    """Atomic JSON state store with schema stamping and backup recovery."""

    path: Path
    schema_version: str | None
    max_backups: int = 5

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.max_backups = max(1, int(self.max_backups or 1))

    def _backup_pattern(self) -> str:
        return f"{self.path.stem}.backup_*.json"

    def _backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.backup_{_stamp()}.json")

    def _bad_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.bad_{_stamp()}.json")

    def _tmp_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.tmp")

    def _backups(self) -> list[Path]:
        if not self.path.parent.exists():
            return []
        return sorted(self.path.parent.glob(self._backup_pattern()), key=lambda item: item.stat().st_mtime)

    def _rotate_backups(self) -> None:
        backups = self._backups()
        excess = len(backups) - self.max_backups
        for backup in backups[: max(0, excess)]:
            try:
                backup.unlink()
            except OSError:
                pass

    def _with_schema(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        if self.schema_version:
            data.setdefault("schema_version", self.schema_version)
        return data

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write state atomically and keep a recoverable snapshot backup."""

        data = self._with_schema(payload if isinstance(payload, dict) else {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_path()
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        shutil.copy2(self.path, self._backup_path())
        self._rotate_backups()
        return data

    def _read_json(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def read(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read state, recovering from the newest valid backup if necessary."""

        fallback = self._with_schema(default or {})
        if not self.path.exists():
            return fallback
        try:
            return self._with_schema(self._read_json(self.path))
        except Exception:  # noqa: BLE001 - corrupt state should recover
            bad_path = self._bad_path()
            try:
                self.path.replace(bad_path)
            except OSError:
                try:
                    shutil.copy2(self.path, bad_path)
                except OSError:
                    pass
            for backup in reversed(self._backups()):
                try:
                    recovered = self._with_schema(self._read_json(backup))
                    recovered["state_recovered_from_backup"] = True
                    recovered["state_recovery_source"] = str(backup)
                    self.write(recovered)
                    return recovered
                except Exception:  # noqa: BLE001
                    continue
            fallback["state_recovered_from_backup"] = False
            fallback["state_recovery_warning"] = "current state JSON was corrupt and no valid backup was available"
            return fallback
