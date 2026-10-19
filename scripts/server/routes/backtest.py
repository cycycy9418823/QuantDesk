"""Backtest API routes for the QuantDesk 8000 research server."""

from __future__ import annotations

from quantdesk.research.sample_provenance import research_sample_context

from quantdesk.research.backtest_return_evidence import closed_trade_return_evidence
from quantdesk.backtest_loaders import read_backtest_daily, validate_backtest_history_sources, align_backtest_research_basis

import json
import os
import re
from datetime import datetime
from pathlib import Path
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


class BacktestSymbolInputError(ValueError):
    """The requested local-history universe violates symbol/path boundaries."""


def _validated_backtest_symbols(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise BacktestSymbolInputError("symbols 必须是六位股票代码的列表。")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value.strip()) is None:
            raise BacktestSymbolInputError("股票代码必须是六位数字（例如600000），不能包含路径或交易所前后缀。")
        symbol = value.strip()
        if symbol not in result:
            result.append(symbol)
    return result


def _validated_history_paths(daily_dir: Path, symbols: list[str]) -> list[tuple[str, Path]]:
    """Resolve all selected paths before any reader opens a CSV."""
    expected = Path(os.path.abspath(daily_dir))
    if expected.resolve() != expected:
        raise BacktestSymbolInputError("日线目录存在路径重定向，无法安全读取回测输入。")
    paths = []
    for symbol in symbols:
        path = expected / f"{symbol}.csv"
        if path.resolve() != path or path.is_symlink():
            raise BacktestSymbolInputError("回测CSV存在路径重定向，只能读取指定日线目录内的原文件。")
        paths.append((symbol, path))
    return paths


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
    symbols = _validated_backtest_symbols(handoff.get("symbols", []))
    return symbols[:max_symbols], handoff


def _load_backtest_histories(
    max_symbols: int,
    symbols: list[str] | None = None,
    candidate_rows_func: CandidateRowsFunc | None = None,
    *,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, pd.DataFrame]:
    """Load local daily CSVs for a bounded backtest sample.

    Current research candidates are preferred when a candidate provider is
    supplied; otherwise the loader pads from ``data/daily``.
    """
    daily_dir = project_root / "data" / "daily"
    selected_symbols = _validated_backtest_symbols(symbols) if symbols is not None else []
    if not selected_symbols and candidate_rows_func is not None:
        try:
            candidates = candidate_rows_func(18)
        except Exception:  # noqa: BLE001
            candidates = []
        # Only provider execution errors retain the historical fallback. Invalid
        # returned identities must not be swallowed and replaced by other files.
        if not isinstance(candidates, list) or any(not isinstance(row, dict) for row in candidates):
            raise BacktestSymbolInputError("候选列表必须包含有效股票代码对象。")
        selected_symbols = _validated_backtest_symbols([row.get("symbol") for row in candidates])
    if not symbols and len(selected_symbols) < max_symbols and daily_dir.exists():
        for path in sorted(daily_dir.glob("*.csv")):
            sym = path.stem
            if re.fullmatch(r"[0-9]{6}", sym) and sym not in selected_symbols:
                selected_symbols.append(sym)
            if len(selected_symbols) >= max_symbols:
                break
    histories: dict[str, pd.DataFrame] = {}
    paths = _validated_history_paths(daily_dir, selected_symbols[:max_symbols])
    for sym, path in paths:
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
            df, _reason = read_backtest_daily(sym, daily_dir, start_date=start_date, end_date=end_date)
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
            start_date = str(body.get("start_date") or "").strip()
            end_date = str(body.get("end_date") or "").strip()
            try:
                from datetime import date
                from quantdesk.trading_calendar import resolve_target_complete_trade_date

                if start_date:
                    date.fromisoformat(start_date)
                if end_date:
                    date.fromisoformat(end_date)
                else:
                    target = resolve_target_complete_trade_date()
                    end_date = str(target.get("target_complete_trade_date") or target.get("target_date") or "")
                if start_date and end_date and start_date > end_date:
                    raise ValueError("start_date_after_end_date")
            except ValueError:
                return jsonify({"ok": False, "message_for_ui": "回测起止日期无效，需 YYYY-MM-DD 且开始不晚于结束。"}), 400
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
                requested_symbols = _validated_backtest_symbols(body["symbols"]) if "symbols" in body else []
                histories = _load_backtest_histories(
                    max_symbols,
                    symbols=requested_symbols or handoff_symbols or None,
                    candidate_rows_func=candidate_rows_func if source != "full_a" else None,
                    start_date=start_date,
                    end_date=end_date,
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
                histories = align_backtest_research_basis(histories)
                price_policy_validation = validate_backtest_history_sources(histories)
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
                from quantdesk.research.official_limit_metadata import load_official_limit_metadata, attach_official_limit_metadata

                metadata = load_official_limit_metadata(list(histories), metadata_dir=project_root / "data" / "metadata", start_date=start_date or None, end_date=end_date or None)
                for symbol, frame in histories.items():
                    histories[symbol] = attach_official_limit_metadata(frame, symbol, metadata)
                config = PortfolioBacktestConfig(use_official_limit_metadata=True, historical_costs=body.get("historical_costs", True) not in (False, "false"))
                result = simulate_portfolio_backtest(histories, config, strategy=strategy)
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
                return_data = closed_trade_return_evidence(result.trades or [])
                payload = {
                    "ok": True,
                    "exists": True,
                    "strategy": strategy,
                    "source": source or ("current_analysis_pool_sample" if candidate_rows_func else "full_a_local_archive_sample"),
                    "start_date": start_date,
                    "end_date": end_date,
                    **research_sample_context({"sample_role": "in_sample", "start_date": start_date, "end_date": end_date}),
                    "sample_limit": max_symbols,
                    "research_evidence": {
                        "universe_basis": "local_archive; bounded sample",
                        "point_in_time_membership_complete": False,
                        "out_of_sample_validation": "not_established",
                        "survivorship_bias_not_excluded": True,
                        "price_preparation": "shared_cli_api_as_of_research_restatement",
                    },
                    "warnings": [*metadata.warnings, *return_data["return_evidence"]["warnings"]],
                    "cost_evidence_status": "complete" if return_data["return_evidence"]["cost_coverage_complete"] else "partial",
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
                    **return_data,
                    "trades_preview_limit": 200,
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
            except BacktestSymbolInputError as exc:
                payload = {"ok": False, "status": "bad_backtest_symbols", "message_for_ui": str(exc)}
                status = 400
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
