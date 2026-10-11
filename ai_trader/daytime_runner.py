"""S4 wiring: one slot → screen + decide + fill, for every configured book.

Default off: nothing here calls a model or the network unless
``AI_TRADER_DAYTIME=1`` *and* a caller injected an ``llm`` callable; the
scheduled round injects the real adapter, tests inject a fake one. Each book is
isolated — one book raising is recorded and the data round continues.

Two owner rules shape the flow:

* the first trading day idles: intents and the would-be fill prices are recorded,
  the ledger stays untouched until the second day;
* ``AI_TRADER_DAYTIME_DRY=1`` forces the same idle behaviour at any time.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ai_trader import daytime_decide, daytime_fill
from ai_trader.ledger import load_ledger
from ai_trader.paths import PROJECT_ROOT, CyclePaths, configured_profiles, profile_paths
from quantdesk.intraday_refresh.snapshot import SPOT_RELATIVE
from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.trading_calendar import load_a_share_trading_calendar
from quantdesk.utils import atomic_write_json

SLOTS_RELATIVE = Path("data/snapshots")
SOURCE_CLOUD_SLOT = "cloud_slot"
SOURCE_LOCAL_SPOT = "local_spot"
SLOT_MINUTES = 5
WATCH_ONLY_SLOT = daytime_decide.WATCH_ONLY_SLOT


def floor_slot(now: datetime, *, minutes: int = SLOT_MINUTES) -> str:
    """The 5-minute slot a moment belongs to, as HHMM."""

    floored = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % minutes)
    return floored.strftime("%H%M")


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeError, csv.Error):
        return []


def _row_times(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    stamps = []
    for row in rows:
        for field in ("fetched_at", "quote_time", "timestamp"):
            value = str(row.get(field) or "").strip()
            if len(value) > 10:
                stamps.append(value)
                break
    return stamps


def slot_rows(root: Path, as_of: str, slot: str) -> dict[str, Any]:
    """This slot's quotes: the immutable slot CSV, else the live spot snapshot."""

    slot_path = Path(root) / SLOTS_RELATIVE / str(as_of)[:10] / f"{slot}.csv"
    if slot_path.is_file():
        rows = _read_csv(slot_path)
        if rows:
            stamps = _row_times(rows)
            evidence = "row_fetched_at" if stamps else "file_mtime"
            times = stamps or [datetime.fromtimestamp(slot_path.stat().st_mtime)
                               .replace(microsecond=0).isoformat()]
            return {"rows": rows, "slot": slot, "source": SOURCE_CLOUD_SLOT,
                    "time": max(times), "time_evidence": evidence,
                    "path": str(slot_path), "reason": ""}
    spot_path = Path(root) / SPOT_RELATIVE
    if spot_path.is_file():
        rows = _read_csv(spot_path)
        if rows:
            stamps = _row_times(rows)
            evidence = "row_fetched_at" if stamps else "file_mtime"
            times = stamps or [datetime.fromtimestamp(spot_path.stat().st_mtime)
                               .replace(microsecond=0).isoformat()]
            return {"rows": rows, "slot": slot, "source": SOURCE_LOCAL_SPOT,
                    "time": max(times), "time_evidence": evidence,
                    "path": str(spot_path), "reason": ""}
    return {"rows": [], "slot": slot, "source": "", "time": "", "time_evidence": "none",
            "path": "", "reason": "no_slot_and_no_spot"}


def dry_state_path(runtime: CyclePaths) -> Path:
    return runtime.daytime_notes.parent / "daytime_dry_state.json"


def _read_dry_state(runtime: CyclePaths) -> dict[str, Any]:
    path = dry_state_path(runtime)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _verified_open_day(root: Path, as_of: str) -> bool:
    try:
        day = date.fromisoformat(as_of).isoformat()
    except (TypeError, ValueError):
        return False
    calendar = load_a_share_trading_calendar(root / "data/calendar/a_share_trading_calendar.csv")
    row = calendar.get("rows_by_date", {}).get(day, {})
    return (calendar.get("is_open_by_date", {}).get(day) is True
            and row.get("row_count") == "1"
            and str(row.get("is_open_raw") or "").strip().lower() in {"1", "1.0", "true", "yes"})


def mark_dry_day_observed(runtime: CyclePaths, *, as_of: str, write: bool = True,
                          root: Path | None = None, observation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """记下"这一天已经作为观察日跑过"；状态按**交易日**推进，不看同日有几条记录。"""

    state = _read_dry_state(runtime)
    day = str(as_of or "")[:10]
    proof = dict(observation or {})
    if (not _verified_open_day(Path(root or PROJECT_ROOT), day)
            or proof.get("status") != "decided" or not proof.get("written")
            or int(proof.get("valid_quotes") or 0) <= 0):
        return {**state, "observation_status": "not_completed", "reason": "observation_evidence_missing"}
    if (state.get("observation_status") != "verified"
            or not _verified_open_day(Path(root or PROJECT_ROOT), str(state.get("completed_dry_day") or ""))):
        state = {}
    observed = [str(item)[:10] for item in (state.get("observed_days") or []) if str(item).strip()]
    if day and day not in observed:
        observed.append(day)
    state = {
        **state,
        "observed_days": observed[-30:],
        "last_observed_day": day or str(state.get("last_observed_day") or ""),
        "completed_dry_day": str(state.get("completed_dry_day") or "") or (observed[0] if observed else ""),
        "observation_status": "verified", "observation": proof,
    }
    if write:
        path = dry_state_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state)
    return state


def is_first_trading_day(runtime: CyclePaths, *, as_of: str = "", root: Path | None = None) -> bool:
    """True while this book has no real trade and no *earlier* day was already observed.

    观察日按**交易日**推进：同一天里写多少条成交/日记记录都不会把首日提前结束（T30）。
    """

    try:
        ledger = load_ledger(runtime.ledger)
    except (OSError, ValueError):
        return True
    if ledger.get("trades"):
        return False
    state = _read_dry_state(runtime)
    completed = str(state.get("completed_dry_day") or "")[:10]
    day = str(as_of or "")[:10]
    project = Path(root or PROJECT_ROOT)
    if (state.get("observation_status") == "verified" and completed and day > completed
            and _verified_open_day(project, completed) and _verified_open_day(project, day)):
        return False
    return True


def run_slot(
    root: Path | None = None,
    *,
    profile: str | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    slot: str = "",
    rows: Sequence[Mapping[str, Any]] | None = None,
    quote_slot: str = "",
    quote_time: str = "",
    source: str = "",
    llm: Callable[..., daytime_decide.ModelCall] | None = None,
    minute_fetcher: Callable[[list[str]], dict[str, list[dict[str, Any]]]] | None = None,
    paths: CyclePaths | None = None,
    write: bool = True,
    dry: bool | None = None,
    idle_first_day: bool = True,
) -> dict[str, Any]:
    """One book, one slot: look at the market, then fill earlier slots' intents.

    ``paths`` overrides the book's files (tests inject a tmp_path book); the
    default resolves the profile's real state directory.
    """

    if not daytime_decide.enabled():
        return {"enabled": False, "reason": "daytime_disabled", "profile": str(profile or ""),
                "slot": "", "calls": 0, "intents": 0, "fills": 0, "model_used": False}

    project = Path(root or PROJECT_ROOT).resolve()
    runtime = paths or profile_paths(profile)
    current = to_shanghai_datetime(now)
    active_slot = str(slot or quote_slot or floor_slot(current))
    quotes = slot_rows(project, current.strftime("%Y-%m-%d"), active_slot)
    quote_rows = list(rows) if rows is not None else quotes["rows"]
    active_source = str(source or quotes["source"])
    active_time = str(quote_time or quotes["time"])
    if quote_rows and not active_time:
        stamps = _row_times(quote_rows)
        active_time = max(stamps) if stamps else ""

    from ai_trader.daytime_screen import build_screen, render_text

    screen = build_screen(project, profile=profile, now=current, paths=runtime,
                          slot_rows=list(quote_rows), slot=active_slot)
    # Injected rows would otherwise be labelled "injected"; the runner knows the
    # real provenance (cloud slot / local spot) and puts it back on the header.
    if active_source:
        screen["source"] = active_source
    if active_time:
        screen["snapshot_time"] = active_time
    screen["source_reason"] = quotes["reason"]
    screen["source_path"] = quotes["path"]

    decision = daytime_decide.decide_slot(
        project, profile=profile, now=current, clock=clock, paths=runtime, llm=llm,
        minute_fetcher=minute_fetcher, write=write, screen=screen,
        screen_text=render_text(screen))
    idle = daytime_fill.dry_mode() if dry is None else bool(dry)
    if idle_first_day and not idle:
        idle = is_first_trading_day(runtime, as_of=current.strftime("%Y-%m-%d"), root=project)
    fill = daytime_fill.fill_slot(
        project, profile=profile, now=current, paths=runtime, rows=quote_rows,
        quote_slot=active_slot, quote_time=active_time, rows_source=active_source,
        write=write, dry=idle)
    dry_state: dict[str, Any] = {}
    if idle and write:
        day = current.strftime("%Y-%m-%d")
        observed_at = daytime_fill._stamp(decision.get("at"))
        valid_quotes = [quote for quote in daytime_fill.quote_index(quote_rows).values()
                        if not quote.get("time_error") and quote.get("trading")
                        and (quote.get("price") or 0) > 0
                        and observed_at is not None
                        and daytime_fill._stamp(quote.get("time")) <= observed_at
                        and daytime_fill._stamp(quote.get("time")).date().isoformat() == day]
        proof = {"status": decision.get("status"),
                 "written": bool(decision.get("written")) and not any(
                     fill.get(key) for key in ("save_error", "evidence_error")),
                 "valid_quotes": len(valid_quotes), "slot": active_slot,
                 "completed_at": decision.get("model_completed_at")}
        try:
            dry_state = mark_dry_day_observed(runtime, as_of=day, root=project, observation=proof)
        except OSError as exc:
            dry_state = {"observation_status": "not_completed", "save_error": f"{type(exc).__name__}:{exc}"}
    return {
        "enabled": True, "profile": str(profile or ""), "slot": active_slot,
        "watch_only": active_slot <= WATCH_ONLY_SLOT,
        "rows": len(quote_rows), "rows_source": active_source, "rows_reason": quotes["reason"],
        "time_evidence": quotes["time_evidence"], "quote_time": active_time,
        "calls": len(decision.get("calls") or []), "intents": len(decision.get("intents") or []),
        "orders": len(decision.get("orders") or []), "status": decision.get("status"),
        "reason": decision.get("reason"), "model_used": bool(decision.get("model_used")),
        "screen_chars": decision.get("screen_chars", 0),
        "elapsed_ms": sum(int(call.get("elapsed_ms") or 0)
                          for call in (decision.get("calls") or [])),
        "fills": fill.get("fills", 0), "blocked": fill.get("blocked", 0),
        "mandate_blocked": fill.get("mandate_blocked", 0),
        "idle_day": bool(idle), "ledger_written": fill.get("ledger_written", False),
        "save_error": fill.get("save_error") or dry_state.get("save_error"),
        "evidence_error": fill.get("evidence_error"),
        "dry_state": dry_state,
        "decision": decision, "fill": fill,
    }


def run_all_profiles(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    slot: str = "",
    rows_provider: Callable[[Path, str, str], Sequence[Mapping[str, Any]]] | None = None,
    llm_factory: Callable[[str], Callable[..., daytime_decide.ModelCall]] | None = None,
    minute_fetcher: Callable[[list[str]], dict[str, list[dict[str, Any]]]] | None = None,
    paths_factory: Callable[[str], CyclePaths] | None = None,
    write: bool = True,
    dry: bool | None = None,
) -> dict[str, Any]:
    """Every configured book for this slot; one book's failure stays its own."""

    if not daytime_decide.enabled():
        return {"enabled": False, "reason": "daytime_disabled", "slot": "",
                "profiles": [], "fills": 0}

    project = Path(root or PROJECT_ROOT).resolve()
    current = to_shanghai_datetime(now)
    active_slot = str(slot or floor_slot(current))
    runs: list[dict[str, Any]] = []
    for profile in configured_profiles():
        label = profile or "default"
        try:
            injected = llm_factory(profile) if llm_factory else None
            extra: dict[str, Any] = {}
            if rows_provider is not None:
                extra["rows"] = rows_provider(project, current.strftime("%Y-%m-%d"), active_slot)
            if paths_factory is not None:
                extra["paths"] = paths_factory(profile)
            run = run_slot(project, profile=profile, now=current, slot=active_slot,
                           clock=clock,
                           llm=injected, minute_fetcher=minute_fetcher, write=write,
                           dry=dry, **extra)
            run["profile"] = label
            runs.append(run)
        except Exception as exc:  # noqa: BLE001 - one book must not kill the data round
            runs.append({"enabled": True, "profile": label, "slot": active_slot,
                         "ok": False, "reason": f"daytime_ai_failed:{type(exc).__name__}",
                         "fills": 0, "intents": 0, "calls": 0})
    return {
        "enabled": True, "slot": active_slot, "profiles": runs,
        "fills": sum(int(run.get("fills") or 0) for run in runs),
        "intents": sum(int(run.get("intents") or 0) for run in runs),
        "calls": sum(int(run.get("calls") or 0) for run in runs),
        "mandate_blocked": sum(int(run.get("mandate_blocked") or 0) for run in runs),
    }


__all__ = [
    "SLOTS_RELATIVE",
    "SOURCE_CLOUD_SLOT",
    "SOURCE_LOCAL_SPOT",
    "WATCH_ONLY_SLOT",
    "floor_slot",
    "is_first_trading_day",
    "run_all_profiles",
    "run_slot",
    "slot_rows",
]
