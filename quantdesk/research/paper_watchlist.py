from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.paper.lifecycle import CandidateDecisionEvent, LifecycleEventStore
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.platform_common import (
    DISCLAIMER,
    PROJECT_ROOT,
    base_payload,
    decision_id,
    finalize_outputs,
    read_json,
    simple_markdown,
    synthetic_candidates,
    truthy,
)
from quantdesk.research.stock_picking_flow import strategy_payload_is_synthetic
from quantdesk.time_utils import now_iso

DEFAULT_PATH = PROJECT_ROOT / "state" / "paper_watchlist.json"


def _resolved_watchlist_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _watchlist_store(path: str | Path) -> PaperStateStore:
    # The legacy watchlist schema deliberately has no schema_version field.
    # PaperStateStore still supplies atomic replace and recoverable backups.
    return PaperStateStore(_resolved_watchlist_path(path), schema_version=None, max_backups=5)


def _write_watchlist(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return _watchlist_store(path).write(payload)


def lifecycle_path_for_watchlist(path: str | Path) -> Path:
    """Return the colocated append-only event log for a watchlist projection."""

    target = _resolved_watchlist_path(path)
    return target.with_name(f"{target.stem}_lifecycle.json")


def _coerce_lifecycle_store(
    value: LifecycleEventStore | str | Path | None,
    *,
    watchlist_path: str | Path,
) -> LifecycleEventStore:
    resolved = lifecycle_path_for_watchlist(watchlist_path) if value is None else value
    return resolved if isinstance(resolved, LifecycleEventStore) else LifecycleEventStore(resolved)


def _row_lifecycle_evidence(row: dict[str, Any]) -> dict[str, Any]:
    eligible = row.get("eligible") if "eligible" in row else row.get("formal_eligible")
    coverage = row.get("coverage") if "coverage" in row else row.get("evidence_coverage")
    return {
        "eligible": eligible,
        "evidence_kind": row.get("evidence_kind"),
        "coverage": coverage,
        "minimum_coverage": row.get("minimum_coverage", 1.0),
        "pit_eligible": row.get("pit_eligible"),
        "price_contract_eligible": row.get("price_contract_eligible"),
    }


def _watchlist_lifecycle_event(
    item: dict[str, Any],
    source_row: dict[str, Any],
    *,
    manual: bool,
) -> CandidateDecisionEvent:
    # Use an allowlist so compatibility rows can never smuggle money/order
    # fields into an unfunded lifecycle event.
    event_payload = {
        "watchlist_id": item.get("watchlist_id"),
        "name": item.get("name"),
        "sector": item.get("sector"),
        "strategy": item.get("strategy"),
        "confidence": item.get("confidence"),
        "rationale": list(item.get("rationale") or []),
        "source_task": item.get("source_task"),
        "initial_reference_price": item.get("initial_reference_price"),
        "composite_score": source_row.get("composite_score"),
        "composite_rank": source_row.get("composite_rank"),
    }
    source_ids = tuple(
        str(value).strip()
        for value in (source_row.get("source_event_ids") or [])
        if str(value).strip()
    )
    return CandidateDecisionEvent(
        event_id=f"lifecycle_{item['watchlist_id']}",
        symbol=str(item["symbol"]),
        occurred_at=str(item.get("added_at") or now_iso()),
        requested_state="paper_watchlist",
        decision_origin="manual" if manual else "system",
        evidence={} if manual else _row_lifecycle_evidence(source_row),
        payload=event_payload,
        source_event_ids=source_ids,
    )


def init_paper_watchlist(path: str | Path = DEFAULT_PATH) -> dict:
    payload = {"ok": True, "status": "passed", "watchlist_id": "paper_watchlist", "items": [], "research_only": True, "disclaimer": DISCLAIMER}
    _write_watchlist(path, payload)
    return payload


def load_paper_watchlist(path: str | Path = DEFAULT_PATH) -> dict:
    target = _resolved_watchlist_path(path)
    if not target.exists():
        return init_paper_watchlist(target)
    payload = _watchlist_store(target).read(default={})
    if payload.get("state_recovery_warning"):
        raise RuntimeError(str(payload["state_recovery_warning"]))
    if not isinstance(payload, dict):
        return init_paper_watchlist(target)
    payload.setdefault("items", [])
    payload.setdefault("research_only", True)
    return payload


def add_watchlist_items(
    items: list[dict],
    path: str | Path = DEFAULT_PATH,
    *,
    lifecycle_store: LifecycleEventStore | str | Path | None = None,
    manual: bool = True,
) -> dict:
    watchlist = load_paper_watchlist(path)
    existing = {str(item.get("symbol")) for item in watchlist.get("items", []) if isinstance(item, dict)}
    resolved_lifecycle_store = _coerce_lifecycle_store(
        lifecycle_store,
        watchlist_path=path,
    )
    lifecycle_events: list[CandidateDecisionEvent] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in existing:
            continue
        item = {
            "watchlist_id": decision_id(),
            "symbol": symbol,
            "name": row.get("name") or "",
            "sector": row.get("sector") or "",
            "added_at": row.get("generated_at") or "",
            "source_task": row.get("source_task") or "strategy_recommendation",
            "strategy": row.get("primary_strategy") or row.get("strategy") or "observe",
            "confidence": row.get("confidence") or 0,
            "rationale": row.get("rationale") or [],
            "initial_reference_price": row.get("latest_price") or row.get("close"),
            "latest_reference_price": row.get("latest_price") or row.get("close"),
            "max_gain_since_added": 0,
            "max_drawdown_since_added": 0,
            "review_status": "watching",
            "review_notes": "",
            "research_only": True,
        }
        if truthy(row.get("synthetic_test_data")):
            item["synthetic_test_data"] = True
            item["evidence_kind"] = str(row.get("evidence_kind") or "synthetic_test_demo")
        elif row.get("evidence_kind"):
            item["evidence_kind"] = str(row.get("evidence_kind"))
        event = _watchlist_lifecycle_event(item, row, manual=manual)
        event_projection = event.to_dict()
        if event_projection["effective_state"] == "excluded_pending_research":
            item["review_status"] = "excluded_pending_research"
            item["review_notes"] = "evidence incomplete; retained only for pending research"
            item["lifecycle_effective_state"] = "excluded_pending_research"
            item["eligibility_reason_codes"] = list(
                event_projection.get("eligibility_reason_codes") or []
            )
        watchlist["items"].append(item)
        lifecycle_events.append(event)
        existing.add(symbol)
    for event in lifecycle_events:
        resolved_lifecycle_store.append(event)
    _write_watchlist(path, watchlist)
    watchlist["status"] = "passed"
    watchlist["watchlist_count"] = len(watchlist.get("items") or [])
    return watchlist


def review_watchlist_items(current_daily: dict | None = None, path: str | Path = DEFAULT_PATH) -> dict:
    watchlist = load_paper_watchlist(path)
    for item in watchlist.get("items", []):
        if isinstance(item, dict):
            item["review_status"] = item.get("review_status") or "watching"
            item["review_notes"] = item.get("review_notes") or "paper review only; no trading action"
    _write_watchlist(path, watchlist)
    watchlist["status"] = "passed"
    watchlist["watchlist_count"] = len(watchlist.get("items") or [])
    return watchlist


def build_paper_watchlist_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    path = cfg.get("watchlist_path") or DEFAULT_PATH
    warnings: list[str] = []
    if cfg.get("init"):
        watchlist = init_paper_watchlist(path)
    else:
        watchlist = load_paper_watchlist(path)
    if cfg.get("add_from_strategy"):
        strategy = read_json("results/strategy_recommendation_latest.json", {})
        items = list(strategy.get("recommendations") or [])
        synthetic_opt_in = truthy(cfg.get("synthetic_test_data"))
        upstream_synthetic = strategy_payload_is_synthetic(strategy)
        if upstream_synthetic and not synthetic_opt_in:
            items = []
            warnings.append(
                "upstream_synthetic_strategy_rejected: enable synthetic_test_data explicitly for test/demo use"
            )
        elif not items and synthetic_opt_in:
            items = [
                {
                    **row,
                    "synthetic_test_data": True,
                    "evidence_kind": "synthetic_test_demo",
                }
                for row in synthetic_candidates()
            ]
            warnings.append("synthetic_test_data: paper watchlist candidates are test/demo data only")
        elif not items:
            warnings.append("strategy_recommendation_missing: no paper watchlist candidates were added")
        elif upstream_synthetic:
            items = [
                {
                    **row,
                    "synthetic_test_data": True,
                    "evidence_kind": "synthetic_test_demo",
                }
                for row in items
                if isinstance(row, dict)
            ]
            warnings.append("synthetic_test_data: upstream paper watchlist candidates are test/demo data only")
        if items:
            lifecycle_store = cfg.get("lifecycle_store") or cfg.get("lifecycle_path")
            watchlist = add_watchlist_items(
                items[: int(cfg.get("top") or 10)],
                path,
                lifecycle_store=lifecycle_store,
                manual=False,
            )
    if cfg.get("review"):
        watchlist = review_watchlist_items(None, path)
    payload = base_payload(
        "paper_watchlist",
        status="warning" if warnings else "passed",
        warnings=warnings,
    )
    payload.update({
        "watchlist_path": str(path),
        "watchlist_count": len(watchlist.get("items") or []),
        "items": list(watchlist.get("items") or []),
    })
    return finalize_outputs(payload, "paper_watchlist", render_paper_watchlist_markdown(payload))


def render_paper_watchlist_markdown(payload: dict) -> str:
    return simple_markdown("Paper Watchlist", payload, "items")
