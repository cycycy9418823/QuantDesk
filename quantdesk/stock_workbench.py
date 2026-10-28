"""Selected-stock reference prices derived from the existing daily scenarios.

This view does not create a strategy, a trade plan, a prediction record or a
ledger entry. The existing research builder owns all scenario calculations.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Mapping

import pandas as pd
import numpy as np

from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.price_basis import PRICE_BASIS_RAW, classify_frame_price_basis, validate_price_frame_set
from quantdesk.quote_evidence import quote_number
from quantdesk.board_rules import classify_board

RULE_VERSION = "daily-scenario-view-v1"


def trade_economics(*, symbol: str, trade_date: str, buy: float, stop: float, profit: float,
                    fee_assumptions: Mapping[str, Any] | None = None,
                    account_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe one minimum lot's scenario space; never size or submit a trade."""
    if any(quote_number(value) is None or float(value) <= 0 for value in (buy, stop, profit)):
        raise ValueError("Scenario prices must be finite and positive")
    rules = classify_board(symbol, trade_date=trade_date)
    quantity = rules.lot_size
    amount = round(buy * quantity, 2)
    risk = round((buy - stop) * quantity, 2) if stop < buy else None
    space = round((profit - buy) * quantity, 2) if profit > buy else None
    fees = fee_assumptions if isinstance(fee_assumptions, Mapping) else {}
    values = {key: quote_number(fees.get(key)) for key in ("commission_rate", "min_commission", "stamp_duty_rate", "transfer_fee_rate", "slippage_bps")}
    invalid = [key for key, value in values.items() if value is None or value < 0]
    # Complete estimates require explicit zero values too. Missing transfer or
    # slippage inputs must not be passed off as a zero-cost assumption.
    complete = not invalid
    buy_commission = max(amount * values["commission_rate"], values["min_commission"]) if values["commission_rate"] is not None and values["min_commission"] is not None and values["commission_rate"] >= 0 and values["min_commission"] >= 0 else None
    estimated_cost = None
    if complete:
        exit_amount = profit * quantity
        estimated_cost = round(buy_commission + max(exit_amount * values["commission_rate"], values["min_commission"])
            + exit_amount * values["stamp_duty_rate"] + (amount + exit_amount) * values["transfer_fee_rate"]
            + (amount + exit_amount) * values["slippage_bps"] / 10000.0, 2)
    account = account_context if isinstance(account_context, Mapping) else {}
    cash = quote_number(account.get("cash"))
    positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    held = [row for row in positions if isinstance(row, Mapping) and str(row.get("symbol") or "") == symbol]
    required = round(amount + buy_commission + amount * ((values["transfer_fee_rate"] or 0) + (values["slippage_bps"] or 0) / 10000.0), 2) if complete else None
    # A known commission component is not the complete round-trip estimate.
    # Preserve unknown comparison/affordability when any fee input is missing.
    below = space <= estimated_cost if space is not None and estimated_cost is not None else None
    comparison_status = "available" if below is not None else ("fee_assumptions_incomplete" if not complete else "observation_space_unavailable")
    return {"minimum_quantity": quantity, "quantity_basis": "shared board minimum lot; not a position suggestion",
            "minimum_lot_gross_amount": amount, "minimum_lot_cash_with_estimated_buy_cost": required,
            "gross_observation_space": space, "loss_to_exit_line": risk,
            "gross_space_to_risk_ratio": round(space / risk, 4) if space is not None and risk and risk > 0 else None,
            "fee_status": "provided" if complete else ("partial" if fees else "missing"),
            "fee_assumption_source": str(fees.get("source") or "caller_supplied" if fees else "not_provided"),
            "fee_assumptions": values, "missing_or_invalid_fee_fields": invalid,
            "minimum_buy_commission": round(buy_commission, 2) if buy_commission is not None else None,
            "estimated_roundtrip_cost_at_observation": estimated_cost,
            "net_observation_space_after_estimated_cost": round(space - estimated_cost, 2) if space is not None and estimated_cost is not None else None,
            "observation_space_below_cost": below, "cost_comparison_status": comparison_status,
            "cost_comparison_note": ("费用假设完整，按所列假设比较观察空间与往返成本。" if comparison_status == "available"
                else "费用假设不完整，暂不比较观察空间与往返成本；已知单项佣金不代表完整费用。" if not complete
                else "没有可单列的上方观察空间，暂不作成本覆盖比较。"),
            "cash_available": cash,
            "minimum_lot_affordable": cash >= required if cash is not None and required is not None else None,
            "account_valuation_date": str(account.get("valuation_date") or "")[:10],
            "held_positions": [{key: row.get(key) for key in ("symbol", "quantity", "sellable_quantity", "last_buy_date")} for row in held],
            "settlement_note": "A股当日买入通常次一交易日才可卖；实际可卖数量以账本证据为准。",
            "risk_note": "退出线只表示情景价差，不是最大亏损保证；跳空、跌停或停牌可能无法在该价退出。",
            "space_note": "上方价位是历史波动观察位，不是预测目标；价差及费用比较不代表胜率或期望收益。",
            "read_only": True, "actionable_order": False}


def build_stock_reference(
    original: pd.DataFrame,
    *,
    symbol: str,
    now: datetime,
    complete_trade_date: date | None,
    calendar: Mapping[str, Any],
    research_builder: Callable[[pd.DataFrame], dict[str, Any]],
    quote: Mapping[str, Any],
    quote_evidence: Mapping[str, Any],
    source_sha256: str = "",
    fee_assumptions: Mapping[str, Any] | None = None,
    account_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = to_shanghai_datetime(now)
    result: dict[str, Any] = {
        "available": False, "status": "missing", "label": "纸面 · 未验证",
        "rule_version": RULE_VERSION, "mode": "日线参考，盘中报价辅助",
        "symbol": symbol, "generated_at": current.isoformat(timespec="seconds"),
        "source_sha256": source_sha256, "as_of": "", "valid_until": "",
        "validity_text": "待核实日线日期和交易日历后重算。",
        "reason_codes": [], "warnings": [], "levels": [], "reasons": [],
        "current_assessment": "观望：资料不足。", "actionable_order": False,
        "research_identity": {"rule_id": RULE_VERSION, "validation_status": "unvalidated",
            "definition": "已有60日区间与历史波动带的情景参考；不是候选排序或MA交叉回测规则。",
            "uncertainty": "历史标准差推算的波动带未经概率校准，不是置信区间或收益预测。",
            "confirmation_status": "requires_price_volume_and_close_review",
            "confirmation_note": "触价不等于确认，量能等自然语言条件仍需事前明确与人工复核。"},
        "event_checks": [{"kind": key, "label": label, "status": "not_checked", "checked_at": None,
            "source": None, "message": "此参考计算未核验该类事件，不能据此认定没有事件或风险。"}
            for key, label in (("earnings", "业绩"), ("announcements", "重大公告"), ("suspension", "停复牌"),
                               ("risk_warning", "风险警示"), ("corporate_actions", "分红送转与除权"))],
    }
    if original is None or original.empty:
        return {**result, "reason_codes": ["daily_history_missing"], "message": "本地日线缺失；报价仍可单独查看。"}
    # The full original frame is classified before date filtering or deduplication.
    basis, reason = classify_frame_price_basis(original)
    policy = validate_price_frame_set({symbol: original}, consumer="feature")
    result["price_basis"] = basis
    if reason or not policy["eligible"] or basis != PRICE_BASIS_RAW:
        return {**result, "status": "blocked", "reason_codes": [reason or "raw_daily_required"],
                "message": "日线价格口径混合、未知或非原始价；不把复权价直接当作实时买卖参考价。"}
    if not complete_trade_date:
        return {**result, "reason_codes": ["complete_trade_date_unverified"], "message": "完整交易日尚未核实。"}
    if "date" not in original.columns or "close" not in original.columns:
        return {**result, "reason_codes": ["daily_columns_missing"], "message": "日线日期或收盘价列缺失。"}
    frame = original.copy()
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        return {**result, "reason_codes": ["daily_date_invalid"], "message": "原始日线包含无效日期，暂不计算参考价。"}
    frame["date"] = dates
    future_count = int((frame["date"].dt.date > complete_trade_date).sum())
    frame = frame.loc[frame["date"].dt.date <= complete_trade_date].copy()
    if frame.empty:
        return {**result, "reason_codes": ["no_complete_daily_history"], "message": "没有已完成交易日的日线。"}
    for col in ("open", "high", "low", "close", "volume"):
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    prices = [col for col in ("open", "high", "low", "close") if col in frame]
    # Explain whether old invalid rows are inside today's requested window;
    # the existing all-history guard below stays in force in either case.
    ordered = frame.sort_values("date")
    current_window = ordered.tail(60)
    if {"open", "high", "low", "close"} <= set(frame):
        invalid_ohlc = ((frame["high"] < frame[["open", "close", "low"]].max(axis=1))
                        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1)))
        bad_dates = frame.loc[invalid_ohlc, "date"].dt.date.astype(str).tolist()
        window_bad = sum(value >= current_window["date"].iloc[0].date().isoformat() for value in bad_dates) if not current_window.empty else 0
        result["history_quality"] = {"full_history_rows": len(frame), "ohlc_invalid_count": len(bad_dates),
            "first_invalid_date": min(bad_dates) if bad_dates else None,
            "current_window_start": current_window["date"].iloc[0].date().isoformat() if not current_window.empty else None,
            "current_window_rows": len(current_window), "current_window_ohlc_invalid_count": window_bad,
            "full_history_guard_retained": True,
            "message": ("旧异常即使在当前60条窗口外仍按现行全历史规则阻止参考；此处仅解释影响，没有绕过价格或完整性校验。"
                        if bad_dates else "未发现高低价关系异常；此检查不代表其他字段或公司行动证据完整。")}
    if not np.isfinite(frame[prices].to_numpy(dtype=float)).all() or not (frame[prices] > 0).all().all():
        return {**result, "reason_codes": ["daily_price_invalid"], "message": "日线包含缺失或无效价格。"}
    if {"open", "high", "low", "close"} <= set(frame):
        bad = (frame["high"] < frame[["open", "close", "low"]].max(axis=1)) | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        if bad.any():
            first_bad = frame.loc[bad, "date"].iloc[0].date().isoformat()
            return {**result, "reason_codes": ["daily_ohlc_invalid"],
                    "message": f"日线有{int(bad.sum())}条高低价关系异常（首例{first_bad}），暂不计算参考；报价仍可单独查看。"}
    duplicate = frame[frame["date"].duplicated(keep=False)]
    if not duplicate.empty and duplicate.groupby("date")[prices].nunique().gt(1).any().any():
        return {**result, "reason_codes": ["conflicting_daily_duplicates"], "message": "同一日期有冲突价格，暂不计算参考。"}
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    daily_date = frame["date"].iloc[-1].date()
    result["as_of"] = daily_date.isoformat()
    result["daily_rows"] = len(frame)
    if future_count:
        result["reason_codes"].append("incomplete_or_future_daily_rows_excluded")
        result["warnings"].append(f"已排除{future_count}条未完成或未来日期日线，参考仅使用已完成交易日。")
    existing = research_builder(frame)
    if not isinstance(existing, dict) or not existing.get("available"):
        return {**result, "reason_codes": result["reason_codes"] + ["daily_history_insufficient"],
                "message": "有效历史不足60条，暂不生成算法参考价。"}
    scenarios = existing.get("scenario_projection") or {}
    band = scenarios.get("five_day_range") or {}
    sr = existing.get("support_resistance") or {}
    last = quote_number((existing.get("price_band") or {}).get("latest"))
    band_low, band_high = quote_number(band.get("low")), quote_number(band.get("high"))
    low60, high60 = quote_number(sr.get("low_60d")), quote_number(sr.get("high_60d"))
    if any(value is None or value <= 0 for value in (last, band_low, band_high, low60, high60)):
        return {**result, "status": "blocked", "reason_codes": ["scenario_levels_invalid"], "message": "已有情景未提供有效价位。"}
    # These are the exact anchors of build_research_scenarios, not new weights.
    buy = round(min(high60, band_high), 2)
    stop = round(max(low60, band_low), 2)
    profit = round(band_high, 2)
    result["trade_economics"] = trade_economics(symbol=symbol, trade_date=result["as_of"], buy=buy, stop=stop,
        profit=profit, fee_assumptions=fee_assumptions, account_context=account_context)
    if result["trade_economics"]["observation_space_below_cost"] is True:
        result["warnings"].append("上方观察空间不足以覆盖所列费用；这些价位仅供观察，不证明值得交易。")
    elif result["trade_economics"]["cost_comparison_status"] == "fee_assumptions_incomplete":
        result["warnings"].append(result["trade_economics"]["cost_comparison_note"])
    cases = {str(item.get("tone")): item for item in scenarios.get("scenarios", []) if isinstance(item, dict)}
    result["levels"] = [
        {"kind": "buy", "label": "买入确认参考位", "price": buy,
         "condition": str(cases.get("strong", {}).get("trigger") or "需要收盘与量能确认。"),
         "basis": "已有进攻情景上沿；触及价位不代表买入信号完成。"},
        {"kind": "stop", "label": "止损 / 退出观察线", "price": stop,
         "condition": str(cases.get("risk", {}).get("trigger") or "跌破时重新核对原观察假设。"),
         "basis": "已有失效情景下沿；未持仓时表示回避风险。"},
        {"kind": "profit", "label": "止盈观察位", "price": profit if profit > buy else None,
         "condition": ("接近已有5日波动带上沿时，观察上冲量能和延续性；不是收益目标。"
                       if profit > buy else "已有波动上沿未高于买入参考位，目前没有可单列的止盈观察位。"),
         "basis": "已有5日历史波动区间上沿。"},
    ]
    result["available"] = True
    result["status"] = "research_reference"
    result["bias"] = str(scenarios.get("bias") or "观望")
    result["reasons"] = [str(scenarios.get("thesis") or ""), str(scenarios.get("structure_note") or "")]
    open_dates = []
    for key, is_open in (calendar.get("is_open_by_date") or {}).items():
        try:
            day = date.fromisoformat(str(key))
        except ValueError:
            continue
        if is_open and day > daily_date:
            open_dates.append(day.isoformat())
    open_dates.sort()
    if open_dates:
        result["valid_until"] = open_dates[0] + "T15:00:00+08:00"
        result["validity_text"] = "适用至 " + open_dates[0] + " 收盘；新日线到达后重算。"
    else:
        result["reason_codes"].append("next_trade_date_unverified")
        result["validity_text"] = "下一交易日未核实；仅保留历史参考，待日历更新。"
    expired = bool(result["valid_until"] and current >= datetime.fromisoformat(result["valid_until"]))
    ready = daily_date == complete_trade_date and bool(open_dates) and not expired
    entry_setup = stop < buy < profit
    if not stop < buy < profit:
        result["reason_codes"].append("reference_levels_not_separated")
        result["warnings"].append("已有价位没有形成分开的买入和止盈空间；先观望，仍保留退出风险参考。")
    if daily_date != complete_trade_date:
        result["reason_codes"].append("daily_reference_stale")
    if expired:
        result["reason_codes"].append("daily_reference_expired")
    result["reference_current"] = ready
    result["entry_setup_available"] = entry_setup
    price = quote_number(quote.get("price"))
    latest_close = quote_number(frame["close"].iloc[-1]) if "close" in frame.columns else None
    result["latest_daily_close"] = round(latest_close, 2) if latest_close is not None else None
    comparable = bool(quote_evidence.get("comparable_to_raw_daily"))
    market_active = bool(quote_evidence.get("market_active"))
    # After the close, or when the quote clock/basis cannot be verified, the
    # completed raw daily close still supports a clearly labelled comparison:
    # it is the last settled price, not an intraday signal.
    compare_price = price if comparable else latest_close
    basis = "intraday_quote" if comparable and market_active else "daily_close"
    closed_note = "" if market_active else "当前休市，不宣称产生盘中信号；"
    if market_active and comparable:
        basis_note = "盘中报价"
    elif result["as_of"]:
        basis_note = closed_note + "以 " + result["as_of"] + " 收盘价比较（日线收盘口径）"
    else:
        basis_note = closed_note + "等待报价核验"
    result["assessment_basis"] = basis
    if not ready:
        result["current_assessment"] = "观望：日线过期或有效期未核实；只展示研究依据。"
    elif compare_price is None:
        result["current_assessment"] = "等待报价核验；上述日线参考保留，暂不判断价格是否触发。"
    elif compare_price <= stop:
        result["current_assessment"] = basis_note + "：已触及退出观察线 " + format(stop, ".2f") + "，原假设需要重新核对；未持仓时优先回避。"
    elif entry_setup and compare_price >= profit:
        result["current_assessment"] = basis_note + "：已触及止盈观察位 " + format(profit, ".2f") + "，观察量能和延续性，不自动卖出。"
    elif not entry_setup:
        result["current_assessment"] = "观望：已有价位未形成分开的买入和止盈空间，保留风险参考。"
    elif compare_price >= buy:
        result["current_assessment"] = basis_note + "：已触及买入确认参考位 " + format(buy, ".2f") + "；仍需量能与次日收盘确认，不判定信号已完成。"
    else:
        result["current_assessment"] = basis_note + "：尚未触及买入确认参考位 " + format(buy, ".2f") + "，继续观望。"
    result["message"] = "参考价来自已有日线情景，规则未改变；当前没有经样本外验证的盈利结论。"
    return result
