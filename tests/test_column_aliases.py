from __future__ import annotations

import unittest

import pandas as pd

from quantdesk.column_aliases import (
    COLUMN_FORWARD_MAP,
    COLUMN_REVERSE_MAP,
    find_column,
    get_value,
    normalize_columns,
)


class TestColumnAliases(unittest.TestCase):
    def test_normalize_chinese_to_english(self) -> None:
        df = pd.DataFrame(
            {"代码": ["000001"], "开盘": [10.0], "收盘": [10.5], "涨跌幅": [1.5]}
        )
        result = normalize_columns(df)
        self.assertIn("code", result.columns)
        self.assertIn("open", result.columns)
        self.assertIn("close", result.columns)
        self.assertIn("pct_chg", result.columns)
        self.assertNotIn("代码", result.columns)

    def test_normalize_already_english(self) -> None:
        df = pd.DataFrame({"code": ["000001"], "close": [10.5]})
        result = normalize_columns(df)
        self.assertEqual(list(result.columns), ["code", "close"])

    def test_normalize_mixed(self) -> None:
        df = pd.DataFrame({"代码": ["000001"], "close": [10.5], "成交额": [1000]})
        result = normalize_columns(df)
        self.assertEqual(list(result.columns), ["code", "close", "amount"])

    def test_find_column(self) -> None:
        df = pd.DataFrame({"开盘": [10.0]})
        self.assertEqual(find_column(df, "open"), "开盘")
        df2 = pd.DataFrame({"open": [10.0]})
        self.assertEqual(find_column(df2, "open"), "open")
        df3 = pd.DataFrame({"high": [11.0]})
        self.assertIsNone(find_column(df3, "open"))

    def test_get_value_from_series(self) -> None:
        row = pd.Series({"代码": "000001", "收盘": 10.5, "涨跌幅": 1.5})
        self.assertEqual(get_value(row, "code"), "000001")
        self.assertEqual(get_value(row, "close"), 10.5)
        self.assertEqual(get_value(row, "pct_chg"), 1.5)

    def test_get_value_from_english_series(self) -> None:
        row = pd.Series({"code": "000001", "close": 10.5, "pct_chg": 1.5})
        self.assertEqual(get_value(row, "code"), "000001")
        self.assertEqual(get_value(row, "close"), 10.5)
        self.assertEqual(get_value(row, "pct_chg"), 1.5)

    def test_get_value_default(self) -> None:
        row = pd.Series({"close": 10.5})
        self.assertIsNone(get_value(row, "open"))
        self.assertEqual(get_value(row, "open", 0.0), 0.0)

    def test_get_value_from_dict(self) -> None:
        row = {"代码": "000001", "成交额": 1000}
        self.assertEqual(get_value(row, "code"), "000001")
        self.assertEqual(get_value(row, "amount"), 1000)

    def test_extended_aliases(self) -> None:
        df = pd.DataFrame({"股票代码": ["000001"], "开盘价": [10.0], "收盘价": [10.5]})
        result = normalize_columns(df)
        self.assertIn("code", result.columns)
        self.assertIn("open", result.columns)
        self.assertIn("close", result.columns)

    def test_forward_map_contains_key_aliases(self) -> None:
        self.assertIn("代码", COLUMN_FORWARD_MAP)
        self.assertIn("股票代码", COLUMN_FORWARD_MAP)
        self.assertIn("涨跌幅", COLUMN_FORWARD_MAP)
        self.assertEqual(COLUMN_FORWARD_MAP["代码"], "code")
        self.assertEqual(COLUMN_FORWARD_MAP["涨跌幅"], "pct_chg")

    def test_reverse_map(self) -> None:
        self.assertEqual(COLUMN_REVERSE_MAP["code"], "代码")
        self.assertEqual(COLUMN_REVERSE_MAP["open"], "开盘")

    def test_amount_aliases(self) -> None:
        row = pd.Series({"成交额(元)": 1000})
        self.assertEqual(get_value(row, "amount"), 1000)
        row2 = pd.Series({"turnover": 2000})
        self.assertEqual(get_value(row2, "amount"), 2000)

    def test_indicators_extra_aliases(self) -> None:
        row = pd.Series({"latest_close": 10.5, "open_price": 10.0, "turnover_volume": 1000, "amount_yuan": 2000})
        self.assertEqual(get_value(row, "close"), 10.5)
        self.assertEqual(get_value(row, "open"), 10.0)
        self.assertEqual(get_value(row, "volume"), 1000)
        self.assertEqual(get_value(row, "amount"), 2000)

    def test_date_extra_aliases(self) -> None:
        df = pd.DataFrame({"datetime": ["2026-01-01"], "交易日期": ["2026-01-02"]})
        self.assertEqual(find_column(df, "date"), "datetime")
        df2 = pd.DataFrame({"交易日期": ["2026-01-02"]})
        self.assertEqual(find_column(df2, "date"), "交易日期")


if __name__ == "__main__":
    unittest.main()
