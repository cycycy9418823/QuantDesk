from __future__ import annotations

import copy
import csv
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from quantdesk.dashboard_candidate_cards import write_dashboard_cards
from quantdesk.daily_open_workflow import md_table, run_daily_open_workflow, write_json, write_md

PUBLISH_SCHEMA_VERSION = "v424_research_outputs_publish_v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RESULT_PAGE_RELATIVE = "reports/daily_research/v421_daily_result_page_latest.md"
_REFRESH_JSON_RELATIVE = "results/data_update/v424_research_outputs_refresh_latest.json"
_REFRESH_MD_RELATIVE = "reports/data_update/v424_research_outputs_refresh_latest.md"
_SUCCESS_RECEIPT_RELATIVE = "results/data_update/v424_research_outputs_publish_receipt_latest.json"
_ATTEMPT_RECEIPT_DIR = "results/data_update/v424_research_outputs_publish_attempts"


def _shanghai_now_text() -> str:
    return datetime.now(_SHANGHAI).isoformat(timespec="seconds")


def _fingerprint(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _count_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _refresh_markdown(payload: dict[str, Any]) -> str:
    return "# v424 Research Outputs Refresh\n\n" + md_table(
        [payload],
        ["status", "refresh_attempted", "refresh_success", "candidate_count", "sector_count", "leaderboard_rows", "result_page_updated"],
    )


def refresh_research_outputs(root: Path, *, reason: str = "manual_gate", network: bool = False, max_symbols: int = 300) -> dict[str, Any]:
    """Refresh the research report chain and record this attempt's publication.

    ``result_page_updated`` refers to THIS attempt (page fingerprint before/after),
    never to a stale page that a failed workflow left untouched. The success state
    is only advanced after the necessary artifacts save; any real save failure
    leaves a traceable failure receipt in the independent attempts directory and
    keeps the previous successful receipt. A failed attempt is never published as
    the latest success.
    """

    root = Path(root)
    attempt_id = uuid4().hex
    started_at = _shanghai_now_text()
    page_path = root / _RESULT_PAGE_RELATIVE
    json_path = root / _REFRESH_JSON_RELATIVE
    md_path = root / _REFRESH_MD_RELATIVE
    artifacts: dict[str, Any] = {
        "result_page": {"relative_path": _RESULT_PAGE_RELATIVE, "before": _fingerprint(page_path)},
        "refresh_json": {"relative_path": _REFRESH_JSON_RELATIVE, "before": _fingerprint(json_path)},
        "refresh_md": {"relative_path": _REFRESH_MD_RELATIVE, "before": _fingerprint(md_path)},
    }

    failed_steps: list[str] = []
    workflow: dict[str, Any] = {}
    dashboard: dict[str, Any] = {}
    try:
        workflow = run_daily_open_workflow(root, network=False, max_symbols=max_symbols)
    except Exception as exc:
        failed_steps.append(f"daily_open_workflow:{exc}")
    try:
        dashboard = write_dashboard_cards(root)
    except Exception as exc:
        failed_steps.append(f"dashboard_cards:{exc}")
    candidate_count = 0
    sector_count = 0
    leaderboard_rows = 0
    try:
        candidate_count = _count_rows(root / "results/daily_research/v420_daily_stock_candidates_latest.csv")
        sector_count = _count_rows(root / "results/daily_research/v420_daily_sector_candidates_latest.csv")
        leaderboard_rows = _count_rows(root / "results/strategy_lab/v420_strategy_leaderboard_latest.csv")
    except Exception as exc:
        failed_steps.append(f"counts:{exc}")

    artifacts["result_page"]["after"] = _fingerprint(page_path)
    workflow_ok = not any(step.startswith("daily_open_workflow") for step in failed_steps)
    result_page_updated = bool(workflow_ok and artifacts["result_page"]["after"] is not None and artifacts["result_page"]["after"] != artifacts["result_page"]["before"])

    payload: dict[str, Any] = {
        "task_id": "v424_research_outputs_refresh",
        "status": "completed" if not failed_steps else "completed_with_warnings",
        "reason": reason,
        "refresh_attempted": True,
        "refresh_success": not failed_steps,
        "failed_steps": list(failed_steps),
        "candidate_count": candidate_count,
        "sector_count": sector_count,
        "leaderboard_rows": leaderboard_rows,
        "result_page_updated": result_page_updated,
        "publish_schema_version": PUBLISH_SCHEMA_VERSION,
        "publish_attempt_id": attempt_id,
        "dashboard_cards_status": dashboard.get("status", "unknown"),
        "daily_workflow_status": (workflow.get("handoff") or {}).get("status", "unknown") if workflow else "unknown",
        "research_only": True,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }

    if failed_steps:
        return _record_failure(root, payload, attempt_id, started_at, failed_steps, artifacts)

    # Attempt the full publication. Necessary artifacts: refresh JSON, refresh
    # Markdown, then the success receipt; a real save failure at any of them must
    # leave a failure receipt and must NOT advance the latest success state.
    try:
        write_json(json_path, payload)
        artifacts["refresh_json"]["after"] = _fingerprint(json_path)
        write_md(md_path, _refresh_markdown(payload))
        artifacts["refresh_md"]["after"] = _fingerprint(md_path)
        receipt = {
            "schema_version": PUBLISH_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "started_at": started_at,
            "finished_at": _shanghai_now_text(),
            "status": "published",
            "result_page_updated": result_page_updated,
            "failed_steps": [],
            "artifacts": artifacts,
        }
        write_json(root / _SUCCESS_RECEIPT_RELATIVE, receipt)
    except Exception as exc:
        failure = [f"publish:{type(exc).__name__}:{exc}"]
        return _record_failure(root, payload, attempt_id, started_at, failure, artifacts)

    return payload


def _record_failure(root: Path, payload: dict[str, Any], attempt_id: str, started_at: str, failed_steps: list[str], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Record a failure without ever overwriting the last success.

    A preliminary receipt freezes the state captured at the failure point (an
    immutable stage record). The best-effort compensation then runs, and a final
    receipt is written whose declared ``after`` fingerprints match the bytes that
    are actually persisted, so the failure evidence stays verifiable.
    """

    payload["refresh_success"] = False
    payload["failed_steps"] = list(failed_steps)
    final_receipt_rel = f"{_ATTEMPT_RECEIPT_DIR}/{attempt_id}.json"
    preliminary_rel = f"{_ATTEMPT_RECEIPT_DIR}/{attempt_id}.preliminary.json"

    preliminary = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "phase": "preliminary",
        "attempt_id": attempt_id,
        "started_at": started_at,
        "finished_at": _shanghai_now_text(),
        "status": "failed",
        "result_page_updated": payload.get("result_page_updated", False),
        "failed_steps": list(failed_steps),
        "artifacts": copy.deepcopy(artifacts),
    }
    # The attempts directory is independent of the failing publication target, so
    # the preliminary evidence is always recorded first.
    write_json(root / _ATTEMPT_RECEIPT_DIR / f"{attempt_id}.preliminary.json", preliminary)

    # Best effort: make sure the latest state does not claim this failed attempt
    # succeeded. If the injected failure was on the latest JSON itself, the old
    # (pre-existing) file is simply left in place.
    try:
        write_json(root / _REFRESH_JSON_RELATIVE, payload)
    except Exception:
        pass
    try:
        write_md(root / _REFRESH_MD_RELATIVE, _refresh_markdown(payload))
    except Exception:
        pass

    # Compensation is finished: record the fingerprints of the bytes now on disk
    # so the final receipt declares an accurate, verifiable final state.
    final_artifacts = copy.deepcopy(artifacts)
    for name, relative in (
        ("result_page", _RESULT_PAGE_RELATIVE),
        ("refresh_json", _REFRESH_JSON_RELATIVE),
        ("refresh_md", _REFRESH_MD_RELATIVE),
    ):
        final_artifacts[name]["after"] = _fingerprint(root / relative)

    final_receipt = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "phase": "final",
        "attempt_id": attempt_id,
        "started_at": started_at,
        "finished_at": _shanghai_now_text(),
        "status": "failed",
        "result_page_updated": payload.get("result_page_updated", False),
        "failed_steps": list(failed_steps),
        "artifacts": final_artifacts,
        "preliminary_receipt_path": preliminary_rel,
    }
    write_json(root / _ATTEMPT_RECEIPT_DIR / f"{attempt_id}.json", final_receipt)
    payload["publish_failure_receipt_path"] = final_receipt_rel
    return payload
