from quantdesk.strategy_lab.limit_up_strategy import generate_limit_up


def test_limit_up_research_has_no_trade_instruction_fields():
    metrics = [
        {"symbol": "300001", "signal_date": "2026-06-02", "close": 12, "avg_volume_20d": 1000, "amount_proxy": 12000, "return_5d": 0.12, "return_20d": 0.2, "annualized_return": 0.3, "sharpe": 1, "max_drawdown": -0.2, "win_rate": 0.52, "turnover": 1, "volatility": 0.5, "limit_up_proxy": True},
    ]
    summary, rows, dist = generate_limit_up(metrics)

    assert summary["research_only"] is True
    assert rows and dist
    assert rows[0]["research_signal_label"] == "limit_up_pattern_research_candidate"
    assert "target_price" not in rows[0]
    assert "recommended_position" not in rows[0]

