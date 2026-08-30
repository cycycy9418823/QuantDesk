from quantdesk.strategy_lab.long_term_strategy import generate_long_term


def test_long_term_strategy_generates_research_candidates():
    metrics = [
        {"symbol": "000002", "signal_date": "2026-06-02", "close": 10, "avg_volume_20d": 1000, "amount_proxy": 10000, "return_5d": 0.01, "return_20d": 0.02, "return_120d": 0.25, "annualized_return": 0.18, "sharpe": 1.2, "max_drawdown": -0.15, "win_rate": 0.55, "turnover": 1, "volatility": 0.22, "limit_up_proxy": False},
    ]
    summary, rows = generate_long_term(metrics)

    assert summary["research_only"] is True
    assert rows
    assert rows[0]["strategy_id"] in {"long_term_low_vol", "long_term_trend"}
    assert "real_money_weight" not in rows[0]

