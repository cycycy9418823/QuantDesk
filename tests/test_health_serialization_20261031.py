from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd


def test_display_cleaning_preserves_values_with_one_sanitization_pass() -> None:
    from scripts.server.helpers import util
    source = {"rows": [{"day": pd.Timestamp("2026-10-30"), "price": np.float64(9.5),
                       "missing": [None, float("nan"), float("inf"), pd.NaT],
                       "counts": np.array([1, 2])}]}
    expected = {"rows": [{"day": "2026-10-30", "price": 9.5,
                         "missing": ["数据缺失"] * 4, "counts": [1, 2]}]}
    with patch.object(util, "sanitize_json_value", wraps=util.sanitize_json_value) as sanitize:
        assert util.clean_payload(source) == expected
    # Nested all-market evidence must not be sanitized again for each subtree.
    assert sanitize.call_count == 1
