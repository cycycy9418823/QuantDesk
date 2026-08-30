"""Read-only freshness and consistency checks for research audit evidence.

Market-data freshness answers whether price inputs are current.  This module
answers a different question: whether candidate-performance and paper-account
evidence is recent and internally consistent enough to trust displayed audit
metrics.  The builder is read-only.  The explicit refresh helper only replaces
the audit-evidence overlay in the persisted data-health dashboard.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quantdesk.paper.auto_paper_portfolio import reconcile_paper_account
from quantdesk.shared import safe_float
from quantdesk.utils import atomic_write_json

EVIDENCE_FRESHNESS_SCHEMA_VERSION = "evidence_freshness_v1"


def _as_of_date(value: date | datetime | str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if text:
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now().date()


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) < 10:
        return ""
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(payload, dict) or not payload:
        return None, "invalid"
    return payload, ""


def _json_observed_date(payload: dict[str, Any]) -> str:
    for key in ("target_date", "updated_at", "generated_at", "as_of", "date"):
        value = _date_text(payload.get(key))
        if value:
            return value
    return ""


def _csv_observed_date(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", "missing"
    latest = ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return "", "invalid"
            for row in reader:
                for key in ("entry_date", "signal_date", "target_date", "date"):
                    candidate = _date_text(row.get(key))
                    if candidate and candidate > latest:
                        latest = candidate
    except (OSError, UnicodeError, csv.Error):
        return "", "invalid"
    return (latest, "") if latest else ("", "invalid")


def _artifact_status(
    root: Path,
    path: Path,
    *,
    as_of: date,
    stale_after_days: int,
    observed_date_loader: Callable[[Path], tuple[str, str]],
) -> dict[str, Any]:
    observed_date, error = observed_date_loader(path)
    result: dict[str, Any] = {
        "path": _relative(root, path),
        "exists": path.is_file(),
        "status": error or "fresh",
        "observed_date": observed_date,
        "date_source": "artifact_content" if observed_date else "",
        "age_calendar_days": None,
    }
    if error:
        return result
    observed = date.fromisoformat(observed_date)
    age_days = (as_of - observed).days
    result["age_calendar_days"] = age_days
    if age_days < 0:
        result["status"] = "invalid"
        result["reason"] = "artifact_date_after_as_of"
    elif age_days > stale_after_days:
        result["status"] = "stale"
    return result


def _json_artifact(
    root: Path,
    path: Path,
    *,
    as_of: date,
    stale_after_days: int,
) -> dict[str, Any]:
    def loader(target: Path) -> tuple[str, str]:
        payload, error = _read_json(target)
        if error:
            return "", error
        observed = _json_observed_date(payload or {})
        if observed:
            return observed, ""
        try:
            return datetime.fromtimestamp(target.stat().st_mtime).date().isoformat(), ""
        except OSError:
            return "", "invalid"

    result = _artifact_status(
        root,
        path,
        as_of=as_of,
        stale_after_days=stale_after_days,
        observed_date_loader=loader,
    )
    if result["exists"] and result["observed_date"]:
        payload, _ = _read_json(path)
        if payload and not _json_observed_date(payload):
            result["date_source"] = "file_mtime"
    return result


_PAPER_SUMMARY_FIELDS = (
    "cash",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
    "total_equity",
)


def _paper_account_consistency(path: Path, *, latest_snapshot: bool = False) -> dict[str, Any]:
    state, error = _read_json(path)
    if error:
        return {
            "status": error,
            "reason": error,
            "drift_fields": [],
            "status_mismatch_symbols": [],
            "legacy_realized_adjustment": None,
            "summary": {},
            "invalid_reason_codes": [],
        }
    source = dict(state or {})
    source_summary: dict[str, Any] | None = None
    if latest_snapshot:
        summary = source.get("summary")
        if not isinstance(summary, dict):
            return {
                "status": "invalid",
                "reason": "paper_account_latest_summary_missing",
                "drift_fields": [],
                "status_mismatch_symbols": [],
                "legacy_realized_adjustment": None,
                "summary": {},
                "invalid_reason_codes": ["paper_account_latest_summary_missing"],
            }
        source_summary = dict(summary)
        projected = dict(summary)
        projected["positions"] = source.get("positions", [])
        projected["closed_positions"] = source.get("closed_positions", [])
        source = projected

    reconciled = reconcile_paper_account(source)
    detail = reconciled.get("accounting_reconciliation")
    detail = detail if isinstance(detail, dict) else {}
    drift_fields = [str(item) for item in detail.get("drift_fields") or []]
    mismatches = [str(item) for item in detail.get("status_mismatch_symbols") or []]
    detail_status = str(detail.get("status") or "").lower()
    invalid_reasons = [str(item) for item in detail.get("invalid_reason_codes") or [] if str(item)]
    if detail_status == "invalid":
        status = "invalid"
    elif drift_fields or mismatches:
        status = "inconsistent"
    else:
        status = "consistent"
    return {
        "status": status,
        "reason": detail.get("reason") if status == "invalid" else "",
        "drift_fields": drift_fields,
        "status_mismatch_symbols": mismatches,
        "legacy_realized_adjustment": detail.get("legacy_realized_adjustment"),
        "detail_market_value": detail.get("detail_market_value"),
        "detail_unrealized_pnl": detail.get("detail_unrealized_pnl"),
        "closed_realized_pnl": detail.get("closed_realized_pnl"),
        "summary": {
            key: safe_float(
                (source_summary if source_summary is not None else reconciled).get(key),
                None,
            )
            for key in _PAPER_SUMMARY_FIELDS
        },
        "invalid_reason_codes": invalid_reasons,
    }


def _formal_tracking_metrics(path: Path) -> dict[str, Any]:
    payload, error = _read_json(path)
    if error:
        return {
            "metrics_available": False,
            "mature_sample_count": 0,
            "reason_codes": [],
        }
    entries = payload.get("entries")
    entries = entries if isinstance(entries, list) else []
    mature_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        horizons = entry.get("horizons")
        if not isinstance(horizons, dict):
            continue
        mature_count += sum(
            1
            for item in horizons.values()
            if isinstance(item, dict) and str(item.get("status") or "").lower() == "mature"
        )
    available = mature_count > 0
    return {
        "metrics_available": available,
        "mature_sample_count": mature_count,
        "reason_codes": [] if available else ["formal_tracking_no_mature_samples"],
    }


def build_evidence_freshness(
    project_root: Path,
    *,
    as_of: date | datetime | str | None = None,
    stale_after_days: int = 3,
) -> dict[str, Any]:
    """Return audit-evidence freshness without changing any project files."""

    root = Path(project_root).resolve()
    as_of_day = _as_of_date(as_of)
    threshold = max(0, int(stale_after_days))
    artifact_specs = {
        "formal_candidate_tracking": (root / "results/candidate_tracking/latest.json", "json"),
        "candidate_ledger": (root / "data/tracking/candidate_ledger.csv", "csv"),
        "candidate_performance": (root / "data/tracking/candidate_performance.csv", "csv"),
        "tracking_summary": (root / "data/tracking/tracking_summary.json", "json"),
        "paper_account_state": (root / "state/auto_paper_portfolio.json", "json"),
        "paper_account_latest": (root / "results/auto_paper_portfolio_latest.json", "json"),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for name, (path, kind) in artifact_specs.items():
        if kind == "csv":
            artifacts[name] = _artifact_status(
                root,
                path,
                as_of=as_of_day,
                stale_after_days=threshold,
                observed_date_loader=_csv_observed_date,
            )
        else:
            artifacts[name] = _json_artifact(
                root,
                path,
                as_of=as_of_day,
                stale_after_days=threshold,
            )

    formal_metrics = _formal_tracking_metrics(artifact_specs["formal_candidate_tracking"][0])
    artifacts["formal_candidate_tracking"].update(formal_metrics)

    paper_state = _paper_account_consistency(artifact_specs["paper_account_state"][0])
    paper_latest = _paper_account_consistency(
        artifact_specs["paper_account_latest"][0],
        latest_snapshot=True,
    )
    state_latest_mismatch_fields: list[str] = []
    state_summary = paper_state.get("summary") or {}
    latest_summary = paper_latest.get("summary") or {}
    # Keep cross-artifact drift visible even when one side is already marked
    # inconsistent by its own detail reconciliation.
    state_latest_mismatch_fields = [
        key
        for key in _PAPER_SUMMARY_FIELDS
        if state_summary.get(key) is not None
        and latest_summary.get(key) is not None
        and abs(float(state_summary[key]) - float(latest_summary[key])) > 0.01
    ]
    paper_account = dict(paper_state)
    paper_account.update(
        {
            "state_status": paper_state["status"],
            "latest_status": paper_latest["status"],
            "latest_drift_fields": list(paper_latest.get("drift_fields") or []),
            "latest_invalid_reason_codes": list(paper_latest.get("invalid_reason_codes") or []),
            "state_latest_mismatch_fields": state_latest_mismatch_fields,
        }
    )
    if paper_latest["status"] == "invalid":
        paper_account["status"] = "invalid"
    elif paper_latest["status"] == "inconsistent" or state_latest_mismatch_fields:
        paper_account["status"] = "inconsistent"
    artifact_statuses = {str(item.get("status") or "invalid") for item in artifacts.values()}
    reason_codes: list[str] = []
    if artifact_statuses & {"missing", "stale"}:
        reason_codes.append("audit_evidence_stale_or_missing")
    if artifact_statuses & {"invalid"}:
        reason_codes.append("audit_evidence_invalid")
    if paper_account["status"] == "inconsistent":
        reason_codes.append("paper_account_inconsistent")
    if paper_state["status"] == "invalid":
        reason_codes.append("paper_account_invalid")
    if paper_latest["status"] == "invalid":
        reason_codes.append("paper_account_latest_invalid")
    elif paper_latest["status"] == "inconsistent":
        reason_codes.append("paper_account_latest_inconsistent")
    if state_latest_mismatch_fields:
        reason_codes.append("paper_account_latest_mismatch")
    reason_codes.extend(formal_metrics["reason_codes"])
    reason_codes = list(dict.fromkeys(reason_codes))

    if paper_account["status"] in {"inconsistent", "invalid"} or "invalid" in artifact_statuses:
        status = "failed"
    elif artifact_statuses & {"missing", "stale"}:
        status = "warning"
    else:
        status = "fresh"
    metrics_available = bool(formal_metrics["metrics_available"])
    metrics_trustworthy = (
        status == "fresh"
        and paper_account["status"] == "consistent"
        and metrics_available
    )
    if metrics_trustworthy:
        message_for_ui = "审计账本与绩效证据新鲜且纸面账户内部一致。"
    elif status == "fresh" and paper_account["status"] == "consistent" and not metrics_available:
        message_for_ui = "审计证据新鲜，但候选跟踪尚无到期样本，当前绩效指标暂无可用样本。"
    else:
        message_for_ui = "审计账本、绩效证据或纸面账户一致性不足，当前绩效指标不应视为可信结论。"
    return {
        "schema_version": EVIDENCE_FRESHNESS_SCHEMA_VERSION,
        "status": status,
        "as_of": as_of_day.isoformat(),
        "stale_after_calendar_days": threshold,
        "metrics_trustworthy": metrics_trustworthy,
        "metrics_available": metrics_available,
        "artifacts": artifacts,
        "paper_account": paper_account,
        "reason_codes": reason_codes,
        "message_for_ui": message_for_ui,
        "read_only": True,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }


_AUDIT_REASON_CODES = {
    "audit_evidence_stale_or_missing",
    "audit_evidence_invalid",
    "audit_evidence_failed",
    "paper_account_inconsistent",
    "paper_account_invalid",
    "paper_account_latest_invalid",
    "paper_account_latest_inconsistent",
    "paper_account_latest_mismatch",
    "formal_tracking_no_mature_samples",
}


def refresh_persisted_evidence_freshness(
    project_root: Path,
    *,
    as_of: date | datetime | str | None = None,
    stale_after_days: int = 3,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh only the audit-evidence overlay in the persisted dashboard."""

    root = Path(project_root).resolve()
    evidence = build_evidence_freshness(
        root,
        as_of=as_of,
        stale_after_days=stale_after_days,
    )
    dashboard_path = (
        root / "results" / "data_health" / "data_health_dashboard_latest.json"
    )
    dashboard, _reason = _read_json(dashboard_path)
    payload = dict(dashboard or {})
    payload["evidence_freshness"] = evidence
    source_status = payload.get("source_status")
    source_status = dict(source_status) if isinstance(source_status, dict) else {}
    source_status["audit_evidence"] = evidence
    payload["source_status"] = source_status
    payload["audit_evidence_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    if provenance:
        payload["audit_evidence_provenance"] = dict(provenance)

    existing_reasons = payload.get("reason_codes")
    reasons = [
        str(item)
        for item in existing_reasons
        if str(item) not in _AUDIT_REASON_CODES
    ] if isinstance(existing_reasons, list) else []
    reasons.extend(str(item) for item in evidence.get("reason_codes") or [])
    if evidence.get("status") == "failed":
        reasons.append("audit_evidence_failed")
        payload["health_level"] = "red"
    payload["reason_codes"] = list(dict.fromkeys(reasons))
    atomic_write_json(dashboard_path, payload)
    return evidence
