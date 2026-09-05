import json
from pathlib import Path

from quantdesk.strategy_lab.research_signal_safety import assert_research_only_text, boundary_payload


ROOT = Path(__file__).resolve().parents[1]


def test_research_signal_safety_rejects_forbidden_action_text():
    assert assert_research_only_text("research candidate watch only")
    assert not assert_research_only_text("目标价 10")
    assert not assert_research_only_text("recommended_position 20%")
    assert boundary_payload()["no_live_trading"] is True


def test_forbidden_signal_columns_are_declared_and_enforced():
    """No daily-signal output may carry a target price or a position instruction.

    Re-anchored from results/daily_research/v420_daily_stock_candidates_latest.csv,
    which is pipeline output: gitignored, absent in a fresh checkout, and not a
    fixture. Running the workflow to produce it would make this safety test depend
    on generated state. Asserting the safety contract instead catches the defect at
    the source - before any CSV is written - and cannot pass merely because today's
    artifact happens to be clean.
    """
    from quantdesk.strategy_lab.research_signal_safety import PROHIBITED_TEXT

    for term in ("target_price", "recommended_position", "real_money_weight"):
        assert term in PROHIBITED_TEXT, f"{term} is no longer a forbidden signal column"
        assert assert_research_only_text(f"symbol,close,{term}") is False, (
            f"a header containing {term} was accepted as research-only"
        )
    assert assert_research_only_text("symbol,close,score") is True, (
        "an ordinary research header was rejected; the guard is too broad to be useful"
    )


def test_v420_boundary_payload_declares_research_only_and_no_trade_advice():
    """The handoff boundary must declare research-only and refuse trade advice.

    Re-anchored from results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json
    onto boundary_payload(), the function that produces those flags. Same reasoning as
    above: the artifact is generated output, the function is the contract.
    """
    from quantdesk.strategy_lab.research_signal_safety import PROHIBITED_USAGE

    payload = boundary_payload()
    assert payload["research_only"] is True
    assert payload["not_trade_instruction"] is True
    assert payload["no_broker"] is True
    assert payload["no_live_trading"] is True
    assert payload["no_auto_execution"] is True
    assert payload["no_buy_sell_advice"] is True
    assert payload["no_real_money_allocation"] is True
    for usage in ("live_trading", "broker_connection", "auto_execution",
                  "buy_sell_recommendation", "real_money_allocation_instruction"):
        assert usage in PROHIBITED_USAGE
        assert usage in payload["prohibited_usage"]

