from __future__ import annotations

import math
from typing import Any

from quantdesk.platform_common import base_payload, finalize_outputs, read_json, simple_markdown, truthy


SYNTHETIC_FACTOR_METRICS = {
    "momentum": {"ic": 0.04},
    "quality": {"ic": 0.03},
    "liquidity": {"ic": 0.02},
}


def _payload_is_synthetic(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    containers = [payload]
    for key in ("input_status", "provenance"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        if truthy(container.get("synthetic_test_data")) or truthy(container.get("uses_synthetic_data")):
            return True
        for key in ("mode", "data_source", "source_mode", "evidence_kind"):
            marker = str(container.get(key) or "").strip().lower()
            if marker in {"synthetic", "synthetic_test_data", "synthetic_test_demo"} or marker.startswith(
                "synthetic_"
            ):
                return True
    return False


def evaluate_recent_factor_effectiveness(factor_payload: dict | None = None, *_args: Any, **_kwargs: Any) -> dict:
    payload = factor_payload if isinstance(factor_payload, dict) else read_json("results/factor_health_latest.json", {})
    factors = payload.get("factor_metrics") if isinstance(payload, dict) else {}
    synthetic = truthy(_kwargs.get("synthetic_test_data"))
    upstream_synthetic = _payload_is_synthetic(payload)
    if upstream_synthetic and not synthetic:
        return {
            "status": "warning",
            "factors": {},
            "warnings": [
                "upstream_synthetic_factor_metrics_rejected: enable synthetic_test_data explicitly for test/demo use"
            ],
            "synthetic_test_data": False,
        }
    warnings: list[str] = []
    if synthetic:
        warnings.append("synthetic_test_data: dynamic factor evidence is test/demo only")
    if not isinstance(factors, dict) or not factors:
        if synthetic:
            factors = SYNTHETIC_FACTOR_METRICS
            warnings.append("synthetic_test_data: fixed factor IC values are test/demo evidence only")
        else:
            return {
                "status": "warning",
                "factors": {},
                "warnings": ["factor_metrics_missing: dynamic weights were not generated"],
                "synthetic_test_data": False,
            }
    valid_factors: dict[str, float] = {}
    for key, value in factors.items():
        raw_ic = value.get("ic") if isinstance(value, dict) else None
        if raw_ic in (None, "") and isinstance(value, dict):
            raw_ic = value.get("ic_mean")
        try:
            ic = float(raw_ic)
        except (TypeError, ValueError):
            warnings.append(f"factor_ic_invalid:{key}")
            continue
        if not math.isfinite(ic):
            warnings.append(f"factor_ic_invalid:{key}")
            continue
        valid_factors[str(key)] = abs(ic)
    if not valid_factors:
        warnings.append("factor_metrics_no_valid_ic: dynamic weights were not generated")
    return {
        "status": "warning" if warnings else "passed",
        "factors": valid_factors,
        "warnings": list(dict.fromkeys(warnings)),
        "synthetic_test_data": bool(synthetic),
    }


def compute_dynamic_factor_weights(effectiveness: dict) -> dict:
    values = dict(effectiveness.get("factors") or {})
    total = sum(values.values()) or 1.0
    return {key: round(value / total, 6) for key, value in values.items()}


def build_dynamic_factor_combo(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    eff = evaluate_recent_factor_effectiveness(
        synthetic_test_data=truthy(cfg.get("synthetic_test_data")),
    )
    weights = compute_dynamic_factor_weights(eff)
    return {
        "dynamic_factor_combo_id": "dynamic_factor_combo_2026W22",
        "weights": weights,
        "weight_sum": round(sum(weights.values()), 6),
        "status": eff.get("status") or "warning",
        "warnings": list(eff.get("warnings") or []),
        "synthetic_test_data": bool(eff.get("synthetic_test_data")),
    }


def build_dynamic_factor_model_report(config: dict | None = None) -> dict:
    combo = build_dynamic_factor_combo(config)
    payload = base_payload(
        "dynamic_factor_model",
        status=str(combo.get("status") or "warning"),
        warnings=list(combo.get("warnings") or []),
    )
    payload.update(combo)
    payload["items"] = [{"name": key, "weight": value} for key, value in combo["weights"].items()]
    return finalize_outputs(payload, "dynamic_factor_model", render_dynamic_factor_model_markdown(payload))


def render_dynamic_factor_model_markdown(payload: dict) -> str:
    return simple_markdown("Dynamic Factor Model", payload, "items")
