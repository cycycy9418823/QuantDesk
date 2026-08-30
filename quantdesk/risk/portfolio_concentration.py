from __future__ import annotations


def evaluate_concentration(positions: list[dict], *, max_symbol_weight: float = 0.2, max_sector_weight: float = 0.4) -> dict:
    alerts = []
    sector_weights: dict[str, float] = {}
    for row in positions:
        weight = float(row.get("weight", 0))
        if weight > max_symbol_weight:
            alerts.append({"type": "single_symbol_weight", "symbol": row.get("symbol"), "severity": "warning", "weight": weight})
        sector = row.get("sector") or "unknown"
        sector_weights[sector] = sector_weights.get(sector, 0) + weight
    for sector, weight in sector_weights.items():
        if weight > max_sector_weight:
            alerts.append({"type": "sector_weight", "sector": sector, "severity": "warning", "weight": weight})
    return {"alerts": alerts, "alert_count": len(alerts), "sector_weights": sector_weights}
