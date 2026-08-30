from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from quantdesk.interprocess_lock import interprocess_file_lock


def _blocking_provider_worker(symbol: str, context: dict[str, object]) -> dict[str, object]:
    """Process-safe fixture: a killed worker must never reach the marker write."""

    if context["provider_name"] == "fast_daily":
        return {
            "symbol": symbol,
            "resolved": True,
            "updated": False,
            "latest_date": str(context["target"]),
            "warnings": [],
            "reason": "",
        }
    time.sleep(0.8)
    marker = Path(str(context["daily_dir"])) / f"{symbol}.late-write"
    marker.write_text("late", encoding="utf-8")
    return {
        "symbol": symbol,
        "resolved": True,
        "updated": False,
        "latest_date": str(context["target"]),
        "warnings": [],
        "reason": "",
    }


def _daily_frame(symbol: str, trade_date: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [trade_date],
            "symbol": [symbol],
            "open": [4.0],
            "high": [4.2],
            "low": [3.9],
            "close": [4.1],
            "volume": [1000.0],
            "amount": [4100.0],
            "adjust": ["raw"],
            "source": ["test"],
            "updated_at": ["2026-07-28T18:00:00+08:00"],
        }
    )


def test_provider_total_deadline_kills_worker_without_late_write_and_releases_outer_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_price_update_engine as engine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    symbol = "600000"
    _daily_frame(symbol, "2026-07-28").to_csv(
        daily_dir / f"{symbol}.csv",
        index=False,
        encoding="utf-8-sig",
    )
    calendar = {
        "is_open_by_date": {
            "2026-07-28": True,
            "2026-07-29": True,
        }
    }
    monkeypatch.setattr(engine, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: calendar)
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})
    monkeypatch.setattr(
        engine,
        "_backfill_target_amount_from_spot_snapshot",
        lambda symbols, target: {
            "provider": "spot_snapshot_amount_fill",
            "status": "skipped",
            "attempted": 0,
            "updated": 0,
            "failed": 0,
            "updated_symbol_list": [],
        },
    )
    monkeypatch.setattr(engine, "_PROVIDER_PROCESS_WORKER", _blocking_provider_worker)

    outer_lock = tmp_path / "daily-chain.lock"
    started = time.monotonic()
    with interprocess_file_lock(outer_lock, timeout_seconds=0):
        payload = engine.update_daily_prices(
            target_date="2026-07-29",
            providers=["blocking_daily"],
            workers=1,
            network=True,
            symbols=symbol,
            output_path=tmp_path / "daily_update.json",
            provider_timeout_seconds=0.15,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert payload["status"] == "failed"
    assert payload["failed_symbol_list"] == [symbol]
    assert payload["failed_symbol_reasons"] == {symbol: "daily_provider_timeout"}
    assert payload["provider_results"][0]["status"] == "timeout"
    assert payload["provider_results"][0]["failed_reason"] == (
        "daily_provider_timeout (1)"
    )
    assert payload["provider_timeout_seconds"] == 0.15
    assert payload["provider_timeout_triggered"] is True

    # A thread-based timeout would return while the provider can still write.
    time.sleep(0.9)
    assert not (daily_dir / f"{symbol}.late-write").exists()
    with interprocess_file_lock(outer_lock, timeout_seconds=0):
        pass


def test_provider_slice_timeout_continues_fallback_within_global_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from quantdesk import daily_price_update_engine as engine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    symbol = "600001"
    _daily_frame(symbol, "2026-07-28").to_csv(
        daily_dir / f"{symbol}.csv",
        index=False,
        encoding="utf-8-sig",
    )
    calendar = {
        "is_open_by_date": {
            "2026-07-28": True,
            "2026-07-29": True,
        }
    }
    monkeypatch.setattr(engine, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: calendar)
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})
    monkeypatch.setattr(
        engine,
        "_backfill_target_amount_from_spot_snapshot",
        lambda symbols, target: {
            "provider": "spot_snapshot_amount_fill",
            "status": "skipped",
            "attempted": 0,
            "updated": 0,
            "failed": 0,
            "updated_symbol_list": [],
        },
    )
    monkeypatch.setattr(engine, "_PROVIDER_PROCESS_WORKER", _blocking_provider_worker)

    payload = engine.update_daily_prices(
        target_date="2026-07-29",
        providers=["slow_daily", "fast_daily"],
        workers=1,
        network=True,
        symbols=symbol,
        output_path=tmp_path / "daily_update.json",
        provider_timeout_seconds=5.0,
        provider_timeout_overrides={
            "slow_daily": 0.15,
            "fast_daily": 3.0,
        },
    )

    assert payload["status"] == "warning"
    assert payload["failed_symbols"] == 0
    assert payload["failed_symbol_reasons"] == {}
    assert [row["status"] for row in payload["provider_results"][:2]] == [
        "timeout",
        "ok",
    ]
    assert payload["provider_global_timeout_triggered"] is False
    assert payload["provider_timeout_events"] == [
        {
            "provider": "slow_daily",
            "scope": "provider_slice",
            "timeout_seconds": 0.15,
            "unfinished_symbol_list": [symbol],
            "reason": "daily_provider_timeout",
        }
    ]
    time.sleep(0.9)
    assert not (daily_dir / f"{symbol}.late-write").exists()
