"""Round 13 收尾：止损硬下限（`MAX_LOSS_FLOOR_PCT`）契约。

问题背景（磁盘实测，2026-08-06）：止损带原为
`min(entry_price * 0.93, support_price * 0.98)`，而 `support_price` 是滚动值
`max(ma20, low_20 * 1.02)`。价格持续下跌时 support 跟着下移，止损带也随之下移，
形成一条无底的带子——收盘价永远落在当日重算后的带子上方。

逐日复算证据（`601975` 自 2026-05-14 入场 @5.41）：止损条件在 32 个交易日"看似成立"，
但每次都是用当日重算的更低带子判定，实际浮亏累计到 -29.8% 仍从未真正清仓。
同期 `002115` -18.7%、`600744` -17.1%，三个仓位挂了 83 天。

一个永远不会认亏的止损，实质上等于没有止损。本文件把"必须认亏的地板"钉成契约。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantdesk.paper.conditional_trade_plan import (
    MAX_LOSS_FLOOR_PCT,
    _price_context,
)


def _context(*, entry: float, reference_close: float, ma20: float, low20: float) -> dict:
    """Build a price context directly, so the floor is tested without I/O."""

    return _price_context(
        symbol="600000",
        reference_close=reference_close,
        entry_price=entry,
        daily_df=pd.DataFrame(),
        feature={"ma20": ma20, "low_20": low20, "high_20": reference_close * 1.05},
    )


def test_floor_is_a_bounded_share_of_entry_price() -> None:
    """地板必须是有界的、可解释的比例，不是任意数字。"""

    assert 0.0 < MAX_LOSS_FLOOR_PCT < 1.0
    # 20% 是"单笔纸面持仓最大可承受亏损"的显式选择；改动它必须同步改本断言，
    # 使任何放宽都无法悄悄发生。
    assert MAX_LOSS_FLOOR_PCT == pytest.approx(0.20)


def test_deep_drawdown_stop_never_falls_below_the_floor() -> None:
    """滚动 support 跌到很低时，止损价被地板托住。"""

    entry = 5.41
    # 复刻 601975 在 2026-08-05 的形态：support 已随价格跌到远低于地板。
    context = _context(entry=entry, reference_close=3.80, ma20=3.5395, low20=3.25)

    raw_stop = min(entry * 0.93, context["support_price"] * 0.98)
    floor = entry * (1.0 - MAX_LOSS_FLOOR_PCT)

    assert raw_stop < floor, "夹具必须构造出 support 已压到地板以下的情形"
    assert context["stop_loss_price"] == pytest.approx(round(floor, 4))
    assert "[floor_binding]" in context["stop_loss_source"]
    assert any("stop_loss_floor_binding" in w for w in context["warnings"])


def test_deep_drawdown_now_triggers_where_it_previously_never_did() -> None:
    """601975 的实际收盘价在新止损下触发，在旧公式下不触发。"""

    entry = 5.41
    latest_close = 3.80
    context = _context(entry=entry, reference_close=latest_close, ma20=3.5395, low20=3.25)

    old_stop = min(entry * 0.93, context["support_price"] * 0.98)

    assert latest_close > old_stop, "旧公式：收盘价在带子上方，不触发（历史事实）"
    assert latest_close <= context["stop_loss_price"], "新公式：必须触发"


def test_normal_market_keeps_the_earlier_technical_stop() -> None:
    """正常行情下地板不介入，技术止损仍然更早触发。"""

    entry = 10.0
    # support 仍高，min() 给出的技术止损高于地板 8.0。
    context = _context(entry=entry, reference_close=10.5, ma20=10.2, low20=9.9)

    floor = entry * (1.0 - MAX_LOSS_FLOOR_PCT)
    raw_stop = min(entry * 0.93, context["support_price"] * 0.98)

    assert raw_stop > floor, "夹具必须构造出技术止损高于地板的情形"
    assert context["stop_loss_price"] == pytest.approx(round(raw_stop, 4))
    assert "[floor_binding]" not in context["stop_loss_source"]
    assert not any("stop_loss_floor_binding" in w for w in context["warnings"])


@pytest.mark.parametrize("entry", [1.0, 5.41, 14.51, 100.0, 1328.36])
def test_stop_never_implies_loss_worse_than_the_floor(entry: float) -> None:
    """对任意入场价：止损价隐含的最大亏损都不超过地板。"""

    # 构造极端下跌：support 远低于地板。
    context = _context(
        entry=entry,
        reference_close=entry * 0.5,
        ma20=entry * 0.45,
        low20=entry * 0.40,
    )

    implied_loss = 1.0 - (context["stop_loss_price"] / entry)

    assert implied_loss <= MAX_LOSS_FLOOR_PCT + 1e-9, (
        f"entry={entry} 的止损隐含亏损 {implied_loss:.4f} 超过地板 {MAX_LOSS_FLOOR_PCT}"
    )


def test_missing_entry_price_still_produces_a_stop_without_floor_claim() -> None:
    """无 entry_price 时不能凭空声称地板生效（地板以入场价为基准）。"""

    context = _price_context(
        symbol="600000",
        reference_close=10.0,
        entry_price=None,
        daily_df=pd.DataFrame(),
        feature={"ma20": 9.8, "low_20": 9.5},
    )

    assert context["stop_loss_price"] == pytest.approx(round(10.0 * 0.93, 4))
    assert context["stop_loss_source"] == "reference_close * 0.93"
    assert "[floor_binding]" not in context["stop_loss_source"]
    assert context["take_profit_half_price"] is None


def test_stop_source_string_states_the_full_formula() -> None:
    """来源串必须写清完整公式，便于审计而非只写一句"止损"。"""

    context = _context(entry=10.0, reference_close=10.5, ma20=10.2, low20=9.9)

    source = context["stop_loss_source"]
    assert "min(entry_price * 0.93, support_price * 0.98)" in source
    assert "max(" in source
    assert "0.80" in source, "地板比例必须出现在来源串里"
