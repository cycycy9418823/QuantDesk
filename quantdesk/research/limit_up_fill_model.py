from __future__ import annotations


def limit_up_fill_probability(limit_type: str) -> dict:
    table = {"one_word_limit_up": 0.05, "sealed_limit_up": 0.15, "broken_limit_up": 0.60, "high_turnover_limit_up": 0.30}
    probability = table.get(limit_type, 0.0)
    warnings = [] if limit_type in table else ["unknown_limit_up_type"]
    assumption = "simulation only"
    return {"limit_type": limit_type, "fill_probability": probability, "warnings": warnings, "assumption": assumption, "assumptions": [assumption]}
