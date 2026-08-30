from __future__ import annotations

import csv
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from quantdesk.strategy_signals import limit_rate_for_symbol

def _limit_up_proxy_threshold(symbol: str) -> float:
    return limit_rate_for_symbol(symbol, "") * 0.95


def load_tail_frame(path: Path, n: int = 260) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-n:]
    except Exception:
        return []


def symbol_metrics(path: Path) -> dict[str, Any] | None:
    rows = load_tail_frame(path, 260)
    if len(rows) < 80:
        return None
    closes = []
    volumes = []
    for row in rows:
        try:
            closes.append(float(row.get("close") or 0))
            volumes.append(float(row.get("volume") or 0))
        except Exception:
            return None
    if len(closes) < 2 or closes[-1] <= 0:
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] > 0]
    vol = pstdev(rets) if len(rets) > 2 else 0.0
    avg = mean(rets) if rets else 0.0
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, c / peak - 1)
    latest_return = closes[-1] / closes[-2] - 1 if closes[-2] > 0 else 0.0
    return {
        "symbol": path.stem,
        "signal_date": rows[-1].get("date", ""),
        "close": closes[-1],
        "return_5d": closes[-1] / closes[-6] - 1 if len(closes) > 6 and closes[-6] > 0 else 0.0,
        "return_20d": closes[-1] / closes[-21] - 1 if len(closes) > 21 and closes[-21] > 0 else 0.0,
        "return_60d": closes[-1] / closes[-61] - 1 if len(closes) > 61 and closes[-61] > 0 else 0.0,
        "return_120d": closes[-1] / closes[-121] - 1 if len(closes) > 121 and closes[-121] > 0 else 0.0,
        "annualized_return": avg * 252,
        "sharpe": (avg / vol * (252 ** 0.5)) if vol > 0 else 0.0,
        "max_drawdown": mdd,
        "win_rate": sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0,
        "turnover": 1.0,
        "volatility": vol * (252 ** 0.5),
        "avg_volume_20d": mean(volumes[-20:]) if volumes else 0.0,
        "amount_proxy": closes[-1] * (mean(volumes[-20:]) if volumes else 0.0),
        "limit_up_proxy": (
            math.isclose(float(rows[-1].get("high") or 0), float(rows[-1].get("close") or 0), abs_tol=0.001)
            and latest_return >= _limit_up_proxy_threshold(path.stem)
        ),
    }


def load_market_metrics(root: Path, max_symbols: int = 300) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "data/daily").glob("*.csv"))[:max_symbols]:
        item = symbol_metrics(path)
        if item:
            rows.append(item)
    return rows


def backtest_summary(strategy_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_boundary = {
        "evidence_kind": "tail_window_proxy",
        "backtest_ready": False,
        "formal_performance_evidence": False,
        "evidence_note": "Latest-tail descriptive proxy; not a formal time-series portfolio backtest.",
    }
    if not candidates:
        return {
            "strategy_id": strategy_id,
            "annualized_return": 0.0,
            "Sharpe": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "cost_stress_return": 0.0,
            "liquidity_risk": "unknown",
            "data_risk_level": "MODERATE_HIGH",
            "robustness_score": 0.0,
            "research_readiness": "insufficient",
            **evidence_boundary,
        }
    return {
        "strategy_id": strategy_id,
        "annualized_return": mean([x["annualized_return"] for x in candidates]),
        "Sharpe": mean([x["sharpe"] for x in candidates]),
        "max_drawdown": min([x["max_drawdown"] for x in candidates]),
        "win_rate": mean([x["win_rate"] for x in candidates]),
        "turnover": mean([x.get("turnover", 1.0) for x in candidates]),
        "cost_stress_return": mean([x["annualized_return"] for x in candidates]) - 0.02,
        "liquidity_risk": "elevated" if any(x["amount_proxy"] <= 0 for x in candidates) else "monitored",
        "data_risk_level": "MODERATE_HIGH",
        "robustness_score": max(0.0, min(1.0, 0.5 + mean([x["sharpe"] for x in candidates]) / 5)),
        "research_readiness": "research_ready_with_warnings",
        **evidence_boundary,
    }
