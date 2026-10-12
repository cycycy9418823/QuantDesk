"""D3/T09：晚间真实成交入口必须用**可信 ST 身份**（±5% 涨跌停），缺证据则显式未知。

全部合成夹具：无网络、无模型调用、不写正式账本。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import trader
from ai_trader.ledger import empty_ledger

SYMBOL = "600001"
DAY = "2026-09-28"


def _ledger() -> dict:
    led = empty_ledger(created_at="2026-09-25T20:20:00+08:00")
    led["as_of"] = "2026-09-25"
    led["cash"] = 100000.0
    led["equity"] = 100000.0
    return led


def _buy(quantity: int = 1000) -> list[dict]:
    return [{"symbol": SYMBOL, "side": "buy", "quantity": quantity, "ref_close": 10.0}]


def _run(*, open_px: float, prev: float, identity, day: str = DAY) -> list[dict]:
    _ledger_after, actions = trader.execute_intents(
        _ledger(),
        as_of=day,
        intents=_buy(),
        open_prices={SYMBOL: open_px},
        prev_closes={SYMBOL: prev},
        live_id=None,
        st_identity=identity,
    )
    return actions


def test_st_identity_narrows_the_limit_and_blocks_the_buy() -> None:
    """2026-07-06 前主板 ST 是 ±5%：开在涨停价 10.50 → 不得成交。

    1A 起涨跌停按前收算到分比较价格（不再用"幅度减容差"），所以用当时的涨停价本身。
    """

    actions = _run(open_px=10.50, prev=10.0, identity={SYMBOL: (True, False)}, day="2026-07-03")

    assert actions and actions[0]["status"] == "rejected", actions
    assert actions[0]["reason"].startswith("limit_up:"), actions[0]["reason"]
    assert actions[0]["is_st"] is True and actions[0]["st_unknown"] is False


def test_main_board_st_band_is_10pct_from_2026_07_06() -> None:
    """HKEX 通函 055/2026：2026-07-06 起主板风险警示股 ±10%，+4.8% 不是涨停。"""

    actions = _run(open_px=10.48, prev=10.0, identity={SYMBOL: (True, False)})
    assert actions and actions[0]["status"] == "filled", actions
    blocked = _run(open_px=11.00, prev=10.0, identity={SYMBOL: (True, False)})
    assert blocked[0]["status"] == "rejected" and blocked[0]["reason"].startswith("limit_up:")


def test_non_st_stock_is_not_blocked_by_that_gap() -> None:
    """正常对照：非 ST 同样 +4.8% 不构成涨停，照旧可以成交。"""

    actions = _run(open_px=10.48, prev=10.0, identity={SYMBOL: (False, False)})

    assert actions and actions[0]["status"] == "filled", actions
    assert actions[0]["is_st"] is False and actions[0]["st_unknown"] is False


def test_unknown_identity_keeps_10pct_rule_but_says_so() -> None:
    """缺证据：仍按既有 10% 规则（不改既定限制），但必须显式标注未知。"""

    actions = _run(open_px=10.48, prev=10.0, identity={SYMBOL: (False, True)})

    assert actions and actions[0]["status"] == "filled", actions
    assert actions[0]["st_unknown"] is True
    assert actions[0]["st_note"] == trader.ST_UNKNOWN_NOTE


def test_no_identity_argument_means_unknown_not_st() -> None:
    """不传身份时不得默认当成 ST（也不能静默当已知非 ST）。"""

    _ledger_after, actions = trader.execute_intents(
        _ledger(),
        as_of=DAY,
        intents=_buy(),
        open_prices={SYMBOL: 10.48},
        prev_closes={SYMBOL: 10.0},
        live_id=None,
    )

    assert actions[0]["status"] == "filled", actions
    assert actions[0]["is_st"] is False and actions[0]["st_unknown"] is True


def test_trusted_identity_prefers_row_flag_and_needs_a_dated_roster(tmp_path: Path) -> None:
    """身份来源：行情行 is_st 列 > 同日快照的清单名字 > 未知；今天的名单证明不了历史。"""

    assert trader.trusted_st_identity(SYMBOL, row={"is_st": True}) == (True, False)
    assert trader.trusted_st_identity(SYMBOL, row={"is_st": "0"}) == (False, False)
    assert trader.trusted_st_identity(SYMBOL, row={}) == (False, True)

    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    master = tmp_path / "data" / "metadata" / "security_master.csv"
    master.parent.mkdir(parents=True)
    master.write_text(
        "symbol,name,list_date,as_of\n"
        f"{SYMBOL},*ST样本,2020-01-01,2026-10-06\n"
        "600002,正常样本,2020-01-01,2026-10-06\n",
        encoding="utf-8",
    )

    # 清单快照是 10/6，交易日是 9/5 → 不能拿今天的名单证明当时身份
    past_bar = {"date": "2026-09-05"}
    assert trader.trusted_st_identity(SYMBOL, row=past_bar, daily_dir=daily) == (False, True)
    assert trader.trusted_st_identity("600002", row=past_bar, daily_dir=daily) == (False, True)
    # 清单快照与交易日一致时才算当时证据
    assert trader.trusted_st_identity(SYMBOL, row={"date": "2026-10-06"}, daily_dir=daily) == (True, False)
    assert trader.trusted_st_identity("600002", row={"date": "2026-10-06"}, daily_dir=daily) == (False, False)
    assert trader.trusted_st_identity("600003", row={"date": "2026-10-06"}, daily_dir=daily) == (False, True)