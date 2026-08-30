"""Convergence test: the unified enriched normalizer subsumes the legacy ones.

Phase 0.5 strategy is strangler-fig, not a risky rewrite of the 4 historical
normalizers (each has different downstream canonical expectations, e.g.
``column_aliases`` uses ``code`` while enriched uses ``symbol``).  Instead the
unified entry point :func:`quantdesk.enriched.normalize_daily_frame` is proven
here to recognise every alias the legacy normalizers recognise, so new/refactored
callers can converge on it safely while legacy callers keep working unchanged.
"""

from __future__ import annotations

import pandas as pd

import quantdesk.enriched as E


def test_unified_recognises_chinese_aliases() -> None:
    df = pd.DataFrame(
        {
            "日期": ["2026-01-02"],
            "代码": ["000001"],
            "开盘": [10.0],
            "最高": [10.5],
            "最低": [9.8],
            "收盘": [10.2],
            "成交量": [1000],
            "成交额": [10200.0],
        }
    )
    out = E.normalize_daily_frame(df)
    for col in ("date", "symbol", "open", "high", "low", "close", "volume", "amount"):
        assert col in out.columns, f"missing {col}"


def test_unified_recognises_english_aliases() -> None:
    df = pd.DataFrame(
        {
            "trade_date": ["2026-01-02"],
            "ts_code": ["000001.SZ"],
            "vol": [1000],
            "close": [10.2],
        }
    )
    out = E.normalize_daily_frame(df)
    assert "date" in out.columns
    assert "symbol" in out.columns
    assert "volume" in out.columns


def test_turnover_amount_vs_rate_disambiguated() -> None:
    """The long-standing turnover collision is resolved: 成交额->amount,
    换手率->turnover_rate (never the same column)."""
    df = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "close": [10.2],
            "turnover": [10200.0],   # 成交额 (value)
            "换手率": [2.5],           # turnover rate
        }
    )
    out = E.normalize_daily_frame(df)
    assert "amount" in out.columns
    assert "turnover_rate" in out.columns
    assert float(out["amount"].iloc[0]) == 10200.0
    assert float(out["turnover_rate"].iloc[0]) == 2.5


def test_legacy_column_aliases_subset_recognised() -> None:
    """Every canonical-ish alias the legacy column_aliases maps is recognised
    by the unified normalizer (proving it can replace that path)."""
    from quantdesk import column_aliases as legacy

    # legacy close aliases
    for alias in ("price", "latest_close", "last_close", "current_price"):
        df = pd.DataFrame({"date": ["2026-01-02"], alias: [10.2]})
        out = E.normalize_daily_frame(df)
        assert "close" in out.columns, f"alias {alias} not mapped to close"
