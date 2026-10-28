"""Search a small idea space on local bars, then keep or kill.

This is not a pre-built stock pool. Templates are question types a junior
trader might try. Parameters and survival come from local evidence only.
LLM is off. Ideas that fail holdout-after-cost never reach the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable
from quantdesk.research.validation_identity import identity_hash

import pandas as pd

from ai_trader.hypotheses import (
    INSUFFICIENT,
    KILLED,
    LIVE,
    REJECTED,
    live_items,
    upsert_hypothesis,
)

ROUND_TRIP_COST = 0.004
MIN_HOLDOUT_OBS = 20
HOLD_DAYS = 5


def _reversal_score(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    past = panel.pct_change(lookback)
    return -past


def _low_vol_score(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    vol = panel.pct_change().rolling(lookback).std()
    return -vol


def _ma_trend_score(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    ma = panel.rolling(lookback).mean()
    return panel / ma - 1.0


@dataclass(frozen=True)
class IdeaTemplate:
    hypothesis_id: str
    title: str
    claim: str
    lookback: int
    score_fn: Callable[[pd.DataFrame, int], pd.DataFrame]
    pick_high: bool = True
    quantile: float = 0.8


TEMPLATES: tuple[IdeaTemplate, ...] = (
    IdeaTemplate(
        hypothesis_id="reversal_5",
        title="5日跌得多的，5日后更容易反弹",
        claim="横截面上过去5日跌幅最大的一组，持有5日的平均收益扣成本后仍为正。",
        lookback=5,
        score_fn=_reversal_score,
    ),
    IdeaTemplate(
        hypothesis_id="low_vol_20",
        title="波动低的股票更稳",
        claim="过去20日波动最低的一组，持有5日的平均收益扣成本后仍为正。",
        lookback=20,
        score_fn=_low_vol_score,
    ),
    IdeaTemplate(
        hypothesis_id="ma20_trend",
        title="站上20日均线的趋势更强",
        claim="收盘价高于20日均线的一组，持有5日的平均收益扣成本后仍为正。",
        lookback=20,
        score_fn=_ma_trend_score,
    ),
)


def _evaluate_template(template: IdeaTemplate, panel: pd.DataFrame) -> dict[str, Any]:
    if panel.shape[0] < template.lookback + HOLD_DAYS + MIN_HOLDOUT_OBS:
        return {
            "hypothesis_id": template.hypothesis_id,
            "title": template.title,
            "claim": template.claim,
            "status": INSUFFICIENT,
            "reason": "历史样本不够，无法诚实检验。",
            "holdout_mean_net": None,
            "holdout_n": 0,
            "lookback": template.lookback,
        }
    score = template.score_fn(panel, template.lookback)
    fwd = panel.shift(-HOLD_DAYS) / panel - 1.0
    valid_dates = score.dropna(how="all").index.intersection(fwd.dropna(how="all").index)
    valid_dates = valid_dates[:-HOLD_DAYS] if len(valid_dates) > HOLD_DAYS else valid_dates
    period_returns: list[float] = []
    for day in valid_dates:
        row = score.loc[day].dropna()
        if row.empty:
            continue
        cutoff = row.quantile(template.quantile)
        if template.pick_high:
            picks = row[row >= cutoff]
        else:
            picks = row[row <= cutoff]
        if picks.empty:
            continue
        basket = fwd.loc[day, picks.index].dropna()
        if basket.empty:
            continue
        period_returns.append(float(basket.mean()))
    if len(period_returns) < MIN_HOLDOUT_OBS:
        return {
            "hypothesis_id": template.hypothesis_id,
            "title": template.title,
            "claim": template.claim,
            "status": INSUFFICIENT,
            "reason": f"有效检验段只有{len(period_returns)}次，少于{MIN_HOLDOUT_OBS}次。",
            "holdout_mean_net": None,
            "holdout_n": len(period_returns),
            "lookback": template.lookback,
        }
    split = int(len(period_returns) * 0.7)
    holdout = period_returns[split:]
    mean_net = float(sum(holdout) / len(holdout) - ROUND_TRIP_COST)
    passed = mean_net > 0
    return {
        "hypothesis_id": template.hypothesis_id,
        "title": template.title,
        "claim": template.claim,
        "status": LIVE if passed else REJECTED,
        "reason": (
            f"扣约{ROUND_TRIP_COST:.2%}来回成本后，后段{HOLD_DAYS}日篮子均收益为{mean_net:.2%}，实际后段{len(holdout)}次。"
            + ("仅是既有研究筛选结果，未证明有效优势。" if passed else "既有筛选未通过。")
        ),
        "holdout_mean_net": round(mean_net, 6),
        "holdout_n": len(holdout),
        "train_n": split,
        "lookback": template.lookback,
        "quantile": template.quantile,
        "validation_status": "unverified" if len(holdout) >= MIN_HOLDOUT_OBS else "insufficient_holdout",
        "validation_label": "研究假设 · 未验证",
        "meets_holdout_minimum": len(holdout) >= MIN_HOLDOUT_OBS,
        "minimum_holdout_observations": MIN_HOLDOUT_OBS,
        "proven_edge": False,
        "validation_limits": ["5-session returns overlap", "same holdout reused for selection among 3 templates",
            "no independent future interval", "fixed approximate round-trip cost", "no multiplicity-adjusted confidence interval"],
        "operational_status_note": "live is a compatibility research-selection status, not validation success",
    }


def rank_today(template: IdeaTemplate, panel: pd.DataFrame, as_of: str) -> list[tuple[str, float]]:
    if as_of not in panel.index:
        usable = panel.index[panel.index <= as_of]
        if len(usable) == 0:
            return []
        as_of = str(usable[-1])
    score = template.score_fn(panel, template.lookback)
    if as_of not in score.index:
        return []
    row = score.loc[as_of].dropna()
    ordered = row.sort_values(ascending=not template.pick_high)
    return [(str(symbol), float(value)) for symbol, value in ordered.items()]


def template_by_id(hypothesis_id: str) -> IdeaTemplate | None:
    for item in TEMPLATES:
        if item.hypothesis_id == hypothesis_id:
            return item
    return None


def research_ideas(panel: pd.DataFrame, book: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    original_attrs = dict(panel.attrs)
    source_policy = original_attrs.get("price_policy_validation") or {}
    blocked = source_policy.get("eligible") is not True
    cutoff = pd.to_datetime(as_of, errors="coerce")
    dates = pd.to_datetime(panel.index, errors="coerce")
    panel = panel.loc[dates <= cutoff].copy() if pd.notna(cutoff) else panel.iloc[:0].copy()
    panel.index = pd.to_datetime(panel.index).strftime("%Y-%m-%d")
    panel = panel.sort_index()
    input_evidence = {**(original_attrs.get("input_evidence") or {}), "as_of": as_of,
        "observed_end": str(panel.index[-1]) if len(panel) else None,
        "observed_rows": len(panel), "price_policy_validation": source_policy,
        "reason_codes": list(source_policy.get("reason_codes") or (["research_price_evidence_missing"] if blocked else []))}
    evaluated: list[dict[str, Any]] = []
    next_book = dict(book)
    for template in TEMPLATES:
        result = _evaluate_template(template, panel if not blocked else panel.iloc[:0])
        if blocked:
            result["reason"] = "研究输入价格口径未核验，本轮不计算正式指标。"
            result["price_contract_status"] = "blocked"
        result["as_of"] = as_of
        result.setdefault("validation_label", "研究假设 · 未验证")
        result.setdefault("validation_status", "insufficient_evidence")
        result["proven_edge"] = False
        result["selection_identity"] = {"as_of": as_of,
            "config_sha256": identity_hash({"hypothesis_id": template.hypothesis_id,
                "lookback": template.lookback, "quantile": template.quantile, "pick_high": template.pick_high,
                "holding_days": HOLD_DAYS, "round_trip_cost": ROUND_TRIP_COST}),
            "data_sha256": sha256(panel.to_csv().encode()).hexdigest()}
        result["born_on"] = result.get("born_on") or as_of
        existing = next(
            (
                dict(row)
                for row in (book.get("items") or [])
                if isinstance(row, dict) and row.get("hypothesis_id") == template.hypothesis_id
            ),
            {},
        )
        if existing.get("born_on"):
            result["born_on"] = existing["born_on"]
        if existing.get("status") == KILLED and existing.get("kill_reason"):
            result["status"] = KILLED
            result["reason"] = str(existing["kill_reason"])
            result["killed_on"] = existing.get("killed_on")
        next_book = upsert_hypothesis(next_book, result)
        evaluated.append(result)
    live = [item for item in evaluated if item.get("status") == LIVE]
    live.sort(key=lambda item: float(item.get("holdout_mean_net") or -999), reverse=True)
    chosen = live[0] if live else None
    if chosen is not None:
        for item in evaluated:
            if item["hypothesis_id"] != chosen["hypothesis_id"] and item.get("status") == LIVE:
                item["status"] = "standby"
                item["reason"] = item.get("reason", "") + " 本轮按既有方法只保留一条研究候选，独立验证待完成。"
                next_book = upsert_hypothesis(next_book, item)
        chosen["status"] = LIVE
        next_book = upsert_hypothesis(next_book, chosen)
    return {
        "book": next_book,
        "evaluated": evaluated,
        "live": chosen,
        "live_count": 1 if chosen else 0,
        "all_rejected": chosen is None,
        "status": "blocked" if blocked else "missing" if panel.empty else "ready",
        "input_evidence": input_evidence,
    }


def kill_live_if_paper_failed(
    book: dict[str, Any],
    *,
    as_of: str,
    live_id: str,
    paper_pnl: float,
    traded_days: int,
) -> dict[str, Any]:
    if traded_days < 5:
        return book
    if paper_pnl >= 0:
        return book
    for row in live_items(book):
        if str(row.get("hypothesis_id")) != live_id:
            continue
        killed = dict(row)
        killed["status"] = KILLED
        killed["killed_on"] = as_of
        killed["kill_reason"] = (
            f"上场后{traded_days}个交易日纸面盈亏{paper_pnl:.2f}元，方法被杀掉。"
        )
        killed["reason"] = killed["kill_reason"]
        return upsert_hypothesis(book, killed)
    return book
