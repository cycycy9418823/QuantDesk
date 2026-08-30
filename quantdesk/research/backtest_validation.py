"""Backtest validation contracts for research-only paper comparison.

The functions in this module are pure diagnostics.  They do not run a
backtest, optimize parameters, write latest artifacts, connect to brokers, or
change paper state.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Mapping, Sequence

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.version import API_SCHEMA_VERSION


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("events", "trades", "rows"):
            if isinstance(value.get(key), list):
                return [dict(row) for row in value[key] if isinstance(row, Mapping)]
        return [dict(value)]
    return []


def _window(mapping: Mapping[str, Any] | None) -> tuple[date | None, date | None]:
    source = mapping if isinstance(mapping, Mapping) else {}
    return _day(source.get("start")), _day(source.get("end"))


def _trade_pnl(row: Mapping[str, Any]) -> float:
    for key in ("pnl", "realized_pnl", "net_pnl", "profit"):
        value = _number(row.get(key))
        if value is not None:
            return value
    entry = _number(row.get("entry_price"))
    exit_price = _number(row.get("exit_price"))
    quantity = _number(row.get("quantity"))
    cost = _number(row.get("cost") or row.get("total_fee")) or 0.0
    if entry is not None and exit_price is not None and quantity is not None:
        return (exit_price - entry) * quantity - cost
    return 0.0


def _equity_values(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _number(row.get("equity") or row.get("nav"))
        if value is not None and value > 0:
            values.append(value)
    return values


def _daily_positive_rate(equity: Sequence[float]) -> float | None:
    if len(equity) < 2:
        return None
    changes = [equity[idx] / equity[idx - 1] - 1.0 for idx in range(1, len(equity)) if equity[idx - 1] > 0]
    if not changes:
        return None
    return sum(1 for item in changes if item > 0) / len(changes)


def _profit_factor(pnls: Sequence[float]) -> float:
    wins = sum(item for item in pnls if item > 0)
    losses = abs(sum(item for item in pnls if item < 0))
    if losses > 0:
        return wins / losses
    return float("inf") if wins > 0 else 0.0


def _turnover(trades: Sequence[Mapping[str, Any]], equity: Sequence[float]) -> float:
    gross = 0.0
    for row in trades:
        direct = _number(row.get("gross_amount") or row.get("gross") or row.get("notional"))
        if direct is not None:
            gross += abs(direct)
            continue
        price = _number(row.get("fill_price") or row.get("price") or row.get("entry_price"))
        quantity = _number(row.get("quantity"))
        if price is not None and quantity is not None:
            gross += abs(price * quantity)
    denominator = sum(equity) / len(equity) if equity else 0.0
    return gross / denominator if denominator > 0 else 0.0


def build_backtest_validation_snapshot(
    *,
    backtest_payload: Mapping[str, Any],
    paper_payload: Mapping[str, Any] | None = None,
    training_window: Mapping[str, Any] | None = None,
    test_window: Mapping[str, Any] | None = None,
    market_regime_payload: Mapping[str, Any] | None = None,
    strategy_brain_payload: Mapping[str, Any] | None = None,
    acceptance_scenario: bool = False,
    as_of: str = "",
) -> dict[str, Any]:
    reasons: list[str] = []
    train_start, train_end = _window(training_window)
    test_start, test_end = _window(test_window)
    if train_end is not None and test_start is not None and train_end >= test_start:
        reasons.append("train_test_overlap")
    if train_start is not None and test_end is not None and train_start > test_end:
        reasons.append("train_window_after_test_window")

    as_of_day = _day(as_of)
    regime = market_regime_payload if isinstance(market_regime_payload, Mapping) else {}
    regime_day = _day(regime.get("as_of") or regime.get("data_cutoff"))
    if regime and regime_day is None:
        reasons.append("market_regime_asof_missing")
    elif as_of_day is not None and regime_day is not None and regime_day > as_of_day:
        reasons.append("market_regime_after_as_of")
    regime_source = str(regime.get("source") or regime.get("classification_source") or "").strip().lower()
    if "strategy_return" in regime_source:
        reasons.append("market_regime_circular_source")

    strategy_brain = strategy_brain_payload if isinstance(strategy_brain_payload, Mapping) else {}
    strategy_brain_mode = str(strategy_brain.get("mode") or "shadow").strip().lower()
    strategy_brain_enabled = bool(strategy_brain.get("enabled"))
    if strategy_brain_enabled and strategy_brain_mode != "shadow" and not acceptance_scenario:
        reasons.append("strategy_brain_not_shadow")
    strategy_brain_status = (
        "acceptance_paper_selection"
        if strategy_brain_enabled and strategy_brain_mode != "shadow" and acceptance_scenario
        else "shadow"
    )

    trades = _rows(backtest_payload.get("trades") if isinstance(backtest_payload, Mapping) else [])
    equity_rows = _rows(backtest_payload.get("equity_curve") if isinstance(backtest_payload, Mapping) else [])
    equity = _equity_values(equity_rows)
    pnls = [_trade_pnl(row) for row in trades]
    trade_count = len(trades)
    trade_win_rate = (sum(1 for item in pnls if item > 0) / trade_count) if trade_count else None
    daily_positive_rate = _daily_positive_rate(equity)
    snapshot = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest_validation_snapshot",
        "status": "blocked" if reasons else "passed",
        "ok": not reasons,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "training_status": "trained" if training_window else "fixed_spec",
        "train_window": dict(training_window or {}),
        "test_window": dict(test_window or {}),
        "daily_positive_rate": daily_positive_rate,
        "trade_win_rate": trade_win_rate,
        "profit_factor": _profit_factor(pnls),
        "turnover": _turnover(trades, equity),
        "trade_count": trade_count,
        "paper_event_count": len(_rows(paper_payload or {})),
        "market_regime_source": regime_source or "",
        "strategy_brain_status": strategy_brain_status,
        "acceptance_scenario": bool(acceptance_scenario),
        "as_of": str(as_of or ""),
        **paper_only_boundary(),
    }
    snapshot["validation_digest"] = _digest(snapshot)
    return snapshot


def _event_key(row: Mapping[str, Any], index: int) -> tuple[str, str, int]:
    return (
        str(row.get("symbol") or row.get("code") or "").strip(),
        str(row.get("side") or row.get("action") or "").strip().lower(),
        index,
    )


def compare_paper_backtest_events(
    paper_events: Sequence[Mapping[str, Any]],
    backtest_events: Sequence[Mapping[str, Any]],
    *,
    fee_abs_tol: float = 1e-6,
) -> dict[str, Any]:
    reasons: list[str] = []
    paper_rows = _rows(list(paper_events))
    backtest_rows = _rows(list(backtest_events))
    if len(paper_rows) != len(backtest_rows):
        reasons.append("event_count_mismatch")
    for index, paper in enumerate(paper_rows):
        backtest = backtest_rows[index] if index < len(backtest_rows) else {}
        if _event_key(paper, index) != _event_key(backtest, index):
            reasons.append("event_identity_mismatch")
        if str(paper.get("execution_date") or "")[:10] != str(backtest.get("execution_date") or "")[:10]:
            reasons.append("execution_date_mismatch")
        signal_day = _day(backtest.get("signal_date") or paper.get("signal_date"))
        execution_day = _day(backtest.get("execution_date"))
        if signal_day is None or execution_day is None or execution_day <= signal_day:
            reasons.append("t_plus_one_mismatch")
        if int(_number(paper.get("quantity")) or -1) != int(_number(backtest.get("quantity")) or -2):
            reasons.append("lot_quantity_mismatch")
        paper_fee = _number(paper.get("total_fee") or paper.get("fee"))
        backtest_fee = _number(backtest.get("total_fee") or backtest.get("fee"))
        if paper_fee is None or backtest_fee is None or abs(paper_fee - backtest_fee) > fee_abs_tol:
            reasons.append("cost_mismatch")
    return {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "paper_backtest_event_comparison",
        "status": "failed" if reasons else "passed",
        "ok": not reasons,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "paper_event_count": len(paper_rows),
        "backtest_event_count": len(backtest_rows),
        **paper_only_boundary(),
    }


def build_validation_run_manifest(
    *,
    config: Mapping[str, Any],
    data: Mapping[str, Any],
    code: Mapping[str, Any],
    seed: int,
    source_snapshot: str = "formal",
) -> dict[str, Any]:
    reasons: list[str] = []
    snapshot = str(source_snapshot or "").strip().lower()
    if snapshot in {"smoke", "synthetic", "temporary", "fixture"}:
        reasons.append(f"{snapshot}_snapshot_not_formal_latest")
    manifest = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest_validation_run_manifest",
        "seed": int(seed),
        "source_snapshot": snapshot or "unknown",
        "config_hash": _digest(dict(config or {})),
        "data_hash": _digest(dict(data or {})),
        "code_hash": _digest(dict(code or {})),
        "formal_latest_eligible": not reasons,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        **paper_only_boundary(),
    }
    manifest["run_manifest_digest"] = _digest(manifest)
    return manifest


__all__ = [
    "build_backtest_validation_snapshot",
    "build_validation_run_manifest",
    "compare_paper_backtest_events",
]
