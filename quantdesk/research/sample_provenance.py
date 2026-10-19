"""Keep historical sample declarations separate from verified holdout evidence."""
from __future__ import annotations

from typing import Any, Mapping

SAMPLE_LIMITATION = "尚未建立样本外验证；历史筛选或统计通过不代表策略已验证。"


def research_sample_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Current entrypoints have no independently verified holdout registration."""
    declared = str(payload.get("declared_sample_role") or payload.get("sample_role") or "unspecified")
    window = payload.get("sample_window")
    if not isinstance(window, dict):
        window = {"requested_start": payload.get("start_date") or "", "requested_end": payload.get("end_date") or ""}
    return {
        "sample_role": "in_sample" if declared == "in_sample" else "unspecified",
        "declared_sample_role": declared,
        "sample_window": dict(window),
        "out_of_sample_validation": "not_established",
        "validation_status": "research_unvalidated",
        "sample_limitation": SAMPLE_LIMITATION,
    }
