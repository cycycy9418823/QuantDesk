"""Stage 6 section 16 duplicate-session audit and bounded repair."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.canonical_write_guard import (
    atomic_restore_from_backup,
    ensure_path_outside_canonical,
    resolved_paths_equal,
    sha256_path,
)
from quantdesk.intraday.stage6_append_only_import import (
    CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    canonical_intraday_write_lock_path,
)
from quantdesk.intraday.intraday_quality_audit import (
    assess_complete_session,
    audit_intraday_file,
)
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json

SECTION_16_REFERENCE = "2026-07-19 user ruling section 16"
EXPECTED_SESSION_ROWS = 240
SEMANTIC_COLUMNS = (
    "symbol",
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "price_basis",
    "source",
)
MARKET_DATA_COLUMNS = tuple(
    column for column in SEMANTIC_COLUMNS if column != "source"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                header = [str(item) for item in (reader.fieldnames or [])]
                rows = [
                    {str(key): str(value or "") for key, value in row.items() if key}
                    for row in reader
                ]
            return header, rows, encoding
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f"{encoding}:{type(exc).__name__}")
    raise ValueError(f"canonical_csv_unreadable:{path.name}:{';'.join(errors)}")


def _row_symbol(row: Mapping[str, Any], fallback: str) -> str:
    return normalize_symbol(row.get("symbol") or fallback)


def _row_date(row: Mapping[str, Any]) -> str:
    value = str(row.get("date") or "").strip()[:10]
    return value or str(row.get("datetime") or "").strip()[:10]


def _timestamp(row: Mapping[str, Any]) -> str:
    value = str(row.get("datetime") or "").strip()
    if value:
        normalized = value.replace("T", " ").replace("/", "-")
        if len(normalized) >= 19:
            return normalized[:19]
    date_text = _row_date(row)
    time_text = str(row.get("time") or "").strip()
    return f"{date_text} {time_text}".strip()


def _stable_hash(values: Iterable[Any]) -> str:
    serialized = json.dumps(
        [str(value or "").strip() for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(serialized)


def _exact_row_hash(row: Mapping[str, Any], header: Sequence[str]) -> str:
    return _stable_hash(row.get(column) for column in header)


def _semantic_row_hash(row: Mapping[str, Any]) -> str:
    return _stable_hash(row.get(column) for column in SEMANTIC_COLUMNS)


def _session_inventory(
    *,
    path: Path,
    symbol: str,
    date_text: str,
    rows: Sequence[Mapping[str, Any]],
    header: Sequence[str],
) -> dict[str, Any]:
    rows_by_timestamp: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_timestamp[_timestamp(row)].append(row)
    timestamp_counts = Counter(
        {timestamp: len(group) for timestamp, group in rows_by_timestamp.items()}
    )
    unique_timestamp_count = len(timestamp_counts)
    duplicate_groups = {
        key: count for key, count in timestamp_counts.items() if key and count > 1
    }
    row_count = len(rows)
    ratio = row_count / unique_timestamp_count if unique_timestamp_count else 0.0
    inferred_copy_count = (
        max(max(timestamp_counts.values(), default=1), int(math.ceil(ratio)))
        if row_count
        else 0
    )
    hash_required = bool(duplicate_groups)
    exact_hashes = (
        [_exact_row_hash(row, header) for row in rows] if hash_required else []
    )
    semantic_hashes = (
        [_semantic_row_hash(row) for row in rows] if hash_required else []
    )
    sources = sorted(
        {str(row.get("source") or "unknown").strip() or "unknown" for row in rows}
    )
    source_counts = Counter(
        str(row.get("source") or "unknown").strip() or "unknown" for row in rows
    )
    shape_reasons: list[str] = []
    reasons: list[str] = []
    if row_count < EXPECTED_SESSION_ROWS:
        shape_reasons.append("row_count_below_expected_session")
    elif row_count in {EXPECTED_SESSION_ROWS + 1, EXPECTED_SESSION_ROWS + 2} and unique_timestamp_count == row_count:
        shape_reasons.append("legacy_boundary_bar_layout")
    elif row_count > EXPECTED_SESSION_ROWS:
        shape_reasons.append("row_count_above_expected_session")
    if duplicate_groups:
        reasons.append("duplicate_timestamp_rows")
        shape_reasons.append("duplicate_timestamp_rows")
    if unique_timestamp_count > EXPECTED_SESSION_ROWS + 2:
        shape_reasons.append("unique_timestamp_count_above_expected_session")
    return {
        "symbol": symbol,
        "date": date_text,
        "canonical_path": str(path),
        "row_count": row_count,
        "unique_timestamp_count": unique_timestamp_count,
        "duplicate_row_count": row_count - unique_timestamp_count,
        "duplicate_group_count": len(duplicate_groups),
        "max_timestamp_multiplicity": max(timestamp_counts.values(), default=0),
        "inferred_copy_count": inferred_copy_count,
        "sources": sources,
        "source_counts": dict(sorted(source_counts.items())),
        "distinct_exact_row_hash_count": (
            len(set(exact_hashes)) if hash_required else row_count
        ),
        "distinct_semantic_row_hash_count": (
            len(set(semantic_hashes)) if hash_required else row_count
        ),
        "exact_duplicate_group_count": sum(
            1
            for timestamp in duplicate_groups
            if len({_exact_row_hash(row, header) for row in rows_by_timestamp[timestamp]})
            == 1
        ),
        "shape_reason_codes": shape_reasons,
        "anomaly_reason_codes": reasons,
        "duplicate_timestamp_samples": sorted(duplicate_groups)[:10],
    }


def _write_inventory_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "symbol",
        "date",
        "row_count",
        "unique_timestamp_count",
        "duplicate_row_count",
        "duplicate_group_count",
        "max_timestamp_multiplicity",
        "inferred_copy_count",
        "sources",
        "source_counts",
        "distinct_exact_row_hash_count",
        "distinct_semantic_row_hash_count",
        "exact_duplicate_group_count",
        "anomaly_reason_codes",
        "shape_reason_codes",
        "canonical_path",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            for key in (
                "sources",
                "source_counts",
                "anomaly_reason_codes",
                "shape_reason_codes",
            ):
                payload[key] = json.dumps(
                    payload.get(key), ensure_ascii=False, sort_keys=True
                )
            writer.writerow(payload)


def _inventory_fields() -> tuple[str, ...]:
    return (
        "symbol",
        "date",
        "row_count",
        "unique_timestamp_count",
        "duplicate_row_count",
        "duplicate_group_count",
        "max_timestamp_multiplicity",
        "inferred_copy_count",
        "sources",
        "source_counts",
        "distinct_exact_row_hash_count",
        "distinct_semantic_row_hash_count",
        "exact_duplicate_group_count",
        "anomaly_reason_codes",
        "shape_reason_codes",
        "canonical_path",
    )


def _inventory_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for key in (
        "sources",
        "source_counts",
        "anomaly_reason_codes",
        "shape_reason_codes",
    ):
        payload[key] = json.dumps(
            payload.get(key), ensure_ascii=False, sort_keys=True
        )
    return payload


def _scan_path_inventory(path: Path) -> list[dict[str, Any]]:
    header, _unused_rows, encoding = _read_rows_header(path)
    required = [
        column
        for column in ("symbol", "datetime", "date", "time", "source")
        if column in header
    ]
    if not required or not ({"datetime", "date"} & set(required)):
        raise ValueError(f"canonical_scan_columns_missing:{path.name}")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for chunk in pd.read_csv(
        path,
        encoding=encoding,
        dtype=str,
        keep_default_na=False,
        usecols=required,
        chunksize=200_000,
    ):
        if "symbol" in chunk.columns:
            raw_symbols = chunk["symbol"].astype(str)
            symbol_map = {
                value: normalize_symbol(value) or normalize_symbol(path.stem)
                for value in raw_symbols.unique().tolist()
            }
            symbols = raw_symbols.map(symbol_map)
        else:
            symbols = pd.Series(normalize_symbol(path.stem), index=chunk.index)
        if "date" in chunk.columns:
            dates = chunk["date"].astype(str).str.slice(0, 10)
        else:
            dates = pd.Series("", index=chunk.index)
        if "datetime" in chunk.columns:
            datetimes = (
                chunk["datetime"]
                .astype(str)
                .str.replace("T", " ", regex=False)
                .str.replace("/", "-", regex=False)
                .str.slice(0, 19)
            )
            dates = dates.mask(dates.str.len() < 10, datetimes.str.slice(0, 10))
        else:
            times = chunk.get("time", pd.Series("", index=chunk.index)).astype(str)
            datetimes = dates + " " + times
        sources = chunk.get(
            "source", pd.Series("unknown", index=chunk.index)
        ).astype(str)
        scan = pd.DataFrame(
            {
                "symbol": symbols,
                "date": dates,
                "timestamp": datetimes,
                "source": sources.mask(sources.str.len() == 0, "unknown"),
            }
        )
        scan = scan.loc[
            scan["symbol"].astype(str).str.len().gt(0)
            & scan["date"].astype(str).str.len().ge(10)
        ]
        for key, group in scan.groupby(["symbol", "date"], sort=False):
            normalized_key = (str(key[0]), str(key[1])[:10])
            state = grouped.setdefault(
                normalized_key,
                {"row_count": 0, "timestamps": Counter(), "sources": Counter()},
            )
            state["row_count"] += int(len(group))
            state["timestamps"].update(
                {
                    str(timestamp): int(count)
                    for timestamp, count in group["timestamp"].value_counts().items()
                }
            )
            state["sources"].update(
                {
                    str(source): int(count)
                    for source, count in group["source"].value_counts().items()
                }
            )
    items: list[dict[str, Any]] = []
    anomaly_keys: set[tuple[str, str]] = set()
    for (symbol, date_text), state in sorted(grouped.items()):
        timestamp_counts: Counter[str] = state["timestamps"]
        row_count = int(state["row_count"])
        unique_count = len(timestamp_counts)
        duplicate_groups = {
            timestamp: count
            for timestamp, count in timestamp_counts.items()
            if timestamp and count > 1
        }
        ratio = row_count / unique_count if unique_count else 0.0
        shape_reasons: list[str] = []
        reasons: list[str] = []
        if row_count < EXPECTED_SESSION_ROWS:
            shape_reasons.append("row_count_below_expected_session")
        elif row_count in {EXPECTED_SESSION_ROWS + 1, EXPECTED_SESSION_ROWS + 2} and unique_count == row_count:
            shape_reasons.append("legacy_boundary_bar_layout")
        elif row_count > EXPECTED_SESSION_ROWS:
            shape_reasons.append("row_count_above_expected_session")
        if duplicate_groups:
            reasons.append("duplicate_timestamp_rows")
            shape_reasons.append("duplicate_timestamp_rows")
        if unique_count > EXPECTED_SESSION_ROWS + 2:
            shape_reasons.append("unique_timestamp_count_above_expected_session")
        if reasons:
            anomaly_keys.add((symbol, date_text))
        items.append(
            {
                "symbol": symbol,
                "date": date_text,
                "canonical_path": str(path),
                "row_count": row_count,
                "unique_timestamp_count": unique_count,
                "duplicate_row_count": row_count - unique_count,
                "duplicate_group_count": len(duplicate_groups),
                "max_timestamp_multiplicity": max(
                    timestamp_counts.values(), default=0
                ),
                "inferred_copy_count": (
                    max(
                        max(timestamp_counts.values(), default=1),
                        int(math.ceil(ratio)),
                    )
                    if row_count
                    else 0
                ),
                "sources": sorted(state["sources"]),
                "source_counts": dict(sorted(state["sources"].items())),
                "distinct_exact_row_hash_count": row_count,
                "distinct_semantic_row_hash_count": row_count,
                "exact_duplicate_group_count": 0,
                "shape_reason_codes": shape_reasons,
                "anomaly_reason_codes": reasons,
                "duplicate_timestamp_samples": sorted(duplicate_groups)[:10],
            }
        )
    if anomaly_keys:
        full_header, rows, _encoding = _read_rows(path)
        anomaly_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            key = (_row_symbol(row, path.stem), _row_date(row))
            if key in anomaly_keys:
                anomaly_rows[key].append(row)
        replacements = {
            key: _session_inventory(
                path=path,
                symbol=key[0],
                date_text=key[1],
                rows=session_rows,
                header=full_header,
            )
            for key, session_rows in anomaly_rows.items()
        }
        items = [
            replacements.get((item["symbol"], item["date"]), item) for item in items
        ]
    return items


def _read_rows_header(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                header = [str(item) for item in (reader.fieldnames or [])]
            if not header:
                raise ValueError("header_missing")
            return header, [], encoding
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            errors.append(f"{encoding}:{type(exc).__name__}")
    raise ValueError(f"canonical_csv_unreadable:{path.name}:{';'.join(errors)}")


def scan_intraday_duplicate_sessions(
    *,
    canonical_dir: Path,
    output_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    """Stream the complete canonical pool and inventory every symbol-date."""

    canonical = Path(canonical_dir)
    output = Path(output_dir)
    ensure_path_outside_canonical(
        output,
        canonical,
        role="duplicate_scan_output_dir",
    )
    anomalies: list[dict[str, Any]] = []
    file_errors: list[dict[str, str]] = []
    empty_file_paths: list[str] = []
    files = sorted(canonical.glob("*.csv"))
    total_bytes = 0
    shape_deviation_count = 0
    inventory_path = output / "intraday_symbol_date_inventory.csv"
    anomalies_path = output / "intraday_duplicate_session_anomalies.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_temp = inventory_path.with_name(
        f".{inventory_path.name}.{uuid.uuid4().hex}.tmp"
    )
    symbol_date_count = 0
    try:
        with inventory_temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=_inventory_fields(),
                extrasaction="ignore",
            )
            writer.writeheader()
            for file_index, path in enumerate(files, start=1):
                total_bytes += path.stat().st_size
                try:
                    rows = _scan_path_inventory(path)
                except (OSError, ValueError, UnicodeError, pd.errors.ParserError) as exc:
                    file_errors.append({"path": str(path), "error": str(exc)})
                    continue
                if not rows:
                    empty_file_paths.append(str(path))
                symbol_date_count += len(rows)
                for row in rows:
                    writer.writerow(_inventory_csv_row(row))
                    if row["shape_reason_codes"]:
                        shape_deviation_count += 1
                    if row["anomaly_reason_codes"]:
                        anomalies.append(row)
                if file_index % 25 == 0:
                    handle.flush()
                    print(
                        f"stage6-section16-scan progress {file_index}/{len(files)}",
                        flush=True,
                    )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(inventory_temp, inventory_path)
    finally:
        inventory_temp.unlink(missing_ok=True)
    duplicate_pollution = [row for row in anomalies if row["duplicate_group_count"]]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "contract_reference": SECTION_16_REFERENCE,
        "run_id": str(run_id),
        "generated_at": now_text(),
        "canonical_dir": str(canonical),
        "status": "failed" if file_errors else ("anomalies_found" if anomalies else "passed"),
        "scanned_file_count": len(files),
        "scanned_file_bytes": total_bytes,
        "symbol_date_count": symbol_date_count,
        "anomalous_symbol_date_count": len(anomalies),
        "shape_deviation_symbol_date_count": shape_deviation_count,
        "duplicate_pollution_symbol_date_count": len(duplicate_pollution),
        "file_error_count": len(file_errors),
        "file_errors": file_errors,
        "empty_file_count": len(empty_file_paths),
        "empty_file_paths": empty_file_paths,
        "inventory_csv_path": str(inventory_path),
        "anomalies_json_path": str(anomalies_path),
        "anomalies": anomalies,
    }
    atomic_write_json(anomalies_path, payload)
    return payload


def _rows_to_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _candidate_sessions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    provenance_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("source") or "unknown").strip() or "unknown",
            str(row.get("updated_at") or "").strip(),
            str(row.get("price_basis") or "unknown").strip() or "unknown",
        )
        provenance_groups[key].append(row)
    candidates: list[dict[str, Any]] = []
    for (source, updated_at, basis), group in sorted(provenance_groups.items()):
        by_timestamp: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in group:
            by_timestamp[_timestamp(row)].append(row)
        copies = max((len(items) for items in by_timestamp.values()), default=1)
        for copy_index in range(copies):
            candidate_rows = [
                values[copy_index]
                for _timestamp_text, values in sorted(by_timestamp.items())
                if copy_index < len(values)
            ]
            if not candidate_rows:
                continue
            frame = _rows_to_frame(candidate_rows)
            symbol = _row_symbol(candidate_rows[0], "")
            date_text = _row_date(candidate_rows[0])
            assessment = assess_complete_session(
                frame,
                symbol=symbol,
                target_date=date_text,
            )
            candidates.append(
                {
                    "source": source,
                    "updated_at": updated_at,
                    "price_basis": basis,
                    "copy_index": copy_index,
                    "rows": candidate_rows,
                    "assessment": assessment,
                }
            )
    return candidates


def _candidate_score(candidate: Mapping[str, Any]) -> tuple[int, int, int, int]:
    assessment = candidate.get("assessment") or {}
    return (
        int(assessment.get("formal_execution_eligible") is True),
        int(assessment.get("unique_session_minutes") or 0),
        int(str(candidate.get("price_basis") or "").lower() == "raw"),
        -len(assessment.get("reason_codes") or []),
    )


def _compare_candidates(
    selected: Mapping[str, Any], alternatives: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    selected_rows = {_timestamp(row): row for row in selected.get("rows") or []}
    comparisons: list[dict[str, Any]] = []
    for alternative in alternatives:
        other_rows = {_timestamp(row): row for row in alternative.get("rows") or []}
        common = sorted(set(selected_rows) & set(other_rows))
        price_differences: list[float] = []
        market_data_mismatch_count = 0
        for timestamp in common:
            if _stable_hash(
                selected_rows[timestamp].get(column)
                for column in MARKET_DATA_COLUMNS
            ) != _stable_hash(
                other_rows[timestamp].get(column)
                for column in MARKET_DATA_COLUMNS
            ):
                market_data_mismatch_count += 1
            for column in ("open", "high", "low", "close"):
                try:
                    price_differences.append(
                        abs(
                            float(selected_rows[timestamp].get(column) or 0)
                            - float(other_rows[timestamp].get(column) or 0)
                        )
                    )
                except (TypeError, ValueError):
                    continue
        comparisons.append(
            {
                "source": str(alternative.get("source") or "unknown"),
                "updated_at": str(alternative.get("updated_at") or ""),
                "common_timestamp_count": len(common),
                "selected_only_timestamp_count": len(set(selected_rows) - set(other_rows)),
                "alternative_only_timestamp_count": len(set(other_rows) - set(selected_rows)),
                "max_ohlc_absolute_difference": max(price_differences, default=0.0),
                "market_data_mismatch_count": market_data_mismatch_count,
            }
        )
    primary = comparisons[0] if comparisons else {}
    return {
        "common_timestamp_count": int(primary.get("common_timestamp_count") or 0),
        "comparisons": comparisons,
    }


def _select_session(
    rows: Sequence[Mapping[str, Any]], header: Sequence[str]
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    by_timestamp: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_timestamp[_timestamp(row)].append(row)
    exact_identical = bool(by_timestamp) and all(
        len({_exact_row_hash(row, header) for row in values}) == 1
        for values in by_timestamp.values()
    )
    if exact_identical:
        selected = [values[0] for _timestamp_text, values in sorted(by_timestamp.items())]
        return selected, {
            "selection_reason": "identical_duplicate_rows_keep_one",
            "selected_source": str(selected[0].get("source") or "unknown") if selected else "",
            "cross_source_comparison": {"common_timestamp_count": len(selected), "comparisons": []},
        }
    candidates = _candidate_sessions(rows)
    if not candidates:
        raise ValueError("duplicate_session_candidates_missing")
    ordered = sorted(candidates, key=_candidate_score, reverse=True)
    selected = ordered[0]
    equal_top_alternatives = [
        candidate
        for candidate in ordered[1:]
        if _candidate_score(candidate) == _candidate_score(selected)
    ]
    if equal_top_alternatives:
        comparison = _compare_candidates(selected, equal_top_alternatives)
        rows_differ = any(
            float(item.get("max_ohlc_absolute_difference") or 0.0) > 0.0
            or int(item.get("market_data_mismatch_count") or 0) > 0
            or int(item.get("selected_only_timestamp_count") or 0) > 0
            or int(item.get("alternative_only_timestamp_count") or 0) > 0
            for item in comparison["comparisons"]
        )
        if rows_differ:
            raise ValueError("ambiguous_equal_quality_duplicate_sessions")
    if selected["assessment"].get("formal_execution_eligible") is not True:
        raise ValueError("no_formally_eligible_duplicate_session_candidate")
    return list(selected["rows"]), {
        "selection_reason": "cross_source_structural_quality_preferred",
        "selected_source": str(selected.get("source") or "unknown"),
        "selected_assessment": selected["assessment"],
        "cross_source_comparison": _compare_candidates(selected, ordered[1:]),
    }


def _write_candidate(
    path: Path,
    *,
    header: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    encoding: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig" if encoding == "utf-8-sig" else encoding, newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(header),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _repair_intraday_duplicate_sessions_locked(
    *,
    canonical_dir: Path,
    anomalies: Sequence[Mapping[str, Any]],
    backup_dir: Path,
    output_path: Path,
    run_id: str,
    apply: bool,
) -> dict[str, Any]:
    """Repair duplicate symbol-dates with retained backups and rollback guards."""

    canonical = Path(canonical_dir)
    duplicate_anomalies = [
        dict(item) for item in anomalies if int(item.get("duplicate_group_count") or 0) > 0
    ]
    by_file: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for item in duplicate_anomalies:
        symbol = normalize_symbol(item.get("symbol"))
        expected = canonical / f"{symbol}.csv"
        supplied = Path(str(item.get("canonical_path") or ""))
        if not symbol or not resolved_paths_equal(supplied, expected):
            raise ValueError(
                f"canonical_intraday_target_scope_violation:{symbol or 'missing'}"
            )
        by_file[expected].append(item)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "contract_reference": SECTION_16_REFERENCE,
        "run_id": str(run_id),
        "generated_at": now_text(),
        "status": "ready" if by_file else "completed",
        "apply": bool(apply),
        "applied": False,
        "requested_symbol_date_count": len(duplicate_anomalies),
        "repaired_symbol_date_count": 0,
        "rollback_performed": False,
        "items": [],
        "reason_codes": [],
    }
    if not by_file or not apply:
        atomic_write_json(Path(output_path), payload)
        payload["output_path"] = str(output_path)
        return payload

    retained_backups = Path(backup_dir) / str(run_id)
    transaction = Path(backup_dir) / f".transaction-{uuid.uuid4().hex}"
    transaction.mkdir(parents=True, exist_ok=False)
    file_records: list[dict[str, Any]] = []
    try:
        for path, file_anomalies in sorted(by_file.items(), key=lambda item: str(item[0])):
            header, rows, encoding = _read_rows(path)
            before = path.read_bytes()
            targets = {
                (str(item["symbol"]), str(item["date"])): item
                for item in file_anomalies
            }
            selected_by_target: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
            metadata_by_target: dict[tuple[str, str], dict[str, Any]] = {}
            for key in targets:
                target_rows = [
                    row
                    for row in rows
                    if (_row_symbol(row, path.stem), _row_date(row)) == key
                ]
                selected, selection = _select_session(target_rows, header)
                selected_by_target[key] = selected
                metadata_by_target[key] = selection
            emitted: set[tuple[str, str]] = set()
            candidate_rows: list[Mapping[str, Any]] = []
            for row in rows:
                key = (_row_symbol(row, path.stem), _row_date(row))
                if key not in targets:
                    candidate_rows.append(row)
                elif key not in emitted:
                    candidate_rows.extend(selected_by_target[key])
                    emitted.add(key)
            candidate_path = transaction / path.name
            _write_candidate(
                candidate_path,
                header=header,
                rows=candidate_rows,
                encoding=encoding,
            )
            backup_path = retained_backups / path.name
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
            before_hash = _sha256_bytes(before)
            if sha256_path(backup_path) != before_hash:
                raise ValueError(f"backup_hash_verification_failed:{path.name}")
            record = {
                "path": path,
                "candidate_path": candidate_path,
                "backup_path": backup_path,
                "before": before,
                "targets": targets,
                "metadata": metadata_by_target,
            }
            file_records.append(record)
            for key, anomaly in targets.items():
                selection = metadata_by_target[key]
                payload["items"].append(
                    {
                        "symbol": key[0],
                        "target_date": key[1],
                        "row_count_before": int(anomaly.get("row_count") or 0),
                        "row_count_after": len(selected_by_target[key]),
                        "selection_reason": selection["selection_reason"],
                        "selected_source": selection["selected_source"],
                        "cross_source_comparison": selection["cross_source_comparison"],
                        "canonical_sha256_before": before_hash,
                        "canonical_sha256_after": "",
                        "backup_path": str(backup_path),
                        "post_repair_audit": {},
                        "applied": False,
                    }
                )
        for record in file_records:
            os.replace(record["candidate_path"], record["path"])
        items_by_key = {
            (item["symbol"], item["target_date"]): item for item in payload["items"]
        }
        for record in file_records:
            after_hash = _sha256_path(record["path"])
            for key in record["targets"]:
                item = items_by_key[key]
                post_audit = audit_intraday_file(
                    key[0], key[1], intraday_dir=canonical
                )
                item["post_repair_audit"] = post_audit
                item["canonical_sha256_after"] = after_hash
                if post_audit.get("formal_execution_eligible") is not True:
                    raise ValueError(f"post_repair_quality_failed:{key[0]}:{key[1]}")
                item["applied"] = True
        payload.update(
            {
                "status": "completed",
                "applied": True,
                "repaired_symbol_date_count": len(payload["items"]),
                "completed_at": now_text(),
            }
        )
    except Exception as exc:  # noqa: BLE001 - rollback must catch every failure
        rollback_errors: list[str] = []
        for record in reversed(file_records):
            try:
                expected_hash = _sha256_bytes(record["before"])
                target = record["path"]
                if target.exists() and sha256_path(target) == expected_hash:
                    restored_hash = expected_hash
                else:
                    restored_hash = atomic_restore_from_backup(
                        target,
                        record["backup_path"],
                        expected_sha256=expected_hash,
                    )
                record["rollback_verified"] = restored_hash == expected_hash
                record["canonical_sha256_restored"] = restored_hash
                if not record["rollback_verified"]:
                    raise ValueError("rollback_hash_verification_failed")
            except (OSError, ValueError) as rollback_exc:
                record["rollback_verified"] = False
                rollback_errors.append(f"{record['path']}:{rollback_exc}")
        items_by_key = {
            (item["symbol"], item["target_date"]): item
            for item in payload["items"]
        }
        for record in file_records:
            for key in record["targets"]:
                item = items_by_key.get(key)
                if item is None:
                    continue
                item["applied"] = False
                item["post_repair_audit"] = {}
                item["canonical_sha256_after"] = ""
                item["rollback_verified"] = bool(record.get("rollback_verified"))
                item["canonical_sha256_restored"] = str(
                    record.get("canonical_sha256_restored") or ""
                )
        payload.update(
            {
                "status": "failed",
                "applied": False,
                "rollback_performed": bool(file_records) and not rollback_errors,
                "rollback_errors": rollback_errors,
                "reason_codes": ["section16_duplicate_repair_transaction_failed"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        )
    finally:
        shutil.rmtree(transaction, ignore_errors=True)
    atomic_write_json(Path(output_path), payload)
    payload["output_path"] = str(output_path)
    return payload


def repair_intraday_duplicate_sessions(
    *,
    canonical_dir: Path,
    anomalies: Sequence[Mapping[str, Any]],
    backup_dir: Path,
    output_path: Path,
    run_id: str,
    apply: bool,
) -> dict[str, Any]:
    """Run duplicate repair under the singleton canonical write lock."""

    canonical = Path(canonical_dir)
    ensure_path_outside_canonical(
        Path(backup_dir) / str(run_id),
        canonical,
        role="duplicate_backup_dir",
    )
    ensure_path_outside_canonical(
        Path(output_path),
        canonical,
        role="duplicate_output",
    )
    if not apply:
        return _repair_intraday_duplicate_sessions_locked(
            canonical_dir=canonical_dir,
            anomalies=anomalies,
            backup_dir=backup_dir,
            output_path=output_path,
            run_id=run_id,
            apply=False,
        )
    canonical.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(
        canonical_intraday_write_lock_path(canonical),
        timeout_seconds=CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    ):
        return _repair_intraday_duplicate_sessions_locked(
            canonical_dir=canonical,
            anomalies=anomalies,
            backup_dir=backup_dir,
            output_path=output_path,
            run_id=run_id,
            apply=True,
        )
