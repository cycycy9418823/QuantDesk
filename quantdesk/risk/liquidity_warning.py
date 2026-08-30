from __future__ import annotations


def evaluate_liquidity(rows: list[dict], *, min_amount: float = 10_000_000) -> dict:
    alerts = []
    for row in rows:
        amount = float(row.get("amount", row.get("avg_amount_5d", 0)) or 0)
        if amount < min_amount:
            alerts.append({"type": "low_liquidity", "symbol": row.get("symbol"), "severity": "warning", "amount": amount})
    return {"alerts": alerts, "alert_count": len(alerts)}


def evaluate_liquidity_warning(row: dict, *, min_amount: float = 10_000_000) -> dict:
    amount = float(row.get("amount", row.get("avg_amount_5d", 0)) or 0)
    score = float(row.get("liquidity_score", 100) or 0)
    severity = "low"
    warnings = []
    if amount < min_amount or score < 30:
        severity = "high" if score < 20 or amount < min_amount * 0.2 else "medium"
        warnings.append("low_liquidity")
    return {"type": "liquidity_warning", "severity": severity, "warnings": warnings, "amount": amount, "liquidity_score": score}
