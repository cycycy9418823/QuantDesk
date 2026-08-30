from quantdesk.strategy_lab.low_price_strategy import generate_low_price


def test_low_price_strategy_generates_research_candidates():
    metrics = [
        {"symbol": "000001", "signal_date": "2026-06-02", "close": 5.0, "avg_volume_20d": 1000, "amount_proxy": 5000, "return_5d": 0.04, "return_20d": 0.08, "annualized_return": 0.1, "sharpe": 1, "max_drawdown": -0.1, "win_rate": 0.5, "turnover": 1, "volatility": 0.2, "limit_up_proxy": False},
    ]
    summary, rows = generate_low_price(metrics)

    assert summary["research_only"] is True
    assert rows
    assert rows[0]["research_signal_label"] == "research_candidate_watch"
    assert "target_price" not in rows[0]
    assert "recommended_position" not in rows[0]

