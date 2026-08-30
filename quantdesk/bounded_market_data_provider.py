from __future__ import annotations

import csv
import multiprocessing as mp
from queue import Empty
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.daily_open_workflow import md_table, write_json, write_md
from quantdesk.daily_price_providers import fetch_daily
from quantdesk.strategy_lab.research_signal_safety import boundary_payload


def provider_config(root: Path, provider_type: str = "akshare", max_symbols: int = 5) -> dict[str, Any]:
    akshare_available = False
    akshare_version = ""
    try:
        import akshare as ak  # type: ignore

        akshare_available = True
        akshare_version = str(getattr(ak, "__version__", "unknown"))
    except Exception:
        pass
    payload = {
        "task_id": "v425_provider_config",
        "status": "completed_with_warnings" if not akshare_available else "completed",
        "provider_type": provider_type,
        "akshare_available": akshare_available,
        "akshare_version": akshare_version,
        "authentication_required": False,
        "credentials_present": False,
        "credential_policy": "no_broker_credentials_required_for_public_market_data; no trading credentials",
        "rate_limit_handling": {"max_symbols_per_run": int(max_symbols), "sleep_seconds_between_calls": 1, "max_retries": 1, "timeout_seconds": 30},
        "session_aware_update": {"intraday_sessions": ["morning_session", "lunch_break", "afternoon_session", "near_close"], "post_close_sessions": ["post_close", "after_hours"], "network_true_required": True},
        "write_policy": "data/daily writes only post-close after staging, schema validation, and atomic replace",
        **boundary_payload(),
    }
    write_json(root / "results/data_update/v425_provider_config_latest.json", payload)
    write_md(root / "reports/data_update/v425_provider_config_latest.md", "# v425 Provider Config\n\n" + md_table([payload], ["status", "provider_type", "akshare_available", "akshare_version", "authentication_required", "credentials_present", "write_policy"]))
    return payload


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


# Canonical marker for synthetic/simulated daily rows. A row whose `source`
# matches this is a placeholder (flat OHLC, zero volume) and must never feed
# research or backtests. The real post-close update path refuses to write one;
# scripts/clean_simulated_rows.py removes any that already leaked into data/daily.
SIMULATED_SOURCE_VALUE = "v425_simulated_provider"
SIMULATED_SOURCE_SUFFIX = "_simulated_provider"


def is_simulated_source(source: Any) -> bool:
    """True if a row's ``source`` marks it as a synthetic/simulated placeholder."""
    text = str(source or "").strip()
    return text == SIMULATED_SOURCE_VALUE or text.endswith(SIMULATED_SOURCE_SUFFIX)


def simulated_daily_row(path: Path, expected_date: str) -> dict[str, str]:
    header, rows = _read_rows(path)
    last = rows[-1] if rows else {}
    symbol = path.stem
    close = str(last.get("close") or last.get("open") or "1")
    row = {col: "" for col in header}
    row.update(
        {
            "date": expected_date,
            "symbol": symbol,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "0",
            "amount": "0",
            "source": SIMULATED_SOURCE_VALUE,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    return row


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value)


def _provider_chain(provider: str) -> list[str]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"akshare", "akshare_daily", "bounded_optional_provider", "bounded_provider_chain"}:
        return ["akshare_daily", "tencent_daily"]
    if normalized in {"tencent", "tencent_daily"}:
        return ["tencent_daily"]
    return [normalized or "akshare_daily"]


def _fetch_daily_worker(queue: Any, provider_name: str, symbol: str, start_date: str, end_date: str, network: bool) -> None:
    try:
        frame, warnings = fetch_daily(provider_name, symbol, start_date, end_date, network=network)
        queue.put(
            {
                "ok": True,
                "records": frame.to_dict("records") if frame is not None and not frame.empty else [],
                "columns": list(frame.columns) if frame is not None else [],
                "warnings": list(warnings or []),
            }
        )
    except BaseException as exc:  # noqa: BLE001 - surfaced to parent as provider failure evidence
        queue.put({"ok": False, "error": repr(exc), "exception_type": type(exc).__name__})


def _fetch_daily_with_timeout(provider_name: str, symbol: str, start_date: str, end_date: str, timeout_seconds: int, *, network: bool = False) -> tuple[pd.DataFrame, list[str]]:
    timeout = max(int(timeout_seconds or 0), 0)
    if timeout <= 0:
        return fetch_daily(provider_name, symbol, start_date, end_date, network=network)

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_fetch_daily_worker, args=(queue, provider_name, symbol, start_date, end_date, network))
    process.daemon = True
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        raise TimeoutError(f"{provider_name} timed out after {timeout} seconds")
    try:
        payload = queue.get(timeout=1)
    except Empty as exc:
        raise RuntimeError(f"{provider_name} worker returned no result; exitcode={process.exitcode}") from exc
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or f"{provider_name} failed"))
    frame = pd.DataFrame(payload.get("records") or [], columns=payload.get("columns") or None)
    return frame, list(payload.get("warnings") or [])


def fetch_bounded_daily_row(symbol: str, expected_date: str, *, provider: str = "akshare", timeout_seconds: int = 30, network: bool = False) -> dict[str, Any]:
    """Fetch one validated daily row from a bounded public market-data provider chain."""

    attempts: list[dict[str, Any]] = []
    if not network:
        return {
            "status": "skipped_network_false",
            "provider_used": "",
            "row": {},
            "attempts": [
                {"provider": provider_name, "status": "skipped_network_false", "warnings": ["network=false; provider fetch skipped"]}
                for provider_name in _provider_chain(provider)
            ],
            "timeout_seconds": int(timeout_seconds),
        }
    for provider_name in _provider_chain(provider):
        try:
            frame, warnings = _fetch_daily_with_timeout(provider_name, symbol, expected_date, expected_date, timeout_seconds, network=network)
            if frame.empty:
                attempts.append({"provider": provider_name, "status": "empty", "warnings": warnings})
                continue
            row_frame = frame.loc[frame["date"].astype(str) == expected_date]
            if row_frame.empty:
                attempts.append({"provider": provider_name, "status": "date_not_returned", "warnings": warnings, "returned_dates": frame["date"].astype(str).tail(3).tolist()})
                continue
            row = {key: _clean_cell(value) for key, value in row_frame.iloc[-1].to_dict().items()}
            row["source"] = provider_name
            row["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            return {
                "status": "ok",
                "provider_used": provider_name,
                "row": row,
                "attempts": attempts + [{"provider": provider_name, "status": "ok", "warnings": warnings}],
                "timeout_seconds": int(timeout_seconds),
            }
        except TimeoutError as exc:
            attempts.append({"provider": provider_name, "status": "timeout", "error": repr(exc)})
        except Exception as exc:  # noqa: BLE001 - provider failures are recorded and fallback continues
            attempts.append({"provider": provider_name, "status": "failed", "error": repr(exc)})
    return {
        "status": "failed",
        "provider_used": "",
        "row": {},
        "attempts": attempts,
        "timeout_seconds": int(timeout_seconds),
    }
