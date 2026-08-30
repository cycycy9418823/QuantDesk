from __future__ import annotations

import threading
import time

import pandas as pd


def test_daily_update_workers_fetch_symbols_concurrently(monkeypatch) -> None:
    from quantdesk import daily_price_update_engine as engine

    symbols = ["600000", "600001", "600002", "600003"]
    rows = [{"symbol": item, "name": item, "included": "true"} for item in symbols]
    status_calls = 0
    written: set[str] = set()
    active = 0
    max_active = 0
    guard = threading.Lock()

    def fake_status(symbol_rows, *, target_date, **_status_scope):
        """Accept the production status scope without coupling this fixture to it."""
        nonlocal status_calls
        status_calls += 1
        fresh = sorted(written) if status_calls > 1 else []
        stale = [item["symbol"] for item in symbol_rows if item["symbol"] not in fresh]
        return {
            "latest_local_date": "2026-07-15",
            "max_local_date": "2026-07-16" if fresh else "2026-07-15",
            "fresh_symbols": fresh,
            "stale_symbols": stale,
            "missing_symbols": [],
            "fresh_count": len(fresh),
            "stale_count": len(stale),
            "missing_count": 0,
            "recent_gap_symbol_count": 0,
            "recent_gap_date_count": 0,
            "missing_recent_trade_dates_by_symbol": {},
            "is_stale": bool(stale),
            "message": "",
        }

    def fake_fetch(provider, symbol, start_date, end_date, *, network):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.08)
        with guard:
            active -= 1
        return (
            pd.DataFrame(
                [
                    {
                        "date": end_date,
                        "symbol": symbol,
                        "open": 10.0,
                        "high": 10.2,
                        "low": 9.9,
                        "close": 10.1,
                        "volume": 1000,
                        "amount": 10100,
                        "adjust": "raw",
                        "source": provider,
                    }
                ]
            ),
            [],
        )

    monkeypatch.setattr(engine, "infer_target_date", lambda value: ("2026-07-16", ""))
    monkeypatch.setattr(engine, "_target_is_safe_to_write", lambda value: (True, ""))
    monkeypatch.setattr(engine, "resolve_symbols", lambda **kwargs: rows)
    monkeypatch.setattr(engine, "local_daily_status_for_symbols", fake_status)
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: {})
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(
        engine,
        "latest_date_for_symbol",
        lambda symbol, **kwargs: "2026-07-16" if symbol in written else "2026-07-15",
    )
    monkeypatch.setattr(engine, "fetch_daily", fake_fetch)
    monkeypatch.setattr(engine, "read_local_daily", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(engine, "merge_daily_frames", lambda existing, fresh: fresh)
    monkeypatch.setattr(engine, "_missing_recent_trade_dates", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        engine,
        "write_daily",
        lambda symbol, frame, **kwargs: written.add(symbol),
    )
    monkeypatch.setattr(
        engine,
        "_backfill_target_amount_from_spot_snapshot",
        lambda *args, **kwargs: {
            "provider": "spot_amount_backfill",
            "status": "skipped",
            "updated": 0,
            "updated_symbol_list": [],
        },
    )
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})
    monkeypatch.setattr(engine, "atomic_write_json", lambda *args, **kwargs: None)

    payload = engine.update_daily_prices(
        target_date="2026-07-16",
        providers=["mootdx_daily"],
        workers=4,
        network=True,
        symbols=symbols,
        write_result=False,
    )

    assert payload["status"] == "completed"
    assert payload["updated_symbols"] == 4
    assert max_active >= 2


def test_mootdx_client_factory_receives_requested_timeout(monkeypatch) -> None:
    from types import SimpleNamespace

    from quantdesk.providers import mootdx_provider

    calls: list[dict] = []
    sentinel = object()

    class FakeQuotes:
        @staticmethod
        def factory(**kwargs):
            calls.append(kwargs)
            return sentinel

    monkeypatch.setattr(
        mootdx_provider.importlib,
        "import_module",
        lambda name: SimpleNamespace(Quotes=FakeQuotes),
    )

    result = mootdx_provider._import_quotes_client(timeout=7)

    assert result is sentinel
    assert calls == [{"market": "std", "timeout": 7}]
