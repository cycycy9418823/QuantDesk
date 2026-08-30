"""Point-in-time (PIT) eligibility audit for staged sector/industry sources.

Round 6 · Part B2.  The Stage 4 replay needs, for every ``(symbol, date)``, the
sector/industry membership **as it was known on or before that date**.  This
module audits the local ``data/vendor_staging`` packs that could conceivably
supply that and grades each source with a strict source/hash/cutoff triple and a
three-level verdict:

* ``pit_eligible``  — the source carries dated membership (an as-of / effective
  date per row) that lets a ``(symbol, date)`` lookup resolve to a record whose
  effective date is ``<= date``; provenance triple is complete.
* ``pit_uncertain`` — the source has *some* date dimension but it cannot be
  proven to be point-in-time (e.g. a single snapshot date shared by all rows, or
  a mutable ``updated_at`` write-timestamp), so using it for an earlier replay
  date risks look-ahead / silent revision.
* ``not_pit``       — the source is a pure current cross-section with no usable
  effective-date dimension, or the PIT-shaped columns exist but are empty.

Read-only.  No network, no import, no canonical write.  It never certifies data
as importable; the strongest verdict is ``pit_eligible`` for a *staging* file,
which a human must still review before any ``--apply`` (Round 6 B3 stop point).

Boundary: Research Only / Paper Account Only / No Broker Route / not advice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_update_staging import atomic_write_text
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENDOR_STAGING_DIR = PROJECT_ROOT / "data" / "vendor_staging"
RESULTS_DIR = PROJECT_ROOT / "results" / "data_health"
REPORTS_DIR = PROJECT_ROOT / "reports" / "pit_sector_evidence"
DEFAULT_OUTPUT_PATH = RESULTS_DIR / "pit_sector_evidence_audit_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "pit_sector_evidence_audit_latest.md"

DISCLAIMER = (
    "仅用于本地暂存数据源的 PIT 资格审计，不接券商、不真实下单、不构成投资建议；"
    "暂存数据不是正式策略证据，任何导入前必须人工复核并由用户确认（Round 6 B3 停止点）。"
)

VERDICT_ELIGIBLE = "pit_eligible"
VERDICT_UNCERTAIN = "pit_uncertain"
VERDICT_NOT_PIT = "not_pit"
PIT_SECTOR_EVIDENCE_PAYLOAD_SCHEMA_VERSION = 2
_MISSING_TARGET_SAMPLE_EDGE_SIZE = 32

# Columns that would carry a per-row point-in-time effective date for sector /
# index membership.  ``in_date``/``out_date`` is the Tushare ths_member shape;
# ``trade_date`` is the dated index-weight shape; the others are defensive.
_EFFECTIVE_DATE_COLUMNS = (
    "in_date",
    "out_date",
    "trade_date",
    "effective_date",
    "as_of_date",
    "start_date",
    "end_date",
)
# A single mutable write-timestamp is NOT a PIT dimension.
_WRITE_TIMESTAMP_COLUMNS = ("updated_at", "fetched_at", "captured_at", "updatedate")
# Columns that indicate the row actually carries sector/industry membership.
_MEMBERSHIP_COLUMNS = ("industry", "sector", "con_code", "con_name", "index_code")


def _serialize_missing_targets(symbols: Sequence[str]) -> dict[str, Any]:
    """Return bounded metadata for the complete canonical missing-symbol set."""

    complete = sorted(
        {
            normalized
            for item in symbols
            if (normalized := normalize_symbol(item))
        }
    )
    truncated = len(complete) > _MISSING_TARGET_SAMPLE_EDGE_SIZE * 2
    sample = (
        complete
        if not truncated
        else complete[:_MISSING_TARGET_SAMPLE_EDGE_SIZE]
        + complete[-_MISSING_TARGET_SAMPLE_EDGE_SIZE:]
    )
    digest = hashlib.sha256("\n".join(complete).encode("utf-8")).hexdigest()
    return {
        "target_symbols_missing_count": len(complete),
        "target_symbols_missing_sha256": f"sha256:{digest}",
        "target_symbols_missing_truncated": truncated,
        "target_symbols_missing": sample,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("/", "-").replace(".", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass
class SectorSourceAudit:
    """PIT verdict for one staged sector/industry source file."""

    path: str
    provider: str
    kind: str
    verdict: str
    row_count: int
    membership_columns: list[str] = field(default_factory=list)
    effective_date_columns: list[str] = field(default_factory=list)
    write_timestamp_columns: list[str] = field(default_factory=list)
    populated_effective_date_columns: dict[str, int] = field(default_factory=dict)
    distinct_effective_dates: int = 0
    effective_date_min: str = ""
    effective_date_max: str = ""
    reason_codes: list[str] = field(default_factory=list)
    source_hash: str = ""
    cutoff: str = ""
    covered_symbol_count: int = 0
    window_start_covered_count: int = 0
    window_end_covered_count: int = 0
    window_fully_covered: bool = False
    target_symbol_count: int = 0
    target_symbols_missing: list[str] = field(default_factory=list)
    source_revision_status: str = "not_assessed"
    source_revision_basis: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "provider": self.provider,
            "kind": self.kind,
            "verdict": self.verdict,
            "row_count": self.row_count,
            "membership_columns": list(self.membership_columns),
            "effective_date_columns": list(self.effective_date_columns),
            "write_timestamp_columns": list(self.write_timestamp_columns),
            "populated_effective_date_columns": dict(self.populated_effective_date_columns),
            "distinct_effective_dates": self.distinct_effective_dates,
            "effective_date_min": self.effective_date_min,
            "effective_date_max": self.effective_date_max,
            "reason_codes": list(self.reason_codes),
            "provenance_triple": {
                "source_path": self.path,
                "source_hash": self.source_hash,
                "cutoff": self.cutoff,
            },
            "covered_symbol_count": self.covered_symbol_count,
            "window_start_covered_count": self.window_start_covered_count,
            "window_end_covered_count": self.window_end_covered_count,
            "window_fully_covered": self.window_fully_covered,
            "target_symbol_count": self.target_symbol_count,
            "source_revision_status": self.source_revision_status,
            "source_revision_basis": self.source_revision_basis,
            "notes": self.notes,
        }
        payload.update(_serialize_missing_targets(self.target_symbols_missing))
        return payload


def _classify_provider_kind(path: Path) -> tuple[str, str]:
    text = str(path).replace("\\", "/").lower()
    name = path.name.lower()
    if "ths_member" in text:
        return "tushare_ths", "index_member_history"
    if "ths_daily" in text:
        return "tushare_ths", "index_ohlcv_series"
    if "index_weight" in text:
        return "tushare_ths", "index_weight_history"
    if "industry_classification" in name:
        return "baostock", "industry_classification_snapshot"
    if "theme_map" in name or "industry_theme" in name:
        return "local_theme_map", "sector_cross_section"
    if "ths_index" in name:
        return "tushare_ths", "index_catalog"
    return "unknown", "unknown"


def _source_cutoff(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _first_column(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    return next((columns[item] for item in candidates if item in columns), None)


def _membership_window_coverage(
    frame: pd.DataFrame,
    columns: dict[str, str],
    *,
    kind: str,
    replay_window_start: str,
    replay_window_end: str,
    target_symbols: Sequence[str] | None,
) -> tuple[int, int, int, bool, list[str]]:
    """Return strict per-symbol coverage for the requested replay window."""

    window_start = _parse_day(replay_window_start)
    window_end = _parse_day(replay_window_end)
    if window_start is None or window_end is None or window_start > window_end:
        return 0, 0, 0, False, []
    symbol_candidates = (
        ("con_code", "symbol", "code")
        if kind == "index_member_history"
        else ("symbol", "con_code", "code", "ts_code")
    )
    symbol_col = _first_column(columns, symbol_candidates)
    start_col = _first_column(
        columns,
        ("in_date", "effective_date", "as_of_date", "start_date", "trade_date"),
    )
    end_col = _first_column(columns, ("out_date", "end_date"))
    if symbol_col is None or start_col is None:
        return 0, 0, 0, False, []

    targets = sorted(
        {
            normalize_symbol(item)
            for item in (target_symbols or [])
            if normalize_symbol(item)
        }
    )

    intervals: dict[str, list[tuple[date, date | None]]] = {}
    for _, row in frame.iterrows():
        symbol = normalize_symbol(row.get(symbol_col))
        started = _parse_day(row.get(start_col))
        ended = _parse_day(row.get(end_col)) if end_col else None
        if not symbol or started is None or (ended is not None and ended < started):
            continue
        intervals.setdefault(symbol, []).append((started, ended))

    start_covered = 0
    end_covered = 0
    full_covered = 0
    symbols_to_check = targets or sorted(intervals)
    missing = [symbol for symbol in targets if symbol not in intervals]
    for symbol in symbols_to_check:
        rows = intervals.get(symbol, [])
        covers_start = any(started <= window_start and (ended is None or ended >= window_start) for started, ended in rows)
        covers_end = any(started <= window_end and (ended is None or ended >= window_end) for started, ended in rows)
        if covers_start:
            start_covered += 1
        if covers_end:
            end_covered += 1
        if any(started <= window_start and (ended is None or ended >= window_end) for started, ended in rows):
            full_covered += 1
    symbol_count = len(intervals)
    target_count = len(targets)
    fully_covered = bool(target_count and full_covered == target_count and not missing)
    return symbol_count, start_covered, end_covered, fully_covered, missing


def audit_sector_source(
    path: Path,
    *,
    replay_window_start: str = "2026-06-22",
    replay_window_end: str = "2026-07-17",
    target_symbols: Sequence[str] | None = None,
) -> SectorSourceAudit:
    """Grade one staged sector/industry file for PIT eligibility."""

    provider, kind = _classify_provider_kind(path)
    try:
        frame = read_csv_frame(path, dtype=str)
    except Exception as exc:  # noqa: BLE001 - fail closed on unreadable staging
        return SectorSourceAudit(
            path=_display_path(path),
            provider=provider,
            kind=kind,
            verdict=VERDICT_NOT_PIT,
            row_count=0,
            reason_codes=[f"unreadable_staging_file:{type(exc).__name__}"],
            source_hash=f"sha256:{_sha256(path)}" if path.exists() else "",
            cutoff=_source_cutoff(path),
            source_revision_status="unavailable",
            source_revision_basis=(
                "The staged file could not be read; revision history and PIT "
                "semantics cannot be established."
            ),
        )

    columns = {str(c).strip().lower(): str(c) for c in frame.columns}
    membership_cols = [orig for low, orig in columns.items() if low in _MEMBERSHIP_COLUMNS]
    effective_cols = [orig for low, orig in columns.items() if low in _EFFECTIVE_DATE_COLUMNS]
    write_ts_cols = [orig for low, orig in columns.items() if low in _WRITE_TIMESTAMP_COLUMNS]

    audit = SectorSourceAudit(
        path=_display_path(path),
        provider=provider,
        kind=kind,
        verdict=VERDICT_NOT_PIT,
        row_count=int(len(frame)),
        membership_columns=membership_cols,
        effective_date_columns=effective_cols,
        write_timestamp_columns=write_ts_cols,
        source_hash=f"sha256:{_sha256(path)}",
        cutoff=_source_cutoff(path),
        source_revision_status="not_proven",
        source_revision_basis=(
            "The staged artifact has no immutable version/revision chain or "
            "provider response manifest proving that historical rows were not "
            "retroactively revised."
        ),
    )

    # How many rows actually populate each effective-date column?
    populated: dict[str, int] = {}
    all_days: list[date] = []
    for col in effective_cols:
        series = frame[col].fillna("").astype(str).str.strip()
        nonblank = series[series != ""]
        populated[col] = int(len(nonblank))
        for value in nonblank.tolist():
            day = _parse_day(value)
            if day is not None:
                all_days.append(day)
    audit.populated_effective_date_columns = populated
    if all_days:
        audit.distinct_effective_dates = len(set(all_days))
        audit.effective_date_min = min(all_days).isoformat()
        audit.effective_date_max = max(all_days).isoformat()
    normalized_targets = sorted(
        {
            normalize_symbol(item)
            for item in (target_symbols or [])
            if normalize_symbol(item)
        }
    )
    (
        audit.covered_symbol_count,
        audit.window_start_covered_count,
        audit.window_end_covered_count,
        audit.window_fully_covered,
        audit.target_symbols_missing,
    ) = _membership_window_coverage(
        frame,
        columns,
        kind=kind,
        replay_window_start=replay_window_start,
        replay_window_end=replay_window_end,
        target_symbols=normalized_targets,
    )
    audit.target_symbol_count = len(normalized_targets)

    has_membership = bool(membership_cols)
    # An effective-date column only counts if it is actually populated.
    populated_effective = [col for col, n in populated.items() if n > 0]
    reasons: list[str] = []

    if not has_membership:
        reasons.append("no_sector_membership_columns")
    if kind == "index_member_history" and not normalized_targets:
        reasons.append("target_replay_universe_missing")
    if not effective_cols:
        reasons.append("no_effective_date_columns")
    elif not populated_effective:
        reasons.append("effective_date_columns_present_but_empty")

    # Verdict logic.
    if kind == "index_ohlcv_series":
        # ths_daily is sector-INDEX price/heat, not per-symbol membership.
        audit.verdict = VERDICT_NOT_PIT
        reasons.append("index_ohlcv_not_membership")
    elif kind == "index_catalog":
        audit.verdict = VERDICT_NOT_PIT
        reasons.append("index_catalog_not_membership")
    elif not has_membership:
        audit.verdict = VERDICT_NOT_PIT
    elif populated_effective and audit.distinct_effective_dates > 1:
        # Genuinely dated membership with more than one effective date.
        if kind == "index_weight_history":
            # Real trade_date history, but broad-index constituents, not
            # industry/sector classification of a stock.
            audit.verdict = VERDICT_UNCERTAIN
            reasons.append("dated_but_broad_index_weight_not_industry_classification")
        elif audit.window_fully_covered:
            audit.verdict = VERDICT_UNCERTAIN
            reasons.append("source_revision_not_proven")
            reasons.append("verified_membership_heat_bundle_missing")
        else:
            audit.verdict = VERDICT_UNCERTAIN
            reasons.append("dated_membership_does_not_cover_replay_window")
    elif populated_effective and audit.distinct_effective_dates == 1:
        # A single shared effective/update date = one snapshot, not a history.
        audit.verdict = VERDICT_UNCERTAIN
        reasons.append("single_snapshot_date_not_a_history")
    elif effective_cols and not populated_effective:
        # PIT-shaped columns exist but are 100% empty (Tushare ths_member).
        audit.verdict = VERDICT_NOT_PIT
        reasons.append("pit_columns_empty")
    else:
        audit.verdict = VERDICT_NOT_PIT

    # A mutable write-timestamp is never a PIT effective date.
    if write_ts_cols and not populated_effective:
        reasons.append("only_mutable_write_timestamp_present")

    if kind == "industry_classification_snapshot" and write_ts_cols:
        # BaoStock's updateDate is a single current snapshot date.  It may be a
        # useful dated observation, but it cannot prove historical membership
        # or revision stability for the whole replay window.
        audit.verdict = VERDICT_UNCERTAIN
        reasons.append("single_snapshot_updatedate_not_pit_history")

    audit.reason_codes = list(dict.fromkeys(reasons))
    if write_ts_cols and not populated_effective:
        audit.source_revision_basis = (
            "Only a mutable write timestamp is present; it cannot establish "
            "the historical effective interval or protect against retroactive "
            "revision."
        )
    elif audit.verdict == VERDICT_ELIGIBLE:
        audit.source_revision_basis = (
            "Effective-date intervals cover the requested window, but an "
            "immutable provider revision/version chain is still not proven; "
            "human review remains mandatory before --apply."
        )
    return audit


def _discover_sector_sources(staging_dir: Path) -> list[Path]:
    """Find staged files that could conceivably carry sector/industry data."""

    if not staging_dir.exists():
        return []
    patterns = [
        "**/ths_member/*.csv",
        "**/ths_daily/*.csv",
        "**/index_weight/**/*.csv",
        "**/industry_classification.csv",
        "**/ths_index.csv",
        "**/*industry_theme*.csv",
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path in sorted(staging_dir.glob(pattern)):
            key = str(path).lower()
            if path.is_file() and key not in seen:
                seen.add(key)
                found.append(path)
    return found


def _sample_paths(paths: list[Path], per_group: int = 3) -> list[Path]:
    """Sample up to ``per_group`` files per (provider, kind) to bound the scan.

    Membership files are numerous (one per index), but their populated dates and
    symbol coverage are content properties, so every membership candidate is
    audited.  Only non-membership evidence is sampled; the policy is recorded in
    the report so coverage remains explicit.
    """

    grouped: dict[tuple[str, str], list[Path]] = {}
    for path in paths:
        grouped.setdefault(_classify_provider_kind(path), []).append(path)
    sampled: list[Path] = []
    for (_, kind), group_paths in grouped.items():
        # Effective-date population and per-symbol coverage are content
        # properties, not schema properties.  Membership candidates therefore
        # receive an exhaustive scan; only non-membership evidence is sampled.
        sampled.extend(
            group_paths if kind == "index_member_history" else group_paths[:per_group]
        )
    return sampled


def run_pit_sector_evidence_audit(
    *,
    staging_dir: Path = VENDOR_STAGING_DIR,
    replay_window_start: str = "2026-06-22",
    replay_window_end: str = "2026-07-17",
    sample_per_group: int = 3,
    target_symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Audit staged sector sources and return an overall PIT eligibility verdict."""

    all_sources = _discover_sector_sources(staging_dir)
    sampled = _sample_paths(all_sources, per_group=sample_per_group)
    audits = [
        audit_sector_source(
            path,
            replay_window_start=replay_window_start,
            replay_window_end=replay_window_end,
            target_symbols=target_symbols,
        ).to_dict()
        for path in sampled
    ]

    verdict_counts = {VERDICT_ELIGIBLE: 0, VERDICT_UNCERTAIN: 0, VERDICT_NOT_PIT: 0}
    for item in audits:
        verdict_counts[item["verdict"]] = verdict_counts.get(item["verdict"], 0) + 1

    # A per-file audit can never authorize import by itself.  Formal PIT use
    # requires a separately verified bundle covering the complete target
    # universe and pairing membership with same-taxonomy dated heat/rotation.
    eligible_sources = [a for a in audits if a["verdict"] == VERDICT_ELIGIBLE]
    window_covered = False

    eligible_window_sources = [
        item
        for item in eligible_sources
        if item.get("window_fully_covered") is True
        and (item.get("provenance_triple") or {}).get("source_path")
        and str((item.get("provenance_triple") or {}).get("source_hash") or "").startswith(
            "sha256:"
        )
        and (item.get("provenance_triple") or {}).get("cutoff")
    ]
    eligible_paths: list[Path] = []
    for item in eligible_window_sources:
        raw_path = Path(str((item.get("provenance_triple") or {}).get("source_path") or ""))
        candidate = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
        if candidate.is_file():
            eligible_paths.append(candidate)
    if eligible_paths:
        proposed_scope = {
            "directories": sorted({_display_path(path.parent) for path in eligible_paths}),
            "files": len(eligible_paths),
            "bytes": sum(path.stat().st_size for path in eligible_paths),
            "date_start": replay_window_start,
            "date_end": replay_window_end,
            "eligible_source_paths": [_display_path(path) for path in sorted(eligible_paths)],
            "reason": "pit_eligible_sources_require_explicit_mid_round_approval",
        }
    else:
        proposed_scope = {
            "directories": [],
            "files": 0,
            "bytes": 0,
            "date_start": "",
            "date_end": "",
            "eligible_source_paths": [],
            "reason": "no_pit_eligible_source_covers_replay_window",
        }

    overall = VERDICT_NOT_PIT
    if verdict_counts[VERDICT_UNCERTAIN] > 0 or eligible_sources:
        overall = VERDICT_UNCERTAIN

    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "payload_schema_version": PIT_SECTOR_EVIDENCE_PAYLOAD_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "pit_sector_evidence_audit",
        "generated_at": now_text(),
        "staging_dir": _display_path(staging_dir),
        "replay_window_start": replay_window_start,
        "replay_window_end": replay_window_end,
        "target_universe": {
            "symbol_count": len(
                {
                    normalize_symbol(item)
                    for item in (target_symbols or [])
                    if normalize_symbol(item)
                }
            ),
            "symbols_sha256": (
                "sha256:"
                + hashlib.sha256(
                    "\n".join(
                        sorted(
                            {
                                normalize_symbol(item)
                                for item in (target_symbols or [])
                                if normalize_symbol(item)
                            }
                        )
                    ).encode("utf-8")
                ).hexdigest()
                if target_symbols
                else ""
            ),
        },
        "discovered_source_count": len(all_sources),
        "audited_source_count": len(sampled),
        "sample_per_group": sample_per_group,
        "sampling_note": (
            "Membership-history candidates are audited exhaustively because date "
            "population and symbol coverage are content properties. Other source "
            "kinds are sampled up to N files per (provider, kind)."
        ),
        "overall_verdict": overall,
        "overall_window_covered": window_covered,
        "verdict_counts": verdict_counts,
        "sources": audits,
        "bundle_gate": {
            "verified_bundle_manifest_present": False,
            "membership_heat_pair_verified": False,
            "target_universe_fully_covered": False,
            "revision_immutability_proven": False,
            "reason_codes": [
                "verified_membership_heat_bundle_missing",
                "source_revision_not_proven",
            ],
        },
        "apply_boundary": {
            "approval_required": True,
            "approved": False,
            "apply_flag": "--apply",
            "canonical_write": False,
            "proposed_scope": proposed_scope,
            "stop_reason": (
                "Round 6 B3 is a hard stop: staging/audit are read-only and "
                "canonical import requires explicit mid-round acceptance."
            ),
        },
        "pit_standard": (
            "For each (symbol, date), sector/industry membership must come from a "
            "source record whose effective date is <= date; source_path + "
            "source_hash + cutoff triple must be complete; a single snapshot date "
            "or a mutable write-timestamp is not PIT."
        ),
        "canonical_write": False,
        "network_used": False,
        "read_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    return payload


def _render_report(payload: dict[str, Any]) -> str:
    rows = list(payload.get("sources") or [])
    apply_boundary = dict(payload.get("apply_boundary") or {})
    proposed_scope = dict(apply_boundary.get("proposed_scope") or {})
    scope_start = str(proposed_scope.get("date_start") or "")
    scope_end = str(proposed_scope.get("date_end") or "")
    scope_date_range = f"{scope_start}..{scope_end}" if scope_start and scope_end else "empty"
    lines = [
        "# Round 6 Part B2 - PIT Sector Evidence Audit",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- payload_schema_version: `{payload.get('payload_schema_version') or '-'}`",
        f"- replay_window: `{payload.get('replay_window_start')}` .. `{payload.get('replay_window_end')}`",
        f"- overall_verdict: `{payload.get('overall_verdict')}`",
        f"- overall_window_covered: `{payload.get('overall_window_covered')}`",
        f"- discovered/audited: `{payload.get('discovered_source_count')}/{payload.get('audited_source_count')}`",
        "- canonical_write: `false`",
        "",
        "| Provider | Kind | Verdict | Rows | Effective range | Window covered | Missing count | Truncated | Missing sample | Revision evidence | Source/hash/cutoff | Reasons |",
        "|---|---|---|---:|---|---|---:|---|---|---|---|---|",
    ]
    for row in rows:
        provenance = dict(row.get("provenance_triple") or {})
        missing_sample = ", ".join(
            str(item) for item in row.get("target_symbols_missing") or []
        ) or "-"
        triple = " / ".join(
            [
                str(provenance.get("source_path") or "-"),
                str(provenance.get("source_hash") or "-")[:24],
                str(provenance.get("cutoff") or "-"),
            ]
        )
        lines.append(
            "| {provider} | {kind} | {verdict} | {rows} | {start}..{end} | {covered} | {missing_count} | {truncated} | {missing_sample} | {revision} | {triple} | {reasons} |".format(
                provider=row.get("provider") or "-",
                kind=row.get("kind") or "-",
                verdict=row.get("verdict") or "-",
                rows=int(row.get("row_count") or 0),
                start=row.get("effective_date_min") or "-",
                end=row.get("effective_date_max") or "-",
                covered=row.get("window_fully_covered") is True,
                missing_count=int(row.get("target_symbols_missing_count") or 0),
                truncated=row.get("target_symbols_missing_truncated") is True,
                missing_sample=missing_sample.replace("|", "/"),
                revision=(
                    f"{row.get('source_revision_status') or '-'}: "
                    f"{row.get('source_revision_basis') or '-'}"
                ).replace("|", "/"),
                triple=triple.replace("|", "/"),
                reasons=", ".join(str(item) for item in row.get("reason_codes") or []) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Apply Boundary",
            "",
            "This audit is read-only. No staged source is imported by this command. "
            "Round 6 B3 requires an explicit mid-round approval before any --apply; "
            "without a PIT-eligible source covering the full replay window, the "
            "proposed apply scope is zero files.",
            "",
            f"- approval_required: `{bool(apply_boundary.get('approval_required'))}`",
            f"- approved: `{bool(apply_boundary.get('approved'))}`",
            f"- files: `{int(proposed_scope.get('files') or 0)}`",
            f"- bytes: `{int(proposed_scope.get('bytes') or 0)}`",
            f"- date_range: `{scope_date_range}`",
            f"- reason: `{proposed_scope.get('reason') or '-'}`",
            "",
            str(payload.get("disclaimer") or DISCLAIMER),
        ]
    )
    return "\n".join(lines) + "\n"


def write_pit_sector_evidence_audit_outputs(
    payload: dict[str, Any],
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    """Persist deterministic B2 evidence without touching staging/canonical data."""

    output = Path(output_path)
    report = Path(report_path)
    serialized = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
    serialized["output_path"] = str(output.resolve())
    serialized["report_path"] = str(report.resolve())
    atomic_write_json(output, serialized)
    atomic_write_text(report, _render_report(serialized), encoding="utf-8")
    return serialized


__all__ = [
    "VERDICT_ELIGIBLE",
    "VERDICT_UNCERTAIN",
    "VERDICT_NOT_PIT",
    "SectorSourceAudit",
    "audit_sector_source",
    "run_pit_sector_evidence_audit",
    "write_pit_sector_evidence_audit_outputs",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_REPORT_PATH",
]
