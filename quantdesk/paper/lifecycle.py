"""Append-only candidate lifecycle evidence for research and paper accounts.

This module records decisions; it does not allocate cash, create orders, or
simulate fills.  Legacy watchlist files remain compatibility projections while
this log provides an auditable, point-in-time lifecycle history.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from quantdesk.paper.state_store import PaperStateStore

LIFECYCLE_SCHEMA_VERSION = "candidate-lifecycle.v1"

RESEARCH_CANDIDATE = "research_candidate"
PAPER_WATCHLIST = "paper_watchlist"
PAPER_POSITION = "paper_position"
EXCLUDED_PENDING_RESEARCH = "excluded_pending_research"
MANUAL_OBSERVATION = "manual_observation"

_REQUESTED_STATES = {
    RESEARCH_CANDIDATE,
    PAPER_WATCHLIST,
    PAPER_POSITION,
    EXCLUDED_PENDING_RESEARCH,
}
_DECISION_ORIGINS = {"system", "manual"}
_FORBIDDEN_UNFUNDED_FIELDS = {
    "cash",
    "quantity",
    "funded",
    "order_intent",
    "fill_price",
}
_FUNDED_GATES = ("allocator", "risk", "simulation_kernel")


class LifecycleError(RuntimeError):
    """Base lifecycle error."""


class LifecycleValidationError(LifecycleError):
    """Raised when an event violates the lifecycle boundary."""


class LifecycleConflictError(LifecycleError):
    """Raised when an existing event id is reused with different content."""


class LifecycleStoreCorruptError(LifecycleError):
    """Raised when neither the primary log nor a backup is trustworthy."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_true(value: Any) -> bool:
    return value is True


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_instant(value: str, *, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise LifecycleValidationError(f"{field_name}_missing")
    try:
        instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleValidationError(f"{field_name}_invalid:{text}") from exc
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _find_forbidden_fields(value: Any, *, prefix: str = "payload") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            clean_key = str(key).strip().lower()
            location = f"{prefix}.{key}"
            if clean_key in _FORBIDDEN_UNFUNDED_FIELDS:
                found.append(location)
            found.extend(_find_forbidden_fields(nested, prefix=location))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found.extend(_find_forbidden_fields(nested, prefix=f"{prefix}[{index}]"))
    return found


def evaluate_candidate_eligibility(
    evidence: Mapping[str, Any] | None,
    *,
    minimum_coverage: float | None = None,
) -> dict[str, Any]:
    """Return a strict, deterministic eligibility decision.

    Missing values and string booleans fail closed.  Only formal evidence with
    explicit PIT and Price Data Contract eligibility may enter an automated
    research/watch lifecycle state.
    """

    source = dict(evidence or {})
    reasons: list[str] = []

    if not _strict_true(source.get("eligible")):
        reasons.append("evidence_not_eligible")

    if str(source.get("evidence_kind") or "").strip().lower() != "formal":
        reasons.append("proxy_or_non_formal_evidence")

    coverage = _finite_number(source.get("coverage"))
    threshold_value = minimum_coverage
    if threshold_value is None:
        threshold_value = _finite_number(source.get("minimum_coverage"))
    threshold = 1.0 if threshold_value is None else float(threshold_value)
    if coverage is None or coverage < threshold:
        reasons.append("evidence_coverage_below_minimum")

    if not _strict_true(source.get("pit_eligible")):
        reasons.append("pit_evidence_not_eligible")

    if not _strict_true(source.get("price_contract_eligible")):
        reasons.append("price_contract_not_eligible")

    return {
        "eligible": not reasons,
        "reason_codes": reasons,
        "coverage": coverage,
        "minimum_coverage": threshold,
        "evidence_kind": str(source.get("evidence_kind") or "missing"),
    }


@dataclass(frozen=True)
class CandidateDecisionEvent:
    """Immutable decision input that is projected into a lifecycle state."""

    event_id: str
    symbol: str
    occurred_at: str
    requested_state: str
    decision_origin: str = "system"
    evidence: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_event_ids: tuple[str, ...] = ()
    gate_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        symbol = str(self.symbol or "").strip()
        requested_state = str(self.requested_state or "").strip().lower()
        decision_origin = str(self.decision_origin or "").strip().lower()
        if not event_id:
            raise LifecycleValidationError("event_id_missing")
        if not symbol:
            raise LifecycleValidationError("symbol_missing")
        _parse_instant(self.occurred_at, field_name="occurred_at")
        if requested_state not in _REQUESTED_STATES:
            raise LifecycleValidationError(f"requested_state_invalid:{requested_state}")
        if decision_origin not in _DECISION_ORIGINS:
            raise LifecycleValidationError(f"decision_origin_invalid:{decision_origin}")

        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "occurred_at", str(self.occurred_at).strip())
        object.__setattr__(self, "requested_state", requested_state)
        object.__setattr__(self, "decision_origin", decision_origin)
        object.__setattr__(self, "evidence", copy.deepcopy(dict(self.evidence or {})))
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload or {})))
        object.__setattr__(
            self,
            "source_event_ids",
            tuple(dict.fromkeys(str(item).strip() for item in self.source_event_ids if str(item).strip())),
        )
        object.__setattr__(self, "gate_evidence", copy.deepcopy(dict(self.gate_evidence or {})))

    def _effective_state_and_reasons(self) -> tuple[str, list[str]]:
        if self.decision_origin == "manual":
            return MANUAL_OBSERVATION, ["manual_observation"]
        if self.requested_state == EXCLUDED_PENDING_RESEARCH:
            return EXCLUDED_PENDING_RESEARCH, ["explicit_research_exclusion"]

        eligibility = evaluate_candidate_eligibility(self.evidence)
        reasons = list(eligibility["reason_codes"])
        if self.requested_state == PAPER_POSITION:
            if reasons:
                raise LifecycleValidationError(
                    "funded_position_evidence_not_eligible:" + ",".join(reasons)
                )
            self._validate_funded_gates()
            return PAPER_POSITION, []
        if reasons:
            return EXCLUDED_PENDING_RESEARCH, reasons
        return self.requested_state, []

    def _validate_funded_gates(self) -> None:
        source_ids = set(self.source_event_ids)
        for gate_name in _FUNDED_GATES:
            gate = self.gate_evidence.get(gate_name)
            if not isinstance(gate, Mapping) or not _strict_true(gate.get("passed")):
                raise LifecycleValidationError(f"{gate_name}_gate_missing")
            gate_event_id = str(gate.get("event_id") or "").strip()
            if not gate_event_id:
                raise LifecycleValidationError(f"{gate_name}_event_id_missing")
            if gate_event_id not in source_ids:
                raise LifecycleValidationError(f"{gate_name}_source_event_missing")

    def _unsigned_dict(self) -> dict[str, Any]:
        effective_state, reasons = self._effective_state_and_reasons()
        forbidden = _find_forbidden_fields(self.payload)
        if effective_state != PAPER_POSITION and forbidden:
            raise LifecycleValidationError(f"unfunded_event_forbidden_field:{forbidden[0]}")

        row: dict[str, Any] = {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "occurred_at": self.occurred_at,
            "requested_state": self.requested_state,
            "effective_state": effective_state,
            "decision_origin": self.decision_origin,
            "evidence": copy.deepcopy(dict(self.evidence)),
            "payload": copy.deepcopy(dict(self.payload)),
            "source_event_ids": list(self.source_event_ids),
            "gate_evidence": copy.deepcopy(dict(self.gate_evidence)),
            "eligibility_reason_codes": reasons,
            "research_only": True,
            "paper_only": True,
            "is_real_order": False,
            "no_broker": True,
        }
        if effective_state == PAPER_POSITION:
            row["funded"] = True
        return row

    @property
    def content_digest(self) -> str:
        return _digest(self._unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        row = self._unsigned_dict()
        row["content_digest"] = _digest(row)
        return row

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "CandidateDecisionEvent":
        return cls(
            event_id=str(row.get("event_id") or ""),
            symbol=str(row.get("symbol") or ""),
            occurred_at=str(row.get("occurred_at") or ""),
            requested_state=str(row.get("requested_state") or ""),
            decision_origin=str(row.get("decision_origin") or "system"),
            evidence=dict(row.get("evidence") or {}),
            payload=dict(row.get("payload") or {}),
            source_event_ids=tuple(row.get("source_event_ids") or ()),
            gate_evidence=dict(row.get("gate_evidence") or {}),
        )


class LifecycleEventStore:
    """Atomic append-only JSON event log with verified backup recovery."""

    def __init__(self, path: str | Path, *, max_backups: int = 5) -> None:
        self.path = Path(path)
        self.max_backups = max(1, int(max_backups or 1))
        self._state_store = PaperStateStore(
            self.path,
            schema_version=LIFECYCLE_SCHEMA_VERSION,
            max_backups=self.max_backups,
        )

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": LIFECYCLE_SCHEMA_VERSION, "events": []}

    def _verify_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(dict(payload))
        if data.get("schema_version") != LIFECYCLE_SCHEMA_VERSION:
            raise LifecycleStoreCorruptError("lifecycle schema mismatch")
        rows = data.get("events")
        if not isinstance(rows, list):
            raise LifecycleStoreCorruptError("lifecycle events must be a list")
        seen: set[str] = set()
        verified: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise LifecycleStoreCorruptError("lifecycle event must be an object")
            try:
                expected = CandidateDecisionEvent.from_dict(raw).to_dict()
            except LifecycleValidationError as exc:
                raise LifecycleStoreCorruptError(f"invalid lifecycle event: {exc}") from exc
            if dict(raw) != expected:
                raise LifecycleStoreCorruptError(
                    f"lifecycle event digest/content mismatch:{expected['event_id']}"
                )
            event_id = expected["event_id"]
            if event_id in seen:
                raise LifecycleStoreCorruptError(f"duplicate lifecycle event id:{event_id}")
            seen.add(event_id)
            verified.append(expected)
        data["events"] = verified
        warnings = [str(item) for item in (data.get("warnings") or []) if str(item).strip()]
        if data.get("state_recovered_from_backup") is True:
            warnings.append("lifecycle_log_recovered_from_backup")
        if warnings:
            data["warnings"] = list(dict.fromkeys(warnings))
        return data

    def _verified_backups(self) -> list[Path]:
        if not self.path.parent.exists():
            return []
        pattern = f"{self.path.stem}.backup_*.json"
        return sorted(
            self.path.parent.glob(pattern),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _recover_verified_backup(self, original_error: Exception) -> dict[str, Any]:
        for backup in self._verified_backups():
            try:
                raw = json.loads(backup.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    continue
                recovered = self._verify_payload(raw)
            except Exception:  # noqa: BLE001 - continue to older verified backup
                continue
            recovered["state_recovered_from_backup"] = True
            recovered["state_recovery_source"] = str(backup)
            recovered["warnings"] = list(
                dict.fromkeys(
                    [*(recovered.get("warnings") or []), "lifecycle_log_recovered_from_backup"]
                )
            )
            self._state_store.write(recovered)
            return self._verify_payload(recovered)
        raise LifecycleStoreCorruptError(
            f"lifecycle log is corrupt and no valid backup is available: {original_error}"
        ) from original_error

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            if self._verified_backups():
                return self._recover_verified_backup(
                    LifecycleStoreCorruptError("lifecycle primary log is missing")
                )
            return self._empty()
        try:
            loaded = self._state_store.read(default=self._empty())
            if loaded.get("state_recovery_warning"):
                raise LifecycleStoreCorruptError(str(loaded["state_recovery_warning"]))
            return self._verify_payload(loaded)
        except (LifecycleStoreCorruptError, LifecycleValidationError) as exc:
            return self._recover_verified_backup(exc)

    def append(self, event: CandidateDecisionEvent | Mapping[str, Any]) -> dict[str, Any]:
        candidate = (
            event
            if isinstance(event, CandidateDecisionEvent)
            else CandidateDecisionEvent.from_dict(event)
        )
        row = candidate.to_dict()
        current = self.read()
        for existing in current["events"]:
            if existing["event_id"] != row["event_id"]:
                continue
            if existing["content_digest"] == row["content_digest"]:
                return {
                    "appended": False,
                    "status": "duplicate_noop",
                    "event_id": row["event_id"],
                }
            raise LifecycleConflictError(
                f"event_id {row['event_id']} already exists with different content"
            )

        output = copy.deepcopy(current)
        output["events"] = [*current["events"], row]
        written = self._state_store.write(output)
        self._verify_payload(written)
        return {"appended": True, "status": "appended", "event": copy.deepcopy(row)}

    def replay(self, *, as_of: str | None = None) -> dict[str, Any]:
        current = self.read()
        cutoff = _parse_instant(as_of, field_name="as_of") if as_of is not None else None
        selected: list[dict[str, Any]] = []
        for row in current["events"]:
            occurred = _parse_instant(str(row["occurred_at"]), field_name="occurred_at")
            if cutoff is None or occurred <= cutoff:
                selected.append(copy.deepcopy(row))
        selected.sort(
            key=lambda row: (
                _parse_instant(str(row["occurred_at"]), field_name="occurred_at"),
                str(row["event_id"]),
            )
        )

        by_symbol: dict[str, dict[str, Any]] = {}
        for row in selected:
            by_symbol[str(row["symbol"])] = copy.deepcopy(row)

        def symbols_for(state: str) -> list[str]:
            return sorted(
                symbol
                for symbol, row in by_symbol.items()
                if row.get("effective_state") == state
            )

        return {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "as_of": as_of,
            "event_count": len(selected),
            "events": selected,
            "by_symbol": by_symbol,
            "research_candidates": symbols_for(RESEARCH_CANDIDATE),
            "paper_watchlist": symbols_for(PAPER_WATCHLIST),
            "paper_positions": symbols_for(PAPER_POSITION),
            "excluded_pending_research": symbols_for(EXCLUDED_PENDING_RESEARCH),
            "manual_observations": symbols_for(MANUAL_OBSERVATION),
            "research_only": True,
            "paper_only": True,
            "is_real_order": False,
            "no_broker": True,
        }


__all__ = [
    "CandidateDecisionEvent",
    "LifecycleConflictError",
    "LifecycleError",
    "LifecycleEventStore",
    "LifecycleStoreCorruptError",
    "LifecycleValidationError",
    "evaluate_candidate_eligibility",
    "LIFECYCLE_SCHEMA_VERSION",
    "RESEARCH_CANDIDATE",
    "PAPER_WATCHLIST",
    "PAPER_POSITION",
    "EXCLUDED_PENDING_RESEARCH",
    "MANUAL_OBSERVATION",
]
