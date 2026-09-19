"""Best-effort shadow adapter for the deterministic paper risk gate.

The adapter observes paper-only intents and writes an independent diagnostic
artifact.  It never changes the submitted legacy action, paper ledger, or
order flow.  Missing local evidence remains missing and therefore fails closed.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_truth.candidate_lifecycle_evidence import derive_candidate_evidence
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper.portfolio_contracts import (
    build_paper_exit_intent,
    build_paper_order_intent,
    build_portfolio_snapshot,
    resolve_paper_capital_profile,
)
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    classify_frame_price_basis,
    validate_price_frame_set,
)
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig
from quantdesk.risk.risk_gate import (
    compute_risk_evidence_source_hash,
    evaluate_paper_risk_gate,
)
from quantdesk.utils import atomic_write_json
from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RISK_GATE_SHADOW_LATEST_JSON = PROJECT_ROOT / "results" / "risk_gate_shadow_latest.json"
RISK_GATE_SHADOW_ERRORS_JSONL = PROJECT_ROOT / "results" / "risk_gate_shadow_errors.jsonl"
RISK_GATE_SHADOW_DAILY_DIR = PROJECT_ROOT / "results" / "risk_gate_shadow_daily"
RISK_GATE_SHADOW_LOCK_NAME = ".risk_gate_shadow_daily.lock"
RISK_GATE_SHADOW_SEAL_SUFFIX = ".sha256"
RISK_GATE_SHADOW_SEAL_SCHEMA = "risk_gate_shadow_archive_seal_v1"
SHA256_PREFIX = "sha256" + ":"
SHADOW_REPORT_HEADING = "## 影子风险门（Shadow Risk Gate）"
RISK_COMPONENTS = ("price", "sector", "liquidity", "returns", "smart_stop")

_UNKNOWN_LABELS = {
    "",
    "-",
    "nan",
    "none",
    "null",
    "unknown",
    "未知",
    "未知板块",
    "未知行业",
    "未知题材",
}


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_shadow_archive_bytes(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def shadow_archive_content_sha256(payload: Mapping[str, Any]) -> str:
    """Return the canonical content digest persisted beside a dated archive."""

    digest = hashlib.sha256(_canonical_shadow_archive_bytes(payload)).hexdigest()
    return SHA256_PREFIX + digest


def shadow_archive_seal_path(archive_path: Path) -> Path:
    path = Path(archive_path)
    return path.with_name(path.name + RISK_GATE_SHADOW_SEAL_SUFFIX)


def shadow_archive_seal_rejection_reason(
    archive_path: Path,
    payload: Mapping[str, Any],
) -> str | None:
    seal_path = shadow_archive_seal_path(archive_path)
    if not seal_path.is_file():
        return "archive_content_sha256_missing"
    try:
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "archive_content_sha256_unreadable"
    if not isinstance(seal, Mapping):
        return "archive_content_sha256_unreadable"
    expected = shadow_archive_content_sha256(payload)
    if (
        seal.get("schema_version") != RISK_GATE_SHADOW_SEAL_SCHEMA
        or seal.get("as_of") != payload.get("as_of")
        or seal.get("archive_filename") != Path(archive_path).name
        or seal.get("canonical_sha256") != expected
    ):
        return "archive_content_sha256_mismatch"
    return None


def verify_shadow_archive_seal(
    archive_path: Path,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    path = Path(archive_path)
    try:
        candidate = dict(payload) if payload is not None else _read_shadow_archive_payload(path)
    except ValueError:
        return False
    return shadow_archive_seal_rejection_reason(path, candidate) is None


def _write_shadow_archive_seal(
    archive_path: Path,
    payload: Mapping[str, Any],
) -> Path:
    seal_path = shadow_archive_seal_path(archive_path)
    atomic_write_json(
        seal_path,
        {
            "schema_version": RISK_GATE_SHADOW_SEAL_SCHEMA,
            "as_of": payload.get("as_of"),
            "archive_filename": Path(archive_path).name,
            "canonical_sha256": shadow_archive_content_sha256(payload),
        },
    )
    return seal_path


def _ensure_shadow_archive_seal(
    archive_path: Path,
    payload: Mapping[str, Any],
    *,
    repair_mismatch: bool,
) -> None:
    reason = shadow_archive_seal_rejection_reason(archive_path, payload)
    if reason is None:
        return
    if reason == "archive_content_sha256_missing" or repair_mismatch:
        _write_shadow_archive_seal(archive_path, payload)
        return
    raise ValueError(f"invalid shadow archive seal: {reason}: {archive_path}")


def _co_located_shadow_archive_dir(target: Path, archive_dir: Path | None) -> Path:
    expected = (target.parent / RISK_GATE_SHADOW_DAILY_DIR.name).resolve(strict=False)
    if archive_dir is None:
        return expected
    selected = Path(archive_dir).resolve(strict=False)
    if selected != expected:
        raise ValueError(
            "archive_dir must be the risk_gate_shadow_daily sibling of latest"
        )
    return selected


def _read_shadow_archive_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid shadow archive: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid shadow archive payload: {path}")
    return dict(payload)


def _validate_daily_summary(payload: Mapping[str, Any]) -> tuple[str, date, datetime]:
    if str(payload.get("task_type") or "") != "risk_gate_shadow_daily_summary":
        raise ValueError("shadow archive requires risk_gate_shadow_daily_summary")
    as_of = str(payload.get("as_of") or "").strip()
    as_of_day = _parse_day(as_of)
    if as_of_day is None or as_of != as_of_day.isoformat():
        raise ValueError("shadow archive as_of must be YYYY-MM-DD")
    generated_at = _parse_timestamp(payload.get("generated_at"))
    if generated_at is None:
        raise ValueError("shadow archive generated_at must be an ISO timestamp")
    return as_of, as_of_day, generated_at


def _archive_days(archive_dir: Path) -> list[date]:
    if not archive_dir.exists():
        return []
    days: list[date] = []
    for path in archive_dir.glob("*.json"):
        parsed = _parse_day(path.stem)
        if parsed is not None and path.stem == parsed.isoformat():
            days.append(parsed)
    return sorted(set(days))


def _archive_payload_equivalent(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> bool:
    existing_copy = dict(existing)
    existing_copy.pop("archive_source", None)
    return existing_copy == dict(incoming)


def _plan_shadow_archive_write(
    payload: Mapping[str, Any], *, archive_dir: Path
) -> tuple[Path, bool]:
    """Return the dated target and whether this observation is the newest.

    Append-only is enforced across dates: once a later ``as_of`` exists, an
    earlier day can never be added or changed.  The current maximum ``as_of``
    may be replaced only by a strictly newer ``generated_at``.  Equal payloads
    are idempotent; equal timestamps with different bytes fail closed.
    """

    as_of, as_of_day, incoming_generated = _validate_daily_summary(payload)
    days = _archive_days(archive_dir)
    if days and as_of_day < days[-1]:
        raise ValueError(
            f"historical shadow archive is immutable: {as_of} < {days[-1].isoformat()}"
        )
    target = archive_dir / f"{as_of}.json"
    if not target.is_file():
        return target, True

    existing = _read_shadow_archive_payload(target)
    existing_as_of, _, existing_generated = _validate_daily_summary(existing)
    if existing_as_of != as_of:
        raise ValueError(f"shadow archive filename/as_of mismatch: {target}")
    if incoming_generated < existing_generated:
        return target, False
    if incoming_generated == existing_generated:
        if _archive_payload_equivalent(existing, payload):
            return target, False
        raise ValueError(
            f"shadow archive generated_at conflict for {as_of}: {payload.get('generated_at')}"
        )
    return target, True


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _symbol(value: Any) -> str:
    text = "".join(char for char in str(value or "") if char.isdigit())
    return text[-6:].zfill(6) if text else ""


def _known_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in _UNKNOWN_LABELS else text


def _first_known_text(*values: Any) -> str | None:
    """Return the first semantically known label, not merely the first truthy one."""

    for value in values:
        known = _known_text(value)
        if known is not None:
            return known
    return None


def _canonical_hex(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_text(*values: Any) -> str:
    items: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in items:
            items.append(text)
    return ";".join(items)


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return SHA256_PREFIX + _canonical_hex(value)


def _source_snapshot(
    value: str | Path | None,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if value is None or str(value).startswith("memory://"):
        return None
    declared = Path(value)
    path = declared if declared.is_absolute() else PROJECT_ROOT / declared
    try:
        cache_key = str(path.resolve(strict=False)).casefold()
    except (OSError, RuntimeError):
        return None
    if cache_key in cache:
        return cache[cache_key]
    try:
        if not path.is_file():
            return None
        content = path.read_bytes()
    except (OSError, RuntimeError):
        return None
    snapshot = {
        "path": path,
        "content": content,
        "file_sha256": SHA256_PREFIX + hashlib.sha256(content).hexdigest(),
    }
    cache[cache_key] = snapshot
    return snapshot


def _file_binding(
    role: str,
    declared: str | Path,
    snapshot: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "role": role,
        "path": str(declared),
        "file_sha256": str(snapshot.get("file_sha256") or ""),
    }


_SOURCE_ROW_CONTAINERS = (
    "standard_candidates",
    "candidates",
    "events",
    "no_fill",
    "blocked",
)


def _snapshot_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    cached = snapshot.get("parsed_rows")
    if isinstance(cached, list):
        return cached
    content = snapshot.get("content")
    path = Path(snapshot.get("path") or "")
    if not isinstance(content, bytes):
        return []
    if path.suffix.lower() == ".json":
        if snapshot.get("json_payload_loaded") is True:
            payload = snapshot.get("json_payload")
        else:
            try:
                parsed = json.loads(content.decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError):
                parsed = None
            payload = parsed if isinstance(parsed, Mapping) else None
            if isinstance(snapshot, dict):
                snapshot["json_payload_loaded"] = True
                snapshot["json_payload"] = payload
        if not isinstance(payload, Mapping):
            return []
        rows = [dict(payload)] if _symbol(payload.get("symbol") or payload.get("code")) else []
        for key in _SOURCE_ROW_CONTAINERS:
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(dict(item) for item in value if isinstance(item, Mapping))
    else:
        frame = _snapshot_csv_frame(snapshot)
        if frame is None:
            return []
        frame = frame.copy()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        rows = [dict(row) for row in frame.to_dict(orient="records")]
    if isinstance(snapshot, dict):
        snapshot["parsed_rows"] = rows
    return rows


def _snapshot_csv_frame(snapshot: Mapping[str, Any]) -> pd.DataFrame | None:
    if snapshot.get("csv_frame_loaded") is True:
        cached = snapshot.get("csv_frame")
        return cached if isinstance(cached, pd.DataFrame) else None
    content = snapshot.get("content")
    path = Path(snapshot.get("path") or "")
    frame: pd.DataFrame | None = None
    if isinstance(content, bytes) and path.suffix.lower() != ".json":
        try:
            frame = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")
            frame.attrs["warnings"] = []
        except Exception as exc:  # noqa: BLE001 - malformed local evidence fails closed
            frame = pd.DataFrame()
            frame.attrs["warnings"] = [
                f"csv_read_failed:{path}:{type(exc).__name__}:{exc}"
            ]
    if isinstance(snapshot, dict):
        snapshot["csv_frame_loaded"] = True
        snapshot["csv_frame"] = frame
    return frame


def _cached_daily_as_of(
    snapshot: Mapping[str, Any] | None,
    daily_dir: Path,
    symbol: str,
    *,
    as_of: str,
) -> tuple[pd.DataFrame, Path | None, list[str], pd.DataFrame]:
    if snapshot is None:
        return _read_daily_as_of(daily_dir, symbol, as_of=as_of)
    as_of_day = _parse_day(as_of)
    cache_key = as_of_day.isoformat() if as_of_day is not None else f"invalid:{as_of}"
    cached_by_day = snapshot.get("daily_as_of")
    if isinstance(cached_by_day, Mapping) and cache_key in cached_by_day:
        return cached_by_day[cache_key]
    result = _read_daily_as_of(
        daily_dir,
        symbol,
        as_of=as_of,
        source_frame=_snapshot_csv_frame(snapshot),
    )
    if isinstance(snapshot, dict):
        cache = snapshot.setdefault("daily_as_of", {})
        if isinstance(cache, dict):
            cache[cache_key] = result
    return result


def _source_row_day(row: Mapping[str, Any]) -> str | None:
    for key in (
        "data_latest_date",
        "latest_date",
        "signal_date",
        "trade_date",
        "date",
        "datetime",
        "occurred_at",
        "generated_at",
    ):
        if row.get(key) not in (None, ""):
            day = _parse_day(row.get(key))
            return day.isoformat() if day is not None else None
    return None


def _matching_source_rows(
    snapshot: Mapping[str, Any] | None,
    *,
    symbol: str,
    target_date: str,
    as_of: str,
    business_candidate: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    as_of_day = _parse_day(as_of)
    target_day = _parse_day(target_date)
    if snapshot is None or as_of_day is None or target_day is None or target_day > as_of_day:
        return []
    rows = [
        row
        for row in _snapshot_rows(snapshot)
        if _symbol(row.get("symbol") or row.get("code")) == symbol
        and _source_row_day(row) == target_day.isoformat()
    ]
    if business_candidate is None:
        return rows
    return [row for row in rows if _business_fields_match(row, business_candidate)]


_BUSINESS_MATCH_FIELDS = (
    "close",
    "amount_ma20",
    "data_latest_date",
    "daily_feature_latest_date",
    "is_target_date_covered",
    "source_qualification",
)


def _business_number(value: Any) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None else None


def _business_field_value(row: Mapping[str, Any], key: str) -> Any:
    if key == "close":
        for alias in ("close", "latest_price", "reference_close", "price", "trigger_price"):
            if row.get(alias) not in (None, ""):
                return row.get(alias)
        return None
    return row.get(key)


def _business_values_equal(left: Any, right: Any, key: str) -> bool:
    if key in {"close", "amount_ma20"}:
        left_number = _business_number(left)
        right_number = _business_number(right)
        if left_number is None or right_number is None:
            return False
        return math.isclose(left_number, right_number, rel_tol=1e-9, abs_tol=1e-6)
    if key in {"data_latest_date", "daily_feature_latest_date"}:
        left_day = _parse_day(left)
        right_day = _parse_day(right)
        return left_day is not None and left_day == right_day
    if key == "is_target_date_covered":
        return bool(left) is bool(right)
    if key == "source_qualification":
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            return _canonical_hex(left) == _canonical_hex(right)
        return str(left).strip() == str(right).strip()
    return str(left).strip() == str(right).strip()


def _business_fields_match(row: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Require every business field declared by both source and candidate to agree."""

    for key in _BUSINESS_MATCH_FIELDS:
        left = _business_field_value(row, key)
        right = _business_field_value(candidate, key)
        if left in (None, "") or right in (None, ""):
            continue
        if not _business_values_equal(left, right, key):
            return False
    candidate_sector = _first_known_text(candidate.get("sector"), candidate.get("industry"))
    source_sector = _first_known_text(row.get("sector"), row.get("industry"))
    if candidate_sector and source_sector and candidate_sector != source_sector:
        return False
    return True


def _price_rows_match(rows: list[dict[str, Any]], reference_price: float) -> bool:
    for row in rows:
        for key in (
            "reference_price",
            "trigger_price",
            "price",
            "close",
            "open",
            "high",
            "low",
        ):
            value = _finite(row.get(key), positive=True)
            if value is not None and math.isclose(
                value,
                reference_price,
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                return True
    return False


def _ledger_core(ledger: Mapping[str, Any]) -> dict[str, Any]:
    top_keys = (
        "schema_version",
        "ledger_id",
        "profile_id",
        "scenario_id",
        "initial_cash",
        "cash",
    )
    account_keys = (
        "profile_id",
        "scenario_id",
        "initial_cash",
        "cash",
        "available_cash",
    )
    position_keys = (
        "symbol",
        "quantity",
        "sellable_quantity",
        "market_value",
        "latest_price",
        "entry_price",
        "avg_cost",
        "entry_date",
        "sector",
    )
    core = {key: copy.deepcopy(ledger.get(key)) for key in top_keys}
    account = ledger.get("account")
    core["account"] = {
        key: copy.deepcopy(account.get(key)) if isinstance(account, Mapping) else None
        for key in account_keys
    }
    positions: list[dict[str, Any]] = []
    for row in ledger.get("positions") or []:
        if not isinstance(row, Mapping):
            continue
        positions.append({key: copy.deepcopy(row.get(key)) for key in position_keys})
    core["positions"] = sorted(
        positions,
        key=lambda row: str(row.get("symbol") or ""),
    )
    return core


def _ledger_binding(
    value: str | Path,
    funded_ledger: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    text = str(value)
    payload_hash = _payload_sha256(funded_ledger)
    if text.startswith("memory://stage4-"):
        return {"role": "ledger", "uri": text, "payload_sha256": payload_hash}
    snapshot = _source_snapshot(value, cache)
    if snapshot is None:
        return None
    try:
        payload = json.loads(bytes(snapshot["content"]).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(payload, Mapping) or _ledger_core(payload) != _ledger_core(funded_ledger):
        return None
    return _file_binding("ledger", value, snapshot)


def _seal(component: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(component)
    sealed["source_hash"] = compute_risk_evidence_source_hash(sealed)
    return sealed


def _candidate_value(candidate: Mapping[str, Any], key: str) -> Any:
    queue: list[tuple[Mapping[str, Any], int]] = [(candidate, 0)]
    seen: set[int] = set()
    while queue:
        item, depth = queue.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        if key in item and item.get(key) not in (None, ""):
            return item.get(key)
        if depth >= 5:
            continue
        for nested_key in ("raw", "source_item", "features", "payload"):
            nested = item.get(nested_key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
    return None


def _candidate_cutoff(candidate: Mapping[str, Any], *, as_of: str) -> str | None:
    as_of_day = _parse_day(as_of)
    if as_of_day is None:
        return None
    cutoff_keys = ("data_latest_date", "latest_date", "trade_date", "date", "generated_at")

    def declared_cutoff(mapping: Mapping[str, Any]) -> tuple[bool, str | None]:
        declared = [mapping.get(key) for key in cutoff_keys if mapping.get(key) not in (None, "")]
        if not declared:
            return False, None
        days = [_parse_day(value) for value in declared]
        # An authoritative declaration that is invalid, in the future, or
        # conflicts within the same provenance layer fails closed.  Never
        # cherry-pick an older nested date to launder newer business values.
        if any(day is None or day > as_of_day for day in days):
            return True, None
        unique = {day.isoformat() for day in days if day is not None}
        return True, next(iter(unique)) if len(unique) == 1 else None

    declared, cutoff = declared_cutoff(candidate)
    if declared:
        return cutoff
    queue: list[tuple[Mapping[str, Any], int]] = [(candidate, 0)]
    seen: set[int] = {id(candidate)}
    while queue:
        item, depth = queue.pop(0)
        if depth >= 5:
            continue
        for nested_key in ("raw", "source_item", "features", "payload"):
            nested = item.get(nested_key)
            if not isinstance(nested, Mapping) or id(nested) in seen:
                continue
            seen.add(id(nested))
            declared, cutoff = declared_cutoff(nested)
            if declared:
                return cutoff
            queue.append((nested, depth + 1))
    return None


def _candidate_lifecycle_evidence(
    candidate: Mapping[str, Any],
    *,
    as_of: str | None = None,
    daily_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the candidate's lifecycle evidence, deriving it when absent.

    A candidate that already carries its own evidence block keeps it verbatim
    (allowlisted), so an upstream producer stays authoritative. Production
    candidate artifacts carry no such block, which left this returning ``{}``
    and collapsed every buy evaluation to ``insufficient_evidence``. When
    ``as_of`` is supplied we derive the block from canonical disk evidence
    instead — the derivation proves each field or leaves it unproven, so an
    undeliverable proof still fails closed rather than being assumed.
    """

    allowed = {
        "eligible",
        "evidence_kind",
        "coverage",
        "minimum_coverage",
        "pit_eligible",
        "price_contract_eligible",
    }
    source = candidate.get("evidence")
    if not isinstance(source, Mapping):
        source = candidate.get("lifecycle_evidence")
    if isinstance(source, Mapping):
        return {
            key: copy.deepcopy(value) for key, value in source.items() if key in allowed
        }
    if not as_of:
        return {}
    derived = derive_candidate_evidence(
        candidate,
        as_of=str(as_of)[:10],
        daily_dir=daily_dir,
    )
    return {key: value for key, value in derived.items() if key in allowed}


def _daily_path(daily_dir: Path, symbol: str) -> Path | None:
    for suffix in (".csv", ".CSV"):
        path = Path(daily_dir) / f"{symbol}{suffix}"
        if path.exists() and path.is_file():
            return path
    return None


def _read_daily_as_of(
    daily_dir: Path,
    symbol: str,
    *,
    as_of: str,
    source_bytes: bytes | None = None,
    source_frame: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, Path | None, list[str], pd.DataFrame]:
    path = _daily_path(daily_dir, symbol)
    if path is None:
        return pd.DataFrame(), None, ["daily_file_missing"], pd.DataFrame()
    if source_frame is not None:
        frame = source_frame
    elif source_bytes is None:
        frame = read_csv_frame(path)
    else:
        try:
            frame = pd.read_csv(io.BytesIO(source_bytes), encoding="utf-8-sig")
            frame.attrs["warnings"] = []
        except Exception as exc:  # noqa: BLE001 - local evidence fails closed visibly
            frame = pd.DataFrame()
            frame.attrs["warnings"] = [
                f"csv_read_failed:{path}:{type(exc).__name__}:{exc}"
            ]
    warnings = [str(item) for item in frame.attrs.get("warnings") or []]
    if frame.empty:
        return pd.DataFrame(), path, warnings or ["daily_frame_empty"], frame
    # Price Data Contract: classify the original frame before filtering,
    # sorting, or deduplication.  Canonical daily files declare this through
    # ``adjust``; provider/path inference and test-only aliases are forbidden.
    observed_basis, basis_reason = classify_frame_price_basis(frame)
    if basis_reason == "unknown_price_basis":
        warnings.append("daily_price_basis_missing")
    elif basis_reason:
        warnings.extend([f"daily_{basis_reason}", "daily_price_basis_not_raw"])
    elif observed_basis != PRICE_BASIS_RAW:
        warnings.append("daily_price_basis_not_raw")
    normalized = frame.copy()
    normalized.columns = [str(column).strip().lower() for column in normalized.columns]
    date_column = next((name for name in ("date", "trade_date", "datetime") if name in normalized.columns), None)
    if date_column is None:
        return pd.DataFrame(), path, [*warnings, "daily_date_column_missing"], frame
    date_text = normalized[date_column].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    parsed = pd.to_datetime(date_text, errors="coerce")
    normalized = normalized.assign(_date=parsed).dropna(subset=["_date"])
    as_of_day = _parse_day(as_of)
    if as_of_day is None:
        return pd.DataFrame(), path, [*warnings, "as_of_invalid"], frame
    normalized = normalized[normalized["_date"].dt.date <= as_of_day]
    normalized = normalized.sort_values("_date").drop_duplicates("_date", keep="last")
    if normalized.empty:
        return pd.DataFrame(), path, [*warnings, "daily_no_rows_as_of"], frame
    return normalized, path, warnings, frame


def _daily_rows(frame: pd.DataFrame, count: int) -> list[dict[str, Any]]:
    required = ("open", "high", "low", "close", "volume", "amount")
    if frame.empty or any(column not in frame.columns for column in required):
        return []
    rows: list[dict[str, Any]] = []
    for _, raw in frame.tail(count).iterrows():
        values = {column: _finite(raw.get(column)) for column in required}
        if any(value is None for value in values.values()):
            return []
        rows.append(
            {
                "date": raw["_date"].date().isoformat(),
                **{column: float(values[column]) for column in required},
            }
        )
    return rows if len(rows) == count else []


def _json_snapshot_payload(
    path: Path | str | None,
    cache: dict[str, dict[str, Any]],
) -> Mapping[str, Any] | None:
    snapshot = _source_snapshot(path, cache)
    if snapshot is None:
        return None
    if snapshot.get("json_payload_loaded") is True:
        cached = snapshot.get("json_payload")
        return cached if isinstance(cached, Mapping) else None
    try:
        payload = json.loads(bytes(snapshot["content"]).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        payload = None
    result = payload if isinstance(payload, Mapping) else None
    snapshot["json_payload_loaded"] = True
    snapshot["json_payload"] = result
    return result


def _sector_membership(
    path: Path | None,
    *,
    as_of: str,
    status_payload: Mapping[str, Any] | None = None,
    source_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """symbol -> sector from the archived same-day capture backing ``path``.

    The status artifact records which snapshots it was derived from; membership
    is read from the snapshot whose ``target_date`` is at or before ``as_of``, so
    the mapping is point-in-time rather than a present-day proxy. Returns an
    empty mapping when the artifact does not certify itself, which leaves the
    caller on its previous ledger-derived behaviour.
    """

    if path is None:
        return {}
    cache = source_cache if source_cache is not None else {}
    payload = status_payload or _json_snapshot_payload(path, cache)
    if payload is None:
        return {}
    as_of_day = _parse_day(as_of)
    payload_day = _parse_day(payload.get("target_date"))
    if not (
        payload.get("ok") is True
        and str(payload.get("status") or "").strip().lower() in {"passed", "ok"}
        and payload.get("evidence_eligible") is True
        and payload.get("pit_eligible") is True
        and as_of_day is not None
        and payload_day is not None
        and payload_day <= as_of_day
        and not (payload.get("reason_codes") or [])
        and not (payload.get("degraded_reasons") or [])
    ):
        return {}

    rows: Any = payload.get("sector_membership")
    if not isinstance(rows, list):
        # Derived status artifacts point back at the snapshots they were built
        # from; follow the newest one at or before as_of for membership.
        candidates: list[tuple[str, Path]] = []
        for item in payload.get("source_paths") or []:
            source = Path(str(item))
            if not source.is_absolute():
                source = PROJECT_ROOT / source
            day = _parse_day(source.parent.name)
            if day is None or (as_of_day is not None and day > as_of_day):
                continue
            candidates.append((day.isoformat(), source))
        for _day, source in sorted(candidates, reverse=True):
            snapshot = _json_snapshot_payload(source, cache)
            if snapshot is None:
                continue
            snapshot_day = _parse_day(snapshot.get("target_date"))
            directory_day = _parse_day(source.parent.name)
            if not (
                snapshot.get("status") == "passed"
                and snapshot.get("evidence_eligible") is True
                and snapshot.get("pit_eligible") is True
                and snapshot_day is not None
                and directory_day is not None
                and snapshot_day == directory_day
                and as_of_day is not None
                and snapshot_day <= as_of_day
                and not (snapshot.get("reason_codes") or [])
                and not (snapshot.get("degraded_reasons") or [])
            ):
                continue
            rows = snapshot.get("sector_membership")
            if isinstance(rows, list):
                break
        else:
            return {}

    mapping: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        member = _symbol(row.get("symbol") or row.get("code"))
        sector = _first_known_text(row.get("sector"), row.get("industry"))
        if member and sector:
            mapping[member] = sector
    return mapping


def _derive_amount_ma20(
    daily_dir: Path,
    symbol: str,
    *,
    as_of: str,
    window: int = 20,
) -> tuple[float | None, str | None, int]:
    """Mean turnover over the last ``window`` canonical daily rows at or before as_of.

    Returns ``(amount, source_path, row_count)``. Any failure yields
    ``(None, None, 0)`` so the caller keeps the liquidity component missing
    rather than substituting an unproven figure. Rows dated after ``as_of`` are
    never read, so the derived value cannot contain look-ahead.
    """

    amount, source_path, row_count, _cutoff = _derive_amount_ma20_with_cutoff(
        daily_dir,
        symbol,
        as_of=as_of,
        window=window,
    )
    return amount, source_path, row_count


def _derive_amount_ma20_with_cutoff(
    daily_dir: Path,
    symbol: str,
    *,
    as_of: str,
    window: int = 20,
    source_bytes: bytes | None = None,
    daily_read: tuple[pd.DataFrame, Path | None, list[str], pd.DataFrame] | None = None,
) -> tuple[float | None, str | None, int, str | None]:
    """Return amount_ma20 plus the actual last daily date used."""

    if not symbol:
        return None, None, 0, None
    if daily_read is None:
        daily_read = _read_daily_as_of(
            Path(daily_dir),
            symbol,
            as_of=as_of,
            source_bytes=source_bytes,
        )
    frame, path, warnings, _original = daily_read
    if path is None or frame is None or frame.empty or "amount" not in frame.columns:
        return None, None, 0, None
    if any(
        marker in str(item)
        for item in warnings or []
        for marker in ("price_basis_missing", "price_basis_not_raw")
    ):
        # Non-raw history is not an accepted basis for paper execution evidence.
        return None, None, 0, None
    try:
        window_size = int(window)
    except (TypeError, ValueError, OverflowError):
        return None, None, 0, None
    if window_size <= 0 or len(frame) < window_size:
        return None, None, 0, None
    tail = frame.tail(window_size)
    amounts = pd.to_numeric(tail["amount"], errors="coerce")
    if len(amounts) != window_size or any(
        not math.isfinite(float(value)) or float(value) < 0
        for value in amounts
    ):
        return None, None, 0, None
    mean_amount = _finite(float(amounts.mean()))
    if mean_amount is None or mean_amount < 0:
        return None, None, 0, None
    cutoff = tail.iloc[-1]["_date"]
    cutoff_text = cutoff.date().isoformat() if not pd.isna(cutoff) else None
    return mean_amount, _source_text(path), window_size, cutoff_text


def _sector_statuses(
    path: Path | None,
    *,
    as_of: str,
    status_payload: Mapping[str, Any] | None = None,
    source_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    if path is None:
        return {}, None, None
    cache = source_cache if source_cache is not None else {}
    payload = status_payload or _json_snapshot_payload(path, cache)
    if payload is None:
        return {}, None, None
    status = str(payload.get("status") or "").strip().lower()
    reason_codes = [str(item) for item in payload.get("reason_codes") or []]
    degraded = [str(item) for item in payload.get("degraded_reasons") or []]
    warnings = [str(item) for item in payload.get("warnings") or []]
    synthetic_declared = bool(
        payload.get("synthetic_test_data") is True
        or str(payload.get("data_mode") or "").strip().lower() in {"synthetic", "test", "fixture"}
        or any("synthetic" in item.lower() for item in [*reason_codes, *degraded, *warnings])
    )
    if not (
        payload.get("ok") is True
        and status in {"passed", "ok"}
        and payload.get("evidence_eligible") is True
        and payload.get("pit_eligible") is True
        and not synthetic_declared
        and not reason_codes
        and not degraded
    ):
        return {}, None, str(path)
    cutoff = None
    for key in ("target_date", "data_cutoff", "latest_date", "generated_at"):
        day = _parse_day(payload.get(key))
        if day is not None:
            cutoff = day.isoformat()
            break
    if cutoff is None or _parse_day(cutoff) is None or _parse_day(as_of) is None or cutoff > as_of[:10]:
        return {}, cutoff, str(path)
    rows: list[Mapping[str, Any]] = []
    for key in ("sector_rankings", "sectors", "top_sectors", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    statuses: dict[str, dict[str, Any]] = {}
    for row in rows:
        sector = _first_known_text(
            row.get("sector"),
            row.get("sector_name"),
            row.get("name"),
        )
        if sector is None:
            continue
        statuses[sector] = {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key in {"status", "sector_status", "heat_score", "rank_drop_5d", "rank_change_5d"}
        }
        if not statuses[sector]:
            statuses.pop(sector, None)
    return statuses, cutoff, str(path)


def prepare_shadow_funded_ledger(state: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy a formal paper ledger and add only the two legacy adapters."""

    ledger = copy.deepcopy(dict(state or {}))
    rows = ledger.get("positions")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                row["price_basis"] = "raw"
                row["funded"] = True
    return ledger


def _with_certified_position_taxonomy(
    ledger: Mapping[str, Any],
    sector_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    enriched = copy.deepcopy(dict(ledger or {}))
    enrichment = (
        sector_evidence.get("funded_position_sector_enrichment")
        if isinstance(sector_evidence, Mapping)
        else None
    )
    if not isinstance(enrichment, Mapping):
        return enriched
    for position in enriched.get("positions") or []:
        if not isinstance(position, dict):
            continue
        symbol = _symbol(position.get("symbol") or position.get("code"))
        sector = _first_known_text(enrichment.get(symbol))
        if not symbol or sector is None:
            continue
        position["sector"] = sector
        position["industry"] = sector
        position["sector_evidence_source"] = "certified_pit_membership"
    return enriched


def collect_local_risk_evidence(
    *,
    funded_ledger: Mapping[str, Any],
    candidate: Mapping[str, Any],
    side: str,
    reference_price: float,
    as_of: str,
    daily_dir: Path,
    candidate_source_path: str | Path,
    ledger_source_path: str | Path,
    sector_rotation_path: Path | None = None,
    price_source_path: str | Path | None = None,
    price_data_cutoff: str | None = None,
    source_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Assemble the five gate components exclusively from local evidence."""

    evidence: dict[str, Any] = {}
    missing: dict[str, list[str]] = {}
    symbol = _symbol(candidate.get("symbol") or candidate.get("code"))
    cutoff = _candidate_cutoff(candidate, as_of=as_of)
    price = _finite(reference_price, positive=True)
    cache = source_cache if source_cache is not None else {}
    candidate_snapshot = _source_snapshot(candidate_source_path, cache)
    candidate_rows = _matching_source_rows(
        candidate_snapshot,
        symbol=symbol,
        target_date=cutoff or "",
        as_of=as_of,
        business_candidate=candidate,
    )
    candidate_binding = (
        _file_binding("candidate", candidate_source_path, candidate_snapshot)
        if candidate_snapshot is not None and candidate_rows
        else None
    )
    ledger_binding = _ledger_binding(
        ledger_source_path,
        funded_ledger,
        cache,
    )
    candidate_payload_hash = _payload_sha256(candidate)
    ledger_payload_hash = _payload_sha256(funded_ledger)
    selected_price_source = (
        price_source_path if price_source_path is not None else candidate_source_path
    )
    price_source = _source_text(selected_price_source)
    if price_data_cutoff is None:
        price_cutoff = cutoff
    else:
        declared_price_day = _parse_day(price_data_cutoff)
        as_of_day = _parse_day(as_of)
        price_cutoff = (
            declared_price_day.isoformat()
            if declared_price_day is not None
            and as_of_day is not None
            and declared_price_day <= as_of_day
            else None
        )
    price_snapshot = _source_snapshot(selected_price_source, cache)
    price_rows = _matching_source_rows(
        price_snapshot,
        symbol=symbol,
        target_date=price_cutoff or "",
        as_of=as_of,
    )
    price_binding = (
        _file_binding("price", selected_price_source, price_snapshot)
        if price_snapshot is not None
        and price is not None
        and _price_rows_match(price_rows, price)
        else None
    )
    reference_price_payload = {
        "symbol": symbol,
        "as_of": str(as_of)[:10],
        "data_cutoff": price_cutoff,
        "value": price,
    }
    if (
        symbol
        and price_cutoff
        and price is not None
        and price_source
        and candidate_binding is not None
        and price_binding is not None
    ):
        evidence["price"] = _seal(
            {
                "symbol": symbol,
                "value": price,
                "price_basis": "raw",
                "consumer": "paper_execution",
                "data_cutoff": price_cutoff,
                "source_path": price_source,
                "source_bindings": [candidate_binding, price_binding],
                "payload_sha256": {
                    "candidate": candidate_payload_hash,
                    "reference_price": _payload_sha256(reference_price_payload),
                },
            }
        )
    else:
        missing["price"] = ["price_local_evidence_missing"]

    positions = [dict(row) for row in funded_ledger.get("positions") or [] if isinstance(row, Mapping)]
    target_sector = _first_known_text(candidate.get("sector"), candidate.get("industry"))
    # Membership and status must be derived from one immutable read of the
    # status artifact; separate reads allow a concurrent replacement to mix
    # incompatible PIT taxonomies inside one decision.
    sector_status_snapshot = _source_snapshot(sector_rotation_path, cache)
    sector_status_payload = _json_snapshot_payload(sector_rotation_path, cache)
    # symbol -> archived sector name, used only to look up sector status.
    pit_membership = _sector_membership(
        sector_rotation_path,
        as_of=as_of,
        status_payload=sector_status_payload,
        source_cache=cache,
    )
    pit_binding = (
        _file_binding("pit_sector_status", sector_rotation_path, sector_status_snapshot)
        if sector_rotation_path is not None
        and sector_status_snapshot is not None
        and pit_membership
        else None
    )
    ledger_labels_by_symbol: dict[str, set[str]] = {}
    position_symbols: set[str] = set()
    for position in positions:
        position_symbol = _symbol(position.get("symbol") or position.get("code"))
        if not position_symbol:
            continue
        position_symbols.add(position_symbol)
        sector = _first_known_text(position.get("sector"), position.get("industry"))
        if sector:
            ledger_labels_by_symbol.setdefault(position_symbol, set()).add(sector)

    sector_by_symbol: dict[str, str] = {}
    funded_position_sector_enrichment: dict[str, str] = {}
    enrichment_sources: dict[str, str] = {}
    pit_enrichment_symbols: set[str] = set()
    for position_symbol in sorted(position_symbols):
        pit_sector = _first_known_text(pit_membership.get(position_symbol))
        labels = ledger_labels_by_symbol.get(position_symbol, set())
        if pit_sector is not None:
            sector_by_symbol[position_symbol] = pit_sector
            if labels != {pit_sector}:
                funded_position_sector_enrichment[position_symbol] = pit_sector
                enrichment_sources[position_symbol] = "pit_membership"
                pit_enrichment_symbols.add(position_symbol)
            continue
        if len(labels) == 1:
            sector_by_symbol[position_symbol] = next(iter(labels))

    # The formal candidate's taxonomy is authoritative for the target symbol;
    # use it when that symbol is already funded, while keeping PIT as the
    # source for every other legacy position.
    if symbol and target_sector:
        if symbol in position_symbols and sector_by_symbol.get(symbol) != target_sector:
            funded_position_sector_enrichment[symbol] = target_sector
            enrichment_sources[symbol] = "formal_candidate"
            pit_enrichment_symbols.discard(symbol)
        sector_by_symbol[symbol] = target_sector
    required_assets = sorted({symbol, *(_symbol(row.get("symbol") or row.get("code")) for row in positions)} - {""})
    if (
        symbol
        and cutoff
        and target_sector
        and candidate_binding is not None
        and ledger_binding is not None
        and (not pit_enrichment_symbols or pit_binding is not None)
        and all(asset in sector_by_symbol for asset in required_assets)
    ):
        sector_bindings = [candidate_binding, ledger_binding]
        if funded_position_sector_enrichment and pit_binding is not None:
            sector_bindings.append(pit_binding)
        evidence["sector"] = _seal(
            {
                "symbol": symbol,
                "value": target_sector,
                "by_symbol": sector_by_symbol,
                "funded_position_sector_enrichment": funded_position_sector_enrichment,
                "funded_position_sector_enrichment_sources": enrichment_sources,
                "data_cutoff": cutoff,
                "source_path": _source_text(
                    candidate_source_path,
                    ledger_source_path,
                    sector_rotation_path if pit_enrichment_symbols else None,
                ),
                "source_bindings": sector_bindings,
                "payload_sha256": {
                    "candidate": candidate_payload_hash,
                    "ledger": ledger_payload_hash,
                    "pit_position_taxonomy": _payload_sha256(
                        funded_position_sector_enrichment
                    ),
                },
            }
        )
    else:
        missing["sector"] = ["sector_asset_local_evidence_missing"]

    # Candidate amount_ma20 is only a declaration. Liquidity evidence is always
    # recomputed from the canonical raw daily tail so a stale or forged candidate
    # value cannot bypass the local data-quality gate.
    target_daily_path = _daily_path(Path(daily_dir), symbol)
    target_daily_snapshot = _source_snapshot(target_daily_path, cache)
    target_daily_read = _cached_daily_as_of(
        target_daily_snapshot,
        Path(daily_dir),
        symbol,
        as_of=as_of,
    )
    amount, derived_source, derived_rows, liquidity_cutoff = _derive_amount_ma20_with_cutoff(
        Path(daily_dir),
        symbol,
        as_of=as_of,
        daily_read=target_daily_read,
    )
    daily_binding = (
        _file_binding("daily", derived_source, target_daily_snapshot)
        if derived_source and target_daily_snapshot is not None
        else None
    )
    amount_source = _source_text(candidate_source_path, derived_source)
    amount_derivation = (
        "min(100, mean(amount over last "
        f"{derived_rows} canonical raw daily rows ending {liquidity_cutoff}) / 1e6)"
    )
    if (
        symbol
        and cutoff
        and liquidity_cutoff
        and amount is not None
        and amount >= 0
        and candidate_binding is not None
        and daily_binding is not None
    ):
        liquidity_score = min(100.0, amount / 1_000_000.0)
        evidence["liquidity"] = _seal(
            {
                "symbol": symbol,
                "amount": amount,
                "liquidity_score": liquidity_score,
                "derivation": amount_derivation,
                "data_cutoff": liquidity_cutoff,
                "source_path": amount_source,
                "source_bindings": [candidate_binding, daily_binding],
                "payload_sha256": {"candidate": candidate_payload_hash},
            }
        )
    else:
        missing["liquidity"] = ["liquidity_local_evidence_missing"]

    frames: dict[str, pd.DataFrame] = {}
    original_frames: dict[str, pd.DataFrame] = {}
    daily_paths: dict[str, Path] = {}
    daily_warnings: dict[str, list[str]] = {}
    for asset in required_assets:
        daily_path = _daily_path(Path(daily_dir), asset)
        daily_snapshot = _source_snapshot(daily_path, cache)
        frame, path, warnings, original = _cached_daily_as_of(
            daily_snapshot,
            Path(daily_dir),
            asset,
            as_of=as_of,
        )
        frames[asset] = frame
        original_frames[asset] = original
        if path is not None:
            daily_paths[asset] = path
        daily_warnings[asset] = warnings
    frame_set_validation = validate_price_frame_set(
        {asset: original_frames[asset] for asset in required_assets},
        consumer="paper_execution",
    )
    raw_daily_ok = bool(required_assets) and bool(
        frame_set_validation.get("eligible")
        and frame_set_validation.get("accepted_price_basis") == PRICE_BASIS_RAW
    ) and all(
        not frames[asset].empty
        and "daily_price_basis_missing" not in daily_warnings[asset]
        and "daily_price_basis_not_raw" not in daily_warnings[asset]
        for asset in required_assets
    )
    return_series: list[pd.Series] = []
    if raw_daily_ok:
        for asset in required_assets:
            frame = frames[asset]
            if "close" not in frame.columns:
                return_series = []
                break
            close = pd.to_numeric(frame["close"], errors="coerce")
            series = pd.Series(close.to_numpy(), index=pd.DatetimeIndex(frame["_date"]), name=asset)
            return_series.append(series.pct_change(fill_method=None).dropna())
    returns_frame = pd.concat(return_series, axis=1, join="inner").dropna() if return_series else pd.DataFrame()
    if (
        len(returns_frame) >= 100
        and list(returns_frame.columns) == required_assets
        and not returns_frame.index.has_duplicates
        and returns_frame.index.is_monotonic_increasing
        and all(math.isfinite(float(value)) for value in returns_frame.to_numpy().ravel())
    ):
        evidence["returns"] = _seal(
            {
                "frame": returns_frame,
                "data_cutoff": returns_frame.index.max().date().isoformat(),
                "source_path": _source_text(*(daily_paths.get(asset) for asset in required_assets)),
            }
        )
    else:
        reasons = ["returns_local_evidence_missing"]
        if any("daily_price_basis_missing" in daily_warnings[asset] for asset in required_assets):
            reasons.append("returns_price_basis_missing")
        if any("daily_price_basis_not_raw" in daily_warnings[asset] for asset in required_assets):
            reasons.append("returns_price_basis_not_raw")
        missing["returns"] = list(dict.fromkeys(reasons))

    statuses, sector_cutoff, sector_source = _sector_statuses(
        sector_rotation_path,
        as_of=as_of,
        status_payload=sector_status_payload,
        source_cache=cache,
    )
    daily_rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    status_by_symbol: dict[str, dict[str, Any]] = {}
    smart_ok = raw_daily_ok
    for position in positions:
        asset = _symbol(position.get("symbol") or position.get("code"))
        # Smart-stop status and membership are one PIT evidence contract. A
        # ledger label may describe the funded position, but it cannot replace
        # a missing archived membership row or certify the status lookup key.
        sector = pit_membership.get(asset)
        rows = _daily_rows(frames.get(asset, pd.DataFrame()), 30)
        status = statuses.get(sector or "")
        if not asset or not rows or not isinstance(status, Mapping) or not status:
            smart_ok = False
            continue
        daily_rows_by_symbol[asset] = rows
        status_by_symbol[asset] = dict(status)
    funded_symbols = {
        _symbol(position.get("symbol") or position.get("code"))
        for position in positions
    } - {""}
    if smart_ok and set(daily_rows_by_symbol) == funded_symbols and sector_cutoff:
        smart_cutoffs = [sector_cutoff]
        smart_cutoffs.extend(rows[-1]["date"] for rows in daily_rows_by_symbol.values() if rows)
        evidence["smart_stop"] = _seal(
            {
                "daily_rows_by_symbol": daily_rows_by_symbol,
                "sector_status_by_symbol": status_by_symbol,
                "data_cutoff": max(smart_cutoffs),
                "source_path": _source_text(
                    *(daily_paths.get(_symbol(row.get("symbol") or row.get("code"))) for row in positions),
                    sector_source,
                ),
            }
        )
    else:
        missing["smart_stop"] = ["smart_stop_local_evidence_missing"]

    return evidence, missing


def evaluate_shadow_risk_order(
    *,
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    side: str,
    reference_price: float,
    requested_quantity: int,
    as_of: str,
    daily_dir: Path,
    candidate_source_path: str | Path,
    ledger_source_path: str | Path,
    sector_rotation_path: Path | None = None,
    price_source_path: str | Path | None = None,
    price_data_cutoff: str | None = None,
    pre_trade_config: PreTradeGateConfig | None = None,
    risk_gate_enforced: bool = False,
    surface: str = "paper_buy_before_commit",
) -> dict[str, Any]:
    """Evaluate one order in shadow mode without mutating its source ledger."""

    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("shadow_side_invalid")
    ledger = prepare_shadow_funded_ledger(state)
    profile = resolve_paper_capital_profile(
        existing_state=ledger,
        existing_state_verified=True,
    )
    symbol = _symbol(candidate.get("symbol") or candidate.get("code"))
    if normalized_side == "buy":
        cutoff = _candidate_cutoff(candidate, as_of=as_of) or as_of
        event = CandidateDecisionEvent(
            event_id=f"risk-shadow-{_canonical_hex({'symbol': symbol, 'as_of': as_of, 'surface': surface})[:24]}",
            symbol=symbol,
            occurred_at=cutoff,
            requested_state="paper_watchlist",
            decision_origin="system",
            evidence=_candidate_lifecycle_evidence(
                candidate, as_of=cutoff, daily_dir=daily_dir
            ),
            payload={
                "name": str(candidate.get("name") or ""),
                "strategy_type": str(candidate.get("strategy_type") or ""),
                "candidate_source": str(candidate.get("candidate_source") or ""),
                "data_latest_date": str(candidate.get("data_latest_date") or ""),
            },
        ).to_dict()
    evidence, missing = collect_local_risk_evidence(
        funded_ledger=ledger,
        candidate=candidate,
        side=normalized_side,
        reference_price=reference_price,
        as_of=as_of,
        daily_dir=daily_dir,
        candidate_source_path=candidate_source_path,
        ledger_source_path=ledger_source_path,
        sector_rotation_path=sector_rotation_path,
        price_source_path=price_source_path,
        price_data_cutoff=price_data_cutoff,
    )
    gate_ledger = _with_certified_position_taxonomy(
        ledger,
        evidence.get("sector") if isinstance(evidence.get("sector"), Mapping) else None,
    )
    snapshot = build_portfolio_snapshot(
        gate_ledger,
        profile,
        as_of=as_of,
        valuation_price_basis="raw",
    )
    if normalized_side == "buy":
        intent = build_paper_order_intent(
            event,
            snapshot,
            reference_price=reference_price,
            reference_price_basis="raw",
            requested_quantity=requested_quantity,
        )
    else:
        intent = build_paper_exit_intent(
            gate_ledger,
            snapshot,
            symbol=symbol,
            reference_price=reference_price,
            reference_price_basis="raw",
            requested_quantity=requested_quantity,
        )
    decision = evaluate_paper_risk_gate(
        intent,
        snapshot,
        profile=profile,
        funded_ledger=gate_ledger,
        evidence=evidence,
        as_of=as_of,
        pre_trade_config=pre_trade_config,
        llm_review=None,
    )
    assembly_reasons = [reason for reasons in missing.values() for reason in reasons]
    reason_codes = list(
        dict.fromkeys([*decision.reason_codes, *intent.reason_codes, *assembly_reasons])
    )
    reduced_scope_sell = normalized_side == "sell"
    sector_component = evidence.get("sector")
    sector_bindings = (
        sector_component.get("source_bindings")
        if isinstance(sector_component, Mapping)
        else []
    )
    ledger_source_binding = next(
        (
            copy.deepcopy(dict(binding))
            for binding in sector_bindings or []
            if isinstance(binding, Mapping) and binding.get("role") == "ledger"
        ),
        None,
    )
    # Main task-book sections 7.3/12 stop point 3b: this value is recorded only.
    # It must not alter, veto, skip, or submit any legacy paper action.
    record = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "risk_gate_shadow_evaluation",
        "surface": surface,
        "evaluated": True,
        "as_of": as_of,
        "symbol": symbol,
        "side": normalized_side,
        "status": decision.status,
        "would_pass": decision.status == "passed",
        "would_block": decision.status == "vetoed",
        "insufficient_evidence": decision.status == "insufficient_evidence",
        "reason_codes": reason_codes,
        "intent_digest": intent.intent_digest,
        "decision_digest": decision.decision_digest,
        "snapshot_digest": snapshot.snapshot_digest,
        "component_source_hashes": {
            name: component.get("source_hash")
            for name, component in evidence.items()
            if isinstance(component, Mapping)
        },
        "ledger_source_binding": ledger_source_binding,
        "missing_components": sorted(missing),
        "missing_component_count": len(missing),
        "missing_component_reason_codes": copy.deepcopy(missing),
        "risk_gate_enforced": bool(risk_gate_enforced),
        "enforcement_applied": False,
        "reduced_scope_sell": reduced_scope_sell,
        "reduced_scope_sell_skipped_checks": (
            ["pre_trade", "concentration_veto", "var_veto"] if reduced_scope_sell else []
        ),
        "liquidity_veto_still_applicable": reduced_scope_sell,
        "reduced_scope_sell_note": (
            "Sell skips pre_trade, concentration veto, and var veto; liquidity_veto remains applicable."
            if reduced_scope_sell
            else ""
        ),
        "shadow_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }
    record["evaluation_id"] = _canonical_hex(record)
    return record


def build_legacy_blocked_shadow_record(
    *,
    candidate: Mapping[str, Any],
    as_of: str,
    reason: str,
    surface: str,
    risk_gate_enforced: bool = False,
) -> dict[str, Any]:
    """Record a legacy cash/lot block without evaluating the risk gate."""

    return {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "risk_gate_shadow_observation",
        "surface": surface,
        "evaluated": False,
        "as_of": as_of,
        "symbol": _symbol(candidate.get("symbol") or candidate.get("code")),
        "side": "buy",
        "status": "legacy_blocked",
        "would_pass": False,
        "would_block": False,
        "insufficient_evidence": False,
        "reason_codes": ["legacy_blocked"],
        "legacy_blocked_reason": str(reason or ""),
        "missing_components": [],
        "missing_component_count": 0,
        "risk_gate_enforced": bool(risk_gate_enforced),
        "enforcement_applied": False,
        "shadow_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }


def summarize_shadow_records(
    records: list[Mapping[str, Any]],
    *,
    as_of: str,
    risk_gate_enforced: bool = False,
) -> dict[str, Any]:
    """Build the standalone daily shadow artifact."""

    items = [copy.deepcopy(dict(record)) for record in records if isinstance(record, Mapping)]
    evaluated = [item for item in items if item.get("evaluated", item.get("status") not in {"legacy_blocked", "shadow_error"})]
    reason_counts = Counter(
        str(reason)
        for item in items
        for reason in item.get("reason_codes") or []
        if str(reason)
    )
    missing_counts = Counter(
        str(component)
        for item in evaluated
        for component in item.get("missing_components") or []
        if str(component)
    )
    insufficient_count = sum(bool(item.get("insufficient_evidence")) for item in evaluated)
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "risk_gate_shadow_daily_summary",
        "status": "shadow_observation",
        "generated_at": _now_text(),
        "as_of": as_of,
        "observed_count": len(items),
        "evaluation_count": len(evaluated),
        "would_pass_count": sum(bool(item.get("would_pass")) for item in evaluated),
        "would_block_count": sum(bool(item.get("would_block")) for item in evaluated),
        "insufficient_evidence_count": insufficient_count,
        "legacy_blocked_count": sum(item.get("status") == "legacy_blocked" for item in items),
        "shadow_error_count": sum(item.get("status") == "shadow_error" for item in items),
        "insufficient_evidence_ratio": (
            insufficient_count / len(evaluated) if evaluated else 0.0
        ),
        "reason_counts": dict(reason_counts),
        "top_reason_codes": [
            {"reason_code": reason, "count": count}
            for reason, count in reason_counts.most_common(10)
        ],
        "missing_component_counts": dict(missing_counts),
        "items": items,
        "risk_gate_enforced": bool(risk_gate_enforced),
        "enforcement_applied": False,
        "shadow_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": "Research Only; Paper Account Only; No Broker Route; Not Investment Advice.",
    }
    return payload


def write_shadow_summary(
    payload: Mapping[str, Any],
    *,
    output_path: Path | None = None,
    archive_dir: Path | None = None,
) -> Path:
    """Atomically write the latest summary and its ``as_of`` daily archive.

    All four production callers already isolate exceptions around this single
    sink.  Deriving the archive from an explicit ``output_path`` keeps every
    fixture/custom root isolated without changing those call sites.
    """

    target = Path(output_path or RISK_GATE_SHADOW_LATEST_JSON).resolve(strict=False)
    daily_dir = _co_located_shadow_archive_dir(target, archive_dir)
    snapshot = copy.deepcopy(dict(payload))
    lock_path = daily_dir / RISK_GATE_SHADOW_LOCK_NAME
    with interprocess_file_lock(lock_path):
        archive_target, should_write = _plan_shadow_archive_write(
            snapshot, archive_dir=daily_dir
        )
        if should_write:
            # Archive first: an archive failure leaves legacy latest untouched.
            # Seal before latest so a partial write cannot become accepted
            # evidence. The lock prevents same-day writers from interleaving.
            atomic_write_json(archive_target, snapshot)
            _write_shadow_archive_seal(archive_target, snapshot)
            atomic_write_json(target, snapshot)
            return target

        archived = _read_shadow_archive_payload(archive_target)
        equivalent_retry = _archive_payload_equivalent(archived, snapshot)
        _ensure_shadow_archive_seal(
            archive_target,
            archived,
            repair_mismatch=equivalent_retry,
        )
        desired_latest = copy.deepcopy(archived)
        desired_latest.pop("archive_source", None)
        current_latest = (
            _read_shadow_archive_payload(target) if target.is_file() else None
        )
        needs_repair = current_latest != desired_latest
        if (
            not needs_repair
            and "archive_source" not in archived
            and target.read_bytes() != archive_target.read_bytes()
        ):
            needs_repair = True
        if needs_repair:
            atomic_write_json(target, desired_latest)
    return target


def backfill_latest_shadow_archive(
    *,
    latest_path: Path | None = None,
    archive_dir: Path | None = None,
) -> Path:
    """Copy the one accepted pre-archive latest payload into its dated bucket.

    This is the explicit Round 11 one-time provenance path.  It never rewrites
    ``latest`` and never fabricates a date; the payload's existing ``as_of`` is
    retained and marked ``archive_source=latest_backfill``.
    """

    latest = Path(latest_path or RISK_GATE_SHADOW_LATEST_JSON).resolve(strict=False)
    daily_dir = _co_located_shadow_archive_dir(latest, archive_dir)
    lock_path = daily_dir / RISK_GATE_SHADOW_LOCK_NAME
    with interprocess_file_lock(lock_path):
        payload = _read_shadow_archive_payload(latest)
        as_of, as_of_day, _ = _validate_daily_summary(payload)
        target = daily_dir / f"{as_of}.json"
        backfilled = copy.deepcopy(payload)
        backfilled["archive_source"] = "latest_backfill"
        if target.is_file():
            existing = _read_shadow_archive_payload(target)
            if existing == backfilled or _archive_payload_equivalent(existing, payload):
                _ensure_shadow_archive_seal(
                    target,
                    existing,
                    repair_mismatch=True,
                )
                return target
            raise ValueError(f"refusing to replace existing shadow archive: {target}")
        days = _archive_days(daily_dir)
        if days and as_of_day < days[-1]:
            raise ValueError(
                f"historical shadow archive is immutable: {as_of} < {days[-1].isoformat()}"
            )
        atomic_write_json(target, backfilled)
        _write_shadow_archive_seal(target, backfilled)
        return target


def record_shadow_error_best_effort(
    *,
    surface: str,
    error: BaseException,
    as_of: str = "",
    error_path: Path | None = None,
) -> bool:
    """Best-effort error journal; all failures are deliberately swallowed."""

    try:
        target = Path(error_path or RISK_GATE_SHADOW_ERRORS_JSONL)
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at": _now_text(),
            "as_of": as_of,
            "surface": str(surface or "unknown_shadow_surface"),
            "status": "shadow_error",
            "error_type": type(error).__name__,
            "error": str(error),
            "shadow_only": True,
            "paper_only": True,
            "no_broker": True,
        }
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def render_shadow_report_section(payload: Mapping[str, Any]) -> list[str]:
    """Render the independent summary without recalculating any decision."""

    top = payload.get("top_reason_codes") if isinstance(payload.get("top_reason_codes"), list) else []
    top_text = ", ".join(
        f"{row.get('reason_code')}={row.get('count')}"
        for row in top
        if isinstance(row, Mapping)
    ) or "none"
    return [
        "",
        SHADOW_REPORT_HEADING,
        "",
        "Research Only · Paper Account Only · No Broker Route · Live Execution Locked · Not Investment Advice · Not Position Advice",
        "",
        "本节仅记录风险门若生效时的判定；当前不拦截、不修改、不跳过任何 legacy 纸面动作。",
        f"- 评估笔数：{payload.get('evaluation_count', 0)}",
        f"- would_pass：{payload.get('would_pass_count', 0)}",
        f"- would_block（仅 vetoed）：{payload.get('would_block_count', 0)}",
        f"- insufficient_evidence：{payload.get('insufficient_evidence_count', 0)}",
        f"- legacy_blocked（未过门）：{payload.get('legacy_blocked_count', 0)}",
        f"- insufficient_evidence 占比：{payload.get('insufficient_evidence_ratio', 0)}",
        f"- top reason_codes：{top_text}",
        f"- risk_gate_enforced（仅记录，无行为效应）：{str(bool(payload.get('risk_gate_enforced'))).lower()}",
        "",
    ]


__all__ = [
    "RISK_GATE_SHADOW_DAILY_DIR",
    "RISK_GATE_SHADOW_LOCK_NAME",
    "RISK_GATE_SHADOW_SEAL_SCHEMA",
    "RISK_GATE_SHADOW_SEAL_SUFFIX",
    "RISK_GATE_SHADOW_ERRORS_JSONL",
    "RISK_GATE_SHADOW_LATEST_JSON",
    "SHADOW_REPORT_HEADING",
    "backfill_latest_shadow_archive",
    "build_legacy_blocked_shadow_record",
    "collect_local_risk_evidence",
    "evaluate_shadow_risk_order",
    "prepare_shadow_funded_ledger",
    "record_shadow_error_best_effort",
    "render_shadow_report_section",
    "shadow_archive_content_sha256",
    "shadow_archive_seal_path",
    "shadow_archive_seal_rejection_reason",
    "summarize_shadow_records",
    "verify_shadow_archive_seal",
    "write_shadow_summary",
]
