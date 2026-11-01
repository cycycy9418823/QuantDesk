"""The shared, unchanged JSON value contract for research reports."""
from __future__ import annotations

import math
from typing import Any

import pandas as pd


def json_safe(value: Any) -> Any:
    """Keep report values and null handling consistent across research modules."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value
