"""Regression coverage for recorded equity dates and common-period comparison."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
import pytest


def observation(day: str, equity: float, **extra: object) -> dict:
    return {"valuation_date": day, "generated_at": f"{day}T20:00:00", "cash": equity,
            "market_value": 0, "total_equity": equity, "total_return_pct": equity - 100,
            "portfolio_review_valid": True, **extra}


def accessor(tmp_path: Path, monkeypatch, rows: object):
    from quantdesk.paper import auto_paper_portfolio as module

    path = tmp_path / "history.json"
    path.write_text(json.dumps({"items": rows}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", path)
    return module, path


def test_equity_history_uses_valuation_date_and_never_overwrites_prior_day(tmp_path, monkeypatch):
    undated = observation("2026-10-21", 130)
    undated.pop("valuation_date")
    module, path = accessor(tmp_path, monkeypatch, [undated,
        observation("2026-10-22", 110, generated_at="2026-10-23T02:49:11+08:00", portfolio_review_valid=False),
        observation("2026-10-23", 120)])
    before = path.read_bytes()
    result = module.auto_paper_portfolio_equity_history()
    assert [(r["date"], r["total_equity"]) for r in result["items"]] == [("2026-10-22", 110), ("2026-10-23", 120)]
    assert result["items"][0]["portfolio_review_valid"] is False
    assert result["history_evidence"]["undated_count"] == 1
    assert result["status"] == "degraded"
    assert path.read_bytes() == before


def test_equity_comparison_rebases_both_series_at_the_same_first_date():
    from quantdesk.decision_board import _aligned_return_series

    source = {"items": [observation("2026-10-22", 110), observation("2026-10-23", 120)]}
    benchmark = {"items": [{"date": "2026-10-22", "close": 100}, {"date": "2026-10-23", "close": 110}]}
    # These dates are explicitly retained by the public history projection.
    for row in source["items"]:
        row["date"] = row["valuation_date"]
    before = deepcopy((source, benchmark))
    result = _aligned_return_series(source, benchmark)
    assert result["items"][0]["paper_return_pct"] == 0
    assert result["items"][-1]["paper_return_pct"] == pytest.approx(9.090909)
    assert result["items"][-1]["benchmark_return_pct"] == 10
    assert (source, benchmark) == before


def test_product_history_counts_only_explicit_valid_valuation_dates():
    from quantdesk.product_integrity import history_coverage

    undated = observation("2026-10-21", 130)
    undated.pop("valuation_date")
    history = {"items": [undated, observation("2026-10-22", 110), observation("2026-10-23", 120)]}
    calendar = {"is_open_by_date": {d: True for d in ["2026-10-21", "2026-10-22", "2026-10-23"]}}
    result = history_coverage(history, calendar=calendar, target_date="2026-10-23")
    assert result["unique_dates"] == 2
    assert result["first_date"] == "2026-10-22"
    assert result["undated_rows"] == 1
    assert result["status"] == "partial"
    assert result["continuous_statistics_eligible"] is False


@pytest.mark.parametrize("changes", [{"cash": None}, {"market_value": -1}, {"cash": True},
                                     {"total_equity": float("nan")}, {"cash": 1}, {"total_equity": 0}])
def test_equity_accessor_never_promotes_invalid_amounts(tmp_path, monkeypatch, changes):
    module, _ = accessor(tmp_path, monkeypatch, [observation("2026-10-22", 110, **changes), observation("2026-10-23", 120)])
    result = module.auto_paper_portfolio_equity_history()
    assert [r["date"] for r in result["items"]] == ["2026-10-23"]
    assert result["history_evidence"]["invalid_count"] == 1
    assert result["status"] == "degraded"


def test_conflicting_same_date_observations_cannot_be_hidden_by_last_wins(tmp_path, monkeypatch):
    from quantdesk.decision_board import _aligned_return_series

    module, _ = accessor(tmp_path, monkeypatch, [observation("2026-10-21", 100), observation("2026-10-22", 110),
        observation("2026-10-22", 111), observation("2026-10-23", 120)])
    result = module.auto_paper_portfolio_equity_history()
    assert [r["date"] for r in result["items"]] == ["2026-10-21", "2026-10-23"]
    assert result["history_evidence"]["conflicting_dates"] == ["2026-10-22"]
    compared = _aligned_return_series(result, {"items": [{"date": "2026-10-21", "close": 100}, {"date": "2026-10-23", "close": 110}]})
    assert compared["items"] == []
    assert compared["status"] == "degraded"


def test_missing_cumulative_return_does_not_discard_valid_equity(tmp_path, monkeypatch):
    first, second = observation("2026-10-22", 110), observation("2026-10-23", 120)
    first.pop("total_return_pct")
    module, _ = accessor(tmp_path, monkeypatch, [first, second])
    result = module.auto_paper_portfolio_equity_history()
    assert len(result["items"]) == 2
    assert result["items"][0]["total_return_pct"] is None


@pytest.mark.parametrize("rows", ["damaged", None, {}])
def test_malformed_history_collection_is_visible(tmp_path, monkeypatch, rows):
    module, path = accessor(tmp_path, monkeypatch, rows)
    before = path.read_bytes()
    result = module.auto_paper_portfolio_equity_history()
    assert result["items"] == []
    assert result["status"] == "degraded"
    assert result["warnings"]
    assert path.read_bytes() == before


def test_history_get_route_preserves_valuation_dates_and_input_bytes(tmp_path, monkeypatch):
    from dataclasses import replace
    from quantdesk.paper.queries.portfolio import PaperQueryPaths, auto_paper_portfolio_equity_history
    from scripts.server.routes.paper_simulation import create_paper_simulation_blueprint

    _, path = accessor(tmp_path, monkeypatch, [observation("2026-10-22", 110, generated_at="2026-10-23T01:00:00"),
                                             observation("2026-10-23", 120)])
    before = path.read_bytes()
    app = Flask(__name__)
    paths = replace(PaperQueryPaths.from_root(tmp_path), history=path)
    app.register_blueprint(create_paper_simulation_blueprint(SimpleNamespace(
        load_equity_history=lambda **kwargs: auto_paper_portfolio_equity_history(paths, **kwargs))))
    client = app.test_client()
    response = client.get("/api/paper-simulation/equity-history")
    assert response.status_code == 200
    assert [r["date"] for r in response.get_json()["items"]] == ["2026-10-22", "2026-10-23"]
    assert client.post("/api/paper-simulation/equity-history").status_code == 405
    assert path.read_bytes() == before


def test_comparison_shows_missing_observations_as_gaps():
    from quantdesk.decision_board import _aligned_return_series

    source = {"items": [dict(observation(d, e), date=d) for d, e in [("2026-10-21", 110), ("2026-10-23", 120)]]}
    benchmark = {"items": [{"date": d, "close": v} for d, v in [("2026-10-21", 100), ("2026-10-22", 105), ("2026-10-23", 110)]]}
    result = _aligned_return_series(source, benchmark)
    assert [r["date"] for r in result["items"]] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert result["items"][1]["paper_return_pct"] is None
    assert result["missing_valuation_dates"] == ["2026-10-22"]
    assert result["status"] == "degraded"


@pytest.mark.parametrize("benchmark", [
    [{"date": "2026-10-22", "close": 100}, {"date": "2026-10-23", "close": 110}],
    [{"date": "2026-10-21", "close": 100}, {"date": "2026-10-22", "close": 110}],
    [{"date": "2026-10-21", "close": 100}, {"date": "2026-10-23", "close": 110}, {"date": "2026-10-23", "close": 111}],
])
def test_comparison_does_not_shorten_or_choose_conflicting_endpoints(benchmark):
    from quantdesk.decision_board import _aligned_return_series

    source = {"items": [dict(observation(d, e), date=d) for d, e in [("2026-10-21", 110), ("2026-10-22", 115), ("2026-10-23", 120)]]}
    result = _aligned_return_series(source, {"items": benchmark})
    assert result["items"] == []
    assert result["status"] == "degraded"


def test_generated_at_alone_never_supplies_comparison_date():
    from quantdesk.decision_board import _aligned_return_series

    result = _aligned_return_series({"items": [{"generated_at": "2026-10-22", "total_equity": 110, "total_return_pct": 10},
                                               {"generated_at": "2026-10-23", "total_equity": 120, "total_return_pct": 20}]},
        {"items": [{"date": "2026-10-22", "close": 100}, {"date": "2026-10-23", "close": 110}]})
    assert result["items"] == []


def test_complete_dated_balanced_observations_still_allow_coverage():
    from quantdesk.product_integrity import history_coverage

    days = ["2026-10-22", "2026-10-23"]
    result = history_coverage({"items": [observation(day, 100) for day in days]},
        calendar={"is_open_by_date": {d: True for d in days}}, target_date=days[-1])
    assert result["status"] == "complete"
    assert result["continuous_statistics_eligible"] is True
