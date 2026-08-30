"""Backtest API routes for the QuantDesk 8000 research server."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

import pandas as pd
from flask import Blueprint, Flask, jsonify, request

from quantdesk.csv_io import read_csv_frame
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    PRICE_BASIS_UNKNOWN,
    classify_frame_price_basis,
    observed_price_basis_labels,
    validate_price_frame_set,
)
from scripts.server.constants import DATA_WRITE_LOCK, project_root
from scripts.server.helpers.util import clean_payload, finite_float, read_json_file


CandidateRowsFunc = Callable[[int], list[dict[str, Any]]]


def _research_paper_boundary_fields() -> dict[str, Any]:
    return {
        "is_real_order": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def _backtest_trade_boundary(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row) if isinstance(row, dict) else {"value": row}
    out.update(_research_paper_boundary_fields())
    out.setdefault("boundary_note", "Historical backtest row only; not an order instruction or broker route.")
    return out


def _execution_price_evidence(basis: str) -> dict[str, Any]:
    uses_raw = str(basis) == PRICE_BASIS_RAW
    return {
        "execution_price_basis": str(basis or PRICE_BASIS_UNKNOWN),
        "execution_price_role": (
            "raw_market_bar_research_proxy" if uses_raw else "same_basis_research_proxy"
        ),
        "uses_raw_execution_price": uses_raw,
        "real_execution_price": False,
    }


def _sanitize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with corrupt prices from local daily CSV inputs."""
    if "close" not in df.columns:
        return df
    close = pd.to_numeric(df["close"], errors="coerce")
    valid = close[(close > 0) & close.notna()]
    median = float(valid.median()) if len(valid) else 0.0
    if median <= 0:
        return df.iloc[0:0]
    cap = median * 50
    ok = close.notna() & (close > 0) & (close <= cap)
    for col in ("open", "high", "low"):
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce")
            ok = ok & (vals.isna() | ((vals > 0) & (vals <= cap)))
    return df[ok].reset_index(drop=True)


def _strategy_screening_backtest_handoff(max_symbols: int) -> tuple[list[str], dict[str, Any]]:
    latest = read_json_file(project_root / "results" / "strategy_screening_funnel_latest.json")
    handoff = latest.get("backtest_handoff") if isinstance(latest.get("backtest_handoff"), dict) else {}
    if handoff.get("status") != "ready":
        return [], handoff if isinstance(handoff, dict) else {}
    symbols = [str(item).strip() for item in (handoff.get("symbols") or []) if str(item).strip()]
    return list(dict.fromkeys(symbols))[:max_symbols], handoff


def _load_backtest_histories(
    max_symbols: int,
    symbols: list[str] | None = None,
    candidate_rows_func: CandidateRowsFunc | None = None,
) -> dict[str, pd.DataFrame]:
    """Load local daily CSVs for a bounded backtest sample.

    Current research candidates are preferred when a candidate provider is
    supplied; otherwise the loader pads from ``data/daily``.
    """
    daily_dir = project_root / "data" / "daily"
    selected_symbols: list[str] = list(dict.fromkeys(str(item).strip() for item in (symbols or []) if str(item).strip()))
    if not selected_symbols and candidate_rows_func is not None:
        try:
            for row in candidate_rows_func(18):
                sym = str(row.get("symbol") or "").strip()
                if sym and sym not in selected_symbols:
                    selected_symbols.append(sym)
        except Exception:  # noqa: BLE001
            selected_symbols = []
    if not symbols and len(selected_symbols) < max_symbols and daily_dir.exists():
        for path in sorted(daily_dir.glob("*.csv")):
            sym = path.stem
            if sym.isdigit() and sym not in selected_symbols:
                selected_symbols.append(sym)
            if len(selected_symbols) >= max_symbols:
                break
    histories: dict[str, pd.DataFrame] = {}
    for sym in selected_symbols[:max_symbols]:
        path = daily_dir / f"{sym}.csv"
        if not path.exists():
            continue
        try:
            df = read_csv_frame(path)
            _basis, basis_reason = classify_frame_price_basis(df)
            if basis_reason:
                # Preserve the untrusted frame long enough for the run-level
                # validator to block the complete requested universe.  Silently
                # dropping it here would change that universe and hide the defect.
                histories[sym] = df
                continue
            if "date" in df.columns:
                df = df.sort_values("date").reset_index(drop=True)
            df = _sanitize_daily(df)
            if len(df) >= 130:
                histories[sym] = df
        except Exception:  # noqa: BLE001
            continue
    return histories


def create_backtest_blueprint(candidate_rows_func: CandidateRowsFunc | None = None) -> Blueprint:
    """Create backtest routes for reading and running bounded local backtests."""

    bp = Blueprint("backtest_api", __name__)

    @bp.route("/api/backtest/strategies", methods=["GET"])
    def get_backtest_strategies() -> Any:
        try:
            from quantdesk.strategy_registry import list_strategy_definitions

            payload = {"ok": True, "strategies": list_strategy_definitions()}
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "strategies": [],
                        "message_for_ui": f"策略清单读取失败：{exc}",
                        "error": str(exc),
                    }
                )
            ), 500
        return jsonify(clean_payload(payload))

    @bp.route("/api/backtest/latest", methods=["GET"])
    def get_backtest_latest() -> Any:
        path = project_root / "results" / "backtest" / "latest.json"
        if not path.exists():
            return jsonify(
                clean_payload(
                    {
                        "ok": True,
                        "exists": False,
                        "message_for_ui": "尚未运行回测。选择策略后点击“运行回测”。",
                    }
                )
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["exists"] = True
        except Exception as exc:  # noqa: BLE001
            return jsonify(clean_payload({"ok": False, "message_for_ui": f"回测结果读取失败：{exc}"})), 500
        return jsonify(clean_payload(data))

    @bp.route("/api/backtest/run", methods=["POST"])
    def post_backtest_run() -> Any:
        """Run a bounded local-history backtest and write results/backtest/latest.json."""
        if not DATA_WRITE_LOCK.acquire(blocking=False):
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "busy",
                        "message_for_ui": "另一个数据写入操作正在进行，请稍后再试。",
                    }
                )
            ), 409
        try:
            body = request.get_json(silent=True) or {}
            strategy = str(body.get("strategy") or "ma_breakout").strip() or "ma_breakout"
            source = str(body.get("source") or "").strip()
            try:
                max_symbols = int(body.get("max_symbols") or 20)
            except (TypeError, ValueError):
                max_symbols = 20
            max_symbols = max(1, min(max_symbols, 50))
            try:
                from quantdesk.backtest import PortfolioBacktestConfig, simulate_portfolio_backtest

                handoff: dict[str, Any] = {}
                handoff_symbols: list[str] = []
                if source == "strategy_screening":
                    handoff_symbols, handoff = _strategy_screening_backtest_handoff(max_symbols)
                    if handoff.get("strategy"):
                        strategy = str(handoff.get("strategy") or strategy)
                requested_symbols = (
                    [str(item).strip() for item in body.get("symbols", []) if str(item).strip()]
                    if isinstance(body.get("symbols"), list)
                    else []
                )
                histories = _load_backtest_histories(
                    max_symbols,
                    symbols=requested_symbols or handoff_symbols or None,
                    candidate_rows_func=candidate_rows_func,
                )
                if not histories:
                    return jsonify(
                        clean_payload(
                            {
                                "ok": False,
                                "source": source or "current_analysis_pool",
                                "backtest_handoff": handoff,
                                "message_for_ui": "本地日线不足，无法回测（每只至少约需 130 个交易日）。",
                            }
                        )
                    )
                price_policy_validation = validate_price_frame_set(
                    histories,
                    consumer="backtest",
                )
                if not price_policy_validation.get("eligible"):
                    reason_codes = [
                        str(item)
                        for item in price_policy_validation.get("reason_codes") or []
                    ]
                    blocked_payload = {
                        "ok": False,
                        "status": "blocked",
                        "source": source or "current_analysis_pool",
                        "backtest_handoff": handoff,
                        "symbol_count": len(histories),
                        "symbols": list(histories),
                        "price_policy_validation": price_policy_validation,
                        **observed_price_basis_labels(
                            PRICE_BASIS_UNKNOWN,
                            reason_codes=reason_codes,
                        ),
                        **_execution_price_evidence(PRICE_BASIS_UNKNOWN),
                        "message_for_ui": (
                            "价格口径校验未通过，已在模拟前阻断："
                            + ", ".join(reason_codes)
                        ),
                        **_research_paper_boundary_fields(),
                        "boundary_note": (
                            "Research Only；价格口径不一致时不运行历史模拟，"
                            "不接券商，不产生真实订单。"
                        ),
                    }
                    return jsonify(clean_payload(blocked_payload)), 422
                result = simulate_portfolio_backtest(histories, PortfolioBacktestConfig(), strategy=strategy)
                equity_df = result.equity_curve
                equity: list[dict[str, Any]] = []
                if equity_df is not None and not equity_df.empty and "equity" in equity_df.columns:
                    step = max(1, len(equity_df) // 600)
                    for _, r in equity_df.iloc[::step].iterrows():
                        equity.append({"date": str(r.get("date", "")), "equity": finite_float(r.get("equity"))})
                m = result.metrics
                accepted_price_basis = str(
                    price_policy_validation.get("accepted_price_basis")
                    or PRICE_BASIS_UNKNOWN
                )
                payload = {
                    "ok": True,
                    "exists": True,
                    "strategy": strategy,
                    "source": source or "current_analysis_pool",
                    "backtest_handoff": handoff,
                    "symbol_count": len(histories),
                    "symbols": list(histories.keys()),
                    "metrics": {
                        "annual_return": finite_float(m.annual_return),
                        "total_return": finite_float(m.total_return),
                        "max_drawdown": finite_float(m.max_drawdown),
                        "sharpe": finite_float(m.sharpe),
                        "daily_positive_rate": finite_float(m.daily_positive_rate),
                        "win_rate": finite_float(m.win_rate),
                        "days": int(m.days or 0),
                        "number_of_trades": int(m.number_of_trades or 0),
                        "final_equity": finite_float(m.final_equity),
                        "benchmark_return": finite_float(m.benchmark_return),
                        "excess_return": finite_float(m.excess_return),
                    },
                    "equity_curve": equity,
                    "trades": [_backtest_trade_boundary(clean_payload(t)) for t in (result.trades or [])[:200]],
                    "assumptions": clean_payload(result.assumptions),
                    "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "message_for_ui": (
                        f"回测完成：策略 {strategy}，{len(histories)} 只样本，{int(m.number_of_trades or 0)} 笔交易。"
                    ),
                    **_research_paper_boundary_fields(),
                    "price_policy_validation": price_policy_validation,
                    **observed_price_basis_labels(accepted_price_basis),
                    **_execution_price_evidence(accepted_price_basis),
                    "boundary_note": "Research Only；历史回测不代表未来收益，不构成投资建议，不接券商，不产生真实订单。",
                }
                out_dir = project_root / "results" / "backtest"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "latest.json").write_text(
                    json.dumps(clean_payload(payload), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                status = 200
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "message_for_ui": f"回测运行失败：{exc}", "error": str(exc)}
                status = 500
            return jsonify(clean_payload(payload)), status
        finally:
            DATA_WRITE_LOCK.release()

    return bp


def create_backtest_read_blueprint(candidate_rows_func: CandidateRowsFunc | None = None) -> Blueprint:
    """Backward-compatible factory name for the backtest blueprint."""
    return create_backtest_blueprint(candidate_rows_func=candidate_rows_func)


def register_backtest_routes(app: Flask, candidate_rows_func: CandidateRowsFunc | None = None) -> None:
    app.register_blueprint(create_backtest_blueprint(candidate_rows_func=candidate_rows_func))


def register_backtest_read_routes(app: Flask, candidate_rows_func: CandidateRowsFunc | None = None) -> None:
    """Backward-compatible registration name."""
    register_backtest_routes(app, candidate_rows_func=candidate_rows_func)
