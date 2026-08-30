from quantdesk.strategy_lab.strategy_leaderboard import build_leaderboard


def test_strategy_leaderboard_outputs_research_rank_not_trade_action():
    payload, rows = build_leaderboard([
        {"metrics": {"strategy_id": "a", "robustness_score": 0.7, "annualized_return": 0.1, "Sharpe": 1.0}},
        {"metrics": {"strategy_id": "b", "robustness_score": 0.4, "annualized_return": 0.2, "Sharpe": 0.5}},
    ])

    assert payload["research_only"] is True
    assert rows[0]["research_rank"] == 1
    assert "strategy_health" in rows[0]
    assert "target_price" not in rows[0]


def test_strategy_leaderboard_preserves_sector_mapping_coverage():
    _payload, rows = build_leaderboard([
        {
            "metrics": {
                "strategy_id": "sector_rotation_family",
                "robustness_score": 0.55,
                "research_readiness": "research_ready_with_theme_mapping",
                "mapping_coverage": 0.92,
            }
        }
    ])

    assert rows[0]["research_readiness"] == "research_ready_with_theme_mapping"
    assert rows[0]["mapping_coverage"] == 0.92
