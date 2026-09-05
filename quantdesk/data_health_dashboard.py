"""Beginner-facing data health dashboard for QuantDesk.

The dashboard is deliberately an aggregator.  It reads existing status
payloads and explains what they mean for a beginner; it does not update data,
connect to providers, generate strategies, or touch portfolio state.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.candidate_contract import (
    attach_theme_map_to_candidates,
    candidate_rows_from_strategy_payload,
    normalize_candidate_row,
)
from quantdesk.daily_data_chain import latest_daily_data_chain
from quantdesk.daily_price_update_engine import daily_price_status
from quantdesk.daily_scheduler_status import daily_data_scheduler_status
from quantdesk.data_chain_consumer_gate import (
    evaluate_data_chain_consumer_gate,
)
from quantdesk.data_freshness_gate import evaluate_data_freshness
from quantdesk.evidence_freshness import build_evidence_freshness
from quantdesk.intraday.intraday_gap_checker import intraday_gap_status
from quantdesk.intraday.intraday_provider_capability import latest_intraday_provider_capability
from quantdesk.intraday.intraday_replay_self_validation import latest_intraday_replay_self_validation
from quantdesk.paper.auto_paper_portfolio import auto_paper_portfolio_latest
from quantdesk.real_intraday_sample_validation import latest_real_intraday_sample_validation
from quantdesk.shared import now_text, read_json, safe_float, write_json
from quantdesk.theme_current_candidate_validator import validate_theme_map_for_current_candidates
from quantdesk.theme_sector_engine import THEME_MAP_PATH, load_theme_map, theme_map_status
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
DAILY_WORKFLOW_LATEST_JSON = RESULTS_DIR / "daily_workflow_latest.json"
DATA_HEALTH_DASHBOARD_LATEST_JSON = RESULTS_DIR / "data_health" / "data_health_dashboard_latest.json"
EXTENDED_DATA_GATES_LATEST_JSON = RESULTS_DIR / "data_health" / "extended_data_gates_latest.json"
FULL_A_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "a_share_symbols.csv"
CANONICAL_DAILY_DIR = PROJECT_ROOT / "data" / "daily"

RISK_DISCLAIMER = "本系统仅用于本地学习与纸面模拟，不接券商、不真实下单、不构成投资建议。"


def _relpath_for_payload(path: Path | str) -> str:
    """Display path relative to the project when possible; keep tmp/out-of-tree paths intact."""

    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(target).replace("\\", "/")

_CHAIN_FAILED_STATUSES = frozenset({"abandoned", "blocked", "failed", "superseded_contract"})
_SCHEDULER_BROKEN_STATUSES = frozenset({"missing", "unmanaged", "disabled", "misconfigured"})
_SCHEDULER_DEGRADED_STATUSES = frozenset({"unavailable", "last_run_failed", "last_run_unknown", "not_supported"})


def _snapshot_status_with_chain(
    daily_chain: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    scheduler: dict[str, Any] | None = None,
) -> str:
    """Reconcile a persisted component verdict with the latest chain attempt.

    The component cards and the data-chain attempt are separate evidence
    surfaces.  A successful card aggregate must not survive a blocked or
    abandoned chain, while an in-progress chain should remain visibly
    provisional.  This helper is intentionally pure so both the CLI writer and
    tests can use the same status vocabulary without probing or writing state.
    """

    payload = daily_chain if isinstance(daily_chain, dict) else {}
    current = snapshot if isinstance(snapshot, dict) else {}
    status = str(current.get("status") or "warning").lower()
    chain_status = str(payload.get("status") or "not_run").lower()
    scheduler_payload = scheduler if isinstance(scheduler, dict) else {}
    scheduler_status = str(scheduler_payload.get("status") or "").lower()
    if chain_status in _CHAIN_FAILED_STATUSES:
        return "failed"
    if scheduler_status in _SCHEDULER_BROKEN_STATUSES:
        return "failed"
    if chain_status in {"running", "partial", "not_run"}:
        if status not in {"failed", "red"}:
            return "warning"
    if scheduler_status in _SCHEDULER_DEGRADED_STATUSES and status not in {"failed", "red"}:
        return "warning"
    return "failed" if status in {"failed", "red"} else status


def _apply_health_evidence_overlay(
    level: str,
    *,
    daily_chain: dict[str, Any],
    scheduler: dict[str, Any],
    evidence_freshness: dict[str, Any],
    snapshot_stale: bool = False,
) -> tuple[str, list[str]]:
    """Escalate presentation health without redefining market freshness."""

    current_level = str(level or "yellow").lower()
    chain_status = str(daily_chain.get("status") or "not_run").lower()
    scheduler_status = str(scheduler.get("status") or "").lower()
    evidence_status = str(evidence_freshness.get("status") or "warning").lower()
    reasons: list[str] = []
    if chain_status in _CHAIN_FAILED_STATUSES:
        reasons.append(f"latest_daily_chain_{chain_status}")
        return "red", reasons
    if scheduler_status in _SCHEDULER_BROKEN_STATUSES:
        reasons.append(f"daily_data_scheduler_{scheduler_status}")
        return "red", reasons
    if evidence_status == "failed":
        reasons.extend(str(item) for item in evidence_freshness.get("reason_codes") or [] if str(item))
        reasons.append("audit_evidence_failed")
        return "red", list(dict.fromkeys(reasons))
    if chain_status in {"running", "partial", "not_run"}:
        reasons.append(f"latest_daily_chain_{chain_status}")
    if scheduler_status in _SCHEDULER_DEGRADED_STATUSES:
        reasons.append(f"daily_data_scheduler_{scheduler_status}")
    if snapshot_stale:
        reasons.append("data_health_snapshot_stale")
    if evidence_status in {"warning", "missing", "stale"}:
        reasons.extend(str(item) for item in evidence_freshness.get("reason_codes") or [] if str(item))
        reasons.append(f"audit_evidence_{evidence_status}")
    if not evidence_freshness.get("metrics_trustworthy", False):
        reasons.extend(str(item) for item in evidence_freshness.get("reason_codes") or [] if str(item))
        reasons.append("audit_metrics_untrustworthy")
    if reasons and current_level != "red":
        return "yellow", list(dict.fromkeys(reasons))
    return current_level, list(dict.fromkeys(reasons))


def _append_evidence_summary(summary: Any, evidence_freshness: dict[str, Any]) -> str:
    """Append the audit-evidence warning once, for both fresh and cached views."""

    base = str(summary or "").strip()
    if evidence_freshness.get("metrics_trustworthy", False):
        return base
    message = str(evidence_freshness.get("message_for_ui") or "").strip()
    if not message:
        message = "审计证据过期、缺失或不一致，当前绩效指标不应视为可信结论。"
    warning = f"审计证据提示：{message}"
    if warning in base or message in base:
        return base
    return f"{base} {warning}".strip()


def build_minute_universe_coverage(chain: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the daily-chain manifest without mixing the historical queue."""

    payload = chain if isinstance(chain, dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    critical = dict(coverage.get("critical") or {})
    critical_daily = dict(coverage.get("critical_daily") or {})
    analysis = dict(coverage.get("current_analysis_pool") or {})
    critical_universe = dict(universe.get("critical") or {})
    analysis_universe = dict(universe.get("current_analysis_pool") or {})
    return {
        "exists": bool(payload.get("exists", bool(payload))),
        "status": str(payload.get("status") or "not_run"),
        "run_id": str(payload.get("run_id") or ""),
        "target_trade_date": str(payload.get("target_date") or "")[:10],
        "generated_at": str(payload.get("generated_at") or payload.get("completed_at") or ""),
        "daily_coverage": dict(coverage.get("daily") or {}),
        "critical_daily_coverage": critical_daily,
        "tiers": {
            "critical": {
                **critical,
                "pool_version": str(critical_universe.get("version") or ""),
                "pool_sha256": str(critical_universe.get("sha256") or ""),
            },
            "current_analysis_pool": {
                **analysis,
                "pool_version": str(analysis_universe.get("version") or ""),
                "pool_sha256": str(analysis_universe.get("sha256") or ""),
            },
            "extended_research_pool": {
                "status": "separate_historical_queue",
                "requested_count": None,
                "coverage_ratio": None,
                "detail": "独立历史扩展队列；不参与当天 914 分析池质量门。",
            },
        },
        "retry_symbols": list(payload.get("retry_symbols") or []),
        "next_action": str(payload.get("next_action") or ""),
    }


def build_formal_daily_chain_status(
    chain: dict[str, Any] | None,
    *,
    scheduler: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Small UI-facing summary of the authoritative 914-symbol data chain."""

    payload = chain if isinstance(chain, dict) else {}
    schedule = scheduler if isinstance(scheduler, dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    components = payload.get("components") if isinstance(payload.get("components"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    analysis = coverage.get("current_analysis_pool")
    analysis = analysis if isinstance(analysis, dict) else {}
    daily = coverage.get("daily")
    daily = daily if isinstance(daily, dict) else {}
    critical_daily = coverage.get("critical_daily")
    critical_daily = critical_daily if isinstance(critical_daily, dict) else {}
    critical_minute = coverage.get("critical")
    critical_minute = critical_minute if isinstance(critical_minute, dict) else {}
    freeze = components.get("conditional_plan_freeze")
    freeze = freeze if isinstance(freeze, dict) else {}
    analysis_universe = universe.get("current_analysis_pool")
    analysis_universe = analysis_universe if isinstance(analysis_universe, dict) else {}
    critical_daily_universe = universe.get("critical_daily_outside_analysis_pool")
    critical_daily_universe = (
        critical_daily_universe if isinstance(critical_daily_universe, dict) else {}
    )
    retry_symbols = [
        str(item)
        for item in list(payload.get("retry_symbols") or [])
        if str(item).strip()
    ]
    failed_symbols = [
        str(item)
        for item in list(payload.get("failed_symbol_list") or [])
        if str(item).strip()
    ]
    missing_or_retry = sorted(dict.fromkeys(retry_symbols + failed_symbols))
    chain_status = str(payload.get("status") or "not_run")
    analysis_status = str(analysis.get("status") or "")
    analysis_requested = int(
        analysis.get("requested_count")
        or analysis_universe.get("symbol_count")
        or 0
    )
    analysis_complete = int(
        analysis.get("complete_session_count")
        or analysis.get("formal_execution_eligible_count")
        or 0
    )
    pool_status = "missing"
    if chain_status == "completed" and analysis_status == "passed":
        pool_status = "full_pool"
    elif chain_status in {"completed", "partial", "blocked", "failed"} or analysis_status:
        pool_status = "partial_pool"
    return {
        "status": chain_status,
        "authoritative": bool(payload.get("authoritative")),
        "contract_version": payload.get("daily_data_chain_contract_version"),
        "run_id": str(payload.get("run_id") or ""),
        "target_date": str(payload.get("target_date") or "")[:10],
        "generated_at": str(payload.get("generated_at") or payload.get("completed_at") or ""),
        "pool_status": pool_status,
        "analysis_pool_requested": analysis_requested,
        "analysis_pool_complete": analysis_complete,
        "analysis_pool_coverage_ratio": analysis.get("coverage_ratio"),
        "analysis_pool_sha256": str(analysis_universe.get("sha256") or ""),
        "daily_status": str(daily.get("status") or ""),
        "daily_coverage_ratio": daily.get("coverage_ratio"),
        "critical_daily_status": str(critical_daily.get("status") or ""),
        "critical_daily_coverage_ratio": critical_daily.get("coverage_ratio"),
        "critical_daily_outside_count": int(critical_daily_universe.get("symbol_count") or 0),
        "critical_daily_outside_symbols": list(critical_daily_universe.get("symbols") or []),
        "critical_minute_status": str(critical_minute.get("status") or ""),
        "critical_minute_coverage_ratio": critical_minute.get("coverage_ratio"),
        "missing_or_retry_symbols": missing_or_retry[:50],
        "missing_or_retry_count": len(missing_or_retry),
        "plan_freeze_status": str(freeze.get("status") or "not_run"),
        "plan_freeze_written": bool(freeze.get("written")),
        "plan_freeze_reason_codes": [
            str(item) for item in list(freeze.get("reason_codes") or []) if str(item)
        ],
        "scheduler": {
            "status": str(schedule.get("status") or "unknown"),
            "enabled": bool(schedule.get("enabled") or schedule.get("automatic_update_configured")),
            "first_run_pending": bool(schedule.get("first_run_pending")),
            "last_task_result": str(schedule.get("last_task_result") or ""),
            "next_run_time": str(schedule.get("next_run_time") or ""),
            "task_name": str(schedule.get("task_name") or "QuantDesk Daily Data Chain"),
        },
        "reason_codes": [
            str(item)
            for item in list(payload.get("errors") or []) + list(payload.get("warnings") or [])
            if str(item)
        ][:50],
    }



def _ai_trader_scope_observation(target_date: str) -> Any:
    """Observe the AI paper scope: its positions plus any committed intent.

    Read-only by construction. A missing/unreadable ledger returns ``None`` so
    the scope reports ``unknown`` / ``not_observed`` instead of a fabricated
    healthy verdict. This never writes to ``state/ai_trader/``.
    """

    from quantdesk.data_scope_health import SCOPE_AI_TRADER, ScopeObservation

    state_dir = PROJECT_ROOT / "state" / "ai_trader"
    ledger = read_json(state_dir / "ledger.json", {})
    if not isinstance(ledger, dict) or not ledger:
        return None

    symbols: set[str] = set()
    for position in list(ledger.get("positions") or []):
        if isinstance(position, dict):
            symbol = str(position.get("symbol") or position.get("code") or "").strip()
            if symbol:
                symbols.add(symbol)

    plan = read_json(state_dir / "plan_latest.json", {})
    if isinstance(plan, dict) and plan.get("committed"):
        for intent in list(plan.get("intents") or []):
            if isinstance(intent, dict):
                symbol = str(intent.get("symbol") or intent.get("code") or "").strip()
                if symbol:
                    symbols.add(symbol)

    if not symbols:
        # An empty book is a real, healthy observation: nothing is required.
        return ScopeObservation(
            scope=SCOPE_AI_TRADER,
            total_symbols=0,
            covered_symbols=0,
            latest_date=str(ledger.get("as_of") or "")[:10],
            target_date=target_date,
            detail="AI 纸面账本当前无持仓与已提交计划。",
        )

    daily_dir = PROJECT_ROOT / "data" / "daily"
    covered = 0
    missing_symbols: list[str] = []
    for symbol in sorted(symbols):
        if (daily_dir / f"{symbol}.csv").exists():
            covered += 1
        else:
            missing_symbols.append(symbol)

    return ScopeObservation(
        scope=SCOPE_AI_TRADER,
        total_symbols=len(symbols),
        covered_symbols=covered,
        missing_symbols=len(missing_symbols),
        latest_date=str(ledger.get("as_of") or "")[:10],
        target_date=target_date,
        symbols_sample=tuple(missing_symbols[:10]),
        detail="AI 纸面持仓与已提交计划标的的本地日线可用性。",
    )


def _full_universe_scope_observation(target_date: str) -> Any:
    """Read the explicit current-universe audit for a persisted health refresh.

    This scan is intentionally *not* called by the 8000 GET path.  The refresh
    writer owns the relatively expensive local audit; GET later reads the
    resulting A2-v2 scope block.  A current-universe audit is required here
    because the daily-chain ``coverage.daily`` block only describes the bounded
    research pool and must never be relabeled as full-A coverage.
    """

    from quantdesk.data_scope_health import SCOPE_FULL_UNIVERSE, ScopeObservation
    from quantdesk.full_a_daily_coverage import build_full_a_daily_coverage

    audit = build_full_a_daily_coverage(
        target_date=target_date,
        universe_path=FULL_A_UNIVERSE_CSV,
        daily_dir=CANONICAL_DAILY_DIR,
    )
    target_count = int(audit.get("at_target_date_count") or 0)
    stale_count = int(audit.get("stale_count") or 0)
    missing_count = int(audit.get("missing_count") or 0) + int(
        audit.get("invalid_count") or 0
    )
    coverage_floor = str(audit.get("coverage_floor_date") or "")[:10]
    dominant_date = str(audit.get("dominant_latest_date") or "")[:10]
    dominant_count = int(audit.get("dominant_latest_date_count") or 0)
    detail = (
        f"当前全量名册 {int(audit.get('universe_total') or 0)} 只："
        f"目标日覆盖 {target_count}，滞后 {stale_count}，缺失/无效 {missing_count}；"
        f"共同覆盖下限 {coverage_floor or '未知'}。"
    )
    if dominant_date:
        detail += f" 最多 {dominant_count} 只最新日线停在 {dominant_date}。"
    samples = [
        *[str(item) for item in audit.get("missing_symbols_sample") or []],
        *[str(item) for item in audit.get("stale_symbols_sample") or []],
    ]
    return ScopeObservation(
        scope=SCOPE_FULL_UNIVERSE,
        total_symbols=int(audit.get("universe_total") or 0),
        covered_symbols=max(0, int(audit.get("canonical_intersection") or 0)),
        stale_symbols=stale_count,
        missing_symbols=missing_count,
        latest_date=coverage_floor,
        target_date=target_date,
        symbols_sample=tuple(dict.fromkeys(samples))[:10],
        detail=detail,
    )


def _scope_observation_from_cached_scope(scope: Any) -> Any | None:
    """Recover the persisted full-universe observation without a GET rescan."""

    if not isinstance(scope, dict):
        return None
    from quantdesk.data_scope_health import SCOPE_FULL_UNIVERSE, ScopeObservation

    if str(scope.get("scope") or "") != SCOPE_FULL_UNIVERSE:
        return None
    return ScopeObservation(
        scope=SCOPE_FULL_UNIVERSE,
        total_symbols=int(scope.get("total_symbols") or 0),
        covered_symbols=int(scope.get("covered_symbols") or 0),
        stale_symbols=int(scope.get("stale_symbols") or 0),
        missing_symbols=int(scope.get("missing_symbols") or 0),
        latest_date=str(scope.get("as_of") or "")[:10],
        target_date=str(scope.get("target_date") or "")[:10],
        coverage_state=str(scope.get("coverage_state") or "complete"),
        symbols_sample=tuple(str(item) for item in scope.get("symbols_sample") or []),
        detail=str(scope.get("detail") or ""),
    )


def _scheduler_probe_for_scope(
    scheduler: dict[str, Any] | None,
    *,
    live: bool,
) -> dict[str, Any]:
    """Normalize scheduler evidence before the pure A2 classifier sees it."""

    payload = scheduler if isinstance(scheduler, dict) else {}
    if "available" in payload:
        return dict(payload)
    if not live:
        return {
            "available": False,
            "snapshot": {
                "status": str(payload.get("status") or "unknown"),
                "as_of": str(payload.get("generated_at") or "")[:10],
                "last_exit_code": payload.get("last_task_result"),
            },
        }
    return {
        "available": True,
        "state": str(payload.get("status") or "unavailable"),
        "last_exit_code": payload.get("last_task_result"),
        "as_of": str(payload.get("generated_at") or "")[:10],
        "task_name": str(payload.get("task_name") or ""),
    }


def _data_alerts_block(
    scope_health: dict[str, Any] | None,
    *,
    previous_alert_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach A3 alert state without writing or pretending external delivery.

    The returned comparison is pure.  Snapshot writers persist it as part of
    their normal data-health artifact; the 8000 GET route merely compares the
    current local facts against that persisted baseline and never mutates it.
    """

    try:
        from quantdesk.data_alerts import build_data_alerts, diff_alert_states

        previous_payload = (
            previous_alert_payload if isinstance(previous_alert_payload, dict) else {}
        )
        previous = previous_payload.get("current")
        if not isinstance(previous, list):
            previous = []
        current = build_data_alerts(scope_health if isinstance(scope_health, dict) else {})
        state = diff_alert_states(previous=previous, current=current)
        return {
            "schema_version": "a3_data_alerts_v1",
            "current": current,
            **state,
            "comparison_source": (
                "persisted_data_health_snapshot" if previous_payload else "no_persisted_alert_baseline"
            ),
            "read_only": True,
        }
    except Exception as exc:  # noqa: BLE001 - a display failure must be visible, not crash GET
        return {
            "schema_version": "a3_data_alerts_v1",
            "current": [],
            "new": [],
            "ongoing": [],
            "resolved": [],
            "counts": {"new": 0, "ongoing": 0, "resolved": 0, "critical": 0},
            "blocks_ai_trader": False,
            "comparison_source": "unavailable",
            "read_only": True,
            "ok": False,
            "reason_code": "data_alerts_unavailable",
            "detail": f"数据告警构建失败：{type(exc).__name__}:{exc}"[:1000],
            "delivery": {
                "channel": "none",
                "external_push": False,
                "user_notified": False,
                "detail": "数据告警构建失败，未执行任何外部通知。",
            },
        }


def _build_scope_health_block(
    chain: dict[str, Any] | None,
    *,
    formal_chain_status: dict[str, Any] | None = None,
    scheduler: dict[str, Any] | None = None,
    full_universe_observation: Any | None = None,
    scheduler_is_live: bool = False,
) -> dict[str, Any]:
    """Attach the A2 four-scope health payload to the dashboard.

    Each scope keeps its own ``as_of``, so a stale full universe and a fresh
    research pool are stated side by side instead of collapsing into one date.
    Failures here must not break the read-only dashboard route, so the block
    degrades to an ``unknown`` payload rather than raising.
    """

    try:
        from quantdesk.data_scope_health import (
            build_scope_health,
            observations_from_daily_chain,
        )

        payload = chain if isinstance(chain, dict) else {}
        target = str(payload.get("target_date") or "")[:10]
        formal = formal_chain_status if isinstance(formal_chain_status, dict) else {}
        daily_coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        pool_daily = daily_coverage.get("daily") if isinstance(daily_coverage.get("daily"), dict) else {}
        pool_has_target_evidence = bool(
            int(pool_daily.get("fresh_count") or 0)
            or int(pool_daily.get("reported_count") or 0)
            or int(pool_daily.get("requested_count") or 0)
        )
        observations = observations_from_daily_chain(
            payload,
            target_date=target,
            full_universe_observation=full_universe_observation,
            # The bounded daily update result is the research-pool evidence.
            # Its target date is an observed attempted target, while failed or
            # stale symbols retain their non-OK verdict through the counts.
            research_pool_latest=target if pool_has_target_evidence else "",
            ai_trader=_ai_trader_scope_observation(target),
        )
        return build_scope_health(
            observations,
            target_date=target,
            scheduler=_scheduler_probe_for_scope(
                scheduler,
                live=scheduler_is_live,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - dashboard GET must stay read-only and safe
        return {
            "ok": False,
            "scopes": {},
            "overall_status": "unknown",
            "reason_code": "scope_health_unavailable",
            "detail": f"范围化数据健康暂不可用：{exc}",
        }


def _status_rank(status: str) -> int:
    return {"green": 0, "yellow": 1, "red": 2}.get(str(status or "").lower(), 1)


def _overall(cards: dict[str, dict[str, Any]]) -> str:
    worst = max((_status_rank(card.get("status", "yellow")) for card in cards.values()), default=1)
    return {0: "green", 1: "yellow", 2: "red"}[worst]


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _daily_card(daily: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    target = _date_text(freshness.get("target_complete_trade_date") or daily.get("target_date"))
    latest = _date_text(freshness.get("local_daily_latest_date") or daily.get("latest_local_date") or daily.get("max_local_date"))
    max_local = _date_text(daily.get("max_local_date") or daily.get("latest_local_date") or freshness.get("local_daily_latest_date"))
    total = int(daily.get("total_symbols") or 0)
    stale = int(daily.get("stale_count") or freshness.get("stale_symbol_count") or 0)
    missing = int(daily.get("missing_count") or freshness.get("missing_symbol_count") or 0)
    covered = max(0, total - stale - missing)
    # The target day's complete bar is present in the pool when the freshest
    # symbol has reached it. If it has, a few lagging/bad-data symbols make the
    # layer DEGRADED (yellow) — today's focus data is still usable — not unusable
    # (red). Red is reserved for "no symbol reached the target day" or missing
    # symbols, i.e. a genuinely unusable daily layer. This avoids painting the
    # whole layer red for a single stale stock while staying honest about gaps.
    target_reached = bool(target and max_local and max_local >= target)
    has_data = bool(latest or max_local)
    if not has_data:
        status = "red"
        value = "暂无本地日线"
        detail = f"目标完整交易日 {target or '-'}：本地没有可用日线。"
        next_action = "先运行数据更新或 data-bootstrap 拉取低价池日线。"
    elif missing or not target_reached:
        status = "red"
        value = f"低价池停留在 {max_local or latest or '-'}"
        if not target_reached:
            detail = (
                f"低价观察池完整日线停留在 {max_local or latest or '-'}，尚未更新到目标完整交易日 {target or '-'}，"
                f"请联网更新数据。（“今日重点”候选数据为独立批次，请以今日重点页为准；本卡只反映低价观察池覆盖。）"
            )
        else:
            detail = f"目标完整交易日 {target or '-'}：缺失 {missing} 只，滞后 {stale} 只，请补全后再复盘。"
        next_action = "联网更新低价池日线到目标交易日，再看复盘结果。"
    elif stale:
        status = "yellow"
        value = f"目标日 {target} 已覆盖（{covered}/{total}）"
        detail = f"目标完整交易日 {target}：{covered}/{total} 只已覆盖到目标日，{stale} 只滞后到更早日期（最旧 {latest or '-'}）。今日重点数据可用，可单独补这几只。"
        next_action = "今日重点数据可用；如需完整覆盖，可单独补滞后的少数股票。"
    else:
        status = "green"
        value = f"已更新到 {latest}"
        detail = f"{covered} / {total} 只低价池股票覆盖目标日期 {target or latest}。"
        next_action = "可继续查看候选与虚拟组合复盘。"
    return {
        "status": status,
        "title": "日线数据",
        "value": value,
        "detail": detail,
        "next_action": next_action,
    }


def _intraday_card(intraday: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    total = int(intraday.get("total_symbols") or 0)
    available = int(intraday.get("available_count") or 0)
    missing = int(intraday.get("missing_count") or intraday.get("missing_intraday_count") or 0)
    stale = int(intraday.get("stale_count") or 0)
    target = _date_text(intraday.get("target_trade_date") or intraday.get("target_date"))
    verified_pool = intraday.get("verified_minute_pool") if isinstance(intraday.get("verified_minute_pool"), dict) else {}
    verified_pool_status = str(verified_pool.get("status") or "").lower()
    verified_pool_available = int(
        intraday.get("verified_minute_pool_available_count")
        or verified_pool.get("available_count")
        or 0
    )
    verified_pool_missing = int(intraday.get("verified_minute_pool_missing_count") or verified_pool.get("missing_count") or 0)
    verified_pool_total = int(verified_pool.get("total_symbols") or verified_pool.get("usable_count") or 0)
    verified_pool_usable = int(verified_pool.get("usable_count") or verified_pool_total or 0)
    verified_pool_target = _date_text(verified_pool.get("target_date"))
    verified_pool_passed = verified_pool_status == "passed" and verified_pool_usable > 0
    verified_pool_provenance: list[str] = []
    if str(verified_pool.get("source_snapshot") or "") == "last_passed":
        verified_pool_provenance.append("来源为最近一次通过质检的快照")
    latest_quality_status = str(verified_pool.get("latest_quality_status") or "").lower()
    quality_label = {
        "passed": "通过",
        "partial": "部分完成",
        "failed": "失败",
        "missing": "缺失",
    }.get(latest_quality_status, "")
    if quality_label:
        verified_pool_provenance.append(f"最新质检状态 {quality_label}")
    provenance_detail = ("；" + "；".join(verified_pool_provenance)) if verified_pool_provenance else ""
    verified_pool_detail = (
        f"已验证分钟池质量通过：{verified_pool_usable}/{verified_pool_total or verified_pool_usable} 可用"
        f"（目标日 {verified_pool_target or '-'}）{provenance_detail}。"
        if verified_pool_passed
        else ""
    )
    verified_scope_detail = (
        f"当前计划命中已验证池 {verified_pool_available}/{total}，未命中 {verified_pool_missing}；"
        if verified_pool_passed and total
        else ""
    )
    latest_available_dates = [
        _date_text(item.get("latest_available_date") or item.get("latest_available_datetime"))
        for item in intraday.get("items") or []
        if isinstance(item, dict)
    ]
    latest_available = max([date for date in latest_available_dates if date], default="")
    if total and available >= total and missing == 0 and stale == 0:
        status = "green"
        if verified_pool_passed and verified_pool_available >= total:
            value = "已验证分钟池覆盖当前计划"
        elif verified_pool_passed and verified_pool_available > 0:
            value = "本地分钟线可用，部分命中已验证池"
        elif verified_pool_passed:
            value = "本地分钟线可用；已验证池待命中"
        else:
            value = "分钟线可用于盘中触发验证"
        detail = f"{available} / {total} 只计划股票已有 {target or '-'} 的 1 分钟线。{verified_scope_detail}{verified_pool_detail}"
        next_action = "可重新执行条件交易计划。"
    elif total and stale > 0 and missing == 0:
        status = "yellow"
        value = "已有最近分钟线，下一计划日尚未可验证"
        detail = (
            f"本地 1 分钟线最新到 {latest_available or '-'}；当前条件计划目标日为 "
            f"{target or '-'}，该日期尚未覆盖，不能用更早分钟线假装盘中触发。{verified_scope_detail}{verified_pool_detail}"
        )
        next_action = "等目标交易日生成分钟线后再验证盘中触发；现在可先查看日线复盘。"
    elif verified_pool_passed:
        status = "yellow"
        value = "已验证分钟池可先用于覆盖内股票"
        if total:
            detail = (
                f"{verified_pool_detail} {verified_scope_detail}当前计划股票 {available}/{total} 只已有目标日 {target or '-'} "
                f"的 1 分钟线，缺少 {missing} 只；未覆盖股票仍按缺分钟线降级。"
            )
        else:
            detail = (
                f"{verified_pool_detail} 当前还没有可检查的条件计划股票；Top200 可先用于纸面回放/研究复盘，"
                "2656 扩展池继续补覆盖。"
            )
        next_action = "覆盖内股票可先做纸面分钟回放；未覆盖股票按缺分钟线降级，等待扩展池或手工导入，不用日线假装成交。"
    else:
        status = "yellow"
        value = "缺少历史分钟线"
        provider_summary = provider.get("summary") if isinstance(provider.get("summary"), dict) else {}
        if provider_summary and int(provider_summary.get("ok_count") or 0) == 0:
            detail = "当前 provider 未能获取目标历史 1 分钟线。"
        elif total:
            detail = f"{available} / {total} 只股票有分钟线，缺少 {missing} 只。"
        else:
            detail = "当前没有可检查的条件计划股票，或尚未生成分钟线缺口状态。"
        next_action = "可手工导入分钟线，或只查看日线复盘。"
    return {
        "status": status,
        "title": "1分钟线",
        "value": value,
        "detail": detail,
        "next_action": next_action,
        "local_total_symbols": total,
        "local_available_count": available,
        "local_missing_count": missing,
        "local_stale_count": stale,
        "verified_minute_pool": {
            "status": verified_pool_status or "missing",
            "usable_count": verified_pool_usable,
            "total_symbols": verified_pool_total,
            "available_count": verified_pool_available,
            "missing_count": verified_pool_missing,
            "target_date": verified_pool_target,
            "source_snapshot": verified_pool.get("source_snapshot") or "",
            "latest_quality_status": verified_pool.get("latest_quality_status") or "",
            "latest_snapshot_path": verified_pool.get("latest_snapshot_path") or "",
            "warnings": list(verified_pool.get("warnings") or []),
            "research_only": True,
            "paper_only": True,
        },
    }


def _theme_card(theme: dict[str, Any], current_candidate_theme: dict[str, Any] | None = None) -> dict[str, Any]:
    current_candidate_theme = current_candidate_theme or {}
    candidate_count = int(current_candidate_theme.get("candidate_count") or 0)
    mapped_current = int(current_candidate_theme.get("mapped_current_candidates") or current_candidate_theme.get("mapped_count") or 0)
    unknown_current = int(current_candidate_theme.get("unknown_current_candidates") or current_candidate_theme.get("unknown_count") or 0)
    total = int(theme.get("total") or theme.get("total_rows") or 0)
    mapped = int(theme.get("mapped_count") or theme.get("total_mapped") or 0)
    unknown = int(theme.get("unknown_count") or 0)
    if candidate_count:
        if unknown_current == 0 and mapped_current == candidate_count:
            status = "green"
            value = "当前候选已映射"
            detail = f"当前候选板块映射 {mapped_current} / {candidate_count}。"
            next_action = "可按板块/题材查看分组。"
        else:
            status = "yellow"
            value = "部分候选题材未知"
            detail = f"当前候选板块映射 {mapped_current} / {candidate_count}，仍有 {unknown_current} 只未知。"
            next_action = "点击生成待补导入草稿，再导入正式映射。"
    elif total and mapped:
        status = "yellow" if unknown else "green"
        value = "已有部分映射" if unknown else "映射较完整"
        detail = f"全局映射 {mapped} / {total}，未知 {unknown}。"
        next_action = "生成候选后再检查当前候选映射。"
    else:
        status = "yellow"
        value = "板块/题材待补"
        detail = str(theme.get("failed_reason") or "尚未获得真实板块/题材映射。")
        next_action = "先生成待补导入草稿，或手工导入 stock_theme_map_import.csv。"
    return {
        "status": status,
        "title": "板块/题材",
        "value": value,
        "detail": detail,
        "next_action": next_action,
    }


def _portfolio_card(portfolio: dict[str, Any], freshness: dict[str, Any], *, daily_status: str = "red") -> dict[str, Any]:
    positions = portfolio.get("positions") if isinstance(portfolio.get("positions"), list) else []
    position_count = len(positions)
    if daily_status == "red":
        # Only block portfolio review when the daily layer is genuinely unusable
        # (no symbol reached the target day). A partial lag (yellow) still allows
        # a broadly-valid valuation, so we degrade to yellow instead of red.
        status = "red"
        value = "等待完整日线"
        detail = "完整日线尚未覆盖目标交易日时，不能可靠判断昨日持仓收益。"
        next_action = "先补日线，再查看昨日持仓复盘。"
    elif freshness.get("is_daily_stale"):
        status = "yellow"
        value = "日线部分滞后"
        detail = "目标日线整体可用，但少数股票滞后，组合估值可能不完整。"
        next_action = "可先复盘，必要时补齐滞后股票后再核对收益。"
    elif portfolio.get("exists") is False or not portfolio:
        status = "yellow"
        value = "尚未读取自动虚拟组合"
        detail = "没有自动虚拟组合快照时，只能先看候选和计划。"
        next_action = "第一次使用时可初始化自动虚拟组合；已有组合则用每日复盘。"
    else:
        status = "green" if freshness.get("portfolio_review_valid", True) else "yellow"
        value = "日线复盘可用"
        detail = f"当前自动虚拟组合持仓 {position_count} 只。完整日线可用于收益复盘；盘中触发需要分钟线。"
        next_action = "查看自动虚拟组合摘要。"
    return {
        "status": status,
        "title": "虚拟仓复盘",
        "value": value,
        "detail": detail,
        "next_action": next_action,
    }


def _summary_text(cards: dict[str, dict[str, Any]], level: str) -> str:
    if level == "red":
        return "关键数据不足。先修复红色数据项，再看候选和复盘。"
    intraday_card = cards.get("intraday_data", {})
    verified_pool = intraday_card.get("verified_minute_pool") if isinstance(intraday_card.get("verified_minute_pool"), dict) else {}
    if intraday_card.get("status") == "yellow" and verified_pool.get("status") == "passed":
        return f"{intraday_card.get('value')}；未覆盖股票仍按缺分钟线降级。"
    if intraday_card.get("status") == "yellow" and "下一计划日" in str(intraday_card.get("value") or ""):
        return "日线数据可用于收盘复盘；分钟线已有最近数据，但下一计划日尚未可验证。"
    if intraday_card.get("status") == "yellow":
        return "日线数据可用于收益复盘，但历史分钟线缺失，当前不能验证盘中触发。"
    return "核心数据状态较完整，可以查看今日候选、自动虚拟组合和条件交易计划。"


def _beginner_steps(cards: dict[str, dict[str, Any]], level: str) -> list[str]:
    steps: list[str] = []
    if cards.get("daily_data", {}).get("status") == "red":
        steps.append("先不要急着看策略结果。当前完整日线不足，候选和复盘可能过时。")
        steps.append("请先点击“联网补全低价池日线”，补完后再运行今日一键复盘。")
    else:
        steps.append("先查看日线数据是否为绿色；绿色表示可以做日线收益复盘。")
        steps.append("再查看自动虚拟组合的日线盈亏。")
    intraday_card = cards.get("intraday_data", {})
    if intraday_card.get("status") == "yellow":
        if "下一计划日" in str(intraday_card.get("value") or ""):
            steps.append("如果盘中验证为黄色，先看详情：这可能只是下一计划日尚未发生或尚未覆盖，不代表今天的分钟线没有下载。")
        else:
            steps.append("如果盘中验证为黄色，不代表策略失败，只是缺少 1 分钟线。")
    if cards.get("theme_map", {}).get("status") == "yellow":
        steps.append("当前候选中仍可能有未知板块，可先生成待补导入草稿，再导入正式映射。")
    if level == "green":
        steps.append("数据状态较完整，可以查看今日候选、自动虚拟组合和条件交易计划。")
    steps.append("所有结果只用于学习观察，不构成投资建议。")
    return steps[:5]


def _candidate_sort_key(row: dict[str, Any]) -> tuple[int, float]:
    rank = row.get("rank")
    try:
        rank_value = int(rank) if rank not in (None, "") else 999999
    except Exception:
        rank_value = 999999
    score = safe_float(row.get("score") or row.get("long_score") or row.get("board_score"), 0.0) or 0.0
    return (rank_value, -score)


def _top_candidates(strategy_payload: dict[str, Any], theme_map: Any, freshness: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    candidates = candidate_rows_from_strategy_payload(strategy_payload)
    if not candidates:
        workflow = read_json(DAILY_WORKFLOW_LATEST_JSON, {})
        auto_strategy = workflow.get("auto_strategy") if isinstance(workflow.get("auto_strategy"), dict) else {}
        candidates = candidate_rows_from_strategy_payload(auto_strategy)
    candidates = attach_theme_map_to_candidates(candidates, theme_map)
    target = _date_text(freshness.get("target_complete_trade_date") or "")
    latest = _date_text(freshness.get("daily_feature_date") or freshness.get("local_daily_latest_date") or "")
    covered = bool(target and latest and latest >= target and not freshness.get("is_daily_stale"))
    output: list[dict[str, Any]] = []
    for row in sorted(candidates, key=_candidate_sort_key)[: max(0, int(limit or 3))]:
        item = normalize_candidate_row(row)
        item.update({
            "sector": row.get("sector") or item.get("sector"),
            "primary_theme": row.get("primary_theme") or row.get("theme") or item.get("primary_theme"),
            "data_latest_date": latest or item.get("data_latest_date"),
            "is_target_date_covered": covered,
            "data_freshness_label": "目标日期候选" if covered else "滞后数据候选",
            "disclaimer": "仅用于学习观察，不构成投资建议。",
        })
        output.append(item)
    return output


def _data_source_trace() -> dict[str, str]:
    return {
        "daily_data": "local_existing -> mootdx_daily -> tencent_daily -> akshare_daily",
        "intraday_data": "local_existing -> mootdx_intraday -> tencent_intraday -> akshare_intraday",
        "theme_map": "local_theme_map -> ths_hotspot -> akshare_theme",
        "news": "cls_news -> eastmoney_news",
        "research_report": "akshare_report -> iwencai_report",
        "announcement": "cninfo_via_akshare -> mootdx_announcement",
        "fundamentals": "mootdx_fundamental -> akshare_basic",
    }


def _list_text(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _snapshot_age_seconds(generated_at: Any) -> float | None:
    """Age of a snapshot timestamp in seconds, or None when unparsable."""

    text = str(generated_at or "").strip()
    if not text:
        return None
    try:
        generated = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(generated.tzinfo) if generated.tzinfo else datetime.now()
    return max(0.0, round((now - generated).total_seconds(), 3))


def _extended_gate_summary(gates: dict[str, Any] | None) -> dict[str, Any]:
    payload = gates if isinstance(gates, dict) else {}
    chain = payload.get("gate_chain") if isinstance(payload.get("gate_chain"), dict) else {}
    queue = chain.get("queue") if isinstance(chain.get("queue"), dict) else {}
    minute = chain.get("minute") if isinstance(chain.get("minute"), dict) else {}
    import_audit = chain.get("import_audit") if isinstance(chain.get("import_audit"), dict) else {}
    import_verification = chain.get("import_verification") if isinstance(chain.get("import_verification"), dict) else {}
    policy = payload.get("operator_action_policy") if isinstance(payload.get("operator_action_policy"), dict) else {}
    stage_progress = queue.get("stage_progress") if isinstance(queue.get("stage_progress"), dict) else {}
    return {
        "extended_data_gates_snapshot_task_type": payload.get("task_type") or "",
        "extended_data_gates_snapshot_status": payload.get("status") or "",
        "extended_data_gates_snapshot_generated_at": payload.get("generated_at") or "",
        "extended_data_gates_snapshot_age_seconds": _snapshot_age_seconds(payload.get("generated_at")),
        "extended_data_current_gate": payload.get("current_gate") or "",
        "extended_data_gate_suggested_next_action": payload.get("suggested_next_action") or "",
        "extended_data_operator_wait_reason": payload.get("operator_wait_reason") or "",
        "extended_data_manual_review_required": bool(policy.get("manual_review_required")),
        "extended_data_manual_review_reason": policy.get("manual_review_reason") or "",
        "extended_data_requires_user_confirmation": bool(policy.get("requires_user_confirmation")),
        "extended_data_confirmation_required_command": policy.get("confirmation_required_command") or "",
        "extended_data_safe_next_commands": _list_text(policy.get("safe_next_commands")),
        "extended_data_blocked_without_confirmation": _list_text(policy.get("blocked_without_confirmation")),
        "extended_data_queue_ok_count": int(queue.get("ok_count") or 0),
        "extended_data_queue_fail_count": int(queue.get("fail_count") or 0),
        "extended_data_queue_traceback_count": int(queue.get("traceback_count") or 0),
        "extended_data_queue_liveness": queue.get("queue_liveness") or "",
        "extended_data_queue_progress_state": queue.get("progress_state") or "",
        "extended_data_queue_stage_progress": queue.get("stage_position_label") or stage_progress.get("stage_position_label") or "",
        "extended_data_queue_stage_requested_count": int(queue.get("stage_requested_count") or 0),
        "extended_data_queue_stage_completed_count": int(queue.get("stage_completed_count") or 0),
        "extended_data_queue_stage_remaining_count": int(queue.get("stage_remaining_count") or 0),
        "extended_data_queue_stage_remaining_keys": _list_text(queue.get("stage_remaining_keys")),
        "tushare_minute_status": minute.get("status") or "",
        "tushare_minute_symbol_done_count": int(minute.get("symbol_done_count") or 0),
        "tushare_minute_symbol_count": int(minute.get("symbol_count") or 0),
        "tushare_minute_target_symbol_count": int(
            minute.get("target_symbol_count") or minute.get("symbol_count") or 0
        ),
        "tushare_minute_last_symbol": minute.get("last_symbol") or "",
        "tushare_minute_traceback_count": int(minute.get("traceback_count") or 0),
        "tushare_minute_suggested_next_action": minute.get("suggested_next_action") or "",
        "tushare_minute_current_available_count": int(minute.get("current_available_count") or 0),
        "tushare_minute_current_available_ratio": minute.get("current_available_ratio"),
        "tushare_minute_current_available_count_source": minute.get("current_available_count_source") or "",
        "tushare_minute_canonical_current_available_count": int(minute.get("canonical_current_available_count") or 0),
        "tushare_minute_staged_current_not_imported_count": int(minute.get("staged_current_not_imported_count") or 0),
        "tushare_minute_canonical_current_available_ratio": minute.get("canonical_current_available_ratio"),
        "tushare_minute_staged_current_not_imported_ratio": minute.get("staged_current_not_imported_ratio"),
        "tushare_minute_missing_or_stale_count": int(minute.get("missing_or_stale_count") or 0),
        "tushare_minute_latest_completed_quality_status": minute.get("latest_completed_quality_status") or "",
        "tushare_minute_latest_completed_quality_total_symbols": int(minute.get("latest_completed_quality_total_symbols") or 0),
        "tushare_minute_latest_completed_quality_usable_count": int(minute.get("latest_completed_quality_usable_count") or 0),
        "tushare_minute_latest_completed_quality_invalid_count": int(minute.get("latest_completed_quality_invalid_count") or 0),
        "tushare_minute_latest_completed_quality_missing_count": int(minute.get("latest_completed_quality_missing_count") or 0),
        "tushare_minute_latest_completed_quality_target_date": minute.get("latest_completed_quality_target_date") or "",
        "tushare_minute_latest_completed_job_symbol_count": int(minute.get("latest_completed_job_symbol_count") or 0),
        "tushare_minute_latest_completed_job_canonical_current_count": int(
            minute.get("latest_completed_job_canonical_current_count") or 0
        ),
        "tushare_minute_coverage_suggested_next_action": (
            minute.get("coverage_suggested_next_action")
            or minute.get("suggested_next_action")
            or ""
        ),
        "extended_import_preapply_gate": import_audit.get("preapply_gate") or "",
        "extended_import_gate": import_verification.get("post_import_gate") or import_audit.get("preapply_gate") or "",
        "extended_import_domain_counts": import_audit.get("domain_counts") if isinstance(import_audit.get("domain_counts"), dict) else {},
        "extended_import_missing_planned_domains": _list_text(import_audit.get("missing_planned_domains")),
        "extended_import_would_improve_domains": _list_text(import_audit.get("would_improve_domains")),
        "extended_import_source_scan_limit_applied": bool(import_audit.get("source_scan_limit_applied")),
        "extended_import_source_scan_limited_domains": _list_text(import_audit.get("source_scan_limited_domains")),
        "extended_import_max_files_per_rule": int(import_audit.get("max_files_per_rule") or 0),
        "extended_import_full_audit_command": import_audit.get("full_audit_command") or "",
        "extended_import_source_scan_full_audit_command": import_audit.get("source_scan_full_audit_command") or "",
    }


def _data_capability_summary(
    cards: dict[str, dict[str, Any]],
    extended: dict[str, Any],
) -> dict[str, Any]:
    """Explain what the current local evidence can and cannot support."""

    available_now: list[dict[str, str]] = []
    if cards.get("daily_data", {}).get("status") in {"green", "yellow"}:
        available_now.append({"label": "日线收益复盘", "status": "available"})
    available_now.append({"label": "只读下载监控", "status": "available"})

    waiting_on: list[dict[str, str]] = []
    minute_done = int(extended.get("tushare_minute_symbol_done_count") or 0)
    minute_target = int(extended.get("tushare_minute_target_symbol_count") or 0)
    minute_status = str(extended.get("tushare_minute_status") or "").lower()
    if (minute_target and minute_done < minute_target) or "running" in minute_status:
        waiting_on.append({"label": "分钟扩展池", "status": "waiting_for_download"})
    if str(extended.get("extended_data_current_gate") or "") == "download_queue":
        waiting_on.append({"label": "扩展数据下载队列", "status": "waiting_for_download"})
    if str(extended.get("extended_import_preapply_gate") or "").startswith("wait_for_download_completion"):
        waiting_on.append({"label": "暂存扩展导入", "status": "waiting_for_download"})

    currently_forbidden = [
        {"label": "导入 apply", "status": "requires_explicit_authorization"},
        {"label": "正式市场数据写入", "status": "protected"},
    ]
    return {
        "research_only": True,
        "paper_only": True,
        "canonical_writes_allowed": False,
        "available_now": available_now,
        "waiting_on": waiting_on,
        "currently_forbidden": currently_forbidden,
        "operator_summary_text": (
            f"现在可用 {len(available_now)} 项；等待下载 {len(waiting_on)} 项；"
            f"当前禁止 {len(currently_forbidden)} 项。"
        ),
    }


def build_data_health_dashboard(
    *,
    daily_price_payload: dict[str, Any] | None = None,
    freshness_payload: dict[str, Any] | None = None,
    intraday_payload: dict[str, Any] | None = None,
    theme_payload: dict[str, Any] | None = None,
    current_candidate_theme_payload: dict[str, Any] | None = None,
    provider_capability_payload: dict[str, Any] | None = None,
    portfolio_payload: dict[str, Any] | None = None,
    daily_workflow_payload: dict[str, Any] | None = None,
    real_sample_payload: dict[str, Any] | None = None,
    self_validation_payload: dict[str, Any] | None = None,
    strategy_payload: dict[str, Any] | None = None,
    extended_gates_payload: dict[str, Any] | None = None,
    daily_data_chain_payload: dict[str, Any] | None = None,
    daily_scheduler_payload: dict[str, Any] | None = None,
    evidence_freshness_payload: dict[str, Any] | None = None,
    full_universe_observation: Any | None = None,
    theme_map: Any | None = None,
    output_path: Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Build the beginner data-health payload.

    ``write`` defaults to False so server GET handlers remain strictly read-only.
    """

    daily = daily_price_payload if isinstance(daily_price_payload, dict) else daily_price_status(only_low_price=True)
    freshness = freshness_payload if isinstance(freshness_payload, dict) else evaluate_data_freshness(daily_price_payload=daily)
    intraday = intraday_payload if isinstance(intraday_payload, dict) else intraday_gap_status(write=False)
    theme = theme_payload if isinstance(theme_payload, dict) else theme_map_status()
    current_theme = (
        current_candidate_theme_payload
        if isinstance(current_candidate_theme_payload, dict)
        else validate_theme_map_for_current_candidates(THEME_MAP_PATH)
    )
    provider = provider_capability_payload if isinstance(provider_capability_payload, dict) else latest_intraday_provider_capability()
    portfolio = portfolio_payload if isinstance(portfolio_payload, dict) else auto_paper_portfolio_latest()
    workflow = daily_workflow_payload if isinstance(daily_workflow_payload, dict) else read_json(DAILY_WORKFLOW_LATEST_JSON, {})
    real_sample = real_sample_payload if isinstance(real_sample_payload, dict) else latest_real_intraday_sample_validation()
    self_validation = self_validation_payload if isinstance(self_validation_payload, dict) else latest_intraday_replay_self_validation()
    strategy = strategy_payload if isinstance(strategy_payload, dict) else read_json(AUTO_STRATEGY_LATEST_JSON, {})
    daily_chain = (
        daily_data_chain_payload
        if isinstance(daily_data_chain_payload, dict)
        else latest_daily_data_chain(PROJECT_ROOT)
    )
    scheduler = (
        daily_scheduler_payload
        if isinstance(daily_scheduler_payload, dict)
        else (
            daily_data_scheduler_status(project_root=PROJECT_ROOT)
            if write
            else {
                "ok": True,
                "status": "not_probed",
                "automatic_update_configured": False,
                "reason_codes": ["scheduler_status_requires_snapshot_refresh"],
                "read_only": True,
            }
        )
    )
    consumer_gate = evaluate_data_chain_consumer_gate(
        daily_chain,
        project_root=PROJECT_ROOT,
    )
    minute_universe_coverage = build_minute_universe_coverage(daily_chain)
    formal_chain_status = build_formal_daily_chain_status(
        daily_chain,
        scheduler=scheduler,
    )
    full_scope = full_universe_observation
    if full_scope is None and write:
        # The expensive current-universe audit is part of an explicit snapshot
        # refresh only.  A normal 8000 GET will reuse the persisted v2 result.
        full_scope = _full_universe_scope_observation(
            str(daily_chain.get("target_date") or "")[:10]
        )
    scope_health = _build_scope_health_block(
        daily_chain,
        formal_chain_status=formal_chain_status,
        scheduler=scheduler,
        full_universe_observation=full_scope,
        scheduler_is_live=bool(write),
    )
    previous_alert_payload: dict[str, Any] = {}
    if write:
        previous_snapshot = read_json(
            output_path or DATA_HEALTH_DASHBOARD_LATEST_JSON,
            {},
        )
        if isinstance(previous_snapshot, dict):
            candidate = previous_snapshot.get("data_alerts")
            if isinstance(candidate, dict):
                previous_alert_payload = candidate
    data_alerts = _data_alerts_block(
        scope_health,
        previous_alert_payload=previous_alert_payload,
    )
    evidence_freshness = (
        evidence_freshness_payload
        if isinstance(evidence_freshness_payload, dict)
        else build_evidence_freshness(PROJECT_ROOT)
    )
    extended_gates = (
        extended_gates_payload
        if isinstance(extended_gates_payload, dict)
        else read_json(EXTENDED_DATA_GATES_LATEST_JSON, {})
    )
    extended_gate_summary = _extended_gate_summary(extended_gates)
    extended_gate_summary["extended_data_gates_snapshot_path"] = EXTENDED_DATA_GATES_LATEST_JSON.relative_to(PROJECT_ROOT).as_posix()
    extended_gate_summary["extended_data_gates_snapshot_exists"] = bool(extended_gates)
    loaded_theme_map = theme_map if theme_map is not None else load_theme_map()

    daily_card = _daily_card(daily, freshness)
    cards = {
        "daily_data": daily_card,
        "intraday_data": _intraday_card(intraday, provider),
        "theme_map": _theme_card(theme, current_theme),
        "portfolio_review": _portfolio_card(portfolio, freshness, daily_status=str(daily_card.get("status") or "red")),
    }
    source_trace = _data_source_trace()
    for key, trace in source_trace.items():
        if key in cards:
            cards[key]["data_source_trace"] = trace
    card_level = _overall(cards)
    # The evidence payload is already computed read-only above.  Apply its
    # presentation overlay on GET as well as on persisted refreshes; this does
    # not alter the market-data cards or probe the scheduler in read-only mode.
    level, degraded_reasons = _apply_health_evidence_overlay(
        card_level,
        daily_chain=daily_chain,
        scheduler=scheduler,
        evidence_freshness=evidence_freshness,
    )
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "health_level": level,
        "market_health_level": card_level,
        "summary_text": _summary_text(cards, card_level),
        "cards": cards,
        "data_source_trace": source_trace,
        "beginner_next_steps": _beginner_steps(cards, level),
        "data_capability_summary": _data_capability_summary(cards, extended_gate_summary),
        "top_observation_candidates": _top_candidates(strategy, loaded_theme_map, freshness, limit=3),
        "daily_data_chain": daily_chain,
        "daily_data_scheduler": scheduler,
        "evidence_freshness": evidence_freshness,
        "health_degraded_reason_codes": degraded_reasons,
        "health_level_overridden_from_cards": level != card_level,
        "formal_daily_chain_status": formal_chain_status,
        "scope_health": scope_health,
        "data_alerts": data_alerts,
        "downstream_consumer_gate": {
            key: consumer_gate.get(key)
            for key in (
                "status",
                "allowed",
                "authoritative",
                "run_id",
                "target_date",
                "reasons",
                "chain_status",
            )
        },
        "minute_universe_coverage": minute_universe_coverage,
        "source_status": {
            "daily_price": daily,
            "freshness": freshness,
            "intraday_gap": intraday,
            "theme_map": theme,
            "current_candidate_theme": current_theme,
            "provider_capability": provider,
            "auto_paper_portfolio": portfolio,
            "daily_workflow": workflow if isinstance(workflow, dict) else {},
            "real_intraday_sample_validation": real_sample,
            "intraday_self_validation": self_validation,
            "extended_data_gates": extended_gates if isinstance(extended_gates, dict) else {},
            "daily_data_chain": daily_chain,
            "daily_data_scheduler": scheduler,
            "audit_evidence": evidence_freshness,
        },
        "risk_disclaimer": RISK_DISCLAIMER,
        "network": False,
        "full_scan": False,
        "is_real_order": False,
        "message": "数据健康状态只读聚合完成；未联网、未更新数据、未改变虚拟组合。",
    }
    payload["summary_text"] = _append_evidence_summary(
        payload["summary_text"], evidence_freshness
    )
    payload.update(extended_gate_summary)
    if write:
        target = output_path or DATA_HEALTH_DASHBOARD_LATEST_JSON
        write_json(target, payload)
        payload["path"] = _relpath_for_payload(target)
    return payload




def build_cached_data_health_dashboard(
    *,
    snapshot_path: Path | None = None,
    daily_data_chain_payload: dict[str, Any] | None = None,
    evidence_freshness_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serve the persisted dashboard with a lightweight live-chain overlay.

    Opening the web page must not rescan hundreds of canonical CSV files.
    Operators refresh the expensive snapshot explicitly with qd data-health.
    """

    target = Path(snapshot_path or DATA_HEALTH_DASHBOARD_LATEST_JSON)
    cached = read_json(target, {})
    if isinstance(cached, dict) and cached:
        payload = dict(cached)
        payload["snapshot_mode"] = "persisted_overlay"
        payload["snapshot_exists"] = True
    else:
        payload = {
            "ok": True,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "health_level": "yellow",
            "summary_text": "尚未生成数据健康快照；当前仅显示每日数据链实时状态。",
            "cards": {},
            "source_status": {},
            "beginner_next_steps": [
                "运行 python scripts/qd.py data-health --network false --scope current_analysis_pool 刷新完整快照。"
            ],
            "risk_disclaimer": RISK_DISCLAIMER,
            "network": False,
            "full_scan": False,
            "is_real_order": False,
            "generated_at": "",
            "snapshot_mode": "live_chain_only",
            "snapshot_exists": False,
        }

    chain = (
        daily_data_chain_payload
        if isinstance(daily_data_chain_payload, dict)
        else latest_daily_data_chain(PROJECT_ROOT)
    )
    minute_coverage = build_minute_universe_coverage(chain)
    cached_scope_health = (
        payload.get("scope_health") if isinstance(payload.get("scope_health"), dict) else {}
    )
    cached_alert_payload = (
        payload.get("data_alerts") if isinstance(payload.get("data_alerts"), dict) else {}
    )
    cached_full_scope = None
    if cached_scope_health.get("schema_version") == "a2_scope_health_v2":
        cached_full_scope = _scope_observation_from_cached_scope(
            (cached_scope_health.get("scopes") or {}).get("full_universe")
        )
    # The task-scheduler probe is local and read-only.  It is deliberately
    # refreshed on GET because the persisted dashboard can otherwise show an
    # old "running" record after the actual task already failed.
    scheduler = daily_data_scheduler_status(project_root=PROJECT_ROOT)
    formal_chain_status = build_formal_daily_chain_status(
        chain,
        scheduler=scheduler,
    )
    evidence_freshness = (
        evidence_freshness_payload
        if isinstance(evidence_freshness_payload, dict)
        else build_evidence_freshness(PROJECT_ROOT)
    )
    source_status = dict(payload.get("source_status") or {})
    source_status["daily_data_chain"] = chain
    source_status["daily_data_scheduler"] = scheduler
    source_status["audit_evidence"] = evidence_freshness
    payload["source_status"] = source_status
    payload["daily_data_chain"] = chain
    payload["daily_data_scheduler"] = scheduler
    payload["minute_universe_coverage"] = minute_coverage
    payload["formal_daily_chain_status"] = formal_chain_status
    payload["evidence_freshness"] = evidence_freshness
    payload["scope_health"] = _build_scope_health_block(
        chain,
        formal_chain_status=formal_chain_status,
        scheduler=scheduler,
        full_universe_observation=cached_full_scope,
        scheduler_is_live=True,
    )
    payload["data_alerts"] = _data_alerts_block(
        payload["scope_health"],
        previous_alert_payload=cached_alert_payload,
    )
    payload["served_at"] = now_text()
    payload["snapshot_generated_at"] = str(payload.get("generated_at") or "")
    age = _snapshot_age_seconds(payload.get("generated_at"))
    payload["snapshot_age_seconds"] = age
    payload["snapshot_stale"] = age is None or age > 900

    current_level = str(payload.get("health_level") or "yellow").lower()
    payload["health_level"], degraded_reasons = _apply_health_evidence_overlay(
        current_level,
        daily_chain=chain,
        scheduler=scheduler,
        evidence_freshness=evidence_freshness,
        snapshot_stale=bool(payload["snapshot_stale"]),
    )
    payload["health_degraded_reason_codes"] = degraded_reasons
    payload["health_level_overridden_from_snapshot"] = bool(
        degraded_reasons and str(payload.get("health_level") or "").lower() != current_level
    )
    payload["summary_text"] = _append_evidence_summary(
        payload.get("summary_text"), evidence_freshness
    )
    payload["ok"] = True
    payload["network"] = False
    payload["full_scan"] = False
    payload["is_real_order"] = False
    payload["message"] = (
        "读取已落盘数据健康快照并叠加实时每日数据链；"
        "本次 GET 未联网、未重扫全池、未修改行情或纸面组合。"
    )
    payload.setdefault("risk_disclaimer", RISK_DISCLAIMER)
    return payload


def latest_data_health_dashboard(path: Path | None = None) -> dict[str, Any]:
    target = path or DATA_HEALTH_DASHBOARD_LATEST_JSON
    payload = read_json(target, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _relpath_for_payload(target),
            "message": "尚未生成数据健康快照。GET 仅叠加实时每日数据链；请运行 qd.py data-health 刷新完整快照。",
            "risk_disclaimer": RISK_DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _relpath_for_payload(target))
    payload.setdefault("risk_disclaimer", RISK_DISCLAIMER)
    return payload
