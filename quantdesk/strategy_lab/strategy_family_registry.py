from __future__ import annotations

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload


def strategy_family_registry() -> dict:
    families = [
        ("combo50_candidate", ["combo_50_50", "low_vol_leg", "rev120_leg"], "monthly", "existing combo50 research universe", ["ST", "delist", "suspension_proxy"]),
        ("low_price_family", ["low_price_reversal", "low_price_volume_breakout", "low_price_trend_repair"], "daily_research", "low price liquid A-share subset", ["ST", "delist", "suspension_proxy", "liquidity"]),
        ("long_term_family", ["long_term_low_vol", "long_term_trend", "long_term_quality_value_proxy"], "monthly", "liquid non-ST A-share subset", ["ST", "delist", "liquidity"]),
        ("limit_up_family", ["first_limit_up_research", "consecutive_limit_up_research", "limit_up_open_reseal_research", "next_day_limit_up_risk"], "daily_research", "daily bars with limit-up proxy", ["T+1", "limit_untradable", "slippage", "liquidity"]),
        ("sector_rotation_family", ["sector_strength_score", "sector_candidate_pool", "sector_stock_match"], "daily_research", "stock_theme_map industry/theme mapping with visible proxy fallback", ["sector_concentration", "mapping_gap", "data_proxy"]),
        ("risk_control_family", ["st_filter", "delist_filter", "suspension_proxy", "liquidity_filter", "abnormal_adjusted_price_filter", "limit_untradable_filter"], "always_on", "all research candidates", ["metadata_gap", "proxy_warning"]),
    ]
    rows = []
    for fid, strategies, rebalance, universe, risks in families:
        rows.append(
            {
                "family_id": fid,
                "strategy_ids": strategies,
                "required_fields": ["date", "symbol", "open", "high", "low", "close", "volume", "amount"],
                "required_metadata": risks,
                "rebalance_frequency": rebalance,
                "universe_rule": universe,
                "risk_filters": risks,
                "backtest_ready": False,
                "evidence_kind": "tail_window_proxy",
                "formal_performance_evidence": False,
                "daily_signal_ready": True,
                "limitation_note": "research proxy implementation; formal metadata blockers may remain",
                "research_only_note": RESEARCH_ONLY_NOTE,
            }
        )
    return {"task_id": "v420_strategy_family_registry", "status": "completed", "families": rows, **boundary_payload()}
