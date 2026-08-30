"""Round 13 收尾：止损棘轮（`_stop_loss_ratchet`）契约。

问题背景（磁盘实测，2026-08-06）：止损带每次例行按**当日**滚动
`support = max(ma20, low_20 * 1.02)` 重算。价格一路下跌时 support 跟着下移，
带子随之下移；而 2026-05-14 至 08-05 的 59 个交易日里例行只跑了 18 天。
漏跑日触发的退出信号，等下次运行时带子已经移开、不再匹配 —— 信号永久丢失。

地板（[[test_round13_stop_loss_floor]]）解决"跌得太深仍不认亏"；棘轮解决
另一半：**带子不该是"例行跑了几天"的函数**。棘轮取入场以来每一天各自算出的
止损价的最大值，所以它只是价格历史的函数，重算恒等、漏跑不丢。

红线：棘轮只决定带子在哪里，不决定是否成交。是否成交仍由分钟证据判定；
本模块绝不据日线补成交、不补放历史。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantdesk.paper.conditional_trade_plan import (
    MAX_LOSS_FLOOR_PCT,
    _price_context,
    _stop_loss_ratchet,
)


def _frame(closes: list[float], *, start: str = "2026-01-05") -> pd.DataFrame:
    """Build a daily frame with business-day dates and low = close * 0.99."""

    dates = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
        }
    )


def _declining(n: int = 60, *, top: float = 10.0, bottom: float = 5.0) -> list[float]:
    step = (top - bottom) / (n - 1)
    return [top - step * i for i in range(n)]


def test_ratchet_keeps_the_highest_stop_ever_declared() -> None:
    """单调下跌：棘轮取历史最高止损，远高于当日重算值。"""

    frame = _frame(_declining())
    entry = 10.0

    ratchet = _stop_loss_ratchet(daily_df=frame, entry_price=entry, entry_date=None)

    ma20 = frame["close"].rolling(20, min_periods=20).mean()
    low20 = frame["low"].rolling(20, min_periods=20).min()
    per_day = (pd.concat([ma20, low20 * 1.02], axis=1).max(axis=1) * 0.98).clip(
        upper=entry * 0.93
    )

    assert ratchet["price"] == pytest.approx(float(per_day.max()))
    assert ratchet["price"] > float(per_day.iloc[-1]), "棘轮必须高于当日重算的止损"


def test_ratchet_never_exceeds_the_fixed_seven_percent_stop() -> None:
    """棘轮只托底，不把止损抬到 entry * 0.93 之上。"""

    # 单调上涨：support 一路走高，若无上限棘轮会被抬到入场价之上。
    frame = _frame([5.0 + 0.2 * i for i in range(60)])
    entry = 5.0

    ratchet = _stop_loss_ratchet(daily_df=frame, entry_price=entry, entry_date=None)

    assert ratchet["price"] == pytest.approx(entry * 0.93)


def test_ratchet_window_starts_at_entry_date() -> None:
    """入场前的高位 support 不能算进棘轮 —— 那时还没有这笔持仓。"""

    # 前 30 天在高位，后 30 天在低位；entry * 0.93 = 9.3 落在两段之间，
    # 使上限不遮蔽"窗口是否限定"这个差异。
    frame = _frame([20.0] * 30 + [6.0] * 30)
    entry = 10.0
    late_entry = str(frame["date"].iloc[45].date())

    full = _stop_loss_ratchet(daily_df=frame, entry_price=entry, entry_date=None)
    scoped = _stop_loss_ratchet(daily_df=frame, entry_price=entry, entry_date=late_entry)

    assert scoped["price"] < full["price"], "限定窗口必须排除入场前的高位带子"
    assert scoped["days"] == 15
    assert scoped["as_of"] >= late_entry


def test_ratchet_is_a_function_of_price_history_not_run_history() -> None:
    """核心契约：删掉中间任意一天的"运行"不改变棘轮 —— 它只读价格。

    棘轮由日线重算，与例行是否在某天跑过无关。这正是漏跑不再丢信号的原因。
    """

    frame = _frame(_declining())
    entry = 10.0

    first = _stop_loss_ratchet(daily_df=frame, entry_price=entry, entry_date=None)
    again = _stop_loss_ratchet(daily_df=frame.copy(), entry_price=entry, entry_date=None)

    assert first == again


def _decline_then_bounce() -> list[float]:
    """长期下跌后小幅反弹 —— 601975 让旧带子失效的真实形态。

    反弹把 close 抬到 ma20 之上，于是当日重算的 `support * 0.98` 落到收盘价
    **之下**，旧公式不再触发；而下跌途中曾宣布过的止损远高于此。
    """

    decline = [10.0 - (5.0 / 49) * i for i in range(50)]
    bounce = [5.0 + 0.06 * (i + 1) for i in range(10)]
    return decline + bounce


def test_skipped_day_no_longer_loses_the_exit_signal() -> None:
    """漏跑期间价格反弹：旧公式带子已重锚到收盘价之下，棘轮仍在价格之上。"""

    closes = _decline_then_bounce()
    entry = 10.0
    frame = _frame(closes)
    latest_close = closes[-1]

    context = _price_context(
        symbol="600000",
        reference_close=latest_close,
        entry_price=entry,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    old_stop = min(entry * 0.93, context["support_price"] * 0.98)

    assert latest_close > old_stop, "旧公式：带子已随 support 重锚到收盘价之下，不触发"
    assert latest_close <= context["stop_loss_price"], "棘轮：仍在带内，必须触发"
    assert "[ratchet_binding]" in context["stop_loss_source"]


def test_ratchet_binding_is_reported_in_source_and_warnings() -> None:
    frame = _frame(_declining())
    context = _price_context(
        symbol="600000",
        reference_close=5.0,
        entry_price=10.0,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    assert "ratchet_since_entry" in context["stop_loss_source"]
    assert "[ratchet_binding]" in context["stop_loss_source"]
    assert any("stop_loss_ratchet_binding" in w for w in context["warnings"])
    assert context["stop_loss_ratchet_price"] is not None
    assert context["stop_loss_ratchet_days"] > 0
    assert context["stop_loss_ratchet_as_of"]


def test_unevaluated_breach_is_surfaced_as_observation_only() -> None:
    """入场以来日线最低已破带 → 必须报出来，且明说不据日线补成交。"""

    frame = _frame(_declining())
    context = _price_context(
        symbol="600000",
        reference_close=5.0,
        entry_price=10.0,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    breach = [w for w in context["warnings"] if "stop_loss_breached_while_unevaluated" in w]
    assert breach, "破带事实必须可见"
    assert "不据日线补成交" in breach[0]
    assert "不补放历史" in breach[0]
    assert context["interval_low_since_entry"] == pytest.approx(
        round(float(frame["low"].tail(len(frame) - 19).min()), 4)
    )


def test_healthy_position_is_untouched_by_the_ratchet() -> None:
    """横盘/上涨持仓：棘轮与当日技术止损同值，不得挂上无信息量的绑定标记。"""

    frame = _frame([10.0 + 0.01 * i for i in range(60)])
    latest = float(frame["close"].iloc[-1])
    context = _price_context(
        symbol="600000",
        reference_close=latest,
        entry_price=10.0,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    assert "[ratchet_binding]" not in context["stop_loss_source"]
    assert not any("stop_loss_ratchet_binding" in w for w in context["warnings"])
    assert latest > context["stop_loss_price"], "健康持仓不应被止损带命中"


def test_floating_point_noise_does_not_declare_a_binding() -> None:
    """棘轮末日与 raw_stop 算同一个量，求和顺序差异不得触发绑定标记。

    rolling(20).mean() 与 tail(20).mean() 的浮点结果可差 1e-12；若不设容差，
    每个正常持仓每天都会挂一条假的 ratchet_binding 警告。
    """

    frame = _frame([8.0] * 60)
    context = _price_context(
        symbol="600000",
        reference_close=8.0,
        entry_price=8.0,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    assert "[ratchet_binding]" not in context["stop_loss_source"]


def test_short_history_yields_no_ratchet_rather_than_a_guess() -> None:
    """不足 20 根日线算不出 support，棘轮必须留空而不是猜一个值。"""

    frame = _frame([10.0] * 12)

    ratchet = _stop_loss_ratchet(daily_df=frame, entry_price=10.0, entry_date=None)

    assert ratchet["price"] is None
    assert ratchet["days"] == 0
    assert ratchet["interval_low"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_df": pd.DataFrame(), "entry_price": 10.0, "entry_date": None},
        {"daily_df": pd.DataFrame({"close": [1.0]}), "entry_price": 10.0, "entry_date": None},
    ],
)
def test_missing_inputs_fail_open_to_no_ratchet(kwargs: dict) -> None:
    assert _stop_loss_ratchet(**kwargs)["price"] is None


@pytest.mark.parametrize("entry_price", [0.0, -1.0, None])
def test_invalid_entry_price_yields_no_ratchet(entry_price: float | None) -> None:
    frame = _frame(_declining())

    assert _stop_loss_ratchet(daily_df=frame, entry_price=entry_price, entry_date=None)["price"] is None


def test_ratchet_and_floor_compose_without_either_being_bypassed() -> None:
    """两个机制同时在场时，止损价是二者的上界，且隐含亏损不超过地板。"""

    entry = 10.0
    frame = _frame(_declining(top=entry, bottom=2.0))
    context = _price_context(
        symbol="600000",
        reference_close=2.0,
        entry_price=entry,
        daily_df=frame,
        feature={},
        entry_date=None,
    )

    floor = entry * (1.0 - MAX_LOSS_FLOOR_PCT)
    ratchet = context["stop_loss_ratchet_price"]

    assert context["stop_loss_price"] >= floor
    assert context["stop_loss_price"] >= ratchet
    assert 1.0 - context["stop_loss_price"] / entry <= MAX_LOSS_FLOOR_PCT + 1e-9


def test_entry_date_after_all_bars_yields_no_ratchet_not_an_exception() -> None:
    frame = _frame(_declining())

    ratchet = _stop_loss_ratchet(daily_df=frame, entry_price=10.0, entry_date="2030-01-01")

    assert ratchet["price"] is None
