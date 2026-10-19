"""Append-only forward PIT sector observations for future replay windows.

The existing sector/theme map is a mutable current cross-section and therefore
must never be projected backwards.  This module gives it a safe forward-only
use: capture the current membership and board observations on the capture date,
seal their source hashes, and make the resulting snapshot usable only from that
date onward.  Historical target dates fail closed.

Round 6 Part C boundary: the only data write is a new
``data/sector_history/YYYY-MM-DD/snapshot.json`` file.  Existing daily,
intraday, universe, feature, metadata, and state assets are never modified.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_truth.em_sector_provider import coarsest_board_per_symbol
from quantdesk.data_update_staging import atomic_write_text
from quantdesk.intraday.canonical_write_guard import (
    ensure_directory_disjoint_from_canonical,
)
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION


DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice. Forward PIT archive only; no historical backfill."
)

_UNKNOWN_LABELS = {
    "",
    "nan",
    "none",
    "null",
    "unknown",
    "pending",
    "待补充",
    "未知",
    "未知板块",
    "未知行业",
    "未知题材",
    "未知概念",
}

# These exact metadata keys describe when an otherwise identical observation
# was captured or persisted.  They are excluded only from the append-only
# content identity; the full snapshot provenance below keeps their raw values.
# Effective dates and entity fields such as ``source_updated_at`` are
# deliberately not listed because they are substantive replay evidence.
_CONTENT_IDENTITY_VOLATILE_TIME_KEYS = frozenset(
    {
        "captured_at",
        "completed_at",
        "cutoff",
        "fetched_at",
        "finished_at",
        "generated_at",
        "observed_at",
        "started_at",
        "updated_at",
    }
)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def _content_identity_projection(value: Any) -> Any:
    """Recursively remove only explicitly volatile capture-time metadata."""

    if isinstance(value, Mapping):
        return {
            key: _content_identity_projection(item)
            for key, item in value.items()
            if str(key).strip().lower()
            not in _CONTENT_IDENTITY_VOLATILE_TIME_KEYS
        }
    if isinstance(value, list):
        return [_content_identity_projection(item) for item in value]
    if isinstance(value, tuple):
        return [_content_identity_projection(item) for item in value]
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_mtime_text(path: Path) -> str:
    return datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat(
        timespec="seconds"
    )


def _source_mtime_day(path: Path) -> date:
    return datetime.fromtimestamp(Path(path).stat().st_mtime).date()


def _local_today() -> date:
    """Return the actual local day; callers cannot relabel a historical capture."""

    return datetime.now().astimezone().date()


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_record(path: Path, *, source: str, cutoff: str) -> dict[str, Any]:
    resolved = Path(path).resolve()
    return {
        "source": source,
        "path": str(resolved),
        "hash": f"sha256:{_sha256_path(resolved)}",
        "cutoff": str(cutoff),
    }


def _usable_label(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in _UNKNOWN_LABELS or text.startswith("未知"):
        return ""
    return text


def _membership_rows(
    path: Path,
    *,
    captured_at: str,
    target_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frame = read_csv_frame(path, dtype=str)
    if frame.empty or "symbol" not in frame.columns:
        return [], {
            "input_count": int(len(frame)),
            "unknown_or_pending_count": int(len(frame)),
            "missing_observation_timestamp_count": int(len(frame)),
            "non_target_observation_timestamp_count": 0,
            "embedded_observation_days": [],
        }
    rows: list[dict[str, Any]] = []
    unknown_or_pending = 0
    missing_observation_timestamp = 0
    non_target_observation_timestamp = 0
    observation_days: set[str] = set()
    source_update_days: set[str] = set()
    target_day = _parse_day(target_date)
    # Round 8 裁定十七 fix.  The observation moment is when *we* captured the
    # live cross-section — the capture time.  It is read from the artifact's own
    # self-declared ``captured_at`` column (written by
    # ``capture_current_sector_membership``); when a row carries no such column
    # value we fall back to the trusted capture-moment parameter, which the
    # archive's provenance gate validates independently (path + sha256 +
    # row_count + same-day ``captured_at``).  The source-side ``updated_at``
    # column is NEVER read as the observation time — that mislabel graded 5,199
    # same-day-captured members on 2026-07-23 as "observation not same day",
    # fabricating a misleading ``non_target`` count.  ``updated_at`` is retained
    # verbatim as ``source_updated_at`` replay evidence and surfaced separately as
    # ``embedded_source_update_days`` transparency.  The same-day standard itself
    # is unchanged; it is now measured against the true capture moment.
    for raw in frame.to_dict(orient="records"):
        symbol = normalize_symbol(raw.get("symbol"))
        source = str(raw.get("source") or "").strip()
        sector = next(
            (
                label
                for label in (
                    _usable_label(raw.get("sector")),
                    _usable_label(raw.get("industry")),
                    _usable_label(raw.get("primary_theme")),
                )
                if label
            ),
            "",
        )
        row_captured = str(raw.get("captured_at") or "").strip()
        observed_ts = row_captured or str(captured_at or "").strip()
        observed_day = _parse_day(row_captured) if row_captured else _parse_day(captured_at)
        source_update_day = _parse_day(raw.get("updated_at"))
        if source_update_day is not None:
            source_update_days.add(source_update_day.isoformat())
        if observed_day is not None:
            observation_days.add(observed_day.isoformat())
        if (
            not symbol
            or not sector
            or source.lower() == "pending"
        ):
            unknown_or_pending += 1
            continue
        if observed_day is None:
            missing_observation_timestamp += 1
        elif target_day is None or observed_day != target_day:
            non_target_observation_timestamp += 1
        rows.append(
            {
                "symbol": symbol,
                "name": str(raw.get("name") or "").strip(),
                "sector": sector,
                "industry": str(raw.get("industry") or "").strip(),
                "source": source or "local_theme_map",
                "source_updated_at": str(raw.get("updated_at") or "").strip(),
                "effective_from": target_date,
                "observed_at": observed_ts,
            }
        )
    return sorted(rows, key=lambda item: (item["symbol"], item["sector"])), {
        "input_count": int(len(frame)),
        "unknown_or_pending_count": unknown_or_pending,
        "missing_observation_timestamp_count": missing_observation_timestamp,
        "non_target_observation_timestamp_count": non_target_observation_timestamp,
        "embedded_observation_days": sorted(observation_days),
        "embedded_source_update_days": sorted(source_update_days),
    }


def _board_rows(path: Path) -> list[dict[str, Any]]:
    frame = read_csv_frame(path)
    if frame.empty:
        return []
    name_col = next(
        (column for column in ("name", "sector", "sector_name", "板块名称") if column in frame.columns),
        None,
    )
    change_col = next(
        (column for column in ("pct_chg", "change", "涨跌幅") if column in frame.columns),
        None,
    )
    if name_col is None or change_col is None:
        return []
    normalized: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        sector = str(raw.get(name_col) or "").strip()
        change = _finite(raw.get(change_col))
        if not sector or change is None:
            continue
        normalized.append(
            {
                "sector": sector,
                "sector_name": sector,
                "observed_pct_chg": round(change, 6),
                "rotation_strength": round(change, 6),
                "rotation_strength_scale": "observed_pct_chg_percent",
                "amount": _finite(raw.get("amount")),
                "leading_stock": str(raw.get("leading_stock") or "").strip(),
            }
        )
    normalized.sort(key=lambda item: (-float(item["observed_pct_chg"]), item["sector"]))
    for index, row in enumerate(normalized, start=1):
        row["current_rank"] = index
    return normalized


def _member_name(raw: Mapping[str, Any]) -> str:
    for key in ("名称", "股票名称", "name", "code_name"):
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return ""


def _member_symbol(raw: Mapping[str, Any]) -> str:
    for key in ("代码", "股票代码", "symbol", "code", "con_code"):
        symbol = normalize_symbol(raw.get(key))
        if symbol:
            return symbol
    return ""


def _default_member_fetcher(provider: str, board: str) -> Any:
    """Fetch one board's current constituents from a same-taxonomy provider.

    Round 10 Part B.  Eastmoney is served by :mod:`quantdesk.data_truth.
    em_sector_provider` rather than by ``akshare`` directly, because akshare's
    Eastmoney helpers hardcode numbered ``push2`` hosts that currently answer
    ``502`` from Eastmoney's own gateway while the ``push2delay`` variants serve
    the identical payload.  THS is no longer routable at all: akshare 1.18.64
    removed ``stock_board_industry_cons_ths``, so every THS board raised
    ``membership_provider_unavailable:ths`` without ever issuing a request --
    which is why the 2026-07-24 chain logged 90 identical failures and why the
    freeze was never rate limiting.  THS is kept in the dispatch table so that
    an unroutable provider still degrades with an explicit reason code instead
    of silently resolving to a different taxonomy.
    """

    key = str(provider or "").strip().lower()
    if key == "eastmoney":
        from quantdesk.data_truth.em_sector_provider import (
            fetch_industry_board_members,
        )

        return fetch_industry_board_members(board)

    function_name = {
        "ths": "stock_board_industry_cons_ths",
    }.get(key, "")
    if not function_name:
        # An unroutable provider must degrade with its own reason code. Importing
        # akshare before this check masked that with ModuleNotFoundError wherever
        # the optional dependency is absent, losing the very reason code this
        # branch exists to produce.
        raise RuntimeError(f"membership_provider_unavailable:{provider}")

    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"membership_provider_unavailable:{provider}:akshare_not_installed"
        ) from exc

    function = getattr(ak, function_name, None)
    if not callable(function):
        raise RuntimeError(f"membership_provider_unavailable:{provider}")
    try:
        return function(symbol=board)
    except TypeError:
        return function(board)


def capture_current_sector_membership(
    project_root: Path,
    *,
    board_cache_path: Path,
    provider: str,
    network: bool = False,
    captured_at: str | None = None,
    member_fetcher: Any | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch a same-taxonomy current membership cross-section into results/.

    This is a forward observation only.  It never writes ``data/universe`` and
    is disabled unless the caller explicitly authorizes network access.  Every
    board in the just-refreshed board cache must return members; partial provider
    responses remain degraded and are not staged as eligible evidence.
    """

    root = Path(project_root).resolve()
    captured = str(captured_at or now_text())
    captured_day = _parse_day(captured)
    board_path = Path(board_cache_path).resolve()
    base = {
        "ok": False,
        "status": "degraded",
        "network": bool(network),
        "source": str(provider or "").strip(),
        "captured_at": captured,
        "board_count": 0,
        "fetched_board_count": 0,
        "row_count": 0,
        "path": "",
        "sha256": "",
        "errors": [],
        "canonical_write": False,
        "data_universe_written": False,
    }
    if not network:
        base["status"] = "skipped"
        base["errors"] = ["network_not_authorized"]
        return base
    if captured_day is None or captured_day != _local_today():
        base["errors"] = ["membership_capture_not_local_today"]
        return base
    if not board_path.is_file():
        base["errors"] = ["sector_board_source_missing"]
        return base

    board_frame = read_csv_frame(board_path, dtype=str)
    name_column = next(
        (
            column
            for column in ("name", "sector", "sector_name", "板块名称", "行业名称")
            if column in board_frame.columns
        ),
        None,
    )
    boards = (
        sorted(
            {
                str(value).strip()
                for value in board_frame[name_column].tolist()
                if _usable_label(value)
            }
        )
        if name_column
        else []
    )
    base["board_count"] = len(boards)
    if not boards:
        base["errors"] = ["sector_board_rows_missing"]
        return base

    fetcher = member_fetcher or _default_member_fetcher
    # Round 10 Part B.  A symbol may legitimately appear under several boards of
    # the same taxonomy: Eastmoney's industry universe is the Shenwan
    # three-level tree, so every stock is reported under its level-1, level-2
    # and level-3 industry at once.  Treating the second sighting as a
    # ``multi_sector_conflict`` (the previous behaviour) would have failed the
    # capture on all ~5.6k symbols the moment the provider started working.
    # Instead every sighting is retained here and the cross-section is collapsed
    # below to the coarsest containing board, because the downstream PIT join
    # (``join_sector_membership_as_of``) requires exactly one active
    # classification per symbol per day and rejects ambiguous rows.
    names: dict[str, str] = {}
    board_symbols: dict[str, set[str]] = {}
    fetched_boards = 0
    errors: list[str] = []
    for board in boards:
        try:
            raw = fetcher(str(provider or ""), board)
            frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw or [])
        except Exception as exc:  # noqa: BLE001 - provider failures are visible/degraded
            errors.append(f"{board}:{type(exc).__name__}:{exc}"[:500])
            continue
        if frame.empty:
            errors.append(f"{board}:empty_membership")
            continue
        board_rows = 0
        for raw_row in frame.to_dict(orient="records"):
            symbol = _member_symbol(raw_row)
            if not symbol:
                continue
            board_symbols.setdefault(board, set()).add(symbol)
            names.setdefault(symbol, _member_name(raw_row))
            board_rows += 1
        if board_rows:
            fetched_boards += 1
        else:
            errors.append(f"{board}:no_normalizable_members")

    resolved_board = coarsest_board_per_symbol(board_symbols)
    rows = sorted(
        (
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "sector": board,
                "industry": board,
                "source": f"{str(provider or '').strip().lower()}_industry_cons",
                # Round 8 裁定十七: the capture artifact self-declares its
                # observation moment.  This is the time *we* observed the live
                # cross-section, so the column is ``captured_at`` — not
                # ``updated_at`` (a source-side revision timestamp).  The archive
                # grades same-day observation against this self-declared column.
                "captured_at": captured,
            }
            for symbol, board in resolved_board.items()
        ),
        key=lambda item: item["symbol"],
    )
    base["fetched_board_count"] = fetched_boards
    base["row_count"] = len(rows)
    base["errors"] = errors[:100]
    if errors or fetched_boards != len(boards) or not rows:
        return base

    frame = pd.DataFrame(
        rows,
        columns=["symbol", "name", "sector", "industry", "source", "captured_at"],
    )
    csv_text = frame.to_csv(index=False, lineterminator="\n")
    content = csv_text.encode("utf-8")
    content_hash = _sha256_bytes(content)
    destination_root = Path(
        output_dir or root / "results" / "data_chain" / "sector_membership_capture"
    ).resolve()
    output_path = destination_root / (
        f"membership_{captured_day.strftime('%Y%m%d')}_{content_hash[:12]}.csv"
    )
    if output_path.exists() and output_path.read_bytes() != content:
        raise ValueError("sector_membership_capture_hash_collision")
    if not output_path.exists():
        # Write the exact bytes that were hashed.  ``Path.write_text`` applies
        # platform newline translation (LF->CRLF on Windows), which would make
        # the on-disk file hash differ from the declared ``content_hash`` and
        # permanently fail the archive's same-day provenance gate.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_once(output_path, content)
    base.update(
        {
            "ok": True,
            "status": "completed",
            "path": str(output_path),
            "sha256": f"sha256:{content_hash}",
            "errors": [],
        }
    )
    return base


def _guard_destination(project_root: Path, target_dir: Path) -> None:
    protected = (
        project_root / "data" / "daily",
        project_root / "data" / "intraday",
        project_root / "data" / "features",
        project_root / "data" / "metadata",
        project_root / "data" / "universe",
        project_root / "data" / "benchmark",
        project_root / "data" / "calendar",
        project_root / "state",
    )
    for canonical in protected:
        ensure_directory_disjoint_from_canonical(
            target_dir,
            canonical,
            role="round6_forward_sector_history",
        )


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _resolve_evidence_path(root: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _same_day_timestamp(value: Any, expected: date | None) -> bool:
    observed = _parse_day(value)
    return expected is not None and observed == expected


def archive_forward_sector_snapshot(
    project_root: Path,
    *,
    target_date: str,
    captured_at: str | None = None,
    theme_map_path: Path | None = None,
    board_cache_path: Path | None = None,
    source_override: Mapping[str, Any] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build and append one forward-only sector observation.

    A snapshot is formally eligible only when it was captured on the same date
    it names.  Replaying a historical target with today's mutable files returns
    ``degraded`` and writes nothing.  Existing same-day bytes are immutable:
    equal content is idempotent, different content raises a fail-closed conflict.
    """

    root = Path(project_root).resolve()
    target = str(target_date or "")[:10]
    target_day = _parse_day(target)
    captured = str(captured_at or now_text())
    captured_day = _parse_day(captured)
    history_root = root / "data" / "sector_history"
    snapshot_path = history_root / target / "snapshot.json"
    revocation_path = history_root / target / "revocation.json"
    _guard_destination(root, history_root)

    reasons: list[str] = []
    if target_day is None or captured_day is None:
        reasons.append("sector_history_date_invalid")
    elif target_day != captured_day:
        reasons.append("historical_target_not_current")
    if target_day is not None and target_day != _local_today():
        reasons.append("target_date_not_local_today")

    override = dict(source_override or {})
    refresh = override.get("sector_refresh")
    refresh_payload = dict(refresh) if isinstance(refresh, Mapping) else {}
    membership_evidence_raw = override.get("membership_observation")
    membership_evidence = (
        dict(membership_evidence_raw)
        if isinstance(membership_evidence_raw, Mapping)
        else {}
    )

    theme_path = Path(theme_map_path or root / "data" / "universe" / "stock_theme_map.csv")
    board_path = Path(board_cache_path or root / "data" / "cache" / "industry_board_latest.csv")
    sources: list[dict[str, Any]] = []
    memberships: list[dict[str, Any]] = []
    rankings: list[dict[str, Any]] = []
    membership_meta: dict[str, Any] = {
        "input_count": 0,
        "unknown_or_pending_count": 0,
        "missing_observation_timestamp_count": 0,
        "non_target_observation_timestamp_count": 0,
        "embedded_observation_days": [],
    }
    if theme_path.exists():
        try:
            if target_day is not None and _source_mtime_day(theme_path) != target_day:
                reasons.append("sector_membership_source_not_same_day")
            memberships, membership_meta = _membership_rows(
                theme_path,
                captured_at=captured,
                target_date=target,
            )
            embedded_days = set(membership_meta.get("embedded_observation_days") or [])
            if not embedded_days:
                reasons.append("sector_membership_observation_timestamp_missing")
            elif target_day is None or embedded_days != {target_day.isoformat()}:
                reasons.append("sector_membership_observation_not_same_day")
            sources.append(
                _source_record(
                    theme_path,
                    source=str(
                        membership_evidence.get("source")
                        or "unverified_membership_snapshot"
                    ),
                    cutoff=_source_mtime_text(theme_path),
                )
            )
        except Exception as exc:  # noqa: BLE001 - archive fails closed
            reasons.append(f"sector_membership_unreadable:{type(exc).__name__}")
    else:
        reasons.append("sector_membership_source_missing")
    if not memberships:
        reasons.append("sector_membership_rows_missing")
    if int(membership_meta.get("unknown_or_pending_count") or 0) > 0:
        reasons.append("sector_membership_unknown_or_pending")
    if int(membership_meta.get("missing_observation_timestamp_count") or 0) > 0:
        reasons.append("sector_membership_observation_timestamp_missing")
    if int(membership_meta.get("non_target_observation_timestamp_count") or 0) > 0:
        reasons.append("sector_membership_observation_not_same_day")

    if board_path.exists():
        try:
            if target_day is not None and _source_mtime_day(board_path) != target_day:
                reasons.append("sector_board_source_not_same_day")
            rankings = _board_rows(board_path)
            sources.append(
                _source_record(
                    board_path,
                    source="industry_board_current_snapshot",
                    cutoff=_source_mtime_text(board_path),
                )
            )
        except Exception as exc:  # noqa: BLE001 - archive fails closed
            reasons.append(f"sector_board_unreadable:{type(exc).__name__}")
    else:
        reasons.append("sector_board_source_missing")
    if not rankings:
        reasons.append("sector_board_rows_missing")

    board_evidence_path = _resolve_evidence_path(root, refresh_payload.get("cache_path"))
    board_evidence_hash = str(refresh_payload.get("cache_sha256") or "")
    if (
        refresh_payload.get("ok") is not True
        or str(refresh_payload.get("status") or "") != "completed"
        or refresh_payload.get("network") is not True
        or not str(refresh_payload.get("source") or "").strip()
        or not board_path.is_file()
        or board_evidence_path != board_path.resolve()
        or board_evidence_hash != f"sha256:{_sha256_path(board_path)}"
        or not _same_day_timestamp(refresh_payload.get("captured_at"), target_day)
    ):
        reasons.append("same_day_sector_refresh_provenance_missing")

    membership_evidence_path = _resolve_evidence_path(
        root, membership_evidence.get("path")
    )
    if (
        membership_evidence.get("ok") is not True
        or str(membership_evidence.get("status") or "") != "completed"
        or not str(membership_evidence.get("source") or "").strip()
        or not theme_path.is_file()
        or membership_evidence_path != theme_path.resolve()
        or str(membership_evidence.get("sha256") or "")
        != f"sha256:{_sha256_path(theme_path)}"
        or int(membership_evidence.get("row_count") or 0)
        != int(membership_meta.get("input_count") or 0)
        or not _same_day_timestamp(membership_evidence.get("captured_at"), target_day)
    ):
        reasons.append("same_day_membership_observation_provenance_missing")

    declared_board_count = int(refresh_payload.get("board_count") or 0)
    if rankings and declared_board_count != len(rankings):
        reasons.append("sector_refresh_board_count_mismatch")

    board_names = {str(row.get("sector") or "").strip() for row in rankings}
    aligned_memberships = [
        row for row in memberships if str(row.get("sector") or "").strip() in board_names
    ]
    aligned_membership_count = len(aligned_memberships)
    alignment_ratio = (
        aligned_membership_count / len(memberships) if memberships else 0.0
    )
    if memberships and aligned_membership_count != len(memberships):
        reasons.append("sector_membership_board_taxonomy_unaligned")

    source_digest = _sha256_bytes(_canonical_bytes({"sources": sources, "override": override}))
    core = {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "task_type": "forward_pit_sector_snapshot",
        "target_date": target,
        "market_as_of": target,
        "usable_from": target,
        "captured_at": captured,
        "observation_mode": "same_day_current_cross_section_capture",
        "historical_backfill": False,
        "append_only": True,
        "canonical_write": False,
        "sector_membership": memberships,
        "sector_rankings": rankings,
        "membership_count": len(memberships),
        "membership_input_count": int(membership_meta.get("input_count") or 0),
        "membership_unknown_count": int(
            membership_meta.get("unknown_or_pending_count") or 0
        ),
        "membership_missing_observation_timestamp_count": int(
            membership_meta.get("missing_observation_timestamp_count") or 0
        ),
        "membership_non_target_observation_timestamp_count": int(
            membership_meta.get("non_target_observation_timestamp_count") or 0
        ),
        "aligned_membership_count": aligned_membership_count,
        "membership_board_alignment_ratio": round(alignment_ratio, 8),
        "aligned_sector_names": sorted(
            {str(row.get("sector") or "") for row in aligned_memberships}
        ),
        "sector_count": len(rankings),
        "reason_codes": list(dict.fromkeys(reasons)),
        "degraded_reasons": list(dict.fromkeys(reasons)),
        "provenance": {
            "source": "local_current_sector_observation",
            "captured_at": captured,
            "cutoff": target,
            "hash": f"sha256:{source_digest}",
            "sources": sources,
            "source_revision_note": (
                "Current cross-sections are sealed by hash and are valid only from "
                "this archive date forward; they are never projected backwards."
            ),
            "extra": override,
        },
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    eligible = not core["reason_codes"]
    core["ok"] = eligible
    core["status"] = "passed" if eligible else "degraded"
    core["evidence_eligible"] = eligible
    core["pit_eligible"] = eligible
    core["snapshot_path"] = str(snapshot_path.resolve())
    identity_payload = {
        "task_type": core["task_type"],
        "target_date": core["target_date"],
        "market_as_of": core["market_as_of"],
        "usable_from": core["usable_from"],
        "observation_mode": core["observation_mode"],
        "historical_backfill": core["historical_backfill"],
        "sector_membership": _content_identity_projection(memberships),
        "sector_rankings": rankings,
        "reason_codes": core["reason_codes"],
        # Do not reuse provenance["hash"] here: that audit hash seals the full
        # raw provenance, including capture times.  The identity projection
        # instead retains the underlying provider/source/path/content hashes,
        # counts and unknown extra fields while dropping only the explicit
        # volatile time keys above.
        "provenance": _content_identity_projection(
            {
                "source": core["provenance"]["source"],
                "sources": core["provenance"]["sources"],
                "extra": core["provenance"]["extra"],
            }
        ),
    }
    content_identity = _sha256_bytes(_canonical_bytes(identity_payload))
    core["content_identity"] = f"sha256:{content_identity}"
    snapshot_hash = _sha256_bytes(_canonical_bytes(core))
    core["snapshot_hash"] = f"sha256:{snapshot_hash}"

    if not eligible or not write:
        core["written"] = False
        return core

    data = _canonical_bytes(core)
    if revocation_path.exists():
        raise ValueError("sector_history_day_revoked")
    if snapshot_path.exists():
        try:
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("sector_history_same_day_conflict:existing_unreadable") from exc
        if str(existing.get("content_identity") or "") == core["content_identity"]:
            result = dict(existing)
            result["status"] = "unchanged_identical"
            result["written"] = False
            result["snapshot_path"] = str(snapshot_path.resolve())
            return result
        raise ValueError("sector_history_same_day_conflict")

    try:
        _write_once(snapshot_path, data)
    except FileExistsError:
        # A concurrent writer won the race.  Re-enter the immutable comparison.
        existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if str(existing.get("content_identity") or "") == core["content_identity"]:
            result = dict(existing)
            result["status"] = "unchanged_identical"
            result["written"] = False
            result["snapshot_path"] = str(snapshot_path.resolve())
            return result
        raise ValueError("sector_history_same_day_conflict")
    core["written"] = True
    return core


def write_sector_snapshot_revocation(
    project_root: Path,
    *,
    target_date: str,
    snapshot_sha256: str,
    reason_codes: list[str],
    audited_at: str | None = None,
) -> dict[str, Any]:
    """Append an immutable fail-closed revocation beside a bad snapshot.

    The snapshot itself is never deleted or rewritten.  Loaders treat the mere
    presence of this sidecar as disqualifying, including when the sidecar is
    unreadable, so an audit failure cannot silently become eligible again.
    """

    root = Path(project_root).resolve()
    target = str(target_date or "")[:10]
    if _parse_day(target) is None:
        raise ValueError("sector_history_revocation_date_invalid")
    history_root = root / "data" / "sector_history"
    _guard_destination(root, history_root)
    snapshot_path = history_root / target / "snapshot.json"
    revocation_path = history_root / target / "revocation.json"
    if not snapshot_path.is_file():
        raise ValueError("sector_history_revocation_snapshot_missing")
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("sector_history_revocation_snapshot_unreadable") from exc
    declared_hash = str(snapshot.get("snapshot_hash") or "")
    if not declared_hash or declared_hash != str(snapshot_sha256 or ""):
        raise ValueError("sector_history_revocation_snapshot_hash_mismatch")
    normalized_reasons = sorted(
        {str(item).strip() for item in reason_codes if str(item).strip()}
    )
    if not normalized_reasons:
        raise ValueError("sector_history_revocation_reason_missing")

    identity = {
        "task_type": "forward_pit_sector_snapshot_revocation",
        "target_date": target,
        "snapshot_hash": declared_hash,
        "reason_codes": normalized_reasons,
    }
    content_identity = f"sha256:{_sha256_bytes(_canonical_bytes(identity))}"
    payload = {
        **identity,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "status": "revoked",
        "evidence_eligible": False,
        "pit_eligible": False,
        "append_only": True,
        "canonical_write": False,
        "audited_at": str(audited_at or now_text()),
        "snapshot_path": str(snapshot_path.resolve()),
        "snapshot_file_sha256": f"sha256:{_sha256_path(snapshot_path)}",
        "content_identity": content_identity,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    data = _canonical_bytes(payload)
    if revocation_path.exists():
        try:
            existing = json.loads(revocation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("sector_history_revocation_conflict:existing_unreadable") from exc
        if str(existing.get("content_identity") or "") == content_identity:
            result = dict(existing)
            result["status"] = "unchanged_identical"
            result["written"] = False
            result["revocation_path"] = str(revocation_path.resolve())
            return result
        raise ValueError("sector_history_revocation_conflict")
    try:
        _write_once(revocation_path, data)
    except FileExistsError:
        existing = json.loads(revocation_path.read_text(encoding="utf-8"))
        if str(existing.get("content_identity") or "") == content_identity:
            result = dict(existing)
            result["status"] = "unchanged_identical"
            result["written"] = False
            result["revocation_path"] = str(revocation_path.resolve())
            return result
        raise ValueError("sector_history_revocation_conflict")
    payload["written"] = True
    payload["revocation_path"] = str(revocation_path.resolve())
    return payload


def verify_snapshot_self_hash(snapshot: Mapping[str, Any]) -> bool:
    """Return True when ``snapshot_hash`` still seals the snapshot's own bytes.

    Recovery/reuse paths must never trust a declared hash: an archived day is
    only identity-verified when recomputing the canonical projection over every
    remaining field reproduces the declared digest.
    """

    declared = str(snapshot.get("snapshot_hash") or "")
    if not declared.startswith("sha256:"):
        return False
    core = {
        key: value for key, value in snapshot.items() if key != "snapshot_hash"
    }
    try:
        recomputed = _sha256_bytes(_canonical_bytes(core))
    except (TypeError, ValueError):
        return False
    return declared == f"sha256:{recomputed}"


def read_sector_membership_as_of(history_root: Path, *, as_of: str) -> dict[str, Any]:
    """Expose last known membership without upgrading it to a current capture."""
    root = Path(history_root)
    requested = _parse_day(as_of)
    context: dict[str, Any] = {"requested_as_of": as_of, "snapshot_date": None,
        "observed_at": None, "age_calendar_days": None, "freshness": "missing",
        "evidence_kind": "missing", "eligible_for_requested_day": False,
        "source_path": None, "snapshot_hash": None, "reason_codes": []}
    if requested is None or not root.exists():
        return {"snapshot": {}, "observation": context}
    candidates = sorted(((_parse_day(path.parent.name), path) for path in root.glob("*/snapshot.json")
        if _parse_day(path.parent.name) is not None and _parse_day(path.parent.name) <= requested), reverse=True)
    rejected: list[str] = []
    for snapshot_day, path in candidates:
        reason = ""
        if (path.parent / "revocation.json").exists():
            rejected.append(f"{snapshot_day}:revoked")
            continue
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("snapshot_too_large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not verify_snapshot_self_hash(payload):
                raise ValueError("snapshot_hash_unverified")
            if (_parse_day(payload.get("target_date")) != snapshot_day
                    or _parse_day(payload.get("usable_from")) != snapshot_day
                    or _parse_day(payload.get("captured_at")) != snapshot_day):
                raise ValueError("snapshot_date_conflict")
            if payload.get("evidence_eligible") is not True or payload.get("status") != "passed":
                raise ValueError("snapshot_not_eligible")
            rows = payload.get("sector_membership")
            if not isinstance(rows, list) or not rows:
                raise ValueError("membership_missing")
            symbols: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("membership_row_invalid")
                symbol = str(row.get("symbol") or "")
                observed = _parse_day(row.get("observed_at"))
                effective = _parse_day(row.get("effective_from"))
                if (not symbol or symbol in symbols or observed is None or effective is None
                        or observed != snapshot_day or effective > snapshot_day):
                    raise ValueError("membership_ambiguous_or_future")
                symbols.add(symbol)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        if reason:
            rejected.append(f"{snapshot_day}:{reason}")
            continue
        same_day = snapshot_day == requested
        context.update(snapshot_date=snapshot_day.isoformat(), observed_at=payload.get("captured_at"),
            age_calendar_days=(requested - snapshot_day).days,
            freshness="same_day" if same_day else "prior_snapshot",
            evidence_kind="same_day_observation" if same_day else "last_known_membership_proxy",
            eligible_for_requested_day=same_day, source_path=str(path.resolve()),
            snapshot_hash=payload["snapshot_hash"], reason_codes=[*rejected,
                *([] if same_day else ["membership_not_observed_on_requested_day", "intervening_membership_changes_unknown"])])
        return {"snapshot": payload, "observation": context}
    context.update(freshness="blocked" if rejected else "missing", reason_codes=rejected)
    return {"snapshot": {}, "observation": context}


def load_forward_sector_snapshot(
    history_root: Path,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Load the newest eligible archived snapshot whose usable date is <= as_of."""

    loaded = read_sector_membership_as_of(history_root, as_of=as_of)
    payload, observation = loaded["snapshot"], loaded["observation"]
    if not payload:
        return {}
    # Return a view, leaving the sealed original object accessible and unedited
    # through read_sector_membership_as_of for callers needing hash verification.
    view = {**payload, "loaded_from": observation["source_path"], "as_of_observation": observation}
    if not observation["eligible_for_requested_day"]:
        view.update(status="partial", evidence_eligible=False, pit_eligible=False,
                    sector_rankings=[], evidence_kind="last_known_membership_proxy")
    return view


__all__ = [
    "archive_forward_sector_snapshot",
    "capture_current_sector_membership",
    "load_forward_sector_snapshot",
    "read_sector_membership_as_of",
    "write_sector_snapshot_revocation",
]
