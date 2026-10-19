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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from quantdesk.daily_session_evidence import validate_baostock_session_evidence
from quantdesk.shared import normalize_symbol

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_RELATIVE = Path("data/metadata/suspension_evidence")
DAILY_RELATIVE = Path("data/daily")
UNIVERSE_RELATIVE = Path("data/universe/a_share_symbols.csv")
RUN_SESSION_PREFIX = "session_"

# Signatures the fetch side records. Kept as data, not hard-coded logic, so a new
# upstream shape is a one-line addition rather than a rewrite.
#
# ``RUN_SESSION_SIGNATURE`` is the *current-run* producer: the bounded gap
# recovery worker asked a provider for the target day and got an explicit
# ``tradestatus=0`` row back. It is deliberately NOT part of the legacy
# allow-list below. A signature in that list is trusted on its own, so adding
# the bridge label there would let a bare legacy metadata entry claim a verified
# exclusion with no provider records, no hash and no single-day range. The label
# is only meaningful after the strict
# ``quantdesk.daily_session_evidence`` validation in
# :func:`load_run_session_evidence`, which builds ``verified_exclusions``
# directly instead of going through the legacy signature allow-list.
RUN_SESSION_SIGNATURE = "provider_tradestatus_zero"

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
    run_id: str = ""
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
# Current-run bridge
# ---------------------------------------------------------------------------


def _normalized_symbol_map(value: Any) -> dict[str, dict[str, Any]]:
    """Same key normalization the daily writer uses, so 000016 == sz.000016."""
    out: dict[str, dict[str, Any]] = {}
    for key, meta in _as_symbol_map(value).items():
        symbol = normalize_symbol(key)
        if symbol:
            out[symbol] = dict(meta)
    return out


def load_run_session_evidence(
    target_date: str,
    *,
    run_evidence: Mapping[str, Any] | None = None,
    run_evidence_dir: str | Path | None = None,
    expected_symbols: Iterable[str] | None = None,
    run_id: str = "",
    source: str = "target_day_gap_recovery",
) -> SuspensionEvidence:
    """Bridge ONE run's validated no-session evidence into this contract.

    The evidence is produced by ``daily_gap_recovery`` and persisted as
    ``session_<symbol>.json`` next to the run. Only what the caller explicitly
    hands over is read: an inline mapping, or one named run directory. This
    never scans a history directory and never takes "the newest" file.

    Every record is re-validated through the existing strict contract
    (:func:`quantdesk.daily_session_evidence.validate_baostock_session_evidence`)
    bound to the *target date* and the *symbol itself*. A record that fails any
    check - wrong symbol, wrong or future date, incomplete range, hash mismatch,
    conflicting tradestatus, or a ``tradestatus=1`` (traded) row - is demoted to
    ``flagged_for_review`` (still a gap), never promoted to a verified
    exclusion.
    """
    target = str(target_date).strip()
    warnings: list[str] = []
    verified: dict[str, dict[str, Any]] = {}
    flagged: dict[str, dict[str, Any]] = {}
    present = False
    wanted = {normalize_symbol(s) for s in (expected_symbols or [])}
    wanted.discard("")

    candidates: dict[str, dict[str, Any]] = {}
    if run_evidence is not None:
        present = True
        for symbol, payload in _normalized_symbol_map(run_evidence).items():
            if wanted and symbol not in wanted:
                continue
            candidates.setdefault(symbol, payload)
    if run_evidence_dir is not None:
        directory = Path(run_evidence_dir)
        if directory.is_dir():
            present = True
            for path in sorted(directory.glob(f"{RUN_SESSION_PREFIX}*.json")):
                symbol = normalize_symbol(path.stem[len(RUN_SESSION_PREFIX):])
                if not symbol or (wanted and symbol not in wanted):
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    flagged[symbol] = {"reason": f"run_session_evidence_unreadable:{type(exc).__name__}"}
                    warnings.append(f"run_session_evidence_unreadable:{symbol}")
                    continue
                if isinstance(payload, Mapping):
                    candidates.setdefault(symbol, dict(payload))
                else:
                    flagged[symbol] = {"reason": "run_session_evidence_not_an_object"}
                    warnings.append(f"run_session_evidence_not_an_object:{symbol}")

    if not present:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=tuple(warnings) or ("run_session_evidence_missing",),
        )

    sources: set[str] = set()
    captured: list[str] = []
    for symbol, payload in sorted(candidates.items()):
        validated = validate_baostock_session_evidence(
            payload,
            expected_symbol=symbol,
            expected_start_date=target,
            expected_end_date=target,
        )
        if not validated.get("valid"):
            reasons = ",".join(list(validated.get("reason_codes") or [])[:4])
            flagged[symbol] = {"reason": "run_session_evidence_rejected:" + reasons}
            warnings.append("run_session_evidence_rejected:" + symbol + ":" + reasons)
            continue
        if target not in (validated.get("suspended_dates") or []):
            # tradestatus=1 means the symbol DID trade on the target day; that
            # contradicts a "no bar" gap, so it can never become an exclusion.
            flagged[symbol] = {"reason": "run_session_evidence_not_suspended_on_target"}
            warnings.append("run_session_evidence_not_suspended_on_target:" + symbol)
            continue
        evidence = validated.get("evidence") or {}
        record = next(
            (
                row
                for row in (evidence.get("records") or [])
                if str(row.get("trade_date")) == target
            ),
            {},
        )
        verified[symbol] = {
            "signature": RUN_SESSION_SIGNATURE,
            "trade_date": target,
            "tradestatus": str(record.get("tradestatus") or "0"),
            "source": str(evidence.get("source") or ""),
            "records_sha256": str(evidence.get("records_sha256") or ""),
            "captured_at": str(evidence.get("captured_at") or ""),
            "provider_code": str(evidence.get("provider_code") or ""),
        }
        sources.add(str(evidence.get("source") or ""))
        captured.append(str(evidence.get("captured_at") or ""))

    return SuspensionEvidence(
        target_date=target,
        verified_exclusions=verified,
        flagged_for_review=flagged,
        source=",".join(sorted(part for part in sources if part)) or source,
        generated_at=max(captured) if captured else "",
        contract="run-scoped provider tradestatus evidence",
        run_id=str(run_id or ""),
        present=True,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def merge_suspension_evidence(
    base: SuspensionEvidence,
    run: SuspensionEvidence | None,
) -> SuspensionEvidence:
    """Current-run evidence wins in BOTH directions; the legacy file is a fallback.

    - A symbol the run *verified* is never left in ``flagged_for_review`` (the
      run is the newer, stricter producer).
    - A symbol the run *rejected* - hash mismatch, wrong range, or an explicit
      ``tradestatus=1`` "it traded" row - must lose any legacy exclusion. The
      current day's counter-argument revokes the old conclusion instead of being
      hidden behind it, and the symbol stays visible as a gap.
    - A legacy exclusion is retained only when the run has nothing to say about
      that symbol.
    - Evidence verified for a different day is refused outright rather than
      silently re-dated to this consumer's target.
    """
    if run is None or not (run.present or run.verified_exclusions or run.flagged_for_review):
        return base
    if run.target_date and base.target_date and str(run.target_date) != str(base.target_date):
        return base
    verified = dict(base.verified_exclusions)
    flagged = dict(base.flagged_for_review)
    for symbol, meta in run.verified_exclusions.items():
        verified[symbol] = meta
        flagged.pop(symbol, None)
    for symbol, meta in run.flagged_for_review.items():
        # A rejected or "traded" record is a live counter-argument: it revokes
        # any legacy exclusion for the same symbol and stays visible as a gap.
        verified.pop(symbol, None)
        flagged[symbol] = meta
    for symbol in sorted(set(verified) & set(flagged)):
        if symbol in run.verified_exclusions:
            flagged.pop(symbol, None)
        else:
            verified.pop(symbol, None)
    return SuspensionEvidence(
        target_date=base.target_date,
        verified_exclusions=verified,
        flagged_for_review=flagged,
        source="+".join(part for part in (base.source, run.source) if part),
        generated_at=max((base.generated_at, run.generated_at)),
        contract="+".join(part for part in (base.contract, run.contract) if part),
        run_id=run.run_id or base.run_id,
        present=base.present or run.present,
        warnings=tuple(dict.fromkeys((*base.warnings, *run.warnings))),
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
    run_evidence: SuspensionEvidence | None = None,
    run_evidence_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Split the universe into complete / verified-excluded / gap.

    ``complete`` is only claimed when every symbol lands in one of the first two
    buckets AND evidence for the date was actually present. A missing evidence
    file keeps suspended symbols in ``gap_symbols``, so a broken fetch side can
    never be mistaken for a clean day.

    Two producers are merged, because one day must not produce two verdicts:
    the legacy ``data/metadata/suspension_evidence`` file, and this run's own
    ``session_<symbol>.json`` records (``run_evidence`` or ``run_evidence_dir``).
    The current run wins where they disagree; the legacy file stays a
    compatible fallback.
    """
    root = Path(project_root or PROJECT_ROOT)
    target = str(target_date)
    base = evidence if evidence is not None else load_suspension_evidence(target_date, project_root=root)
    run = run_evidence
    if run is None and run_evidence_dir is not None:
        run = load_run_session_evidence(
            target_date,
            run_evidence_dir=run_evidence_dir,
            expected_symbols=universe_symbols,
        )
    run_rejection = ""
    if run is not None and run.target_date and str(run.target_date) != target:
        # Evidence verified for another day must not exclude this day's gaps.
        run_rejection = f"run_evidence_target_date_mismatch:{run.target_date}"
        run = None
    ev = merge_suspension_evidence(base, run)
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
    if run_rejection:
        warnings.append(run_rejection)
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
        "run_evidence_present": bool(run.present) if run is not None else False,
        "run_verified_excluded_count": len(run.verified_exclusions) if run is not None else 0,
        "run_flagged_for_review_symbols": sorted(run.review_symbols) if run is not None else [],
        "run_evidence_source": run.source if run is not None else "",
        "run_evidence_run_id": run.run_id if run is not None else "",
        "warnings": warnings,
    }
