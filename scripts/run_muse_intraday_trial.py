"""Bounded Muse data-only trial; source packages and output root stay separate.

The platform may call ``--wake`` repeatedly. It launches at most one local
supervisor. A failed snapshot can retry once inside its original budget; missing snapshots are never
backfilled. No model, paper-book, notification or AI observation entry is used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.utils import atomic_write_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = "quantdesk_muse_intraday_trial_v1"
KINDS = ("snapshot", "minute")


def local_now() -> datetime:
    return datetime.now(SHANGHAI)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected_json_object")
    return data


def load_config(path: Path, expected_sha: str) -> dict[str, Any]:
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != expected_sha:
        raise ValueError("trial_config_hash_mismatch")
    config = json.loads(blob)
    if config.get("schema") != SCHEMA or config.get("data_only") is not True:
        raise ValueError("invalid_trial_contract")
    root = Path(config["root"])
    if not root.is_absolute() or root.resolve() != (path.parent / "project").resolve():
        raise ValueError("trial_root_not_isolated")
    if any((root / p).exists() for p in ("state/ai_trader/ledger.json", "state/auto_paper_portfolio.json")):
        raise ValueError("trial_root_contains_formal_ledger")
    sources = config.get("source_sha256")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("missing_source_manifest")
    for relative, expected in sources.items():
        file = (root / relative).resolve()
        if not file.is_relative_to(root.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("trial_source_hash_mismatch:" + relative)
    if config.get("network_authorized") is not True:
        raise ValueError("trial_network_not_authorized")
    kinds = config.get("enabled_kinds", list(KINDS))
    if not isinstance(kinds, list) or not kinds or len(kinds) != len(set(kinds)) or any(k not in KINDS for k in kinds):
        raise ValueError("invalid_enabled_kinds")
    dates = config.get("trading_dates")
    if not isinstance(dates, list) or not 1 <= len(dates) <= 3 or dates != sorted(set(dates)):
        raise ValueError("invalid_trial_dates")
    calendar = root / "data/calendar/a_share_trading_calendar.csv"
    with calendar.open(encoding="utf-8-sig", newline="") as handle:
        open_days = {r["date"] for r in csv.DictReader(handle)
                     if r.get("is_open", "").lower() in {"true", "1"}}
    for day in dates:
        datetime.strptime(day, "%Y-%m-%d")
        if day not in open_days:
            raise ValueError("trial_date_not_verified_open:" + day)
    for key, upper in (("snapshot_budget_seconds", 150), ("minute_budget_seconds", 240)):
        value = config[key]
        if isinstance(value, bool) or not 0 < float(value) <= upper:
            raise ValueError("invalid_budget:" + key)
    seed = config.get("minute_seed")
    if not isinstance(seed, list) or len(seed) > 100:
        raise ValueError("invalid_minute_seed")
    seen = set()
    for row in seed:
        symbol = row.get("symbol", "")
        if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            raise ValueError("invalid_minute_symbol")
        seen.add(symbol)
    config["config_sha256"] = expected_sha
    return config


def slots(kind: str) -> list[str]:
    windows = ((9 * 60 + 30, 11 * 60 + 25), (13 * 60, 14 * 60 + 55))
    if kind == "minute":
        windows = ((9 * 60 + 35, 11 * 60 + 30), (13 * 60 + 5, 15 * 60 + 5))
    return [f"{m // 60:02d}{m % 60:02d}" for start, end in windows for m in range(start, end + 1, 5)]


def eligible(config: dict[str, Any], kind: str, slot: str, now: datetime) -> str:
    if kind not in KINDS:
        return "unknown_collection_kind"
    if kind not in config.get("enabled_kinds", list(KINDS)):
        return "collector_not_enabled"
    if now.date().isoformat() not in config["trading_dates"]:
        return "outside_trial_dates"
    try:
        stamp = datetime.strptime(slot, "%Y-%m-%dT%H%M").replace(tzinfo=SHANGHAI)
    except ValueError:
        return "invalid_slot"
    if stamp.date() != now.date() or stamp.strftime("%H%M") not in slots(kind):
        return "outside_collection_schedule"
    lag = (now - stamp).total_seconds()
    lower = 30 if kind == "minute" else 0
    if not lower <= lag <= 60:
        return "missed_or_future_slot_no_replay"
    return ""


def receipt_path(config: dict[str, Any], kind: str, slot: str) -> Path:
    # Only validated date/slot values reach this path.
    return Path(config["root"]).parent / "scheduler/slots" / slot[:10] / f"{kind}_{slot[11:]}.json"


def scope_from_seed(config: dict[str, Any], movers: list[dict[str, Any]]) -> dict[str, Any]:
    from quantdesk.intraday_refresh.scope import PRIORITY_ORDER, MAX_MINUTE_SYMBOLS

    entries = {r["symbol"]: {**r, "reasons": list(r.get("reasons", []))} for r in config["minute_seed"]}
    for mover in movers:
        symbol = str(mover.get("symbol", ""))
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        reason = str(mover.get("reason") or "snapshot_mover")
        priority = "high" if "limit" in reason else "normal"
        row = entries.setdefault(symbol, {"symbol": symbol, "priority": priority, "reasons": []})
        if reason not in row["reasons"]:
            row["reasons"].append(reason)
        if PRIORITY_ORDER[priority] < PRIORITY_ORDER[row["priority"]]:
            row["priority"] = priority
    selected = sorted(entries.values(), key=lambda r: (PRIORITY_ORDER[r["priority"]], r["symbol"]))[:MAX_MINUTE_SYMBOLS]
    return {"symbols": selected, "counts": {"total": len(selected)}, "read_only": True,
            "warnings": ["trial_seed_frozen_at:" + config["seed_as_of"], "ai_intraday_selection_not_enabled"]}


def fetch_trial_snapshot(symbols: list[str], budget_seconds: float, *, trade_date: str = "") -> Any:
    """Reserve time for an alternate endpoint before archiving one capture."""
    from quantdesk import snapshot_history

    return snapshot_history.resilient_network_fetch(symbols, budget_seconds=budget_seconds, trade_date=trade_date)


def collect(config: dict[str, Any], kind: str, slot: str, now: datetime) -> dict[str, Any]:
    root = Path(config["root"])
    if kind == "snapshot":
        from quantdesk.snapshot_history import collect_market_snapshot

        return collect_market_snapshot(
            root, network=True, now=now, slot_time=slot[11:],
            fetcher=lambda symbols: fetch_trial_snapshot(symbols, config["snapshot_budget_seconds"],
                                                        trade_date=now.date().isoformat()),
            provenance={"config_sha256": config["config_sha256"],
                        "source_sha256": dict(config["source_sha256"]),
                        "trial_id": config["trial_id"]},
        )
    from quantdesk.intraday_refresh.collector import collect_intraday_refresh
    from quantdesk.intraday_refresh.snapshot import _synced_snapshot, select_mover_symbols

    archived, reasons = _synced_snapshot(root, now)
    movers = select_mover_symbols(archived["rows"]) if archived else []
    # Minute scope uses only this trial's verified archive. A missing archive
    # cannot trigger another full-market request or borrow local PC history.
    class ArchivedRows(list):
        pass

    rows = ArchivedRows(archived["rows"] if archived else [])
    rows.pagination = {
        "complete": bool(archived and archived["ok"]),
        "reason_codes": list(archived["evidence"].get("reason_codes", [])) if archived else reasons,
        "archive_evidence": archived["evidence"] if archived else {},
    }
    result = collect_intraday_refresh(
        root, network=True, now=now, run_id="muse-minute-" + slot.replace(":", ""),
        scheduler_instance=config["trial_id"],
        scope_builder=lambda *args, **kwargs: scope_from_seed(config, movers),
        snapshot_fetcher=lambda: (rows, archived["source"] if archived else "trial_archive_unavailable"),
        max_round_seconds=config["minute_budget_seconds"], max_symbol_seconds=8.0,
    )
    result["trial_scope"] = scope_from_seed(config, movers)
    result["trial_snapshot_evidence"] = archived["evidence"] if archived else {"reason_codes": reasons, "available": False}
    return result


def snapshot_retry_budget(config: dict[str, Any], slot: str, previous: dict[str, Any], now: datetime) -> float:
    """A known empty/failed fetch may retry once; archives and history cannot."""
    if (previous.get("kind") != "snapshot" or previous.get("slot") != slot
            or previous.get("status") not in {"provider_empty", "provider_failed"}
            or previous.get("collection_succeeded")
            or previous.get("config_sha256") != config["config_sha256"]
            or now.date().isoformat() not in config["trading_dates"]):
        return 0.0
    try:
        if int(previous.get("attempt", 1)) != 1:
            return 0.0
        stamp = datetime.strptime(slot, "%Y-%m-%dT%H%M").replace(tzinfo=SHANGHAI)
        started = datetime.fromisoformat(previous.get("slot_started_at") or previous["started_at"])
        if started.tzinfo is None or started > now or not 0 <= (now - stamp).total_seconds() < 240:
            return 0.0
        if (Path(config["root"]) / "data/snapshots" / slot[:10] / f"{slot[11:]}.csv").exists():
            return 0.0
        left = float(config["snapshot_budget_seconds"]) - max(0.0, (now - started).total_seconds())
        return left if left >= 10 else 0.0
    except (ValueError, KeyError, TypeError):
        return 0.0


def _read_slot_receipt(path: Path) -> dict[str, Any]:
    """A corrupt prior receipt is not permission to collect the slot again."""
    try:
        return read_json(path)
    except (OSError, ValueError, UnicodeError):
        return {"status": "invalid_receipt", "terminal": True, "reason": "slot_receipt_unreadable"}


def run_once(
    config: dict[str, Any], kind: str, slot: str, *, now: datetime | None = None,
    collector: Callable[..., dict[str, Any]] = collect,
) -> dict[str, Any]:
    current = now or local_now()
    reason = eligible(config, kind, slot, current)
    if reason and reason != "missed_or_future_slot_no_replay":
        return {"status": "skipped", "reason": reason, "network_attempted": False}
    path = receipt_path(config, kind, slot)
    previous = _read_slot_receipt(path) if path.exists() else {}
    retry_budget = snapshot_retry_budget(config, slot, previous, current) if kind == "snapshot" else 0.0
    if reason and not (reason == "missed_or_future_slot_no_replay" and retry_budget):
        return {"status": "skipped", "reason": reason, "network_attempted": False}
    if previous and not retry_budget:
        return {"status": "already_attempted", "slot": slot, "network_attempted": False, "receipt": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    # The receipt still reserves the slot. Only this small guarded transition
    # can replace a known failure, and it saves the full previous receipt first.
    with interprocess_file_lock(path.with_suffix(".attempt.lock"), timeout_seconds=0):
        if path.exists():
            previous = _read_slot_receipt(path)
            retry_budget = snapshot_retry_budget(config, slot, previous, current) if kind == "snapshot" else 0.0
            if not retry_budget:
                return {"status": "already_attempted", "slot": slot, "network_attempted": False, "receipt": str(path)}
            saved = path.parent / "attempts" / f"{path.stem}.{previous.get('attempt', 1)}.json"
            saved.parent.mkdir(parents=True, exist_ok=True)
            prior_bytes = path.read_bytes()
            if saved.exists():
                if saved.read_bytes() != prior_bytes:
                    return {"status": "already_attempted", "slot": slot, "network_attempted": False,
                            "reason": "attempt_history_conflict", "receipt": str(path)}
            else:
                with saved.open("xb") as handle:
                    handle.write(prior_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
        return _run_slot_attempt(config, kind, slot, current, path, previous, retry_budget, collector)


def _run_slot_attempt(
    config: dict[str, Any], kind: str, slot: str, current: datetime, path: Path,
    previous: dict[str, Any], retry_budget: float, collector: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    granted = retry_budget or float(config[kind + "_budget_seconds"])
    evidence = {"kind": kind, "slot": slot, "status": "running", "started_at": current.isoformat(),
                "finished_at": "", "pid": os.getpid(), "config_sha256": config["config_sha256"],
                "data_only": True, "network_attempted": True, "process_identity": process_identity(os.getpid()),
                "attempt": int(previous.get("attempt", 0)) + 1,
                "slot_started_at": previous.get("slot_started_at") or previous.get("started_at") or current.isoformat(),
                "granted_budget_seconds": granted, "terminal": False}
    try:
        if previous:
            atomic_write_json(path, evidence)
        else:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(evidence, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
    except FileExistsError:
        return {"status": "already_attempted", "slot": slot, "network_attempted": False,
                "receipt": str(path)}
    try:
        attempt_config = {**config, kind + "_budget_seconds": granted}
        payload = collector(attempt_config, kind, slot, current)
        status = str(payload.get("status") or "unknown")
        successful = status in {"written", "complete", "fresh", "ready", "partial"}
        # A completed loop with no rows is never reported as collection success.
        count = int(payload.get("row_count", 0)) if kind == "snapshot" else int(payload.get("appended_rows", 0))
        existing_fresh = int(payload.get("fresh_count", 0)) if kind == "minute" else 0
        evidence.update(status=status, collection_succeeded=successful and (count > 0 or existing_fresh > 0),
                        collected_rows=count, payload=payload)
    except Exception as exc:
        evidence.update(status="failed", collection_succeeded=False, error_type=type(exc).__name__,
                        error=str(exc)[:600])
    evidence["finished_at"] = local_now().isoformat()
    retry = snapshot_retry_budget(config, slot, evidence, datetime.fromisoformat(evidence["finished_at"])) if kind == "snapshot" else 0
    evidence.update(terminal=not bool(retry), retryable=bool(retry), remaining_slot_budget_seconds=retry)
    atomic_write_json(path, evidence)
    return evidence


def process_identity(pid: int) -> str:
    """Linux boot + process start ticks, guarding against PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = stat.rsplit(")", 1)[1].split()[19]
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + ticks
    except (OSError, IndexError):
        return ""


def supervisor_status(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["root"]).parent / "scheduler/supervisor.json"
    try:
        state = read_json(path)
    except (OSError, ValueError):
        return {"status": "not_running"}
    identity = process_identity(int(state.get("pid") or 0))
    alive = bool(identity and identity == state.get("process_identity") and state.get("status") == "running")
    return {**state, "alive": alive}


def reconcile_interrupted(config: dict[str, Any]) -> int:
    """A missing old worker is interrupted, with its actual end time unknown."""
    count = 0
    for path in (Path(config["root"]).parent / "scheduler/slots").glob("*/*.json"):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue  # Corrupt evidence still reserves the slot; never replay it.
        if record.get("status") != "running":
            continue
        identity = process_identity(int(record.get("pid") or 0))
        if identity and identity == record.get("process_identity"):
            continue
        record.update(status="interrupted", collection_succeeded=False, finished_at="",
                      interruption_observed_at=local_now().isoformat(), reason="worker_missing_after_restart")
        atomic_write_json(path, record)
        count += 1
    return count


def serve(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    directory = Path(config["root"]).parent / "scheduler"
    children: dict[str, tuple[subprocess.Popen, float, Path]] = {}
    with interprocess_file_lock(directory / "supervisor.lock", timeout_seconds=0):
        reconcile_interrupted(config)
        state = {"status": "running", "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
                 "enabled_kinds": config.get("enabled_kinds", list(KINDS)),
                 "started_at": local_now().isoformat(), "config_sha256": config["config_sha256"]}
        last_heartbeat = 0.0
        while True:
            now, tick = local_now(), time.monotonic()
            for key, (child, deadline, receipt) in list(children.items()):
                over_budget = tick >= deadline and child.poll() is None
                if over_budget:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
                if child.poll() is not None:
                    try:
                        result = read_json(receipt)
                    except (OSError, ValueError):
                        result = {"slot": key, "status": "running"}
                    if result.get("status") == "running":
                        result.update(status="interrupted", collection_succeeded=False,
                                      finished_at=local_now().isoformat(), returncode=child.returncode,
                                      reason="worker_budget_exceeded" if over_budget else "worker_exited_without_receipt")
                        atomic_write_json(receipt, result)
                    del children[key]
            final = datetime.fromisoformat(config["trading_dates"][-1] + "T15:11:00").replace(tzinfo=SHANGHAI)
            changed = hashlib.sha256(config_path.read_bytes()).hexdigest() != config["config_sha256"]
            if (now > final or changed) and not children:
                state.update(status="configuration_changed" if changed else "expired", finished_at=now.isoformat())
                atomic_write_json(directory / "supervisor.json", state)
                return state
            if tick - last_heartbeat >= 30:
                state.update(heartbeat_at=now.isoformat(), workers=len(children))
                atomic_write_json(directory / "supervisor.json", state)
                last_heartbeat = tick
            slot = now.strftime("%Y-%m-%dT%H") + f"{now.minute // 5 * 5:02d}"
            for kind in KINDS:
                if changed:
                    continue
                path = receipt_path(config, kind, slot)
                previous = _read_slot_receipt(path) if path.exists() else {}
                retry_budget = snapshot_retry_budget(config, slot, previous, now) if kind == "snapshot" else 0.0
                if eligible(config, kind, slot, now) and not retry_budget:
                    continue
                key = kind + ":" + slot
                if key in children or (path.exists() and not retry_budget):
                    continue
                # A still-running previous round cannot overlap the same writer.
                if any(k.startswith(kind + ":") for k in children):
                    continue
                log = directory / "worker.log"
                with log.open("ab") as handle:
                    proc = subprocess.Popen(
                        [sys.executable, "-B", str(Path(__file__).resolve()), "--config", str(config_path),
                         "--config-sha", config["config_sha256"], "--once", kind, "--slot", slot],
                        stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                    )
                budget = (retry_budget or float(config[kind + "_budget_seconds"])) + 12
                children[key] = (proc, tick + budget, path)
            time.sleep(5)


def wake(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    directory = Path(config["root"]).parent / "scheduler"
    expires = datetime.fromisoformat(config["trading_dates"][-1] + "T15:11:00").replace(tzinfo=SHANGHAI)
    if local_now() > expires:
        return {"status": "expired", "network_attempted": False}
    with interprocess_file_lock(directory / "wakeup.lock", timeout_seconds=0):
        existing = supervisor_status(config)
        if existing.get("alive"):
            return {"status": "already_running", "pid": existing["pid"]}
        with (directory / "supervisor.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--config", str(config_path),
                 "--config-sha", config["config_sha256"], "--serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        time.sleep(0.3)
        status = supervisor_status(config)
        return {"status": "started" if status.get("alive") else "start_unconfirmed", "pid": proc.pid,
                "supervisor": status, "collection_succeeded": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", choices=KINDS)
    group.add_argument("--wake", action="store_true")
    group.add_argument("--serve", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--slot", default="")
    args = parser.parse_args(argv)
    config = load_config(args.config, args.config_sha)
    if args.once:
        result = run_once(config, args.once, args.slot)
        view = {k: v for k, v in result.items() if k != "payload"}
    elif args.serve:
        view = serve(config, args.config)
    elif args.wake:
        view = wake(config, args.config)
    else:
        view = supervisor_status(config)
    print(json.dumps(view, ensure_ascii=False))
    return 1 if view.get("status") in {"failed", "start_unconfirmed", "interrupted"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
