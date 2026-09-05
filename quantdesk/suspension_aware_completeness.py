"""Three-way completeness for a target trade date.

The daily-data gate used to be binary: a symbol either had a bar for the target
date or it was a gap. Suspended symbols fit neither. A suspended day genuinely
has no trading, so writing a bar for it would be fabricating a price - and this
repository's own history proves the convention: 600929 carries 2,043 historical
bars and, before tonight's bug, not one of them had zero volume.

Leaving them counted as gaps is equally wrong: the reconciliation could never
report `completed`, so the operator would see a permanent false "incomplete".

    has a bar            -> complete
    verified suspended   -> verified exclusion (counts as complete)
    anything else        -> gap, retry tomorrow

The suspension evidence is produced by the fetch side, once per target date, at
``data/metadata/suspension_evidence/<YYYYMMDD>.json``. This module only consumes
it. It never fetches, never writes a bar, and never invents an exclusion: a
symbol absent from the evidence file stays a gap.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_RELATIVE = Path("data/metadata/suspension_evidence")
DAILY_RELATIVE = Path("data/daily")
UNIVERSE_RELATIVE = Path("data/universe/a_share_symbols.csv")

# Signatures the fetch side records. Kept as data, not hard-coded logic, so a new
# upstream shape is a one-line addition rather than a rewrite.
KNOWN_SIGNATURES = (
    "zero_trade_with_estimates",  # close=0, volume=0, valuation present
    "zero_volume_with_last_close",  # close=last traded price, volume=0
)


@dataclass(frozen=True)
class SuspensionEvidence:
    """Parsed evidence for one target date."""

    target_date: str
    verified_exclusions: dict[str, dict[str, Any]] = field(default_factory=dict)
    flagged_for_review: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = ""
    generated_at: str = ""
    contract: str = ""
    present: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def excluded_symbols(self) -> frozenset[str]:
        return frozenset(self.verified_exclusions)

    @property
    def review_symbols(self) -> frozenset[str]:
        return frozenset(self.flagged_for_review)


def _as_symbol_map(value: Any) -> dict[str, dict[str, Any]]:
    """Accept either a mapping keyed by symbol or a list of rows.

    The fetch side currently emits a mapping. A list of ``{"symbol": ...}`` rows
    is accepted too so a future format change does not silently produce an empty
    exclusion set - which would turn every suspended symbol back into a gap.
    """
    if isinstance(value, Mapping):
        return {str(k): (dict(v) if isinstance(v, Mapping) else {"value": v}) for k, v in value.items()}
    if isinstance(value, list):
        out: dict[str, dict[str, Any]] = {}
        for item in value:
            if isinstance(item, Mapping):
                sym = str(item.get("symbol") or item.get("code") or "").strip()
                if sym:
                    out[sym] = dict(item)
            elif isinstance(item, str) and item.strip():
                out[item.strip()] = {}
        return out
    return {}


def evidence_path(target_date: str, *, project_root: Path | None = None) -> Path:
    root = Path(project_root or PROJECT_ROOT)
    compact = str(target_date).replace("-", "")
    return root / EVIDENCE_RELATIVE / f"{compact}.json"


def load_suspension_evidence(target_date: str, *, project_root: Path | None = None) -> SuspensionEvidence:
    """Read one date's evidence. A missing file is reported, never guessed at."""
    path = evidence_path(target_date, project_root=project_root)
    if not path.is_file():
        return SuspensionEvidence(
            target_date=str(target_date),
            present=False,
            warnings=(f"suspension_evidence_missing:{path.as_posix()}",),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SuspensionEvidence(
            target_date=str(target_date),
            present=False,
            warnings=(f"suspension_evidence_unreadable:{type(exc).__name__}",),
        )
    if not isinstance(payload, dict):
        return SuspensionEvidence(
            target_date=str(target_date),
            present=False,
            warnings=("suspension_evidence_not_an_object",),
        )

    warnings: list[str] = []
    declared = str(payload.get("target_date") or "").strip()
    if declared and declared != str(target_date):
        # Consuming another date's evidence would exclude the wrong symbols.
        return SuspensionEvidence(
            target_date=str(target_date),
            present=False,
            warnings=(f"suspension_evidence_date_mismatch:{declared}",),
        )

    verified = _as_symbol_map(payload.get("verified_exclusions"))
    flagged = _as_symbol_map(payload.get("flagged_for_review"))

    overlap = sorted(set(verified) & set(flagged))
    if overlap:
        # Ambiguous status must not silently resolve to "complete".
        for sym in overlap:
            verified.pop(sym, None)
        warnings.append("suspension_evidence_overlap_demoted_to_gap:" + ",".join(overlap))

    unknown = sorted(sym for sym, meta in verified.items() if str(meta.get("signature") or "") not in KNOWN_SIGNATURES)
    if unknown:
        warnings.append("suspension_signature_unknown:" + ",".join(unknown[:12]))
        for sym in unknown:
            flagged.setdefault(sym, verified.pop(sym))

    return SuspensionEvidence(
        target_date=str(target_date),
        verified_exclusions=verified,
        flagged_for_review=flagged,
        source=str(payload.get("source") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        contract=str(payload.get("contract") or ""),
        present=True,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Bar reading
# ---------------------------------------------------------------------------


def _last_date_of(path: Path) -> str:
    """Last non-empty row's date, read from the file tail."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = [ln for ln in tail.splitlines() if ln.strip()]
    if not rows:
        return ""
    return rows[-1].split(",")[0].strip().lstrip("﻿")


def _has_bar_for(path: Path, target_date: str) -> bool:
    """True when the file's last row is the target date.

    Only the last row is examined on purpose: canonical daily files are
    append-only and chronological, so a target-date bar anywhere but the tail
    would itself be a corruption worth surfacing elsewhere.
    """
    return _last_date_of(path) == str(target_date)


def _load_universe(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                sym = str(row.get("symbol") or "").strip()
                if sym:
                    out.append(sym)
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# The three-way verdict
# ---------------------------------------------------------------------------


def classify_universe_completeness(
    target_date: str,
    *,
    project_root: Path | None = None,
    universe_symbols: list[str] | None = None,
    evidence: SuspensionEvidence | None = None,
) -> dict[str, Any]:
    """Split the universe into complete / verified-excluded / gap.

    ``complete`` is only claimed when every symbol lands in one of the first two
    buckets AND the evidence file was actually present. A missing evidence file
    keeps suspended symbols in ``gap_symbols``, so a broken fetch side can never
    be mistaken for a clean day.
    """
    root = Path(project_root or PROJECT_ROOT)
    ev = evidence if evidence is not None else load_suspension_evidence(target_date, project_root=root)
    symbols = list(universe_symbols) if universe_symbols is not None else _load_universe(root / UNIVERSE_RELATIVE)
    daily_dir = root / DAILY_RELATIVE

    with_bar: list[str] = []
    excluded: list[str] = []
    gaps: list[str] = []
    missing_file: list[str] = []

    for sym in symbols:
        path = daily_dir / f"{sym}.csv"
        if _has_bar_for(path, target_date):
            with_bar.append(sym)
            continue
        if ev.present and sym in ev.excluded_symbols:
            excluded.append(sym)
            continue
        if not path.is_file():
            missing_file.append(sym)
        gaps.append(sym)

    # A symbol may be BOTH suspended and carrying a bar - that means a bar was
    # written for a day with no trading, which is the bug this contract forbids.
    contradictions = sorted(set(with_bar) & ev.excluded_symbols) if ev.present else []

    warnings = list(ev.warnings)
    if contradictions:
        warnings.append("bar_written_for_verified_suspended_symbol:" + ",".join(contradictions[:12]))

    complete = not gaps and not contradictions and ev.present
    return {
        "target_date": str(target_date),
        "universe_count": len(symbols),
        "complete": complete,
        "with_bar_count": len(with_bar),
        "verified_excluded_count": len(excluded),
        "gap_count": len(gaps),
        "verified_excluded_symbols": excluded,
        "gap_symbols": gaps,
        "missing_file_symbols": missing_file,
        "contradiction_symbols": contradictions,
        "evidence_present": ev.present,
        "evidence_source": ev.source,
        "evidence_generated_at": ev.generated_at,
        "flagged_for_review_count": len(ev.flagged_for_review),
        "flagged_for_review_symbols": sorted(ev.review_symbols),
        "warnings": warnings,
    }
