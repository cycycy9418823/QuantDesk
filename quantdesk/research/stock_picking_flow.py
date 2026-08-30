from __future__ import annotations

from typing import Any

from quantdesk.platform_common import (
    base_payload,
    finalize_outputs,
    read_json,
    simple_markdown,
    stale_date,
    synthetic_candidates,
    truthy,
)


def strategy_payload_is_synthetic(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    containers = [payload]
    for key in ("input_status", "provenance"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        if truthy(container.get("synthetic_test_data")) or truthy(container.get("uses_synthetic_data")):
            return True
        for key in ("mode", "data_source", "source_mode", "evidence_kind"):
            marker = str(container.get(key) or "").strip().lower()
            if marker in {"synthetic", "synthetic_test_data", "synthetic_test_demo"} or marker.startswith(
                "synthetic_"
            ):
                return True
    return any(
        truthy(row.get("synthetic_test_data"))
        or str(row.get("evidence_kind") or "").strip().lower().startswith("synthetic")
        for row in (payload.get("recommendations") or [])
        if isinstance(row, dict)
    )


def load_stock_picking_inputs(config: dict | None = None) -> tuple[dict, list[str]]:
    cfg = dict(config or {})
    warnings: list[str] = []
    if truthy(cfg.get("synthetic_test_data")):
        return {
            "sector": {"status": "passed", "top_sectors": [{"sector": "电力", "count": 1}, {"sector": "通信", "count": 1}]},
            "strategy": {"status": "passed", "recommendations": synthetic_candidates(), "strategy_counts": {"long_swing": 1, "board": 1, "observe": 1}},
            "daily": {"status": "synthetic"},
            "data_health": {"status": "synthetic"},
        }, ["synthetic_test_data: stock candidates are test/demo evidence only"]
    inputs = {
        "sector": read_json(cfg.get("sector_rotation_json") or "results/sector_rotation_latest.json", {}),
        "strategy": read_json(cfg.get("strategy_recommendation_json") or "results/strategy_recommendation_latest.json", {}),
        "daily": read_json(cfg.get("daily_json") or "results/daily_workflow_latest.json", {}),
        "data_health": read_json(cfg.get("data_health_json") or "results/data_health/data_health_dashboard_latest.json", {}),
    }
    strategy_payload = inputs.get("strategy") if isinstance(inputs.get("strategy"), dict) else {}
    upstream_synthetic = strategy_payload_is_synthetic(strategy_payload)
    if upstream_synthetic:
        inputs["strategy"] = {}
        warnings.append(
            "strategy_input_synthetic_rejected: enable synthetic_test_data explicitly for test/demo use"
        )
    elif strategy_payload:
        upstream_status = str(strategy_payload.get("status") or "").strip().lower()
        if upstream_status and upstream_status != "passed":
            warnings.append(f"strategy_input_status:{upstream_status}")
        warnings.extend(
            f"strategy_input:{item}"
            for item in (strategy_payload.get("warnings") or [])
            if str(item).strip()
        )
        if not strategy_payload.get("recommendations"):
            warnings.append("strategy_recommendations_missing")
    for key, value in inputs.items():
        if not value:
            warnings.append(f"{key}_input_missing")
    return inputs, warnings


def merge_sector_and_strategy_recommendations(
    sector_payload: dict | None,
    strategy_payload: dict | None,
    daily_payload: dict | None,
) -> dict:
    recs = list((strategy_payload or {}).get("recommendations") or [])
    candidates: list[dict[str, Any]] = []
    for row in recs:
        if not isinstance(row, dict):
            continue
        candidates.append({
            "symbol": row.get("symbol") or "",
            "name": row.get("name") or "",
            "sector": row.get("sector") or "unknown",
            "strategy": row.get("primary_strategy") or row.get("strategy") or "observe",
            "confidence": int(row.get("confidence") or 0),
            "stars": int(row.get("stars") or 1),
            "rationale": list(row.get("rationale") or ["strategy recommendation snapshot"]),
            "warnings": list(row.get("warnings") or []),
            "risk_flags": list(row.get("risk_flags") or []),
            "source_modules": ["sector_rotation", "strategy_recommendation", "daily_workflow"],
            "latest_price": row.get("close") or row.get("latest_price"),
            "as_of_date": (daily_payload or {}).get("target_date") or "",
            "suggested_review_date": stale_date(1),
            "research_only": True,
        })
    candidates.sort(key=lambda item: (item.get("confidence") or 0, item.get("stars") or 0), reverse=True)
    return {
        "top_candidates": candidates,
        "sector_summary": list((sector_payload or {}).get("top_sectors") or (sector_payload or {}).get("top_sectors_preview") or [])[:10],
        "strategy_summary": dict((strategy_payload or {}).get("strategy_counts") or {}),
    }


def build_stock_picking_flow_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    synthetic = truthy(cfg.get("synthetic_test_data"))
    inputs, warnings = load_stock_picking_inputs(cfg)
    merged = merge_sector_and_strategy_recommendations(inputs.get("sector"), inputs.get("strategy"), inputs.get("daily"))
    if synthetic:
        for item in merged["top_candidates"]:
            item["synthetic_test_data"] = True
            item["evidence_kind"] = "synthetic_test_demo"
    status = "warning" if warnings else "passed"
    payload = base_payload("stock_picking_flow", status=status, warnings=warnings)
    payload.update({
        "input_status": {key: "available" if value else "missing" for key, value in inputs.items()},
        **merged,
        "risk_summary": {"risk_flag_count": sum(len(row.get("risk_flags") or []) for row in merged["top_candidates"])},
        "paper_watchlist_suggestions": merged["top_candidates"][: int(cfg.get("top") or 20)],
        "review_tasks": ["review data integrity", "review sector context", "review paper watchlist tomorrow"],
        "top_candidate_count": len(merged["top_candidates"]),
        "price_adjustment": cfg.get("price_adjustment") or "forward",
        "synthetic_test_data": synthetic,
    })
    return finalize_outputs(payload, "stock_picking_flow", render_stock_picking_flow_markdown(payload))


def render_stock_picking_flow_markdown(payload: dict) -> str:
    return simple_markdown("Stock Picking Flow", payload, "top_candidates")
