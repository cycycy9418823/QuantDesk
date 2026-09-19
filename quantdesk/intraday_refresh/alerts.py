"""Data-outage alerts wired into the existing data-health alert deliverer.

Triggers evaluated here (deduplicated + recovery handled by
``quantdesk.data_health_alert_delivery.dispatch_data_health_alerts``):
  - critical intraday minute data stale (from the intraday manifest)
  - intraday provider all-failed
  - 15:40 daily refresh failure (from results/data_refresh latest report)
  - target-date full-A real gap (from Codex's suspension-aware completeness)
  - universe refresh failure with no usable local cache

Three gates, mirroring the project standard: explicit ``network``, explicit
``push`` (plus the ``QUANTDESK_DATA_HEALTH_ALERTS_PUSH`` environment switch),
and ``dry_run`` default-on. The SendKey is loaded by the deliverer from the
environment only. Unit tests inject a fake sender and never send for real.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.data_health_alert_delivery import dispatch_data_health_alerts

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SENDKEY_ENV = "QUANTDESK_SERVERCHAN_SENDKEY"
LEGACY_SENDKEY_ENV = "SERVERCHAN_SEND_KEY"


def _load_sendkey() -> str | None:
    """SendKey only ever comes from the environment (project standard)."""

    for name in (SENDKEY_ENV, LEGACY_SENDKEY_ENV):
        value = os.environ.get(name)
        if value and value.strip():
            return value
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _alert(
    dedup_key: str,
    reason_code: str,
    scope: str,
    severity: str = "warning",
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "dedup_key": dedup_key,
        "reason_code": reason_code,
        "scope": scope,
        "severity": severity,
        "affected": {"symbols_sample": (symbols or [])[:5]},
    }


def evaluate_alerts(
    project_root: Path | None = None,
    *,
    intraday_manifest: dict[str, Any] | None = None,
    daily_report: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (current_alerts, resolved_alerts) for the deliverer payload."""

    root = Path(project_root or PROJECT_ROOT)
    current: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    im = intraday_manifest
    if im is None:
        im = _read_json(root / "results" / "intraday" / "intraday_refresh_latest.json")
    if im:
        status = str(im.get("status") or "")
        if status == "stale" and int(im.get("stale_count") or 0) > 0:
            current.append(
                _alert(
                    "intraday_critical_stale",
                    "critical_scope_minute_data_stale",
                    "intraday_1m",
                    "critical",
                    symbols=[str(e.get("symbol")) for e in (im.get("provider_status") or [])][:5],
                )
            )
        elif status in ("ready", "partial"):
            resolved.append(_alert("intraday_critical_stale", "intraday_recovered", "intraday_1m"))
        if "provider_all_failed" in (im.get("reason_codes") or []):
            current.append(
                _alert(
                    "intraday_provider_all_failed",
                    "intraday_provider_all_failed",
                    "intraday_1m",
                    "critical",
                )
            )
        elif status in ("ready", "partial", "stale"):
            resolved.append(_alert("intraday_provider_all_failed", "intraday_provider_recovered", "intraday_1m"))
        # 2026-09-10: rounds died at the task's 10-minute limit and left no
        # manifest, so nothing alerted.  A self-bounded round now says so.
        if status == "timed_out" or im.get("not_attempted_symbols"):
            current.append(
                _alert(
                    "intraday_round_timed_out",
                    "intraday_round_budget_exhausted",
                    "intraday_1m",
                    "critical",
                    symbols=[str(item) for item in (im.get("not_attempted_symbols") or [])][:5],
                )
            )
        elif status in ("ready", "partial"):
            resolved.append(
                _alert("intraday_round_timed_out", "intraday_round_within_budget", "intraday_1m")
            )

    dr = daily_report
    if dr is None:
        dr = _read_json(root / "results" / "data_refresh" / "data_refresh_latest.json")
    if dr:
        if dr.get("overall_ok") is False:
            families = dr.get("families") or {}
            failed = [k for k, v in families.items() if not (v or {}).get("ok")]
            current.append(
                _alert(
                    "daily_refresh_failed",
                    "daily_data_refresh_failed:" + ",".join(failed[:4]),
                    "daily_refresh",
                    "critical",
                )
            )
        else:
            resolved.append(_alert("daily_refresh_failed", "daily_refresh_recovered", "daily_refresh"))

    try:
        from quantdesk.suspension_aware_completeness import classify_universe_completeness

        completeness = classify_universe_completeness(
            str(dr.get("target_date") or datetime.now().strftime("%Y-%m-%d")),
            project_root=root,
        )
        gap_count = int(completeness.get("gap_count") or 0)
        if gap_count > 0:
            current.append(
                _alert(
                    "full_a_target_gap",
                    "full_a_daily_gap_count:" + str(gap_count),
                    "full_a_daily",
                    "warning",
                    symbols=(completeness.get("gap_symbols") or [])[:5],
                )
            )
        else:
            resolved.append(_alert("full_a_target_gap", "full_a_daily_complete", "full_a_daily"))
    except Exception:  # noqa: BLE001 - completeness is advisory for alerts
        pass

    universe_path = root / "data" / "universe" / "a_share_symbols.csv"
    universe_ok = universe_path.is_file() and universe_path.stat().st_size > 100
    if not universe_ok:
        current.append(
            _alert(
                "universe_unavailable",
                "universe_refresh_failed_and_no_local_cache",
                "universe",
                "critical",
            )
        )
    else:
        resolved.append(_alert("universe_unavailable", "universe_available", "universe"))

    _ = now
    return current, resolved


def dispatch_intraday_alerts(
    project_root: Path | None = None,
    *,
    network: bool = False,
    push: bool = False,
    dry_run: bool = True,
    intraday_manifest: dict[str, Any] | None = None,
    daily_report: dict[str, Any] | None = None,
    sender: Any = None,
) -> dict[str, Any]:
    """Evaluate triggers and hand them to the existing deliverer."""

    root = Path(project_root or PROJECT_ROOT)
    current, resolved = evaluate_alerts(root, intraday_manifest=intraday_manifest, daily_report=daily_report)
    health_payload = {
        "scope_health": {"target_date": datetime.now().strftime("%Y-%m-%d")},
        "data_alerts": {"current": current, "resolved": resolved},
    }
    kwargs: dict[str, Any] = {
        "network": network,
        "push": push,
        "dry_run": dry_run,
        "health_payload": health_payload,
        "_sendkey_loader": _load_sendkey,
    }
    if sender is not None:
        kwargs["sender"] = sender
    return dispatch_data_health_alerts(root, **kwargs)


