"""Intraday 1-minute data gap checks for current QuantDesk plans.

All checks are local and read-only unless ``write=True`` is explicitly passed
to persist the latest status JSON.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

from quantdesk.candidate_contract import candidate_rows_from_strategy_payload
from quantdesk.intraday.intraday_schema import intraday_status_for_file
from quantdesk.market_clock import (
    CN_TIMEZONE,
    previous_cn_a_share_trading_day,
    to_shanghai_datetime,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, write_json
from quantdesk.tushare_minute_quality import build_verified_minute_pool_snapshot
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
STATE_DIR = PROJECT_ROOT / "state"
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"

CONDITIONAL_TRADE_PLANS_LATEST_JSON = RESULTS_DIR / "conditional_trade_plans_latest.json"
AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
DAILY_WORKFLOW_LATEST_JSON = RESULTS_DIR / "daily_workflow_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
INTRADAY_GAP_STATUS_LATEST_JSON = RESULTS_DIR / "intraday_gap_status_latest.json"

DISCLAIMER = "仅用于本地纸面模拟，不接券商、不真实下单、不构成投资建议。"


def _add_symbol(items: OrderedDict[str, dict[str, Any]], row: dict[str, Any], *, source: str) -> None:
    symbol = normalize_symbol(row.get("symbol"))
    if not symbol:
        return
    current = items.get(symbol, {"symbol": symbol, "name": row.get("name") or symbol, "sources": []})
    current["name"] = current.get("name") or row.get("name") or symbol
    current.setdefault("sources", [])
    if source not in current["sources"]:
        current["sources"].append(source)
    items[symbol] = current


def _plan_trade_date(plan: dict[str, Any], fallback: str = "") -> str:
    return str(plan.get("trade_date") or fallback or "")[:10]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
def _parse_iso_date(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _parse_iso_datetime(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return to_shanghai_datetime(datetime.fromisoformat(text))
    except ValueError:
        return None


def _source_evidence(
    path: Path,
    *,
    label: str,
    target_trade_date: str,
    required: bool,
    target_sensitive: bool,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "label": label,
        "path": _display_path(path),
        "required": bool(required),
        "exists": path.exists(),
        "read_ok": False,
        "sha256": "",
        "size_bytes": 0,
        "record_count": 0,
        "as_of": "",
        "target_trade_date": str(target_trade_date or "")[:10],
        "alignment": "not_applicable",
        "formal_eligible": False,
        "error": "",
        "generated_at": "",
        "source_data_cutoff": "",
        "plan_freeze_deadline": "",
        "plan_freeze_status": "not_applicable",
        "reason_codes": [],
    }
    if not path.exists():
        evidence["error"] = "source_missing"
        evidence["alignment"] = "missing"
        return evidence
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        evidence["read_ok"] = isinstance(payload, dict)
        evidence["sha256"] = hashlib.sha256(raw).hexdigest()
        evidence["size_bytes"] = len(raw)
        if isinstance(payload, dict):
            rows = payload.get("symbols") or payload.get("positions")
            if isinstance(rows, list):
                evidence["record_count"] = len(rows)
            evidence["generated_at"] = str(payload.get("generated_at") or "")
            evidence["source_data_cutoff"] = str(
                payload.get("source_data_cutoff") or ""
            )[:10]
            evidence["as_of"] = str(
                payload.get("trade_date")
                or payload.get("plan_date")
                or payload.get("target_trade_date")
                or payload.get("target_date")
                or payload.get("generated_at")
                or payload.get("updated_at")
                or ""
            )[:10]
        if not evidence["read_ok"]:
            evidence["error"] = "source_json_not_object"
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"{type(exc).__name__}:{exc}"[:500]
    if not evidence["read_ok"]:
        evidence["alignment"] = "read_failed"
    elif target_sensitive:
        if not evidence["as_of"]:
            evidence["alignment"] = "unknown_as_of"
        elif evidence["as_of"] == evidence["target_trade_date"]:
            evidence["alignment"] = "aligned"
        else:
            evidence["alignment"] = "stale"
    else:
        evidence["alignment"] = "read_ok"
    if label == "plan" and evidence["read_ok"] and evidence["alignment"] == "aligned":
        reasons: list[str] = []
        trade_day = _parse_iso_date(evidence["target_trade_date"])
        generated_at = _parse_iso_datetime(evidence["generated_at"])
        source_cutoff = _parse_iso_date(evidence["source_data_cutoff"])
        if trade_day is None:
            reasons.append("plan_trade_date_invalid")
        if generated_at is None:
            reasons.append("plan_generated_at_missing_or_invalid")
        if source_cutoff is None:
            reasons.append("plan_source_cutoff_missing_or_invalid")
        if trade_day is not None:
            freeze_deadline = datetime.combine(
                trade_day,
                time(9, 30),
                tzinfo=CN_TIMEZONE,
            )
            evidence["plan_freeze_deadline"] = freeze_deadline.isoformat()
            if generated_at is not None and generated_at >= freeze_deadline:
                reasons.append("plan_generated_after_freeze")
            if source_cutoff is not None:
                previous_session = previous_cn_a_share_trading_day(trade_day)
                if source_cutoff > previous_session:
                    reasons.append("plan_source_cutoff_after_previous_session")
        evidence["reason_codes"] = reasons
        evidence["plan_freeze_status"] = "blocked" if reasons else "passed"
        if reasons:
            evidence["alignment"] = (
                "retroactive"
                if "plan_generated_after_freeze" in reasons
                else "plan_freeze_invalid"
            )
    evidence["formal_eligible"] = bool(
        evidence["read_ok"]
        and (
            not target_sensitive
            or evidence["alignment"] == "aligned"
        )
    )
    return evidence


def _build_source_manifest(
    *,
    source: str,
    target_trade_date: str,
    paths: dict[str, Path],
) -> dict[str, Any]:
    requested = (source or "current").strip().lower()
    preplan = requested == "preplan"
    required_labels = {
        "plan": requested in {"current", "plans", "all"},
        "paper_state": requested in {"current", "positions", "all"}
        or (preplan and paths["paper_state"].exists()),
        "paper_latest": requested in {"current", "positions", "all"},
        "strategy": requested in {"current", "candidates", "all"},
        "workflow": requested in {"current", "candidates", "all"},
    }
    target_sensitive = {"plan": True, "strategy": True, "workflow": True}
    entries = [
        _source_evidence(
            paths[label],
            label=label,
            target_trade_date=target_trade_date,
            required=required_labels[label],
            target_sensitive=target_sensitive.get(label, False),
        )
        for label in paths
        if required_labels[label]
    ]
    failures = [
        item["label"]
        for item in entries
        if item["required"] and not item["formal_eligible"]
    ]
    payload = {
        "status": "passed" if not failures else "blocked",
        "formal_eligible": not failures,
        "required_source_count": len(entries),
        "failed_sources": failures,
        "sources": entries,
        "target_trade_date": str(target_trade_date or "")[:10],
        "source": requested,
    }
    if preplan:
        payload.update(
            {
                "ignored_preplan_sources": [
                    "plan",
                    "paper_latest",
                    "strategy",
                    "workflow",
                ],
                "reason_codes": [
                    "preplan_excludes_derivative_future_plan_sources"
                ],
                "paper_state_initialized": paths["paper_state"].exists(),
            }
        )
    return payload


def collect_intraday_required_symbols(
    *,
    source: str = "current",
    target_trade_date: str = "",
    symbols: list[str] | None = None,
    plan_path: Path | None = None,
    state_path: Path | None = None,
    strategy_path: Path | None = None,
    portfolio_latest_path: Path | None = None,
    workflow_path: Path | None = None,
) -> dict[str, Any]:
    plan_path = Path(plan_path or CONDITIONAL_TRADE_PLANS_LATEST_JSON)
    state_path = Path(state_path or AUTO_PAPER_PORTFOLIO_STATE_JSON)
    strategy_path = Path(strategy_path or AUTO_STRATEGY_LATEST_JSON)
    portfolio_latest_path = Path(portfolio_latest_path or AUTO_PAPER_PORTFOLIO_LATEST_JSON)
    workflow_path = Path(workflow_path or DAILY_WORKFLOW_LATEST_JSON)
    items: OrderedDict[str, dict[str, Any]] = OrderedDict()
    plan = read_json(plan_path, {}) if plan_path.exists() else {}
    trade_date = target_trade_date or _plan_trade_date(plan)

    requested = (source or "current").strip().lower()
    source_manifest = _build_source_manifest(
        source=requested,
        target_trade_date=trade_date,
        paths={
            "plan": plan_path,
            "paper_state": state_path,
            "paper_latest": portfolio_latest_path,
            "strategy": strategy_path,
            "workflow": workflow_path,
        },
    )
    if requested == "symbols":
        for symbol in symbols or []:
            _add_symbol(items, {"symbol": symbol, "name": symbol}, source="symbols")
    if requested in {"current", "plans", "all"}:
        for row in plan.get("symbols") or []:
            if isinstance(row, dict):
                _add_symbol(items, row, source="conditional_plan")
    if requested in {"current", "positions", "all", "preplan"}:
        position_sources = (
            ((state_path, "auto_paper_state"),)
            if requested == "preplan"
            else (
                (state_path, "auto_paper_state"),
                (portfolio_latest_path, "auto_paper_latest"),
            )
        )
        for payload_path, label in position_sources:
            payload = read_json(payload_path, {}) if payload_path.exists() else {}
            for row in payload.get("positions") or []:
                if isinstance(row, dict):
                    _add_symbol(items, row, source=label)
    if requested in {"current", "candidates", "all"}:
        strategy_payload = read_json(strategy_path, {}) if strategy_path.exists() else {}
        for row in candidate_rows_from_strategy_payload(strategy_payload):
            _add_symbol(items, row, source="auto_strategy_latest")
        workflow = read_json(workflow_path, {}) if workflow_path.exists() else {}
        auto_strategy = (
            workflow.get("auto_strategy")
            if isinstance(workflow.get("auto_strategy"), dict)
            else {}
        )
        for row in candidate_rows_from_strategy_payload(auto_strategy):
            _add_symbol(items, row, source="daily_workflow_latest")

    return {
        "target_trade_date": trade_date,
        "items": list(items.values()),
        "source": requested,
        "plan_exists": plan_path.exists(),
        "plan_id": plan.get("plan_id") if isinstance(plan, dict) else "",
        "source_manifest": source_manifest,
        "source_manifest_status": source_manifest.get("status"),
        "source_manifest_formal_eligible": source_manifest.get("formal_eligible"),
    }


def intraday_gap_status(
    *,
    target_trade_date: str = "",
    source: str = "current",
    symbols: list[str] | None = None,
    intraday_dir: Path | None = None,
    output_path: Path | None = None,
    plan_path: Path | None = None,
    state_path: Path | None = None,
    strategy_path: Path | None = None,
    portfolio_latest_path: Path | None = None,
    workflow_path: Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    intraday_dir = Path(intraday_dir or INTRADAY_1M_DIR)
    output_path = Path(output_path or INTRADAY_GAP_STATUS_LATEST_JSON)
    required = collect_intraday_required_symbols(
        source=source,
        target_trade_date=target_trade_date,
        symbols=symbols,
        plan_path=plan_path,
        state_path=state_path,
        strategy_path=strategy_path,
        portfolio_latest_path=portfolio_latest_path,
        workflow_path=workflow_path,
    )
    trade_date = target_trade_date or required.get("target_trade_date") or ""
    verified_pool = build_verified_minute_pool_snapshot(project_root=PROJECT_ROOT)
    verified_symbols = {
        normalize_symbol(symbol)
        for symbol in (verified_pool.get("symbols") if isinstance(verified_pool.get("symbols"), list) else [])
        if normalize_symbol(symbol)
    }
    items: list[dict[str, Any]] = []
    available_symbols: list[str] = []
    missing_symbols: list[str] = []
    invalid_symbols: list[str] = []
    stale_symbols: list[str] = []
    insufficient_symbols: list[str] = []
    verified_available_symbols: list[str] = []
    verified_missing_symbols: list[str] = []
    for row in required.get("items") or []:
        symbol = normalize_symbol(row.get("symbol"))
        path = intraday_dir / f"{symbol}.csv"
        status = intraday_status_for_file(path, symbol=symbol, target_date=trade_date)
        in_verified_pool = bool(symbol and symbol in verified_symbols)
        item = {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "sources": row.get("sources") or [],
            "path": str(path.relative_to(PROJECT_ROOT)) if path.is_absolute() and PROJECT_ROOT in path.parents else str(path),
            **status,
            "minute_pool_status": (
                "verified_minute_pool"
                if in_verified_pool
                else (
                    "missing_from_verified_minute_pool"
                    if verified_pool.get("status") == "passed"
                    else "verified_minute_pool_unavailable"
                )
            ),
            "minute_pool_quality_status": verified_pool.get("quality_status") or verified_pool.get("status") or "missing",
            "minute_pool_target_date": verified_pool.get("target_date") or "",
        }
        state = str(item.get("status") or "")
        if state == "available":
            available_symbols.append(symbol)
            if in_verified_pool:
                verified_available_symbols.append(symbol)
        elif state == "missing_intraday" or not item.get("exists"):
            missing_symbols.append(symbol)
        elif state == "stale_intraday":
            stale_symbols.append(symbol)
        elif state == "insufficient_intraday":
            insufficient_symbols.append(symbol)
        else:
            invalid_symbols.append(symbol)
        if symbol and not in_verified_pool:
            verified_missing_symbols.append(symbol)
        items.append(item)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "target_trade_date": trade_date,
        "scope": "current_candidates_and_positions",
        "source": source,
        "plan_id": required.get("plan_id") or "",
        "total_symbols": len(items),
        "available_count": len(available_symbols),
        "missing_count": len(missing_symbols),
        "invalid_count": len(invalid_symbols),
        "stale_count": len(stale_symbols),
        "insufficient_count": len(insufficient_symbols),
        "available_symbols": available_symbols,
        "missing_symbols": missing_symbols,
        "invalid_symbols": invalid_symbols,
        "stale_symbols": stale_symbols,
        "insufficient_symbols": insufficient_symbols,
        "verified_minute_pool": {
            key: value
            for key, value in verified_pool.items()
            if key not in {"symbols"}
        },
        "verified_minute_pool_available_count": len(verified_available_symbols),
        "verified_minute_pool_missing_count": len(verified_missing_symbols),
        "verified_minute_pool_available_symbols": verified_available_symbols,
        "verified_minute_pool_missing_symbols": verified_missing_symbols[:50],
        "items": items,
        "message": "分钟线状态只读检查完成。缺少分钟线时不会用收盘价、最高价或最低价假装成交。",
        "boundary_note": "完整日线控制收益复盘；分钟线控制盘中触发验证。",
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_intraday_gap_status(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or INTRADAY_GAP_STATUS_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {"ok": True, "exists": False, "path": _display_path(path), "message": "尚未生成分钟线缺口状态。", "disclaimer": DISCLAIMER}
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload
