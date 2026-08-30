from quantdesk.strategy_lab.strategy_family_registry import strategy_family_registry


def test_v420_strategy_families_registered():
    payload = strategy_family_registry()
    family_ids = {row["family_id"] for row in payload["families"]}

    assert "combo50_candidate" in family_ids
    assert "low_price_family" in family_ids
    assert "long_term_family" in family_ids
    assert "limit_up_family" in family_ids
    assert "sector_rotation_family" in family_ids
    assert "risk_control_family" in family_ids
    assert payload["research_only"] is True
    assert all(row["daily_signal_ready"] for row in payload["families"])

