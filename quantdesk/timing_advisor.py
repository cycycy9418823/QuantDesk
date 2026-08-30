from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any, Mapping

from quantdesk.market_clock import get_market_session, next_cn_a_share_trading_day, to_shanghai_datetime
from quantdesk.trust_engine import TrustAssessment, trust_assessment_to_dict

DISCLAIMER = "时机提示仅用于观察和复盘，不构成投资建议，也不会触发真实交易。"

SESSION_PRE_OPEN = "pre_open"
SESSION_MORNING = "morning_session"
SESSION_LUNCH = "lunch_break"
SESSION_AFTERNOON = "afternoon_session"
SESSION_AFTER_CLOSE = "after_close"
SESSION_HOLIDAY = "holiday"


@dataclass
class TimingAdvice:
    symbol: str
    name: str
    market_session: str
    timing_state: str
    timing_label: str
    ready_for_review: bool
    suggested_action: str
    reasons: list[str]
    wait_conditions: list[str]
    pre_action_checklist: list[str]
    invalidation_conditions: list[str]
    risk_warnings: list[str]
    next_review_time: str
    disclaimer: str


def _assessment_dict(assessment: TrustAssessment | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(assessment, TrustAssessment):
        return trust_assessment_to_dict(assessment)
    return dict(assessment)


def _base_time(now: datetime | None) -> datetime:
    return to_shanghai_datetime(now)


def _next_review(now: datetime, session: str) -> str:
    if session in {SESSION_PRE_OPEN, SESSION_MORNING, SESSION_LUNCH, SESSION_AFTERNOON}:
        return f"{now.date().isoformat()} 15:00 后"
    if session == SESSION_AFTER_CLOSE:
        return f"{next_cn_a_share_trading_day(now).isoformat()} 开盘前"
    return f"{next_cn_a_share_trading_day(now).isoformat()} 开盘前"


def _normalize_session(session: str) -> str:
    if session in {"market_holiday", "non_trading_day"}:
        return SESSION_HOLIDAY
    return session


def advise_timing(
    assessment: TrustAssessment | Mapping[str, Any],
    now: datetime | None = None,
    context: Mapping[str, Any] | None = None,
) -> TimingAdvice:
    data = _assessment_dict(assessment)
    current = _base_time(now)
    session = _normalize_session(str((context or {}).get("market_session") or get_market_session(current)))
    action_state = str(data.get("action_state") or "只观察")
    data_score = float(data.get("data_reliability_score") or 0)
    trust_score = float(data.get("trust_score") or 0)

    reasons: list[str] = []
    risk_warnings: list[str] = list(data.get("risks") or [])[:4]
    wait_conditions = [
        "数据状态保持 OK 或 INFO。",
        "消息状态没有明显异常。",
        "价格仍在历史波动区间内，未触及异常区域。",
        "收盘后重新复核趋势和市场环境。",
    ]
    pre_action_checklist = list(data.get("required_checks") or [])[:5]
    invalidation = list(data.get("invalidation_conditions") or [])[:5]

    timing_state = "observe"
    timing_label = "观察"
    suggested_action = "只做观察和计划复核。"
    ready_for_review = False

    if action_state == "暂不参考":
        timing_state = "wait"
        timing_label = "等待"
        suggested_action = "先处理阻塞问题，暂不参考该结果。"
    elif data_score < 60:
        timing_state = "paper_only"
        timing_label = "仅虚拟验证"
        suggested_action = "数据或消息仍需核对，最多先做虚拟验证。"
    elif session == SESSION_PRE_OPEN:
        timing_state = "wait"
        timing_label = "等待"
        suggested_action = "盘前先制定观察计划，不做盘中判断。"
    elif session == SESSION_MORNING and current.time() < time(10, 0):
        timing_state = "observe"
        timing_label = "观察"
        suggested_action = "开盘前半小时波动较大，先观察承接和量能。"
    elif session == SESSION_MORNING:
        if trust_score >= 76 and action_state in {"条件满足后再评估", "深度复核"}:
            timing_state = "conditional_review"
            timing_label = "条件满足后再评估"
            suggested_action = "仅可在所有前置检查通过后继续深度复核，不构成行动建议。这仍不是交易指令，仅为工具内部状态。"
            ready_for_review = True
        else:
            timing_state = "paper_only" if action_state == "虚拟验证" else "observe"
            timing_label = "仅虚拟验证" if timing_state == "paper_only" else "观察"
            suggested_action = "先观察，不提升为更积极状态。"
    elif session == SESSION_LUNCH:
        timing_state = "observe"
        timing_label = "观察"
        suggested_action = "午休只做复盘和计划，不做盘中动作判断。"
    elif session == SESSION_AFTERNOON and current.time() >= time(14, 30):
        timing_state = "paper_only"
        timing_label = "仅虚拟验证"
        suggested_action = "尾盘波动可能加大，仅做收盘前复核或虚拟验证，不进行实时判断。"
    elif session == SESSION_AFTERNOON:
        if trust_score >= 76 and action_state in {"条件满足后再评估", "深度复核"}:
            timing_state = "conditional_review"
            timing_label = "条件满足后再评估"
            suggested_action = "继续观察盘中价格和量能，条件完整后再复核。这仍不是交易指令，仅为工具内部状态。"
            ready_for_review = True
        else:
            timing_state = "paper_only" if action_state == "虚拟验证" else "observe"
            timing_label = "仅虚拟验证" if timing_state == "paper_only" else "观察"
            suggested_action = "继续观察，先不提高状态。"
    elif session == SESSION_AFTER_CLOSE:
        timing_state = "after_close_review"
        timing_label = "收盘后复盘"
        suggested_action = "复盘今日表现，刷新虚拟持仓结算。"
    else:
        timing_state = "after_close_review"
        timing_label = "收盘后复盘"
        suggested_action = "市场休市，只做计划和复盘。"

    if action_state == "深度复核" and timing_state == "conditional_review" and trust_score >= 86:
        timing_state = "small_position_review"
        timing_label = "深度复核"
        suggested_action = "进入严格复核清单，仍需完成全部风险检查。"
    if action_state == "暂不参考":
        ready_for_review = False
    if ready_for_review and "这仍不是交易指令" not in suggested_action:
        suggested_action = f"{suggested_action} 这仍不是交易指令，仅为工具内部状态。"
    if risk_warnings:
        reasons.append("存在风险提示，不能只看策略原始分。")
    reasons.append(f"当前市场时段：{session}。")

    return TimingAdvice(
        symbol=str(data.get("symbol") or data.get("code") or "").zfill(6),
        name=str(data.get("name") or ""),
        market_session=session,
        timing_state=timing_state,
        timing_label=timing_label,
        ready_for_review=bool(ready_for_review),
        suggested_action=suggested_action,
        reasons=reasons,
        wait_conditions=wait_conditions,
        pre_action_checklist=pre_action_checklist,
        invalidation_conditions=invalidation,
        risk_warnings=risk_warnings,
        next_review_time=_next_review(current, session),
        disclaimer=DISCLAIMER,
    )


def timing_advice_to_dict(advice: TimingAdvice) -> dict[str, Any]:
    return asdict(advice)
