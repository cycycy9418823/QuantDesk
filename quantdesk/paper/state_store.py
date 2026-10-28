"""Robust JSON state store for local paper-account ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

StateValidator = Callable[[dict[str, Any]], list[str]]


@dataclass(frozen=True)
class PaperStateRead:
    """Read-only evidence; an available backup is not an applied recovery."""

    status: str
    data: dict[str, Any] | None
    reason_codes: tuple[str, ...] = ()
    source: str = ""
    source_sha256: str = ""
    recovery_source: str = ""
    recovery_sha256: str = ""


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


@dataclass
class PaperStateStore:
    """Atomic JSON state store with schema stamping and backup recovery."""

    path: Path
    schema_version: str | None
    max_backups: int = 5
    validator: StateValidator | None = None
    identity_fields: tuple[str, ...] = ()

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

    def _identity_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.identity.json")

    def _identity(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return {key: data[key] for key in self.identity_fields if data.get(key) is not None}

    def read_snapshot(self, *, expected_identity: Mapping[str, Any] | None = None) -> PaperStateRead:
        """Inspect original bytes and candidate backups without moving or writing files.

        A missing legacy ledger without an independent identity cannot silently
        adopt an arbitrary backup. Its recovery remains an explicit owner action.
        """

        raw: bytes | None = None
        data: dict[str, Any] | None = None
        reason = "paper_state_missing"
        try:
            raw = self.path.read_bytes()
            decoded = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(decoded, dict):
                raise ValueError("paper_state_not_object")
            data = decoded
            errors = self.validator(data) if self.validator else []
            if not errors:
                return PaperStateRead("ready", self._with_schema(data), source=str(self.path),
                                      source_sha256=hashlib.sha256(raw).hexdigest())
            reason = "paper_state_invalid"
        except FileNotFoundError:
            errors = []
        except (OSError, UnicodeError, ValueError) as exc:
            errors = [type(exc).__name__]
            reason = "paper_state_corrupt_or_unreadable"

        identity = dict(expected_identity or {})
        if not identity and data:
            identity = self._identity(data)
        if not identity and self.identity_fields:
            try:
                marker = self._read_json(self._identity_path())
                if marker.get("schema") == "paper_state_identity_v1" and isinstance(marker.get("identity"), dict):
                    identity = dict(marker["identity"])
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
        reasons = [reason, *errors]
        for backup in reversed(self._backups()):
            try:
                backup_bytes = backup.read_bytes()
                candidate = json.loads(backup_bytes.decode("utf-8-sig"))
                if not isinstance(candidate, dict) or (self.validator and self.validator(candidate)):
                    reasons.append("paper_state_backup_invalid")
                    continue
                if self.identity_fields and not identity:
                    reasons.append("paper_state_backup_identity_unverified")
                    continue
                if any(candidate.get(key) != value for key, value in identity.items()):
                    reasons.append("paper_state_backup_identity_mismatch")
                    continue
                return PaperStateRead("recovery_available", self._with_schema(candidate),
                    tuple(dict.fromkeys(reasons)), str(self.path),
                    hashlib.sha256(raw).hexdigest() if raw is not None else "",
                    str(backup), hashlib.sha256(backup_bytes).hexdigest())
            except (OSError, UnicodeError, ValueError):
                reasons.append("paper_state_backup_invalid")
        previously_written = bool(self._backups() or self._identity_path().exists()
                                  or list(self.path.parent.glob(f"{self.path.stem}.bad_*.json")))
        status = "missing" if raw is None and not previously_written and reason == "paper_state_missing" else "invalid"
        return PaperStateRead(status, None, tuple(dict.fromkeys(reasons)), str(self.path),
                              hashlib.sha256(raw).hexdigest() if raw is not None else "")

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
        if self.validator:
            errors = self.validator(data)
            if errors:
                raise ValueError("paper_state_write_invalid:" + ",".join(errors))
            if self.path.exists() and self.read_snapshot().status != "ready":
                raise ValueError("paper_state_requires_explicit_recovery")
            if not self.path.exists() and (self._identity_path().exists() or self._backups()
                    or list(self.path.parent.glob(f"{self.path.stem}.bad_*.json"))):
                raise ValueError("paper_state_requires_explicit_recovery")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_path()
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        # flush + fsync before the atomic replace: a power cut must leave either
        # the old file or the complete new one, never a torn ledger.
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)
        backup = self._backup_path()
        shutil.copy2(self.path, backup)
        try:
            with backup.open("rb+") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
        self._rotate_backups()
        if self.identity_fields:
            marker = self._identity_path()
            temporary = marker.with_name(marker.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"schema": "paper_state_identity_v1", "identity": self._identity(data)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(marker)
        return data

    def _read_json(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def read(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read state, recovering from the newest valid backup if necessary."""

        fallback = self._with_schema(default or {})
        if self.validator:
            result = self.read_snapshot()
            if result.status == "ready" and result.data is not None:
                return result.data
            return {"state_recovered_from_backup": False, "ledger_read_status": result.status,
                    "state_recovery_source": result.recovery_source,
                    "state_recovery_warning": ",".join(result.reason_codes)}
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
