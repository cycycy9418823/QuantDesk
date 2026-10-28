"""Explicit minute observations and immutable, as-of-aware revisions.

Clock time alone never proves a provider's bar is final. Legacy rows remain
unknown. This contract describes evidence, not a strategy or an order route.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
LIFECYCLE_DIR = "lifecycle_v1"
FORMAL_EVIDENCE_STANDARD = "finalized_session_evidence_v3"
LIFECYCLE_COLUMNS = (
    "bar_start", "bar_end", "is_final", "finality", "revision", "provider",
    "source_quote_time", "fetched_at", "aggregation_method", "session_id",
    "timestamp_convention", "volume_unit", "amount_unit", "volume_semantics",
    "quality_status", "revision_reason", "supersedes_hash", "evidence_hash",
)
_CONTENT_FIELDS = (
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
    "price_basis", "bar_start", "bar_end", "is_final", "finality", "provider",
    "source_quote_time", "aggregation_method", "session_id", "timestamp_convention",
    "volume_unit", "amount_unit", "volume_semantics",
)
_IDENTITY_FIELDS = (
    "provider", "bar_start", "bar_end", "timestamp_convention", "price_basis",
    "volume_unit", "amount_unit", "volume_semantics",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "<na>", "nat"} else text


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None
                else parsed.astimezone(SHANGHAI))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def _bool(value: Any) -> bool | None:
    if value is True or _text(value).lower() in {"true", "1", "1.0"}:
        return True
    if value is False or _text(value).lower() in {"false", "0", "0.0"}:
        return False
    if not _text(value):
        return None
    raise ValueError("invalid_is_final")


def _revision(value: Any) -> int:
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError("minute_event_revision_invalid") from None
    if isinstance(value, bool) or not math.isfinite(number) or number < 1 or not number.is_integer():
        raise ValueError("minute_event_revision_invalid")
    return int(number)


def normalize_bar_observation(
    row: Mapping[str, Any], *, fetched_at: str, provider: str = "",
    convention: str = "",
) -> dict[str, Any]:
    """Preserve unknown metadata; derive bounds only from an explicit profile."""
    result = dict(row)
    result["provider"] = _text(row.get("provider")) or provider or _text(row.get("source"))
    result["fetched_at"] = fetched_at
    result["timestamp_convention"] = _text(row.get("timestamp_convention")) or convention or "unknown"
    stamp = _stamp(row.get("datetime"))
    start, end = _stamp(row.get("bar_start")), _stamp(row.get("bar_end"))
    if stamp and not start and not end:
        if result["timestamp_convention"] == "minute_start":
            start, end = stamp, stamp + timedelta(minutes=1)
        elif result["timestamp_convention"] == "minute_end":
            start, end = stamp - timedelta(minutes=1), stamp
    flag = _bool(row.get("is_final"))
    finality = _text(row.get("finality")) or ("final" if flag is True else "provisional" if flag is False else "unknown")
    if finality not in {"unknown", "provisional", "final", "revised"}:
        raise ValueError("invalid_finality")
    final = finality in {"final", "revised"}
    if flag is not None and flag != final:
        raise ValueError("conflicting_finality")
    result.update(bar_start=_iso(start), bar_end=_iso(end), finality=finality,
                  is_final=final, session_id=_text(row.get("session_id")) or (stamp.date().isoformat() if stamp else ""))
    for key in ("volume_unit", "amount_unit", "volume_semantics", "aggregation_method"):
        result[key] = _text(row.get(key)) or "unknown"
    result["source_quote_time"] = _text(row.get("source_quote_time"))
    result["quality_status"] = bar_evidence_status(result)["quality_status"]
    return result


def bar_evidence_status(row: Mapping[str, Any]) -> dict[str, Any]:
    """Data eligibility only; it never proves suspension/limits/T+1 tradability."""
    reasons: list[str] = []
    numeric: dict[str, float] = {}
    for name in ("open", "high", "low", "close", "volume"):
        try:
            numeric[name] = float(row.get(name))
        except (TypeError, ValueError):
            numeric[name] = float("nan")
    if not all(math.isfinite(v) for v in numeric.values()):
        reasons.append("invalid_ohlcv")
    elif (min(numeric[n] for n in ("open", "high", "low", "close")) <= 0
          or numeric["volume"] < 0):
        reasons.append("invalid_ohlcv")
    else:
        from quantdesk.intraday.minute_storage import envelope_violation
        if envelope_violation(*(numeric[n] for n in ("open", "high", "low", "close"))):
            reasons.append("invalid_ohlcv")
    fields_valid = not reasons
    start, end = _stamp(row.get("bar_start")), _stamp(row.get("bar_end"))
    label, observed = _stamp(row.get("datetime")), _stamp(row.get("fetched_at"))
    convention = _text(row.get("timestamp_convention"))
    bounds_valid = bool(start and end and end - start == timedelta(minutes=1)
                        and start.second == 0 and start.microsecond == 0)
    if not bounds_valid:
        reasons.append("bar_bounds_unknown_or_invalid")
    elif (start.date() != end.date() or not ((9, 30) <= (start.hour, start.minute) < (11, 30)
                                           or (13, 0) <= (start.hour, start.minute) < (15, 0))):
        reasons.append("outside_continuous_session")
    if convention not in {"minute_start", "minute_end"}:
        reasons.append("timestamp_convention_unknown")
    elif label is None or (convention == "minute_start" and label != start) or (convention == "minute_end" and label != end):
        reasons.append("bar_label_bounds_mismatch")
    if not label or _text(row.get("session_id")) != label.date().isoformat():
        reasons.append("session_id_mismatch")
    if not observed:
        reasons.append("fetched_at_unknown")
    if end and observed and end > observed:
        reasons.append("bar_not_closed")
    try:
        final = _bool(row.get("is_final")) is True and _text(row.get("finality")) in {"final", "revised"}
    except ValueError:
        final = False
    if not final:
        reasons.append("bar_finality_unverified")
    quote_time = _stamp(row.get("source_quote_time"))
    if quote_time is None:
        reasons.append("source_quote_time_unknown")
    elif (end and quote_time < end) or (observed and quote_time > observed):
        reasons.append("source_quote_time_inconsistent")
    if _text(row.get("price_basis")) != "raw":
        reasons.append("price_basis_not_raw")
    if _text(row.get("volume_unit")) not in {"shares", "hands"}:
        reasons.append("volume_unit_unknown")
    elif _text(row.get("volume_unit")) != "shares":
        reasons.append("volume_unit_not_normalized")
    if _text(row.get("amount")) and _text(row.get("amount_unit")) != "CNY":
        reasons.append("amount_unit_unknown_or_not_normalized")
    if _text(row.get("volume_semantics")) != "per_minute":
        reasons.append("volume_semantics_unknown")
    if not _text(row.get("provider")) or _text(row.get("aggregation_method")) in {"", "unknown"}:
        reasons.append("provider_provenance_unknown")
    if numeric.get("volume") == 0:
        reasons.append("zero_volume_no_trade_evidence")
    status = ("signal_eligible" if not reasons else "provisional" if _text(row.get("finality")) == "provisional"
              else "field_complete" if fields_valid else "observed")
    return {"fields_valid": fields_valid, "signal_eligible": not reasons,
            "execution_blocked": bool(reasons), "quality_status": status,
            "reason_codes": reasons, "is_final": final}


def frame_bar_evidence(frame: Any) -> dict[str, Any]:
    """Legacy geometry remains a separate diagnostic, never a finality claim."""
    present = frame is not None and not frame.empty and any(key in frame.columns for key in LIFECYCLE_COLUMNS)
    if not present:
        return {"lifecycle_present": False, "finality": "unverified", "signal_eligible": False,
                "execution_blocked": True, "final_bar_count": 0,
                "reason_codes": ["legacy_bar_lifecycle_unverified"]}
    records = frame.to_dict("records")
    statuses = [bar_evidence_status(row) for row in records]
    reasons = sorted({reason for item in statuses for reason in item["reason_codes"]})
    return {"lifecycle_present": present, "finality": "final" if statuses and all(s["is_final"] for s in statuses) else "unverified",
            "signal_eligible": bool(statuses) and all(s["signal_eligible"] for s in statuses),
            "execution_blocked": not statuses or any(s["execution_blocked"] for s in statuses),
            "final_bar_count": sum(s["is_final"] for s in statuses), "reason_codes": reasons}


def _content_hash(row: Mapping[str, Any]) -> str:
    content: dict[str, Any] = {key: _text(row.get(key)) for key in _CONTENT_FIELDS}
    for key in ("open", "high", "low", "close", "volume", "amount"):
        try:
            value = float(row.get(key))
            content[key] = value if math.isfinite(value) else None
        except (TypeError, ValueError):
            content[key] = None
    content["is_final"] = _bool(row.get("is_final")) is True
    if content["finality"] == "revised":
        content["finality"] = "final"
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def observation_hash(row: Mapping[str, Any]) -> str:
    """Bind observation time and parent chain as well as market values."""
    proof = {key: _text(row.get(key)) for key in ("fetched_at", "supersedes_hash", "revision_reason")}
    proof["revision"] = _revision(row.get("revision"))
    proof["content_hash"] = _content_hash(row)
    return hashlib.sha256(json.dumps(proof, sort_keys=True).encode("utf-8")).hexdigest()


def select_bar_observations(frame: Any, *, as_of: str = "") -> Any:
    """Validate revision chains before selecting the version visible at as_of."""
    import pandas as pd

    if "evidence_hash" not in frame.columns:
        if any(key in frame.columns for key in (
            "revision", "supersedes_hash", "revision_reason", "finality", "is_final",
            "bar_start", "bar_end", "fetched_at",
        )):
            raise ValueError("minute_event_integrity_missing")
        if as_of:
            raise ValueError("legacy_minute_as_of_unverified")
        return frame
    cutoff = _stamp(as_of) if as_of else None
    if as_of and cutoff is None:
        raise ValueError("invalid_as_of")
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    visible: dict[tuple[str, str], dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        if observation_hash(row) != _text(row.get("evidence_hash")):
            raise ValueError("minute_event_hash_mismatch")
        key = _text(row.get("symbol")), _text(row.get("datetime"))
        old = latest.get(key)
        observed = _stamp(row.get("fetched_at"))
        if observed is None or (old and observed < _stamp(old["fetched_at"])):
            raise ValueError("minute_event_time_invalid")
        try:
            revision = _revision(row.get("revision"))
        except (ValueError, TypeError):
            raise ValueError("minute_event_revision_invalid") from None
        if revision != (_revision(old["revision"]) + 1 if old else 1):
            raise ValueError("minute_event_revision_invalid")
        if _text(row.get("supersedes_hash")) != (_text(old["evidence_hash"]) if old else ""):
            raise ValueError("minute_event_parent_invalid")
        if _text(row.get("finality")) == "revised" and old is None:
            raise ValueError("minute_event_revision_requires_parent")
        if old and any(_text(old.get(key)) != _text(row.get(key)) for key in _IDENTITY_FIELDS):
            raise ValueError("bar_identity_changed_requires_new_source_segment")
        if old and _bool(old.get("is_final")) is True:
            if _bool(row.get("is_final")) is not True or not _text(row.get("revision_reason")):
                raise ValueError("minute_event_final_revision_invalid")
        latest[key] = row
        if cutoff is None or observed <= cutoff:
            visible[key] = row
    result = pd.DataFrame(list(visible.values()), columns=frame.columns)
    result.attrs["lifecycle_materialized"] = True
    return result


def lifecycle_write_path(path: Path, *, history_now: datetime | None = None) -> Path:
    """Old archive bytes are immutable; use a separate new-schema segment."""
    from quantdesk.intraday.minute_storage import minute_write_path
    path = minute_write_path(path, history_now=history_now)
    if path.parent.name == LIFECYCLE_DIR:
        return path
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            columns = next(csv.reader(stream), [])
        if "evidence_hash" in columns:
            return path
        return path.parent / LIFECYCLE_DIR / path.name
    alternate = path.parent / LIFECYCLE_DIR / path.name
    return alternate if alternate.exists() else path


def read_bar_observation_log(path: Path) -> Any:
    """Read original decimal text and validate the complete recorded chain."""
    import pandas as pd

    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "evidence_hash" not in (reader.fieldnames or []):
            raise ValueError("minute_event_integrity_missing")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("minute_event_row_invalid")
        frame = pd.DataFrame(rows, columns=reader.fieldnames)
    select_bar_observations(frame)
    return frame


def merge_bar_observation_logs(existing: Any, incoming: Any) -> Any:
    """Preserve full event chains and the first receipt of unchanged evidence."""
    import pandas as pd

    select_bar_observations(incoming)
    if existing is None or existing.empty:
        existing = pd.DataFrame(columns=incoming.columns)
    else:
        select_bar_observations(existing)
    records = existing.to_dict("records")
    known_hashes = {_text(row.get("evidence_hash")) for row in records}
    latest = {(_text(row.get("symbol")), _text(row.get("datetime"))): row for row in records}
    for row in incoming.to_dict("records"):
        if observation_hash(row) != _text(row.get("evidence_hash")):
            raise ValueError("minute_event_hash_mismatch")
        if _text(row.get("evidence_hash")) in known_hashes:
            continue
        key = _text(row.get("symbol")), _text(row.get("datetime"))
        old = latest.get(key)
        if old and _content_hash(old) == _content_hash(row):
            continue
        records.append(row)
        known_hashes.add(_text(row.get("evidence_hash")))
        latest[key] = row
    result = pd.DataFrame(records, columns=incoming.columns if not records else None)
    select_bar_observations(result)
    result["revision"] = result["revision"].map(_revision)
    return result.sort_values(["datetime", "revision"], kind="stable").reset_index(drop=True)


def append_bar_observations(
    path: Path, rows: Sequence[Mapping[str, Any]], *, now_iso: str,
    base_columns: Sequence[str], atomic_append: Callable[..., None],
) -> tuple[int, list[str], str]:
    """Append revisions atomically; a final correction needs explicit parent proof."""
    observed_at = _stamp(now_iso)
    if observed_at is None:
        return 0, [f"minute_lifecycle_rejected:{path.stem}:observation_time_invalid"], ""
    path = lifecycle_write_path(path, history_now=observed_at)
    columns = list(base_columns) + list(LIFECYCLE_COLUMNS)
    scanned = path.stat() if path.is_file() else None
    latest: dict[str, dict[str, Any]] = {}
    if scanned and scanned.st_size:
        try:
            stored_rows = []
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != columns:
                    return 0, [f"minute_header_mismatch:{path.name}"], ""
                for row in reader:
                    if None in row or any(v is None for v in row.values()) or observation_hash(row) != row["evidence_hash"]:
                        raise ValueError("invalid_event")
                    stored_rows.append(row)
                    latest[row["datetime"]] = row
            import pandas as pd
            select_bar_observations(pd.DataFrame(stored_rows))
        except (ValueError, TypeError, csv.Error):
            return 0, [f"minute_stored_row_invalid:{path.name}:lifecycle_integrity"], ""
    stored_latest = max(latest, default="")
    accepted: list[dict[str, Any]] = []
    reasons: list[str] = []
    for raw in rows:
        try:
            row = normalize_bar_observation(raw, fetched_at=now_iso)
            stamp = _stamp(row.get("datetime"))
            if stamp is None or row.get("symbol") != path.stem or row.get("price_basis") != "raw":
                raise ValueError("identity_or_price_basis_invalid")
            if not bar_evidence_status(row)["fields_valid"]:
                raise ValueError("invalid_ohlcv")
            if row["is_final"]:
                evidence = bar_evidence_status(row)
                if any(code in evidence["reason_codes"] for code in (
                    "bar_not_closed", "bar_bounds_unknown_or_invalid", "fetched_at_unknown",
                    "bar_label_bounds_mismatch", "outside_continuous_session", "session_id_mismatch",
                )):
                    raise ValueError("invalid_final_bar_bounds")
            row["datetime"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
            row["date"], row["time"] = row["datetime"][:10], row["datetime"][11:]
            old = latest.get(row["datetime"])
            if row["finality"] == "revised" and old is None:
                raise ValueError("revision_requires_parent")
            if old and any(_text(old.get(key)) != _text(row.get(key)) for key in _IDENTITY_FIELDS):
                raise ValueError("bar_identity_changed_requires_new_source_segment")
            if old and observed_at < _stamp(old["fetched_at"]):
                raise ValueError("observation_time_reversed")
            if old and _content_hash(old) == _content_hash(row):
                reasons.append(f"duplicate_minute_bar:{path.stem}:{row['datetime']}")
                continue
            revision = int(old["revision"]) + 1 if old else 1
            if old and _bool(old["is_final"]) is True:
                if not row["is_final"]:
                    raise ValueError("final_bar_downgrade")
                if (int(raw.get("revision") or 0) != revision or raw.get("supersedes_hash") != old["evidence_hash"]
                        or not _text(raw.get("revision_reason"))):
                    raise ValueError("final_bar_revision_requires_parent_proof")
                row["finality"] = "revised"
            row.update(revision=revision, supersedes_hash=old["evidence_hash"] if old else "",
                       updated_at=now_iso)
            row["evidence_hash"] = observation_hash(row)
            latest[row["datetime"]] = row
            accepted.append(row)
        except (ValueError, TypeError) as exc:
            reasons.append(f"minute_lifecycle_rejected:{path.stem}:{exc}")
    if not accepted:
        return 0, reasons, stored_latest
    buffer = io.StringIO(newline="")
    if scanned and scanned.st_size:
        with path.open("rb") as stream:
            stream.seek(-1, 2)
            if stream.read(1) not in {b"\n", b"\r"}:
                buffer.write("\n")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    if not scanned or not scanned.st_size:
        writer.writeheader()
    writer.writerows({key: row.get(key, "") for key in columns} for row in accepted)
    try:
        atomic_append(path, buffer.getvalue().encode("utf-8"), scanned=scanned)
    except OSError as exc:
        return 0, [*reasons, f"minute_append_failed:{path.name}:{type(exc).__name__}"], stored_latest
    return len(accepted), reasons, max(latest, default="")
