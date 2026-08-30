"""Independent per-scope data health, so one date can never speak for all.

Workflow A2 of the 8000+8001 product closure requires four *separate* freshness
scopes to be visible at the same time:

1. ``full_universe``        — every local daily bar under ``data/daily``
2. ``research_pool``        — the symbols the 8000 desk actually studies
3. ``ai_trader``            — AI paper positions, committed intents, candidates
4. ``minute_and_scheduler`` — minute coverage plus real scheduled-task state

The motivating defect is concrete. ``data_health_dashboard._daily_card()`` is
labelled 低价观察池 but derives its verdict from a single ``latest`` /
``max_local`` date, so it cannot state a stale full universe and a fresh
research pool at once. On 2026-08-28 that was the live situation: 4,625 of
5,886 full-universe files were still stopped at 08-21 while 910 of 914
research-pool symbols had reached 08-28. A single ``latest_date`` has to lie
about one of them.

Design rules that follow from the task book:

- Every scope carries its own ``as_of``, counts, ``status`` and ``reason_code``.
  There is deliberately **no** top-level ``latest_date``; a flat field is what
  lets a caller collapse the scopes again.
- Missing data is ``blocked``; mere lag is ``degraded``. They are different
  failures and must not share a reason code.
- An unobserved scope is ``unknown`` with ``not_observed``, never a silent
  ``ok``.
- A live scheduler probe outranks a stored snapshot, and a real failure is
  never rendered as ``running``.
- ``provisional`` / ``partial`` minute data is marked non-formal, per
  ``docs/price_data_contract.md``.

This module is pure: it accepts observations and returns a payload. It performs
no file, network, ledger, plan or LLM access, which is what makes it safe to
call from a read-only 8000 GET route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

SCOPE_FULL_UNIVERSE = "full_universe"
SCOPE_RESEARCH_POOL = "research_pool"
SCOPE_AI_TRADER = "ai_trader"
SCOPE_MINUTE_SCHEDULER = "minute_and_scheduler"

#: Declared in display order. Every scope in this tuple always appears in the
#: payload, so an unobserved scope surfaces as ``unknown`` instead of vanishing.
SCOPE_ORDER: tuple[str, ...] = (
    SCOPE_FULL_UNIVERSE,
    SCOPE_RESEARCH_POOL,
    SCOPE_AI_TRADER,
    SCOPE_MINUTE_SCHEDULER,
)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_FULL_UNIVERSE: "全量本地日线",
    SCOPE_RESEARCH_POOL: "当前研究池日线",
    SCOPE_AI_TRADER: "AI 纸面相关范围",
    SCOPE_MINUTE_SCHEDULER: "分钟线与任务调度",
}

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_DEGRADED = "degraded"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"

#: Worst-wins ordering for the aggregate verdict. ``unknown`` ranks above
#: ``ok`` because an unobserved scope is not evidence of health.
_STATUS_RANK: dict[str, int] = {
    STATUS_OK: 0,
    STATUS_WARNING: 1,
    STATUS_UNKNOWN: 2,
    STATUS_DEGRADED: 3,
    STATUS_BLOCKED: 4,
}

#: Minute coverage states. Only ``complete`` may feed formal consumers; see
#: ``docs/price_data_contract.md`` on provisional/partial data.
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_PROVISIONAL = "provisional"
COVERAGE_MISSING = "missing"
COVERAGE_UNKNOWN = "unknown"

_FORMAL_COVERAGE_STATES = frozenset({COVERAGE_COMPLETE})

#: Live scheduler probe states that mean the task is not dependably running.
#: States that prove the task did not complete normally. ``timeout`` and
#: ``missed`` belong here: a run that timed out or never fired is a failure, and
#: reporting it as a healthy run is exactly the "real failure shown as running"
#: case the product boundary forbids.
_SCHEDULER_FAILED_STATES = frozenset(
    {
        "last_run_failed",
        "failed",
        "timeout",
        "timed_out",
        "missed",
        "misconfigured",
        "disabled",
        "missing",
        "unmanaged",
    }
)
_SCHEDULER_UNCLEAR_STATES = frozenset({"last_run_unknown", "unavailable", "not_supported"})


@dataclass(frozen=True)
class ScopeObservation:
    """One scope's measured coverage. Counts are raw observations, not verdicts."""

    scope: str
    total_symbols: int = 0
    covered_symbols: int = 0
    stale_symbols: int = 0
    missing_symbols: int = 0
    latest_date: str = ""
    target_date: str = ""
    coverage_state: str = COVERAGE_COMPLETE
    symbols_sample: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _worst(statuses: Iterable[str]) -> str:
    worst = STATUS_OK
    for status in statuses:
        if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(worst, 0):
            worst = status
    return worst


def _unobserved_scope(scope: str, target_date: str) -> dict[str, Any]:
    """An unmeasured scope. Never ``ok``: absence of data is not health."""

    return {
        "scope": scope,
        "label": SCOPE_LABELS.get(scope, scope),
        "status": STATUS_UNKNOWN,
        "reason_code": "not_observed",
        "as_of": "",
        "target_date": target_date,
        "total_symbols": 0,
        "covered_symbols": 0,
        "stale_symbols": 0,
        "missing_symbols": 0,
        "coverage_state": COVERAGE_UNKNOWN,
        "formal_use_allowed": False,
        "detail": "该范围本轮没有观测数据，状态未知。",
    }


def _classify(observation: ScopeObservation, target_date: str) -> tuple[str, str, str]:
    """Return ``(status, reason_code, detail)`` for one observation.

    Precedence is deliberate: missing data blocks, an unreached target day
    blocks, then coverage-state and lag degrade. Missing and stale never share
    a reason code, so a consumer can tell "no data" from "old data".
    """

    total = _int(observation.total_symbols)
    covered = _int(observation.covered_symbols)
    stale = _int(observation.stale_symbols)
    missing = _int(observation.missing_symbols)
    latest = _text(observation.latest_date)
    coverage_state = _text(observation.coverage_state) or COVERAGE_COMPLETE

    if coverage_state == COVERAGE_MISSING or (missing and covered <= 0):
        return (
            STATUS_BLOCKED,
            "coverage_missing",
            f"缺失 {missing or total} 项，该范围不可用。",
        )
    if missing:
        return (
            STATUS_BLOCKED,
            "coverage_incomplete_missing",
            f"缺失 {missing} 项（已覆盖 {covered}/{total}），补齐后才算完整。",
        )
    if not latest and total:
        return (STATUS_UNKNOWN, "latest_date_unknown", "无法确定该范围的最新数据日期。")
    if target_date and latest and latest < target_date:
        return (
            STATUS_DEGRADED,
            "behind_target_date",
            f"最新数据停在 {latest}，尚未追平目标交易日 {target_date}。",
        )
    if coverage_state == COVERAGE_PROVISIONAL:
        return (
            STATUS_DEGRADED,
            "coverage_provisional",
            f"数据为临时/盘中口径（已覆盖 {covered}/{total}），不得进入正式用途。",
        )
    if coverage_state == COVERAGE_PARTIAL:
        return (
            STATUS_DEGRADED,
            "coverage_partial",
            f"覆盖不完整（{covered}/{total}），仅可作为参考。",
        )
    if stale:
        return (
            STATUS_DEGRADED,
            "symbols_stale",
            f"{stale} 项滞后于目标交易日（已覆盖 {covered}/{total}）。",
        )
    if total and covered < total:
        return (
            STATUS_WARNING,
            "coverage_below_total",
            f"已覆盖 {covered}/{total}，尚有未覆盖项。",
        )
    if not total:
        return (STATUS_UNKNOWN, "not_observed", "该范围本轮没有观测数据，状态未知。")
    return (
        STATUS_OK,
        "coverage_complete",
        f"{covered}/{total} 项已覆盖目标交易日 {target_date or latest}。",
    )


def scope_status(observation: ScopeObservation, target_date: str = "") -> dict[str, Any]:
    """Build one scope's verdict, keeping its own ``as_of`` and counts intact."""

    effective_target = _text(target_date) or _text(observation.target_date)
    status, reason_code, detail = _classify(observation, effective_target)
    coverage_state = _text(observation.coverage_state) or COVERAGE_COMPLETE
    return {
        "scope": observation.scope,
        "label": SCOPE_LABELS.get(observation.scope, observation.scope),
        "status": status,
        "reason_code": reason_code,
        # Per-scope date. There is no global equivalent, by design.
        "as_of": _text(observation.latest_date),
        "target_date": effective_target,
        "total_symbols": _int(observation.total_symbols),
        "covered_symbols": _int(observation.covered_symbols),
        "stale_symbols": _int(observation.stale_symbols),
        "missing_symbols": _int(observation.missing_symbols),
        "coverage_state": coverage_state,
        "formal_use_allowed": coverage_state in _FORMAL_COVERAGE_STATES and status == STATUS_OK,
        "symbols_sample": list(observation.symbols_sample)[:10],
        "detail": _text(observation.detail) or detail,
    }


def scheduler_scope_status(
    probe: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    target_date: str = "",
) -> dict[str, Any]:
    """Judge scheduler health, preferring a live probe over a stored snapshot.

    A real failure must never read as ``running``: the 20:20 cycle exiting with
    code 4 is the exact case that used to surface as a healthy run. When no live
    probe is available the snapshot is reported as ``stale_snapshot`` evidence
    and keeps the snapshot's own date, so an old record cannot impersonate
    current truth.
    """

    probe = dict(probe or {})
    snapshot = dict(snapshot or {})
    target = _text(target_date)

    if probe.get("available"):
        state = _text(probe.get("state")) or "unknown"
        exit_code = probe.get("last_exit_code")
        as_of = _text(probe.get("as_of")) or _text(snapshot.get("as_of"))
        if state in _SCHEDULER_FAILED_STATES:
            exit_text = "" if exit_code in (None, "") else f"，退出码 {exit_code}"
            return {
                "scope": SCOPE_MINUTE_SCHEDULER,
                "label": SCOPE_LABELS[SCOPE_MINUTE_SCHEDULER],
                "status": STATUS_BLOCKED if state != "last_run_failed" else STATUS_DEGRADED,
                "reason_code": f"scheduler_{state}",
                "as_of": as_of,
                "target_date": target,
                "evidence_source": "live_probe",
                "scheduler_state": state,
                "last_exit_code": exit_code,
                "formal_use_allowed": False,
                "detail": f"计划任务实测状态为 {state}{exit_text}；这不是正常执行。",
            }
        if state in _SCHEDULER_UNCLEAR_STATES:
            return {
                "scope": SCOPE_MINUTE_SCHEDULER,
                "label": SCOPE_LABELS[SCOPE_MINUTE_SCHEDULER],
                "status": STATUS_DEGRADED,
                "reason_code": f"scheduler_{state}",
                "as_of": as_of,
                "target_date": target,
                "evidence_source": "live_probe",
                "scheduler_state": state,
                "last_exit_code": exit_code,
                "formal_use_allowed": False,
                "detail": f"计划任务实测状态为 {state}，无法确认最近一次执行结果。",
            }
        return {
            "scope": SCOPE_MINUTE_SCHEDULER,
            "label": SCOPE_LABELS[SCOPE_MINUTE_SCHEDULER],
            "status": STATUS_OK,
            "reason_code": "scheduler_healthy",
            "as_of": as_of,
            "target_date": target,
            "evidence_source": "live_probe",
            "scheduler_state": state,
            "last_exit_code": exit_code,
            "formal_use_allowed": True,
            "detail": f"计划任务实测状态为 {state}。",
        }

    # No live probe: report the snapshot honestly as stale evidence.
    snapshot_as_of = _text(snapshot.get("as_of"))
    if not snapshot:
        return {
            "scope": SCOPE_MINUTE_SCHEDULER,
            "label": SCOPE_LABELS[SCOPE_MINUTE_SCHEDULER],
            "status": STATUS_UNKNOWN,
            "reason_code": "scheduler_not_observed",
            "as_of": "",
            "target_date": target,
            "evidence_source": "none",
            "scheduler_state": "unknown",
            "last_exit_code": None,
            "formal_use_allowed": False,
            "detail": "没有实测探针，也没有历史快照，调度状态未知。",
        }
    behind = bool(target and snapshot_as_of and snapshot_as_of < target)
    return {
        "scope": SCOPE_MINUTE_SCHEDULER,
        "label": SCOPE_LABELS[SCOPE_MINUTE_SCHEDULER],
        "status": STATUS_DEGRADED if behind else STATUS_UNKNOWN,
        "reason_code": "scheduler_snapshot_only",
        "as_of": snapshot_as_of,
        "target_date": target,
        "evidence_source": "stale_snapshot",
        "scheduler_state": _text(snapshot.get("status")) or "unknown",
        "last_exit_code": snapshot.get("last_exit_code"),
        "formal_use_allowed": False,
        "detail": (
            f"没有实测探针，只有 {snapshot_as_of or '未知日期'} 的历史快照；"
            "不能据此断定计划任务当前正常。"
        ),
    }


def build_scope_health(
    observations: Iterable[ScopeObservation],
    target_date: str = "",
    scheduler: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate per-scope verdicts without ever merging their dates.

    The returned payload intentionally omits any top-level ``latest_date`` /
    ``local_daily_latest_date``: a flat field is exactly what allows a stale
    full universe and a fresh research pool to be shown as one number again.
    The aggregate is worst-wins, and each scope keeps its own verdict, so a
    blocked pool cannot hide a healthy AI scope and vice versa.
    """

    target = _text(target_date)
    scopes: dict[str, dict[str, Any]] = {}

    for observation in observations or ():
        scope_name = _text(observation.scope)
        if not scope_name:
            continue
        scopes[scope_name] = scope_status(observation, target)

    if scheduler is not None:
        # A raw probe must be classified, never stored verbatim: passing the
        # dict through unchanged is how a failed or timed-out task ends up
        # displayed as whatever state it claims for itself (e.g. "running").
        block = dict(scheduler)
        already_classified = all(
            key in block for key in ("status", "reason_code", "evidence_source")
        )
        if already_classified:
            scopes[SCOPE_MINUTE_SCHEDULER] = block
        else:
            snapshot = block.get("snapshot")
            scheduler_scope = scheduler_scope_status(
                probe=block,
                snapshot=snapshot if isinstance(snapshot, dict) else None,
                target_date=target,
            )
            # Minute coverage and task state are one display scope but two
            # independent facts.  A failed task must not erase a provisional
            # minute-data warning (or vice versa), otherwise operators see only
            # whichever signal was processed last.
            minute_scope = scopes.get(SCOPE_MINUTE_SCHEDULER)
            if isinstance(minute_scope, dict):
                merged = dict(minute_scope)
                merged.update(
                    {
                        "scheduler_state": scheduler_scope.get("scheduler_state"),
                        "last_exit_code": scheduler_scope.get("last_exit_code"),
                        "scheduler_status": scheduler_scope.get("status"),
                        "scheduler_reason_code": scheduler_scope.get("reason_code"),
                        "scheduler_detail": scheduler_scope.get("detail"),
                        "evidence_source": scheduler_scope.get("evidence_source"),
                        "scheduler_as_of": scheduler_scope.get("as_of"),
                        "formal_use_allowed": bool(minute_scope.get("formal_use_allowed"))
                        and bool(scheduler_scope.get("formal_use_allowed")),
                    }
                )
                merged["status"] = _worst(
                    [
                        _text(minute_scope.get("status")) or STATUS_UNKNOWN,
                        _text(scheduler_scope.get("status")) or STATUS_UNKNOWN,
                    ]
                )
                merged["reason_code"] = (
                    _text(scheduler_scope.get("reason_code"))
                    if merged["status"] == _text(scheduler_scope.get("status"))
                    else _text(minute_scope.get("reason_code"))
                )
                merged["detail"] = "；".join(
                    dict.fromkeys(
                        item
                        for item in (
                            _text(minute_scope.get("detail")),
                            _text(scheduler_scope.get("detail")),
                        )
                        if item
                    )
                )
                scopes[SCOPE_MINUTE_SCHEDULER] = merged
            else:
                scopes[SCOPE_MINUTE_SCHEDULER] = scheduler_scope

        # Keep a scheduler-specific marker even when there was no minute
        # observation to merge.  Alert construction uses it to avoid emitting
        # the same failed task once as a generic scope problem and again as a
        # scheduler problem.
        current_scheduler_scope = scopes.get(SCOPE_MINUTE_SCHEDULER)
        if isinstance(current_scheduler_scope, dict):
            current_scheduler_scope.setdefault(
                "scheduler_reason_code", scheduler_scope.get("reason_code")
            )
            current_scheduler_scope.setdefault(
                "scheduler_status", scheduler_scope.get("status")
            )

    for scope_name in SCOPE_ORDER:
        scopes.setdefault(scope_name, _unobserved_scope(scope_name, target))

    ordered = {name: scopes[name] for name in SCOPE_ORDER if name in scopes}
    for name, payload in scopes.items():  # keep any caller-supplied extra scope
        ordered.setdefault(name, payload)

    overall = _worst(payload.get("status", STATUS_UNKNOWN) for payload in ordered.values())
    blocked = [name for name, payload in ordered.items() if payload.get("status") == STATUS_BLOCKED]
    degraded = [
        name
        for name, payload in ordered.items()
        if payload.get("status") in {STATUS_DEGRADED, STATUS_WARNING}
    ]
    unknown = [name for name, payload in ordered.items() if payload.get("status") == STATUS_UNKNOWN]

    return {
        "schema_version": "a2_scope_health_v2",
        "target_date": target,
        "overall_status": overall,
        "scopes": ordered,
        "blocked_scopes": blocked,
        "degraded_scopes": degraded,
        "unknown_scopes": unknown,
        "boundary": "Research Only / Paper Account Only；本页只读，不联网、不写账本。",
    }


def _coverage_observation(
    scope: str,
    coverage: Mapping[str, Any] | None,
    *,
    target_date: str,
    latest_date: str = "",
    coverage_state: str = COVERAGE_COMPLETE,
) -> ScopeObservation | None:
    """Map one daily-chain coverage block into an observation.

    Returns ``None`` when the block carries no usable evidence, so the scope
    surfaces as ``unknown`` / ``not_observed`` rather than a fabricated ``ok``.
    """

    block = dict(coverage or {})
    if not block:
        return None
    requested = _int(block.get("requested_count") or block.get("symbol_count"))
    complete = _int(
        block.get("complete_session_count")
        or block.get("formal_execution_eligible_count")
        or block.get("covered_count")
        or block.get("fresh_count")
    )
    # A provider failure is not interchangeable with an absent artifact, but
    # neither can qualify the scope as complete for a target date.  Preserve the
    # stronger of the two counts so downstream consumers fail closed instead of
    # treating a failed target-bar fetch as a healthy pool.
    missing = max(
        _int(block.get("missing_count")),
        _int(block.get("failed_count")),
    )
    stale = _int(block.get("stale_count"))
    if not requested and not complete and not missing and not stale:
        return None
    if requested and complete and not missing and not stale:
        stale = max(0, requested - complete)
    return ScopeObservation(
        scope=scope,
        total_symbols=requested or complete,
        covered_symbols=complete,
        stale_symbols=stale,
        missing_symbols=missing,
        latest_date=_text(latest_date) or _text(block.get("latest_date")),
        target_date=target_date,
        coverage_state=coverage_state,
    )


def observations_from_daily_chain(
    chain: Mapping[str, Any] | None,
    *,
    target_date: str = "",
    full_universe_observation: ScopeObservation | None = None,
    research_pool_latest: str = "",
    ai_trader: ScopeObservation | None = None,
) -> list[ScopeObservation]:
    """Derive scope observations from an already-loaded daily-chain manifest.

    This is deliberately pure and cheap: it reads the manifest dict the 8000
    dashboard already holds, so surfacing four scopes costs no extra filesystem
    scan of the 5,886 canonical daily files. Scopes with no evidence are simply
    omitted and therefore reported as ``unknown``.
    """

    payload = dict(chain or {})
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    target = _text(target_date) or _text(payload.get("target_date"))[:10]

    observations: list[ScopeObservation] = []

    # Scope 1: current full-A coverage comes from its own explicit audit.  The
    # chain's ``coverage.daily`` block is deliberately *not* a fallback: it is
    # the bounded current-analysis-pool update result and labeling it full-A
    # recreates the exact 2026-08-28 truthfulness defect.
    if full_universe_observation is not None:
        observations.append(full_universe_observation)

    # Scope 2: the symbols the 8000 desk actually studies.
    pool = _coverage_observation(
        SCOPE_RESEARCH_POOL,
        coverage.get("daily"),
        target_date=target,
        latest_date=research_pool_latest,
    )
    if pool is not None:
        observations.append(pool)

    # Scope 3: AI paper positions / committed intents, supplied by the caller
    # because it owns the ledger read (this module stays filesystem-free).
    if ai_trader is not None:
        observations.append(ai_trader)

    # Scope 4: minute coverage. Anything short of a passed critical tier is
    # non-formal per docs/price_data_contract.md.
    critical = coverage.get("critical") if isinstance(coverage.get("critical"), dict) else {}
    if critical:
        critical_status = _text(critical.get("status")).lower()
        if critical_status in {"passed", "complete", "ok"}:
            minute_state = COVERAGE_COMPLETE
        elif critical_status in {"missing", "absent", "not_run"}:
            minute_state = COVERAGE_MISSING
        elif critical_status in {"provisional", "partial_daily"}:
            minute_state = COVERAGE_PROVISIONAL
        else:
            minute_state = COVERAGE_PARTIAL
        minute = _coverage_observation(
            SCOPE_MINUTE_SCHEDULER,
            critical,
            target_date=target,
            coverage_state=minute_state,
        )
        if minute is not None:
            observations.append(minute)

    return observations


def ai_trader_scope_is_usable(health: Mapping[str, Any]) -> bool:
    """True only when the AI scope is verifiably healthy.

    Used by the A4 supervision gate: a stale *full universe* alone must not
    block the AI trader, so this looks at the AI scope only.
    """

    scope = (health.get("scopes") or {}).get(SCOPE_AI_TRADER) or {}
    return scope.get("status") == STATUS_OK
