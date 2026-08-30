from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quantdesk import technical_indicators
from quantdesk.data_metadata_schema import validate_metadata_dir
from quantdesk.data_truth.adjustment_factor_audit import audit_adjustment_factors
from quantdesk.data_truth.price_adjustment_consistency import cross_validate_adjustment_factors


def _all_nan(series: pd.Series) -> bool:
    return bool(series.isna().all())


def test_technical_indicators_keep_insufficient_windows_unknown() -> None:
    close = pd.Series([10.0, 10.5, 11.0], dtype=float)
    volume = pd.Series([1000.0, 1200.0, 1400.0], dtype=float)
    frame = pd.DataFrame(
        {
            "high": [10.2, 10.7, 11.2],
            "low": [9.8, 10.1, 10.8],
            "close": close,
            "volume": volume,
        }
    )

    assert _all_nan(technical_indicators.calculate_ma(close, [5])["ma5"])
    assert _all_nan(technical_indicators.calculate_macd(close)["dif"])
    assert _all_nan(technical_indicators.calculate_macd(close)["dea"])
    assert _all_nan(technical_indicators.calculate_macd(close)["macd"])
    assert _all_nan(technical_indicators.calculate_kdj(frame)["k"])
    assert _all_nan(technical_indicators.calculate_kdj(frame)["d"])
    assert _all_nan(technical_indicators.calculate_kdj(frame)["j"])
    assert _all_nan(technical_indicators.calculate_rsi(close, [6])["rsi6"])
    assert _all_nan(technical_indicators.calculate_volume_ma(volume, [5])["vol_ma5"])
    assert _all_nan(technical_indicators.calculate_returns(close, [5])["return_5d"])
    assert _all_nan(technical_indicators.calculate_volatility(close, period=20))


def test_technical_indicators_do_not_backfill_rolling_windows() -> None:
    close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0], dtype=float)
    volume = pd.Series([100.0, 110.0, 120.0, 130.0, 140.0], dtype=float)

    ma = technical_indicators.calculate_ma(close, [5])["ma5"]
    vol_ma = technical_indicators.calculate_volume_ma(volume, [5])["vol_ma5"]
    ret = technical_indicators.calculate_returns(close, [1])["return_1d"]

    assert ma.iloc[:4].isna().all()
    assert ma.iloc[4] == 12.0
    assert vol_ma.iloc[:4].isna().all()
    assert vol_ma.iloc[4] == 120.0
    assert np.isnan(ret.iloc[0])


def test_metadata_validator_accepts_tushare_canonical_adjustment_schema(tmp_path: Path) -> None:
    (tmp_path / "adjustment_factors.csv").write_text(
        "\ufefftrade_date,symbol,adj_factor,source,provider\n"
        "20260102,600000,1.23,tushare,proxy\n",
        encoding="utf-8",
    )
    (tmp_path / "adjustment_factor.csv").write_text(
        "\ufefftrade_date,symbol,adj_factor,source,provider\n"
        "20260102,600000,1.23,tushare,proxy\n"
        "20260103,600000,1.24,tushare,proxy\n",
        encoding="utf-8",
    )

    payload = validate_metadata_dir(tmp_path)

    adjustment = next(item for item in payload["results"] if item["category"] == "adjustment_factors")
    assert adjustment["valid"] is True
    assert payload["adjustment_factor_canonical"]["canonical_file"] == "adjustment_factors.csv"
    assert payload["adjustment_factor_canonical"]["status"] == "canonical_available_alias_size_mismatch"
    assert payload["warnings"]


def test_metadata_validator_allows_nullable_tushare_dates_and_wider_history_keys(tmp_path: Path) -> None:
    (tmp_path / "security_master.csv").write_text(
        "symbol,name,list_date,delist_date,exchange,board,status,source,provider\n"
        "600000,浦发银行,19991110,,SSE,main,listed,tushare,proxy\n",
        encoding="utf-8",
    )
    (tmp_path / "st_history.csv").write_text(
        "symbol,name,st_start_date,st_end_date,st_type,source,provider,change_reason\n"
        "000001,样例A,20200101,,ST,tushare,proxy,ongoing\n"
        "000001,样例A,20200101,20210101,*ST,tushare,proxy,changed\n",
        encoding="utf-8",
    )
    (tmp_path / "delist_history.csv").write_text(
        "symbol,name,delist_date,last_trade_date,delist_reason,source,provider,fetched_at\n"
        "000002,样例B,20200101,20191231,reason-a,tushare,proxy,20260101\n"
        "000002,样例B,20200101,20191230,reason-b,tushare,proxy,20260101\n",
        encoding="utf-8",
    )

    payload = validate_metadata_dir(tmp_path)
    by_category = {item["category"]: item for item in payload["results"]}

    assert by_category["security_master"]["valid"] is True
    assert by_category["st_history"]["valid"] is True
    assert by_category["delist_history"]["valid"] is True


def test_metadata_validator_warns_not_fails_on_duplicate_history_rows(tmp_path: Path) -> None:
    (tmp_path / "st_history.csv").write_text(
        "symbol,name,st_start_date,st_end_date,st_type,source,provider,change_reason\n"
        "000001,样例A,20200101,,ST,tushare,proxy,ongoing\n"
        "000001,样例A,20200101,,ST,tushare,proxy,ongoing\n",
        encoding="utf-8",
    )

    payload = validate_metadata_dir(tmp_path)
    st_history = next(item for item in payload["results"] if item["category"] == "st_history")

    assert st_history["valid"] is True
    assert any("duplicate keys" in warning for warning in st_history["read_warnings"])


def test_adjustment_factor_audit_reads_utf8_sig_symbols(tmp_path: Path) -> None:
    path = tmp_path / "adjustment_factors.csv"
    path.write_text(
        "\ufefftrade_date,symbol,adj_factor,source,provider\n"
        "20260102,1,1.0,tushare,proxy\n"
        "20260103,600000,1.1,tushare,proxy\n",
        encoding="utf-8",
    )

    result = audit_adjustment_factors(path, {"000001", "600000"}, {"600000"}, {"000001"})

    assert result["exists"] is True
    assert result["symbol_count"] == 2
    assert result["candidate_holdings_coverage"] == 1.0
    assert result["has_provider"] is True
    assert result["read_mode"] == "chunked_utf8_sig"


def test_price_adjustment_consistency_reads_utf8_sig_adjustment_file(tmp_path: Path) -> None:
    existing = tmp_path / "adjustment_factors.csv"
    existing.write_text(
        "\ufefftrade_date,symbol,adj_factor,source,provider\n"
        "20260102,1,1.5,tushare,proxy\n",
        encoding="utf-8",
    )
    implied = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "20260102",
                "raw_close": 10.0,
                "qfq_close": 15.0,
                "implied_adj_factor": 1.5,
                "usable_as_formal_adj_factor": False,
            }
        ]
    )

    merged, summary = cross_validate_adjustment_factors(implied, existing)

    assert len(merged) == 1
    assert summary["conclusion"] == "existing_adjustment_factors_validated_for_pilot"
