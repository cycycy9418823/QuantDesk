from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantdesk.candidate_tracking import build_candidate_performance, price_on_or_after
from quantdesk import daily_candidate_generator as dcg
from quantdesk.daily_candidate_generator import _entry_price
from quantdesk.stock_screener import ScreenCondition, _check_condition
from quantdesk.strategy_lab.rolling_strategy_backtest import symbol_metrics


def test_stock_screener_boolean_string_false_is_false() -> None:
    assert _check_condition({"flag": "false"}, ScreenCondition("flag", "==", False)) is True
    assert _check_condition({"flag": "false"}, ScreenCondition("flag", "==", True)) is False


def test_limit_up_proxy_uses_board_specific_limit_rate(tmp_path: Path) -> None:
    rows = []
    for idx in range(79):
        rows.append({"date": f"2026-01-{(idx % 28) + 1:02d}", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1000})
    rows.append({"date": "2026-04-30", "open": 11, "high": 11, "low": 11, "close": 11, "volume": 1000})
    path = tmp_path / "300001.csv"
    pd.DataFrame(rows).to_csv(path, index=False)

    metrics = symbol_metrics(path)

    assert metrics is not None
    assert metrics["limit_up_proxy"] is False


def test_candidate_entry_price_uses_next_trading_day_open(tmp_path: Path) -> None:
    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    pd.DataFrame(
        [
            {"date": "2026-06-25", "open": 9.0, "high": 10.5, "low": 8.8, "close": 10.0, "volume": 1000},
            {"date": "2026-06-26", "open": 11.0, "high": 12.5, "low": 10.8, "close": 12.0, "volume": 1000},
        ]
    ).to_csv(daily / "000001.csv", index=False)

    assert _entry_price(tmp_path, "000001", "2026-06-25") == "11.00"
    assert price_on_or_after(tmp_path, "000001", "2026-06-25") == ("2026-06-26", 11.0)


def test_candidate_entry_price_falls_back_to_next_day_close_and_performance_starts_at_entry_day(tmp_path: Path) -> None:
    daily = tmp_path / "data" / "daily"
    tracking = tmp_path / "data" / "tracking"
    daily.mkdir(parents=True)
    tracking.mkdir(parents=True)
    pd.DataFrame(
        [
            {"date": "2026-06-25", "open": 9.0, "high": 10.5, "low": 8.8, "close": 10.0, "volume": 1000},
            {"date": "2026-06-26", "open": 0.0, "high": 12.5, "low": 10.8, "close": 12.0, "volume": 1000},
            {"date": "2026-06-29", "open": 12.5, "high": 13.5, "low": 12.4, "close": 13.0, "volume": 1000},
        ]
    ).to_csv(daily / "000001.csv", index=False)
    pd.DataFrame(
        [
            {
                "entry_date": "2026-06-25",
                "ts_code": "000001.SZ",
                "stock_name": "sample",
                "strategy_type": "long",
                "entry_price": "12.0000",
                "entry_signal": "signal",
                "data_source": "formal",
            }
        ]
    ).to_csv(tracking / "candidate_ledger.csv", index=False)

    assert _entry_price(tmp_path, "000001", "2026-06-25") == "12.00"
    payload = build_candidate_performance(
        tmp_path,
        provenance={
            "trigger": "scheduled_daily_review",
            "authoritative_chain_run_id": "daily-data-chain-20260625T100000Z-fixture",
            "source_hash": "sha256:fixture",
        },
    )
    perf = pd.read_csv(tracking / "candidate_performance.csv")

    assert payload["performance_rows"] == 1
    assert payload["provenance"]["trigger"] == "scheduled_daily_review"
    assert payload["provenance"]["authoritative_chain_run_id"].endswith("fixture")
    assert payload["provenance"]["source_hash"] == "sha256:fixture"
    assert payload["generated_at"]
    assert round(float(perf.loc[0, "hold_1d_ret"]), 6) == round((13.0 - 12.0) / 12.0, 6)


def test_daily_candidate_generator_filters_untradable_metrics_before_strategy(monkeypatch, tmp_path: Path) -> None:
    seen_symbols: list[str] = []
    metrics = [
        {"symbol": "000001", "name": "normal", "signal_date": "2026-06-26", "close": 10.0, "history_days": 200, "avg_volume_20d": 1000, "limit_up_proxy": False},
        {"symbol": "000002", "name": "one-word", "signal_date": "2026-06-26", "close": 11.0, "history_days": 200, "avg_volume_20d": 1000, "limit_up_proxy": True},
    ]

    monkeypatch.setattr(dcg, "_load_name_map", lambda root: {"000001": "normal", "000002": "one-word"})
    monkeypatch.setattr(dcg, "_spot_market_universe", lambda root, *, limit: ["000001", "000002"])
    monkeypatch.setattr(dcg, "_load_market_metrics_for_symbols", lambda root, symbols: metrics)
    monkeypatch.setattr(dcg, "_strategy_weights", lambda root: {})

    def fake_long(rows):
        seen_symbols.extend(str(row.get("symbol")) for row in rows)
        return {"ok": True}, [
            {
                "symbol": row["symbol"],
                "name": row.get("name"),
                "signal_date": row["signal_date"],
                "strategy_id": "long_test",
                "research_score": 1.0,
            }
            for row in rows
        ]

    monkeypatch.setattr(dcg, "generate_long_term", fake_long)
    monkeypatch.setattr(dcg, "generate_low_price", lambda rows: ({"ok": True}, []))
    monkeypatch.setattr(dcg, "generate_limit_up", lambda rows: ({"ok": True}, [], {}))

    payload = dcg.generate_daily_recommendations(tmp_path, append_ledger=False)

    assert seen_symbols == ["000001"]
    assert payload["candidate_count"] == 1
    assert payload["excluded_candidate_count"] == 1
    assert payload["excluded_candidate_samples"][0]["symbol"] == "000002"
    assert "one_word_limit_up_not_buyable" in payload["excluded_candidate_samples"][0]["exclude_reasons"]


def test_daily_candidate_strategy_weight_read_error_is_visible(monkeypatch, tmp_path: Path) -> None:
    metrics = [
        {"symbol": "000001", "name": "normal", "signal_date": "2026-06-26", "close": 10.0, "history_days": 200, "avg_volume_20d": 1000, "limit_up_proxy": False},
    ]

    monkeypatch.setattr(dcg, "_load_name_map", lambda root: {"000001": "normal"})
    monkeypatch.setattr(dcg, "_spot_market_universe", lambda root, *, limit: ["000001"])
    monkeypatch.setattr(dcg, "_load_market_metrics_for_symbols", lambda root, symbols: metrics)
    monkeypatch.setattr(dcg, "read_json", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad strategy weight json")))
    monkeypatch.setattr(dcg, "generate_low_price", lambda rows: ({"ok": True}, []))
    monkeypatch.setattr(dcg, "generate_limit_up", lambda rows: ({"ok": True}, [], {}))
    monkeypatch.setattr(
        dcg,
        "generate_long_term",
        lambda rows: (
            {"ok": True},
            [
                {
                    "symbol": "000001",
                    "name": "normal",
                    "signal_date": "2026-06-26",
                    "strategy_id": "long_test",
                    "research_score": 1.0,
                }
            ],
        ),
    )

    payload = dcg.generate_daily_recommendations(tmp_path, append_ledger=False)

    assert "strategy_weights_unreadable:bad strategy weight json" in payload["warnings"]


def _write_weights(root: Path, payload: dict) -> None:
    import json

    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "strategy_effectiveness_weights.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_strategy_weights_are_neutral_by_default_even_when_file_exists(tmp_path: Path) -> None:
    """Research weights must not tilt the formal pool unless explicitly opted in."""

    _write_weights(tmp_path, {"weight_by_bucket": {"打板复核": 1.4, "长期/波段复核": 0.6}})

    weights, warnings = dcg._strategy_weights_with_warnings(tmp_path)

    assert weights == {}
    assert "strategy_weights_not_consumed:neutral_default_requires_explicit_opt_in" in warnings
    # The convenience reader used by the generator inherits the neutral default.
    assert dcg._strategy_weights(tmp_path) == {}


def test_strategy_weights_opt_in_requires_full_governance_provenance(tmp_path: Path) -> None:
    _write_weights(tmp_path, {"weight_by_bucket": {"打板复核": 1.4}, "publication_status": "approved"})

    weights, warnings = dcg._strategy_weights_with_warnings(tmp_path, consume_research_weights=True)

    assert weights == {}
    assert any(w.startswith("strategy_weights_ungoverned:missing_") for w in warnings)


def test_strategy_weights_opt_in_requires_approved_publication_status(tmp_path: Path) -> None:
    _write_weights(
        tmp_path,
        {
            "weight_by_bucket": {"打板复核": 1.4},
            "version": "v1",
            "as_of": "2026-08-18",
            "config_hash": "abc123",
            "adjudication_id": "adj-001",
            "publication_status": "draft",
        },
    )

    weights, warnings = dcg._strategy_weights_with_warnings(tmp_path, consume_research_weights=True)

    assert weights == {}
    assert "strategy_weights_not_approved:publication_status=draft" in warnings


def test_strategy_weights_consumed_only_when_governed_and_approved(tmp_path: Path) -> None:
    _write_weights(
        tmp_path,
        {
            "weight_by_bucket": {"打板复核": 1.4, "长期/波段复核": 0.6},
            "version": "v1",
            "as_of": "2026-08-18",
            "config_hash": "abc123",
            "adjudication_id": "adj-001",
            "publication_status": "approved",
        },
    )

    weights, warnings = dcg._strategy_weights_with_warnings(tmp_path, consume_research_weights=True)

    assert weights == {"打板复核": 1.4, "长期/波段复核": 0.6}
    assert warnings == []
