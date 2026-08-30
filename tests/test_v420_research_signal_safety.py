import json
from pathlib import Path

from quantdesk.strategy_lab.research_signal_safety import assert_research_only_text, boundary_payload


ROOT = Path(__file__).resolve().parents[1]


def test_research_signal_safety_rejects_forbidden_action_text():
    assert assert_research_only_text("research candidate watch only")
    assert not assert_research_only_text("目标价 10")
    assert not assert_research_only_text("recommended_position 20%")
    assert boundary_payload()["no_live_trading"] is True


def test_daily_signal_outputs_have_no_forbidden_columns():
    csv_path = ROOT / "results/daily_research/v420_daily_stock_candidates_latest.csv"
    header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0].lower()
    forbidden = ["target_price", "recommended_position", "real_money_weight"]

    for term in forbidden:
        assert term not in header


def test_v420_handoff_research_only_and_no_trade_advice():
    payload = json.loads((ROOT / "results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json").read_text(encoding="utf-8-sig"))

    assert payload["research_only"] is True
    assert payload["generated_trade_advice"] is False
    assert payload["candidate_upgraded"] is False

