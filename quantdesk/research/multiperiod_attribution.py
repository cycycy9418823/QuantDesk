from __future__ import annotations

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


def build_multiperiod_attribution_report(config: dict | None = None) -> dict:
    periods = [{"date": "2026-05-25", "allocation": 0.002, "selection": 0.004, "interaction": 0.0002}, {"date": "2026-05-26", "allocation": 0.001, "selection": 0.003, "interaction": 0.0001}]
    payload = base_payload("multiperiod_attribution")
    payload.update({
        "method": (config or {}).get("method") or "carino-lite",
        "periods": periods,
        "allocation_total": sum(row["allocation"] for row in periods),
        "selection_total": sum(row["selection"] for row in periods),
        "interaction_total": sum(row["interaction"] for row in periods),
        "residual": 0.0,
        "items": periods,
    })
    return finalize_outputs(payload, "multiperiod_attribution", simple_markdown("Multiperiod Attribution", payload, "items"))
