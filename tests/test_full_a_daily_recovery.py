from __future__ import annotations

import json
import builtins
from pathlib import Path

import pandas as pd
import pytest


def test_legacy_daily_update_uses_existing_mootdx_fallback_for_bse(monkeypatch) -> None:
    import scripts.update_market_data as updater

    def fail_legacy(*_args, **_kwargs):
        raise RuntimeError("legacy provider unavailable")

    monkeypatch.setattr(updater, "fetch_hist_daily", fail_legacy)
    monkeypatch.setattr(updater, "fetch_sina_daily", fail_legacy)
    monkeypatch.setattr(
        updater,
        "_fetch_mootdx_daily",
        lambda *_args, **_kwargs: pd.DataFrame(
            [
                {
                    "date": "2026-08-17",
                    "symbol": "920038",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 100,
                    "amount": 1020,
                }
            ]
        ),
    )

    frame, provider, errors, _extra = updater.fetch_daily_provider_chain(
        "920038",
        "20260101",
        "20260817",
        "",
        expected_end_date="20260817",
    )

    assert provider == "mootdx_daily"
    assert errors["eastmoney_hist"] == "legacy provider unavailable"
    assert frame.iloc[0]["adjust"] == "raw"
    assert frame.iloc[0]["source"] == "mootdx_daily"


def test_review_reconciles_dead_chain_metadata_instead_of_leaving_running(
    tmp_path: Path,
) -> None:
    import scripts.run_daily_review_orchestrator as review

    metadata_path = tmp_path / "state" / "daily_data_chain.lock.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "run_id": "dead-run",
                "pid": 999999999,
                "status": "running",
                "target_date": "2026-08-17",
            }
        ),
        encoding="utf-8",
    )
    latest = tmp_path / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("{}", encoding="utf-8")

    with pytest.raises(review.AbandonedDailyChainProducer, match="dead-run"):
        with review.locked_authoritative_chain_event(
            project_root=tmp_path,
            manifest_path=latest,
            timeout_seconds=0,
        ):
            pass

    reconciled = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert reconciled["status"] == "abandoned"
    assert reconciled["recovery_required"] is True
    assert reconciled["abandoned_reason"] == "producer_pid_not_alive"


def test_daily_price_fallback_cli_exposes_serial_rate_limit(monkeypatch) -> None:
    import scripts.update_daily_price_fallback as cli

    calls: list[dict] = []
    monkeypatch.setattr(
        cli,
        "update_daily_prices",
        lambda **kwargs: calls.append(kwargs)
        or {"ok": True, "processed_symbols": []},
    )
    monkeypatch.setattr(builtins, "print", lambda *_args, **_kwargs: None)

    assert (
        cli.main(
            [
                "--symbols",
                "920038",
                "--enable-network",
                "--workers",
                "1",
                "--sleep",
                "1.5",
            ]
        )
        == 0
    )
    assert calls[0]["workers"] == 1
    assert calls[0]["sleep_seconds"] == 1.5


def test_daily_price_engine_waits_between_serial_network_symbols(monkeypatch) -> None:
    from quantdesk import daily_price_update_engine as engine

    symbols = ["600000", "600001"]
    rows = [{"symbol": symbol, "name": symbol, "included": "true"} for symbol in symbols]
    sleeps: list[float] = []

    monkeypatch.setattr(engine, "infer_target_date", lambda _value: ("2026-08-17", ""))
    monkeypatch.setattr(engine, "_target_is_safe_to_write", lambda _value: (True, ""))
    monkeypatch.setattr(engine, "resolve_symbols", lambda **_kwargs: rows)
    monkeypatch.setattr(
        engine,
        "local_daily_status_for_symbols",
        lambda symbol_rows, **_kwargs: {
            "latest_local_date": "2026-08-16",
            "max_local_date": "2026-08-16",
            "coverage_date": "2026-08-16",
            "raw_bar_coverage_date": "2026-08-16",
            "fresh_symbols": [],
            "stale_symbols": [item["symbol"] for item in symbol_rows],
            "missing_symbols": [],
            "fresh_count": 0,
            "stale_count": len(symbol_rows),
            "missing_count": 0,
            "recent_gap_symbol_count": 0,
            "recent_gap_date_count": 0,
            "missing_recent_trade_dates_by_symbol": {},
            "is_stale": True,
            "message": "",
        },
    )
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: {})
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda _name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(
        engine,
        "_fetch_merge_write_symbol",
        lambda provider, symbol, **kwargs: {
            "symbol": symbol,
            "updated": True,
            "resolved": True,
            "latest_date": kwargs["target"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(engine.time, "sleep", lambda seconds: sleeps.append(float(seconds)))
    monkeypatch.setattr(
        engine,
        "_backfill_target_amount_from_spot_snapshot",
        lambda *_args, **_kwargs: {
            "provider": "spot_amount_backfill",
            "status": "skipped",
            "updated": 0,
            "updated_symbol_list": [],
        },
    )
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})
    monkeypatch.setattr(engine, "atomic_write_json", lambda *_args, **_kwargs: None)

    payload = engine.update_daily_prices(
        target_date="2026-08-17",
        providers=["mootdx_daily"],
        symbols=symbols,
        workers=4,
        network=True,
        sleep_seconds=1.25,
        write_result=False,
    )

    assert payload["updated_symbols"] == 2
    assert sleeps == [1.25]


def test_daily_chain_rate_contract_serializes_network_acquisition() -> None:
    from quantdesk.daily_data_chain import _daily_update_rate_contract

    assert _daily_update_rate_contract(
        network=True,
        dry_run=False,
        requested_workers=8,
    ) == (1, 1.0)
    assert _daily_update_rate_contract(
        network=False,
        dry_run=False,
        requested_workers=8,
    ) == (8, 0.0)
    assert _daily_update_rate_contract(
        network=True,
        dry_run=True,
        requested_workers=8,
    ) == (8, 0.0)


def test_resume_run_preserves_global_cache_inventory_snapshot(tmp_path: Path) -> None:
    import scripts.update_market_data as updater

    inventory_path = tmp_path / "cache_inventory.json"
    original = {
        "universe_total": 5543,
        "csv_count": 5543,
        "expected_latest_trading_day": "2026-08-17",
    }
    inventory_path.write_text(json.dumps(original), encoding="utf-8")

    status = {
        "resume": True,
        "universe_total": 12,
        "results": [{"symbol": "920038", "output_path": "data/daily/920038.csv"}],
    }
    updater.write_cache_inventory(status, inventory_path)

    assert json.loads(inventory_path.read_text(encoding="utf-8")) == original
    assert status["cache_inventory_preserved"] is True


def test_custom_update_root_uses_sibling_inventory_path(tmp_path: Path) -> None:
    import scripts.update_market_data as updater

    status_path = tmp_path / "cache" / "update_status.json"
    resolved = updater.resolve_cache_inventory_path(
        {"data": {}},
        status_path=status_path,
        daily_dir=tmp_path / "daily",
    )

    assert resolved == tmp_path / "cache" / "cache_inventory.json"


def test_daily_chain_passes_scheduler_safe_rate_contract_into_provider() -> None:
    """Guard the wiring, not just the helper.

    `_daily_update_rate_contract` returning (1, 1.0) is useless unless the chain
    actually hands those values to `update_daily_prices`. This asserts the call
    site consumes the contract instead of re-deriving `batch_workers`.
    """

    import inspect

    from quantdesk.daily_data_chain import _run_daily_data_chain_impl

    source = inspect.getsource(_run_daily_data_chain_impl)
    contract_call = source.index("_daily_update_rate_contract(")
    update_call = source.index("update_daily_prices(", contract_call)
    window = source[update_call : update_call + 1200]

    assert "workers=daily_workers" in window
    assert "sleep_seconds=daily_sleep_seconds" in window
    # The raw requested worker count must not reach the provider on this path.
    assert "workers=batch_workers" not in window


def test_daily_chain_manifest_declares_data_and_research_scopes_separately() -> None:
    """full-A daily *data* coverage must never read as full-A *strategy* research.

    The formal research path still uses resolve_symbols(only_low_price=True), so
    the manifest has to state both scopes explicitly rather than letting a wide
    data-coverage number imply a wide research scan.
    """

    import inspect

    from quantdesk.daily_data_chain import _run_daily_data_chain_impl

    source = inspect.getsource(_run_daily_data_chain_impl)

    assert '"market_data_scope": "all_a"' in source
    assert '"research_scope": "current_analysis_pool"' in source
    assert '"full_a_research_scan": False' in source
    # The bounded research selector must remain the declared research scope.
    assert "resolve_symbols(only_low_price=True)" in source
