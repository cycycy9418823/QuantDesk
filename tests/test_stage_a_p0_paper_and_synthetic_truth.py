from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantdesk.paper import paper_trading
from quantdesk.paper.position_management_engine import normalize_position_for_t1
from quantdesk.research import dynamic_factor_model
from quantdesk.research import paper_watchlist
from quantdesk.research import return_explain
from quantdesk.research import stock_picking_flow
from quantdesk.risk import smart_stop


def test_manual_paper_before_first_bar_does_not_use_future_daily_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "000001.csv").write_text(
        "date,close,pct_chg,vol\n"
        "2026-06-20,10.0,0.0,1000\n"
        "2026-06-21,11.0,9.9,1000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paper_trading, "DAILY_DIR", daily_dir)

    augmented = paper_trading._augment_trade_metadata_from_local_daily(
        "000001",
        {"trade_date": "2026-06-19"},
        "2026-06-19",
    )

    assert not augmented.get("daily_bar_date")
    assert "pct_chg" not in augmented
    assert "suspended" not in augmented


def test_manual_paper_invalid_trade_date_does_not_select_latest_daily_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "000001.csv").write_text(
        "date,close,pct_chg,vol\n"
        "2026-06-20,10.0,0.0,1000\n"
        "2026-06-21,11.0,9.9,1000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paper_trading, "DAILY_DIR", daily_dir)

    augmented = paper_trading._augment_trade_metadata_from_local_daily(
        "000001",
        {"trade_date": "not-a-date"},
        "not-a-date",
    )

    assert not augmented.get("daily_bar_date")
    assert "pct_chg" not in augmented
    assert "suspended" not in augmented


def test_manual_paper_public_endpoints_reject_invalid_explicit_trade_dates(
    tmp_path: Path,
) -> None:
    buy_path = tmp_path / "invalid_buy.json"
    buy_result = paper_trading.paper_buy(
        "000001",
        "sample",
        10.0,
        100,
        path=buy_path,
        metadata={"trade_date": "not-a-date"},
    )

    sell_path = tmp_path / "invalid_sell.json"
    portfolio = paper_trading.create_default_paper_portfolio()
    portfolio["positions"] = [
        {
            "symbol": "000001",
            "name": "sample",
            "shares": 100,
            "buy_price": 10.0,
            "buy_date": "2026-06-20",
            "available_shares": 0,
        }
    ]
    paper_trading.save_paper_portfolio(portfolio, sell_path)
    sell_result = paper_trading.paper_sell(
        "000001",
        10.0,
        100,
        path=sell_path,
        metadata={"trade_date": "not-a-date"},
    )

    assert buy_result["ok"] is False
    assert paper_trading.load_paper_portfolio(buy_path)["positions"] == []
    assert sell_result["ok"] is False
    sell_portfolio = paper_trading.load_paper_portfolio(sell_path)
    assert sell_portfolio["positions"][0]["shares"] == 100
    assert sell_portfolio["trades"] == []


def test_reverse_replay_does_not_unfreeze_future_buy() -> None:
    normalized = normalize_position_for_t1(
        {
            "symbol": "000001",
            "quantity": 100,
            "sellable_quantity": 0,
            "frozen_quantity": 100,
            "entry_date": "2026-06-20",
            "last_buy_date": "2026-06-20",
        },
        "2026-06-19",
    )

    assert normalized["sellable_quantity"] == 0
    assert normalized["frozen_quantity"] == 100


def test_same_day_timestamp_does_not_unfreeze_t1_position() -> None:
    normalized = normalize_position_for_t1(
        {
            "symbol": "000001",
            "quantity": 100,
            "sellable_quantity": 0,
            "frozen_quantity": 100,
            "entry_date": "2026-06-20",
            "last_buy_date": "2026-06-20",
        },
        "2026-06-20 14:00:00",
    )

    assert normalized["sellable_quantity"] == 0
    assert normalized["frozen_quantity"] == 100


def test_dynamic_factor_missing_metrics_do_not_invent_ic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_factor_model, "read_json", lambda *_args, **_kwargs: {})

    result = dynamic_factor_model.evaluate_recent_factor_effectiveness({"factor_metrics": {}})

    assert result["status"] in {"warning", "failed", "missing", "degraded"}
    assert result["factors"] == {}
    assert any("missing" in str(item).lower() for item in result.get("warnings") or [])


def test_dynamic_factor_invalid_ic_rows_do_not_receive_default_values() -> None:
    result = dynamic_factor_model.evaluate_recent_factor_effectiveness(
        {
            "factor_metrics": {
                "missing": {},
                "bad_text": {"ic": "not-a-number"},
                "not_finite": {"ic": float("nan")},
            }
        }
    )

    assert result["status"] in {"warning", "failed", "missing", "degraded"}
    assert result["factors"] == {}
    assert any("invalid" in str(item).lower() for item in result.get("warnings") or [])


def test_dynamic_factor_upstream_synthetic_requires_explicit_opt_in() -> None:
    payload = {
        "data_source": "synthetic_test_data",
        "factor_metrics": {"momentum": {"ic": 0.2}},
    }

    rejected = dynamic_factor_model.evaluate_recent_factor_effectiveness(payload)
    allowed = dynamic_factor_model.evaluate_recent_factor_effectiveness(
        payload,
        synthetic_test_data=True,
    )

    assert rejected["factors"] == {}
    assert rejected["status"] == "warning"
    assert any("synthetic" in str(item).lower() for item in rejected.get("warnings") or [])
    assert allowed["factors"] == {"momentum": 0.2}
    assert allowed["status"] == "warning"
    assert allowed["synthetic_test_data"] is True


def test_dynamic_factor_accepts_canonical_ic_mean_without_fabricating_defaults() -> None:
    result = dynamic_factor_model.evaluate_recent_factor_effectiveness(
        {
            "data_source": "local_daily",
            "factor_metrics": {
                "momentum": {"ic_mean": -0.2},
                "quality": {"ic": 0.1, "ic_mean": 0.9},
            },
        }
    )
    weights = dynamic_factor_model.compute_dynamic_factor_weights(result)

    assert result["status"] == "passed"
    assert result["factors"] == {"momentum": 0.2, "quality": 0.1}
    assert weights == {"momentum": 0.666667, "quality": 0.333333}


def test_stock_picking_missing_recommendations_do_not_invent_candidates() -> None:
    result = stock_picking_flow.merge_sector_and_strategy_recommendations({}, {}, {})

    assert result["top_candidates"] == []


def test_stock_picking_explicit_synthetic_candidates_are_labeled_test_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stock_picking_flow, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = stock_picking_flow.build_stock_picking_flow_report(
        {"synthetic_test_data": True, "top": 3}
    )

    assert result["status"] == "warning"
    assert result["synthetic_test_data"] is True
    assert any("synthetic_test_data" in str(item) for item in result.get("warnings") or [])
    assert result["top_candidates"]
    assert all(item.get("evidence_kind") == "synthetic_test_demo" for item in result["top_candidates"])


def test_stock_picking_rejects_upstream_synthetic_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.research.strategy_recommender import build_strategy_recommendation_report

    synthetic_strategy = build_strategy_recommendation_report(
        {"synthetic_test_data": True, "top": 3}
    )

    def fake_read_json(path: str | Path, default: Any = None) -> Any:
        if "strategy" in str(path).lower():
            return synthetic_strategy
        return {}

    monkeypatch.setattr(stock_picking_flow, "read_json", fake_read_json)
    monkeypatch.setattr(stock_picking_flow, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = stock_picking_flow.build_stock_picking_flow_report(
        {"synthetic_test_data": False}
    )

    assert result["top_candidates"] == []
    assert result["status"] == "warning"
    assert any("synthetic" in str(item).lower() for item in result.get("warnings") or [])


def test_stock_picking_propagates_upstream_warning_when_recommendations_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_json(path: str | Path, default: Any = None) -> Any:
        if "strategy" in str(path).lower():
            return {
                "status": "warning",
                "warnings": ["no_recommendations"],
                "recommendations": [],
            }
        return {"status": "passed"}

    monkeypatch.setattr(stock_picking_flow, "read_json", fake_read_json)
    monkeypatch.setattr(stock_picking_flow, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = stock_picking_flow.build_stock_picking_flow_report(
        {"synthetic_test_data": False}
    )

    assert result["top_candidates"] == []
    assert result["status"] == "warning"
    assert any("recommend" in str(item).lower() for item in result.get("warnings") or [])


def test_paper_watchlist_missing_strategy_does_not_add_synthetic_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    paper_watchlist.init_paper_watchlist(watchlist_path)
    original_read_json = paper_watchlist.read_json
    add_calls: list[list[dict[str, Any]]] = []

    def fake_read_json(path: str | Path, default: Any = None) -> Any:
        if str(path).replace("\\", "/").endswith("results/strategy_recommendation_latest.json"):
            return {}
        return original_read_json(path, default)

    def record_add(items: list[dict[str, Any]], path: str | Path) -> dict[str, Any]:
        add_calls.append(items)
        return original_read_json(path, {})

    monkeypatch.setattr(paper_watchlist, "read_json", fake_read_json)
    monkeypatch.setattr(paper_watchlist, "add_watchlist_items", record_add)
    monkeypatch.setattr(paper_watchlist, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = paper_watchlist.build_paper_watchlist_report(
        {"watchlist_path": str(watchlist_path), "add_from_strategy": True, "top": 10}
    )

    assert add_calls == []
    assert result["watchlist_count"] == 0
    assert result["status"] in {"warning", "failed", "missing", "degraded"}
    assert any("missing" in str(item).lower() for item in result.get("warnings") or [])


def test_paper_watchlist_explicit_synthetic_items_keep_test_demo_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    paper_watchlist.init_paper_watchlist(watchlist_path)
    original_read_json = paper_watchlist.read_json

    def fake_read_json(path: str | Path, default: Any = None) -> Any:
        if str(path).replace("\\", "/").endswith("results/strategy_recommendation_latest.json"):
            return {}
        return original_read_json(path, default)

    monkeypatch.setattr(paper_watchlist, "read_json", fake_read_json)
    monkeypatch.setattr(paper_watchlist, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = paper_watchlist.build_paper_watchlist_report(
        {
            "watchlist_path": str(watchlist_path),
            "add_from_strategy": True,
            "synthetic_test_data": True,
            "top": 1,
        }
    )
    persisted = original_read_json(watchlist_path, {})

    assert result["status"] == "warning"
    assert any("synthetic_test_data" in str(item) for item in result.get("warnings") or [])
    assert persisted["items"][0]["synthetic_test_data"] is True
    assert persisted["items"][0]["evidence_kind"] == "synthetic_test_demo"


def test_paper_watchlist_rejects_upstream_synthetic_without_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.research.strategy_recommender import build_strategy_recommendation_report

    watchlist_path = tmp_path / "paper_watchlist.json"
    paper_watchlist.init_paper_watchlist(watchlist_path)
    original_read_json = paper_watchlist.read_json
    add_calls: list[list[dict[str, Any]]] = []
    synthetic_strategy = build_strategy_recommendation_report(
        {"synthetic_test_data": True, "top": 3}
    )

    def fake_read_json(path: str | Path, default: Any = None) -> Any:
        if str(path).replace("\\", "/").endswith("results/strategy_recommendation_latest.json"):
            return synthetic_strategy
        return original_read_json(path, default)

    def record_add(items: list[dict[str, Any]], path: str | Path) -> dict[str, Any]:
        add_calls.append(items)
        return original_read_json(path, {})

    monkeypatch.setattr(paper_watchlist, "read_json", fake_read_json)
    monkeypatch.setattr(paper_watchlist, "add_watchlist_items", record_add)
    monkeypatch.setattr(paper_watchlist, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = paper_watchlist.build_paper_watchlist_report(
        {"watchlist_path": str(watchlist_path), "add_from_strategy": True, "top": 10}
    )

    assert add_calls == []
    assert result["status"] == "warning"
    assert any("synthetic" in str(item).lower() for item in result.get("warnings") or [])


def test_smart_stop_missing_daily_does_not_invent_ohlcv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smart_stop, "PROJECT_ROOT", tmp_path)

    rows, warnings = smart_stop.load_daily_rows_for_position(
        {"symbol": "600000", "latest_price": 10.0},
        {"daily_dir": str(tmp_path / "missing_daily")},
    )

    assert rows == []
    assert any("daily_rows_missing" in str(item) for item in warnings)
    assert not any("using_reference" in str(item) for item in warnings)


def test_return_explain_missing_evidence_does_not_invent_contributors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(return_explain, "finalize_outputs", lambda payload, *_args, **_kwargs: payload)

    result = return_explain.build_return_explain_report({"synthetic_test_data": False})

    assert result["status"] in {"warning", "failed", "missing", "degraded"}
    assert result["top_contributors"] == []
    assert result["sector_contributors"] == []
    assert not any("synthetic" in str(item).lower() for item in result.get("items") or [])
