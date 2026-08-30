from __future__ import annotations

PROHIBITED_USAGE = [
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
]

PROHIBITED_TEXT = [
    "建议买入",
    "建议卖出",
    "目标价",
    "仓位建议",
    "必须买",
    "明天买",
    "target_price",
    "recommended_position",
    "real_money_weight",
]

RESEARCH_ONLY_NOTE = "research_only_review_not_trade_instruction"


def assert_research_only_text(text: str) -> bool:
    lowered = text.lower()
    return not any(term.lower() in lowered for term in PROHIBITED_TEXT)


def boundary_payload() -> dict:
    return {
        "research_only": True,
        "not_trade_instruction": True,
        "prohibited_usage": PROHIBITED_USAGE,
        "prohibited_usage_note": RESEARCH_ONLY_NOTE,
        "no_broker": True,
        "no_live_trading": True,
        "no_auto_execution": True,
        "no_buy_sell_advice": True,
        "no_real_money_allocation": True,
    }

