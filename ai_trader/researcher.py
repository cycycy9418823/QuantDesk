"""Search a small idea space on local bars, then keep or kill.

This is not a pre-built stock pool. Templates are question types a junior
trader might try. Parameters and survival come from local evidence only.
LLM is off. Ideas that fail holdout-after-cost never reach the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

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
            f"扣大约{ROUND_TRIP_COST:.2%}来回成本后，样本外{HOLD_DAYS}日篮子均收益为{mean_net:.2%}。"
            + ("通过，允许上场。" if passed else "未通过，不准下场交易。")
        ),
        "holdout_mean_net": round(mean_net, 6),
        "holdout_n": len(holdout),
        "train_n": split,
        "lookback": template.lookback,
        "quantile": template.quantile,
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
    evaluated: list[dict[str, Any]] = []
    next_book = dict(book)
    for template in TEMPLATES:
        result = _evaluate_template(template, panel)
        result["as_of"] = as_of
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
                item["reason"] = item.get("reason", "") + " 同类中不是最好，本轮只让一条方法上场。"
                next_book = upsert_hypothesis(next_book, item)
        chosen["status"] = LIVE
        next_book = upsert_hypothesis(next_book, chosen)
    return {
        "book": next_book,
        "evaluated": evaluated,
        "live": chosen,
        "live_count": 1 if chosen else 0,
        "all_rejected": chosen is None,
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
