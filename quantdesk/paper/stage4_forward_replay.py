"""Round 7 Part D — forward-window tri-account replay runner (T0 .. T0+18).

Stage 4's real-fill delivery pivoted from the fixed historical windows (which a
future forward archive can never retroactively certify) to a forward window
anchored at T0 — the first day a ``sector_history_archive`` snapshot passes every
qualification gate.  This module is the runner scaffolding for that forward
window.  It never fabricates T0, never runs the network, never executes
``--apply``, and never touches the real paper ledger.

It exposes three milestone contracts, all driven off the Round 7 readiness
artifact (``round7_forward_pit_readiness_latest.json``):

* ``detect_t0_readiness``   — is T0 anchored yet, and what are the milestone
  calendar dates?  Until T0 exists this reports ``blocked_pit_evidence_pending``.
* ``interim_evidence_pack`` — at T0+4 / T0+8, count candidates / gate outcomes /
  would-fill without asserting a return conclusion (sample still forming).
* ``terminal_evaluation``   — at T0+18, run the real tri-account replay over the
  forward window and judge the sample honestly (``insufficient_sample`` when
  effective sells < the minimum sample size).

The real run waits for T0 and the calendar; this round produces no real fills.
The fixture tests prove the three milestone output contracts deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
READINESS_LATEST = (
    PROJECT_ROOT / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
)

# Minimum effective sell sample below which no return conclusion is drawn.
MIN_EFFECTIVE_SAMPLE = 30

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice."
)


def _load_readiness(readiness_path: Path | None = None) -> dict[str, Any]:
    path = Path(readiness_path or READINESS_LATEST)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _milestone_dates(readiness: Mapping[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {"T0": None, "T0+4": None, "T0+8": None, "T0+18": None}
    label_map = {
        "T0": "T0",
        "T0+4 trading sessions": "T0+4",
        "T0+8 trading sessions": "T0+8",
        "T0+18 trading sessions": "T0+18",
    }
    for entry in readiness.get("milestones", []) or []:
        key = label_map.get(str(entry.get("relative_to_t0") or ""))
        if key is not None:
            out[key] = entry.get("calendar_date")
    return out


@dataclass
class ForwardWindowStatus:
    t0: str | None
    current_eligible_forward_days: int
    milestone_dates: dict[str, str | None]
    ready_for_interim: bool
    ready_for_terminal: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": "stage4_forward_window_status",
            "t0": self.t0,
            "current_eligible_forward_days": self.current_eligible_forward_days,
            "milestone_dates": dict(self.milestone_dates),
            "ready_for_interim": self.ready_for_interim,
            "ready_for_terminal": self.ready_for_terminal,
            "status": self.status,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": DISCLAIMER,
        }


def detect_t0_readiness(readiness_path: Path | None = None) -> ForwardWindowStatus:
    """Report whether T0 is anchored and how many forward days have accrued.

    ``ready_for_interim`` requires >=5 eligible forward days (T0..T0+4);
    ``ready_for_terminal`` requires >=19 (T0..T0+18).  Until T0 exists both are
    ``False`` and the status is ``blocked_pit_evidence_pending``.
    """

    readiness = _load_readiness(readiness_path)
    t0 = readiness.get("t0")
    eligible_days = int(readiness.get("current_eligible_forward_days") or 0)
    milestone_dates = _milestone_dates(readiness)
    ready_for_interim = t0 is not None and eligible_days >= 5
    ready_for_terminal = t0 is not None and eligible_days >= 19
    if t0 is None:
        status = "blocked_pit_evidence_pending"
    elif ready_for_terminal:
        status = "terminal_window_ready"
    elif ready_for_interim:
        status = "interim_window_ready"
    else:
        status = "t0_anchored_accumulating"
    return ForwardWindowStatus(
        t0=t0 if isinstance(t0, str) else None,
        current_eligible_forward_days=eligible_days,
        milestone_dates=milestone_dates,
        ready_for_interim=ready_for_interim,
        ready_for_terminal=ready_for_terminal,
        status=status,
    )


@dataclass
class InterimEvidencePack:
    milestone: str  # "T0+4" | "T0+8"
    as_of: str | None
    account_candidate_counts: dict[str, int]
    account_gate_pass_counts: dict[str, int]
    account_would_fill_counts: dict[str, int]
    conclusion_drawn: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": "stage4_forward_interim_evidence",
            "milestone": self.milestone,
            "as_of": self.as_of,
            "account_candidate_counts": dict(self.account_candidate_counts),
            "account_gate_pass_counts": dict(self.account_gate_pass_counts),
            "account_would_fill_counts": dict(self.account_would_fill_counts),
            # Interim packs never draw a return conclusion; the sample is still forming.
            "conclusion_drawn": False,
            "note": (
                "Interim counts only. No return/PnL conclusion is drawn at an "
                "interim milestone; the forward sample is still accumulating."
            ),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": DISCLAIMER,
        }


def interim_evidence_pack(
    milestone: str,
    account_results: Mapping[str, Any],
    *,
    as_of: str | None = None,
) -> InterimEvidencePack:
    """Summarise candidate/gate/would-fill counts per account at an interim milestone.

    ``account_results`` maps ``account_id`` to an object exposing
    ``candidate_count``, ``stage_counts`` (a mapping with ``fill``/``no_fill``/
    ``evidence`` keys), so both a real ``AccountReplayResult`` and a fixture stub
    satisfy the contract.  ``would_fill`` counts candidates that passed the gate
    (reached the evidence/fill stage), i.e. everything not stopped at
    snapshot/intent/gate.
    """

    if milestone not in {"T0+4", "T0+8"}:
        raise ValueError(f"interim milestone must be T0+4 or T0+8, got {milestone!r}")
    candidate_counts: dict[str, int] = {}
    gate_pass_counts: dict[str, int] = {}
    would_fill_counts: dict[str, int] = {}
    for account_id, result in account_results.items():
        stage_counts = dict(getattr(result, "stage_counts", {}) or {})
        candidate_counts[account_id] = int(getattr(result, "candidate_count", 0) or 0)
        # Gate-passing candidates are those that reached evidence assembly, a
        # no-fill rejection, or an actual fill (all are strictly post-gate).
        gate_pass = (
            int(stage_counts.get("evidence", 0))
            + int(stage_counts.get("no_fill", 0))
            + int(stage_counts.get("fill", 0))
        )
        gate_pass_counts[account_id] = gate_pass
        would_fill_counts[account_id] = int(stage_counts.get("fill", 0))
    return InterimEvidencePack(
        milestone=milestone,
        as_of=as_of,
        account_candidate_counts=candidate_counts,
        account_gate_pass_counts=gate_pass_counts,
        account_would_fill_counts=would_fill_counts,
    )


@dataclass
class TerminalEvaluation:
    as_of: str | None
    window_start: str | None
    window_end: str | None
    total_candidates: int
    total_fills: int
    effective_sell_sample: int
    sample_sufficient: bool
    verdict: str
    account_manifests: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": "stage4_forward_terminal_evaluation",
            "schema_version": API_SCHEMA_VERSION,
            "as_of": self.as_of,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_candidates": self.total_candidates,
            "total_fills": self.total_fills,
            "effective_sell_sample": self.effective_sell_sample,
            "min_effective_sample": MIN_EFFECTIVE_SAMPLE,
            "sample_sufficient": self.sample_sufficient,
            "verdict": self.verdict,
            "account_manifests": list(self.account_manifests),
            "risk_gate_enforced": False,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": DISCLAIMER,
        }


def terminal_evaluation(
    account_results: Sequence[Any],
    *,
    effective_sell_sample: int,
    as_of: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> TerminalEvaluation:
    """Judge the forward sample honestly at T0+18.

    ``effective_sell_sample`` is the number of realised round-trip sells with a
    usable return.  When it is below ``MIN_EFFECTIVE_SAMPLE`` the verdict is
    ``insufficient_sample`` regardless of the fill count — the runner never
    dresses a thin sample up as a return conclusion.
    """

    total_candidates = 0
    total_fills = 0
    manifests: list[dict[str, Any]] = []
    for result in account_results:
        total_candidates += int(getattr(result, "candidate_count", 0) or 0)
        total_fills += int(getattr(result, "fill_count", 0) or 0)
        if hasattr(result, "to_manifest"):
            manifests.append(result.to_manifest())
    sample_sufficient = int(effective_sell_sample) >= MIN_EFFECTIVE_SAMPLE
    verdict = "return_conclusion_eligible" if sample_sufficient else "insufficient_sample"
    return TerminalEvaluation(
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        total_candidates=total_candidates,
        total_fills=total_fills,
        effective_sell_sample=int(effective_sell_sample),
        sample_sufficient=sample_sufficient,
        verdict=verdict,
        account_manifests=manifests,
    )


def run_forward_replay_if_ready(
    *,
    readiness_path: Path | None = None,
    replay_fn: Callable[[ForwardWindowStatus], Sequence[Any]] | None = None,
    effective_sample_fn: Callable[[Sequence[Any]], int] | None = None,
) -> dict[str, Any]:
    """Top-level guard: run the terminal replay only when T0+18 is reached.

    ``replay_fn`` and ``effective_sample_fn`` are injected so the real driver
    (``stage4_tri_account_replay.replay_account``) and the effective-sell counter
    are supplied by the caller; this keeps the readiness guard testable offline
    and free of heavy data dependencies.  Until T0+18 is reached, it returns the
    readiness status and runs nothing.
    """

    status = detect_t0_readiness(readiness_path)
    if not status.ready_for_terminal:
        return {
            "ran": False,
            "reason": status.status,
            "status": status.to_dict(),
        }
    if replay_fn is None or effective_sample_fn is None:
        return {
            "ran": False,
            "reason": "terminal_ready_but_no_replay_fn_injected",
            "status": status.to_dict(),
        }
    account_results = list(replay_fn(status))
    effective = int(effective_sample_fn(account_results))
    evaluation = terminal_evaluation(
        account_results,
        effective_sell_sample=effective,
        as_of=status.milestone_dates.get("T0+18"),
        window_start=status.t0,
        window_end=status.milestone_dates.get("T0+18"),
    )
    return {
        "ran": True,
        "reason": "terminal_window_ready",
        "status": status.to_dict(),
        "evaluation": evaluation.to_dict(),
    }


__all__ = [
    "ForwardWindowStatus",
    "InterimEvidencePack",
    "TerminalEvaluation",
    "detect_t0_readiness",
    "interim_evidence_pack",
    "terminal_evaluation",
    "run_forward_replay_if_ready",
    "MIN_EFFECTIVE_SAMPLE",
]
