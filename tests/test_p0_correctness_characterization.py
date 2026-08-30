from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.daily_feature_engine import build_daily_features
from quantdesk.market_regime_observer import build_market_regime_snapshot
from quantdesk.research.point_in_time_universe import is_tradable_at, universe_at
from quantdesk.scenario_curve_builder import build_scenario_curves
from scripts import run_backtest as run_backtest_cli
from scripts import run_combined_score_backtest as combined_score_cli


def _write_daily(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")


def _mixed_basis_rows(symbol: str = "000001") -> list[dict[str, object]]:
    return [
        {
            "date": "2024-01-02",
            "symbol": symbol,
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0,
            "volume": 1_000,
            "amount": 10_000,
            "adjust": "raw",
            "source": "local_existing",
        },
        {
            "date": "2024-01-03",
            "symbol": symbol,
            "open": 10.5,
            "high": 10.7,
            "low": 10.3,
            "close": 10.5,
            "volume": 1_100,
            "amount": 11_550,
            "adjust": "qfq",
            "source": "tencent_daily",
        },
    ]


def test_market_intel_target_date_caps_all_daily_consumers(tmp_path: Path) -> None:
    _write_daily(
        tmp_path / "data" / "daily" / "000001.csv",
        [
            {"date": "2024-01-02", "close": 10.0, "amount": 100_000_000},
            {"date": "2024-01-03", "close": 9.5, "amount": 100_000_000},
            {"date": "2024-01-04", "close": 20.0, "amount": 100_000_000},
        ],
    )

    regime = build_market_regime_snapshot(
        target_date="2024-01-03",
        scope="specified_symbols",
        symbols=["000001"],
        project_root=tmp_path,
    )
    curves = build_scenario_curves(
        candidates_payload={
            "sectors": [
                {
                    "sector": "sample",
                    "candidates": [
                        {
                            "symbol": "000001",
                            "name": "sample",
                            "style": "observe",
                            "latest_close": 9.5,
                        }
                    ],
                }
            ]
        },
        target_date="2024-01-03",
        project_root=tmp_path,
    )

    breadth = regime["breadth"]
    future_curve_dates = [
        str(row.get("date") or "")
        for curve in curves.get("curves") or []
        for row in curve.get("historical_reference_curve") or []
        if str(row.get("date") or "") > "2024-01-03"
    ]
    assert breadth["down_count"] == 1
    assert breadth["up_count"] == 0
    assert breadth["limit_up_count"] == 0
    assert future_curve_dates == []


def test_market_intel_historical_cutoff_does_not_fall_back_to_current_spot(tmp_path: Path) -> None:
    _write_daily(
        tmp_path / "data" / "daily" / "000001.csv",
        [{"date": "2024-01-04", "close": 20.0, "amount": 100_000_000}],
    )
    spot_path = tmp_path / "data" / "cache" / "spot_snapshot_latest.csv"
    spot_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "name": "sample",
                "price": 20.0,
                "pct_chg": 10.0,
                "amount": 100_000_000,
            }
        ]
    ).to_csv(spot_path, index=False, encoding="utf-8")

    regime = build_market_regime_snapshot(
        target_date="2024-01-03",
        scope="specified_symbols",
        symbols=["000001"],
        project_root=tmp_path,
    )

    assert regime["breadth"]["up_count"] == 0
    assert regime["breadth"]["limit_up_count"] == 0
    assert regime["turnover"]["sample_amount"] is None


def test_daily_features_fail_closed_on_mixed_raw_qfq(tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "000001.csv", _mixed_basis_rows())
    output_path = tmp_path / "daily_features.csv"
    summary_path = tmp_path / "daily_features_summary.json"

    payload = build_daily_features(
        only_low_price=False,
        daily_dir=daily_dir,
        output_path=output_path,
        summary_path=summary_path,
        symbols=["000001"],
    )

    evidence = json.dumps(
        {"warnings": payload.get("warnings"), "skipped": payload.get("skipped")},
        ensure_ascii=False,
    ).lower()
    assert payload["feature_count"] == 0
    assert payload["price_basis"] == "mixed_or_unknown"
    assert "mixed" in evidence and ("basis" in evidence or "adjust" in evidence)


def test_backtest_blocks_mixed_raw_qfq_before_simulation(tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    _write_daily(daily_dir / "000001.csv", _mixed_basis_rows())

    frame, reason = run_backtest_cli._read_daily("000001", daily_dir)

    assert frame.empty
    assert "mixed" in reason.lower() and ("basis" in reason.lower() or "adjust" in reason.lower())


def _pit_daily_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, date in enumerate(pd.bdate_range("2024-01-02", periods=70)):
        close = 10.0 + index * 0.03
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "symbol": "000001",
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 10_000 if index >= 20 and date.weekday() == 4 else 1_000,
                "amount": close * (10_000 if index >= 20 and date.weekday() == 4 else 1_000),
                "adjust": "raw",
            }
        )
    return rows


def test_pit_row_filter_covers_date_boundaries_reasons_and_structured_audit() -> None:
    from quantdesk.research.point_in_time_universe import filter_rows_for_pit_membership

    listings = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "list_date": pd.Timestamp("2024-01-02"),
                "delist_date": pd.Timestamp("2024-01-04"),
            },
            {"symbol": "000003", "list_date": pd.NaT, "delist_date": pd.NaT},
            {
                "symbol": "000004",
                "list_date": pd.Timestamp("2020-01-01"),
                "delist_date": pd.NaT,
                "st_start_date": pd.Timestamp("2024-01-03"),
                "st_end_date": pd.Timestamp("2024-01-03"),
            },
            {
                "symbol": "000005",
                "list_date": pd.Timestamp("2020-01-01"),
                "delist_date": pd.NaT,
                "suspend_start_date": pd.Timestamp("2024-01-03"),
                "suspend_end_date": pd.Timestamp("2024-01-03"),
            },
            {
                "symbol": "000006",
                "list_date": pd.Timestamp("2020-01-01"),
                "delist_date": pd.NaT,
            },
        ]
    )
    rows = pd.DataFrame(
        [
            {"date": "2024-01-01", "symbol": "000001", "value": 1},
            {"date": "2024-01-02", "symbol": "000001", "value": 2},
            {"date": "2024-01-04", "symbol": "000001", "value": 3},
            {"date": "2024-01-05", "symbol": "000001", "value": 4},
            {"date": "2024-01-03", "symbol": "000002", "value": 5},
            {"date": "2024-01-03", "symbol": "000003", "value": 6},
            {"date": "2024-01-03", "symbol": "000004", "value": 7},
            {"date": "2024-01-03", "symbol": "000005", "value": 8},
            {"date": "2024-01-03", "symbol": "000006", "value": 9},
        ]
    )

    included, audit = filter_rows_for_pit_membership(rows, listings)

    assert included["value"].tolist() == [2, 3, 9]
    assert audit["checked_dates"] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
    ]
    assert audit["requested_row_count"] == 9
    assert audit["included_row_count"] == 3
    assert audit["excluded_row_count"] == 6
    assert audit["reason_counts"] == {
        "delisted": 1,
        "missing_list_date": 1,
        "missing_listing": 1,
        "not_listed_yet": 1,
        "st_excluded": 1,
        "suspended_excluded": 1,
    }
    assert audit["by_date"]["2024-01-03"] == {
        "requested_row_count": 5,
        "included_row_count": 1,
        "excluded_row_count": 4,
        "reason_counts": {
            "missing_list_date": 1,
            "missing_listing": 1,
            "st_excluded": 1,
            "suspended_excluded": 1,
        },
    }
    assert set(audit["coverage_info"]) >= {
        "list_date_coverage",
        "delist_date_coverage",
        "st_coverage",
        "suspension_coverage",
    }
    assert audit["survivorship_bias_risk"] == "high"


def test_delist_date_is_last_tradable_day_for_all_pit_helpers() -> None:
    listings = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "list_date": pd.Timestamp("2020-01-01"),
                "delist_date": pd.Timestamp("2024-01-04"),
            }
        ]
    )

    assert is_tradable_at("000001", "2024-01-04", listings)[0] is True
    assert is_tradable_at("000001", "2024-01-05", listings)[0] is False
    assert universe_at("2024-01-04", listings)[0] == ["000001"]
    assert universe_at("2024-01-05", listings)[0] == []


def test_backtest_keeps_end_date_delisted_symbol_for_eligible_early_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_dir = tmp_path / "daily"
    rows = _pit_daily_rows()
    _write_daily(daily_dir / "000001.csv", rows)
    listings = tmp_path / "historical_listings.csv"
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "list_date": rows[0]["date"],
                "delist_date": "2024-03-15",
                "source": "user_supplied",
            }
        ]
    ).to_csv(listings, index=False, encoding="utf-8")

    captured_payload: dict[str, object] = {}
    captured_histories: dict[str, pd.DataFrame] = {}
    real_simulate = run_backtest_cli.simulate_portfolio_backtest

    def capture_simulation(
        histories: dict[str, pd.DataFrame],
        config: object,
        *,
        strategy: str,
    ) -> object:
        captured_histories.update({symbol: frame.copy() for symbol, frame in histories.items()})
        return real_simulate(histories, config, strategy=strategy)

    def capture_payload(_path: Path, payload: dict[str, object]) -> None:
        if payload.get("task_type") == "backtest":
            captured_payload.update(payload)

    monkeypatch.setattr(run_backtest_cli, "simulate_portfolio_backtest", capture_simulation)
    monkeypatch.setattr(run_backtest_cli, "write_json", capture_payload)
    monkeypatch.setattr(run_backtest_cli, "_write_report", lambda _payload: None)
    monkeypatch.setattr(run_backtest_cli, "_attach_experiment_log", lambda payload: payload)
    monkeypatch.setattr(run_backtest_cli, "export_backtest_result", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_backtest_cli, "render_equity_curve", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_backtest_cli, "render_monthly_returns", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        run_backtest_cli,
        "resolve_target_complete_trade_date",
        lambda value=None: {
            "target_complete_trade_date": value or str(rows[-1]["date"]),
            "target_date_warnings": [],
        },
    )

    assert run_backtest_cli.main(
        [
            "--symbols",
            "000001",
            "--scope",
            "specified_symbols",
            "--mode",
            "portfolio",
            "--strategy",
            "ma_breakout",
            "--start-date",
            str(rows[0]["date"]),
            "--end-date",
            str(rows[-1]["date"]),
            "--daily-dir",
            str(daily_dir),
            "--pit-universe",
            "true",
            "--historical-listings-csv",
            str(listings),
        ]
    ) == 0
    assert list(captured_histories) == ["000001"]
    filtered_history = captured_histories["000001"]
    assert filtered_history["date"].min() == pd.Timestamp(rows[0]["date"])
    assert filtered_history["date"].max() == pd.Timestamp("2024-03-15")
    audit = captured_payload["pit_filter_audit"]
    assert audit["requested_row_count"] == len(rows)
    assert audit["included_row_count"] == len(filtered_history)
    assert audit["excluded_row_count"] == len(rows) - len(filtered_history)
    assert audit["reason_counts"]["delisted"] == len(rows) - len(filtered_history)
    assert audit["by_date"]["2024-03-18"]["reason_counts"] == {"delisted": 1}


@pytest.mark.parametrize(
    ("listing_overrides", "reason_code"),
    [
        ({"delist_date": pd.Timestamp("2024-01-03")}, "delisted"),
        (
            {
                "st_start_date": pd.Timestamp("2024-01-04"),
                "st_end_date": pd.Timestamp("2024-01-04"),
            },
            "st_excluded",
        ),
        (
            {
                "suspend_start_date": pd.Timestamp("2024-01-04"),
                "suspend_end_date": pd.Timestamp("2024-01-04"),
            },
            "suspended_excluded",
        ),
    ],
)
def test_combined_score_rechecks_pit_membership_on_actual_entry_date(
    listing_overrides: dict[str, object],
    reason_code: str,
) -> None:
    from quantdesk.research.combined_score_backtest import run_combined_score_backtest

    score_panel = pd.DataFrame(
        [{"date": "2024-01-03", "symbol": "000001", "combined_score": 1.0}]
    )
    price_panel = pd.DataFrame(
        [
            {"date": "2024-01-03", "symbol": "000001", "close": 10.0},
            {"date": "2024-01-04", "symbol": "000001", "close": 11.0},
            {"date": "2024-01-05", "symbol": "000001", "close": 12.0},
        ]
    )
    listing = {
        "symbol": "000001",
        "list_date": pd.Timestamp("2020-01-01"),
        "delist_date": pd.NaT,
    }
    listing.update(listing_overrides)
    listings = pd.DataFrame([listing])
    pit_status = {
        "available": True,
        "pit_universe": True,
        "list_date_coverage": 1.0,
        "delist_date_coverage": 0.0,
        "st_coverage": 0.0,
        "suspend_coverage": 0.0,
        "survivorship_bias_risk": "high",
    }

    payload = run_combined_score_backtest(
        score_panel,
        price_panel,
        top_n=1,
        holding_days=1,
        execution_lag_days=1,
        transaction_cost_bps=0,
        slippage_bps=0,
        historical_listings=listings,
        pit_universe_status=pit_status,
    )

    assert payload["rebalances_preview"] == []
    entry_audit = payload["pit_filter_audit"]["stages"]["entry"]
    assert entry_audit["requested_row_count"] == 1
    assert entry_audit["included_row_count"] == 0
    assert entry_audit["excluded_row_count"] == 1
    assert entry_audit["reason_counts"] == {reason_code: 1}


def test_combined_score_pit_off_preserves_default_ranking_and_execution() -> None:
    from quantdesk.research.combined_score_backtest import run_combined_score_backtest

    score_panel = pd.DataFrame(
        [
            {"date": "2024-01-03", "symbol": "000001", "combined_score": 1.0},
            {"date": "2024-01-03", "symbol": "000002", "combined_score": 2.0},
        ]
    )
    price_panel = pd.DataFrame(
        [
            {"date": date, "symbol": symbol, "close": close}
            for date, close in (
                ("2024-01-03", 10.0),
                ("2024-01-04", 11.0),
                ("2024-01-05", 12.0),
            )
            for symbol in ("000001", "000002")
        ]
    )
    kwargs = {
        "top_n": 1,
        "holding_days": 1,
        "execution_lag_days": 1,
        "transaction_cost_bps": 0,
        "slippage_bps": 0,
    }

    default_payload = run_combined_score_backtest(score_panel, price_panel, **kwargs)
    explicit_off_payload = run_combined_score_backtest(
        score_panel,
        price_panel,
        pit_universe_status={"available": True, "pit_universe": True},
        historical_listings=None,
        **kwargs,
    )

    assert default_payload["rebalances_preview"] == explicit_off_payload["rebalances_preview"]
    assert default_payload["rebalances_preview"][0]["selected_symbols"] == ["000002"]
    assert default_payload["performance"] == explicit_off_payload["performance"]
    assert default_payload["pit_filter_audit"]["pit_universe_used"] is False
    assert explicit_off_payload["pit_filter_audit"]["pit_universe_used"] is False


def test_backtest_applies_pit_membership_at_each_rebalance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daily_dir = tmp_path / "daily"
    rows = _pit_daily_rows()
    _write_daily(daily_dir / "000001.csv", rows)
    listings = tmp_path / "historical_listings.csv"
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "list_date": "2024-03-01",
                "delist_date": "",
                "source": "user_supplied",
            }
        ]
    ).to_csv(listings, index=False, encoding="utf-8")

    captured: dict[str, object] = {}

    def capture_payload(_path: Path, payload: dict[str, object]) -> None:
        if payload.get("task_type") == "backtest":
            captured.update(payload)

    monkeypatch.setattr(run_backtest_cli, "write_json", capture_payload)
    monkeypatch.setattr(run_backtest_cli, "_write_report", lambda _payload: None)
    monkeypatch.setattr(run_backtest_cli, "_attach_experiment_log", lambda payload: payload)
    monkeypatch.setattr(run_backtest_cli, "export_backtest_result", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_backtest_cli, "render_equity_curve", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_backtest_cli, "render_monthly_returns", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        run_backtest_cli,
        "resolve_target_complete_trade_date",
        lambda value=None: {"target_complete_trade_date": value or "2024-04-08", "target_date_warnings": []},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_backtest.py",
            "--symbols",
            "000001",
            "--scope",
            "specified_symbols",
            "--mode",
            "portfolio",
            "--strategy",
            "ma_breakout",
            "--start-date",
            rows[0]["date"],
            "--end-date",
            rows[-1]["date"],
            "--daily-dir",
            str(daily_dir),
            "--pit-universe",
            "true",
            "--historical-listings-csv",
            str(listings),
            "--json",
            "true",
        ],
    )

    assert run_backtest_cli.main() == 0
    assert captured, "backtest payload was not captured"
    early_fills = [
        trade
        for trade in captured.get("trades") or []
        if str(trade.get("symbol") or "") == "000001"
        and not bool(trade.get("blocked"))
        and str(trade.get("date") or trade.get("entry_date") or "") < "2024-03-01"
    ]
    assert early_fills == []


def test_combined_score_applies_pit_membership_at_each_signal_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_path = tmp_path / "combined_score.json"
    score_rows = []
    for signal_date in ("2024-02-01", "2024-03-04"):
        score_rows.extend(
            [
                {"date": signal_date, "symbol": "000001", "combined_score": 1.0},
                {"date": signal_date, "symbol": "000002", "combined_score": 2.0},
            ]
        )
    score_path.write_text(
        json.dumps({"task_type": "factor_combination", "data_source": "local_daily", "score_panel": score_rows}),
        encoding="utf-8",
    )

    daily_dir = tmp_path / "daily"
    dates = pd.bdate_range("2024-01-31", "2024-03-08")
    for symbol, offset in (("000001", 0.0), ("000002", 1.0)):
        _write_daily(
            daily_dir / f"{symbol}.csv",
            [
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "symbol": symbol,
                    "open": 10.0 + offset + index * 0.01,
                    "high": 10.2 + offset + index * 0.01,
                    "low": 9.8 + offset + index * 0.01,
                    "close": 10.0 + offset + index * 0.01,
                    "volume": 1_000,
                    "amount": 10_000,
                    "adjust": "raw",
                }
                for index, date in enumerate(dates)
            ],
        )
    listings = tmp_path / "historical_listings.csv"
    pd.DataFrame(
        [
            {"symbol": "000001", "list_date": "2000-01-01", "delist_date": "", "source": "user_supplied"},
            {"symbol": "000002", "list_date": "2024-03-01", "delist_date": "", "source": "user_supplied"},
        ]
    ).to_csv(listings, index=False, encoding="utf-8")

    monkeypatch.setattr(
        combined_score_cli,
        "write_combined_score_backtest_outputs",
        lambda payload, **_kwargs: payload,
    )
    monkeypatch.setattr(combined_score_cli, "compute_benchmark_comparison", lambda *_args, **_kwargs: {})
    args = combined_score_cli._parse_args(
        [
            "--profile",
            "general",
            "--top-n",
            "1",
            "--rebalance-frequency",
            "weekly",
            "--holding-days",
            "1",
            "--execution-lag-days",
            "1",
            "--transaction-cost-bps",
            "0",
            "--slippage-bps",
            "0",
            "--combined-score-json",
            str(score_path),
            "--daily-dir",
            str(daily_dir),
            "--historical-listings-csv",
            str(listings),
            "--use-point-in-time-universe",
            "true",
            "--pit-mode",
            "off",
            "--write-latest",
            "false",
            "--output-dir",
            str(tmp_path / "results"),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )

    payload = combined_score_cli.run(args)
    early = next(item for item in payload["rebalances_preview"] if item["signal_date"] == "2024-02-01")
    assert early["selected_symbols"] == ["000001"]
    signal_audit = payload["pit_filter_audit"]["stages"]["signal"]
    assert signal_audit["checked_dates"] == ["2024-02-01", "2024-03-04"]
    assert signal_audit["requested_row_count"] == 4
    assert signal_audit["included_row_count"] == 3
    assert signal_audit["excluded_row_count"] == 1
    assert signal_audit["reason_counts"] == {"not_listed_yet": 1}
    assert signal_audit["by_date"]["2024-02-01"]["reason_counts"] == {
        "not_listed_yet": 1
    }
    assert payload["pit_filter_audit"]["survivorship_bias_risk"] == "high"
