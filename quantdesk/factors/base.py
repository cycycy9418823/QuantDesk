"""Base factor classes for the QuantDesk factor framework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class FactorResult:
    name: str = ""
    value: float | None = None
    available: bool = False
    reason: str = ""
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "available": self.available,
            "reason": self.reason,
            "unit": self.unit,
        }


class Factor:
    """Abstract base for a factor calculator."""

    name: str = ""
    category: str = ""
    description: str = ""

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        """Compute the factor from a daily DataFrame.

        Returns FactorResult with available=False if data is missing.
        """
        return FactorResult(name=self.name, available=False, reason="未实现")
