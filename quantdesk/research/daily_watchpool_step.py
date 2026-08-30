"""观察池（paper watchlist）接入每日编排器的步骤封装。

这是模拟盘流水线里「候选池 → 观察池」的一环：把综合评分（技术面为主）排序后的
top 候选写入本地观察池，并复盘已有观察项。它只做本地纸面观察——不联网、不下单、
不接实盘、不产生任何成交。编排器只需调用一个函数即可拿到可直接 ``add_step`` 的负载。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk.paper.lifecycle import LifecycleEventStore
from quantdesk.research.composite_candidate_scoring import compute_composite_scores
from quantdesk.research.paper_watchlist import (
    DEFAULT_PATH,
    add_watchlist_items,
    load_paper_watchlist,
    review_watchlist_items,
)
from quantdesk.time_utils import now_iso

DISCLAIMER = (
    "观察池仅做本地纸面观察与复盘：综合评分技术面为主，"
    "不联网、不下单、不接实盘，也不产生任何成交。"
)


def _watchlist_symbols(watchlist: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("symbol"))
        for item in watchlist.get("items", [])
        if isinstance(item, dict) and item.get("symbol")
    }


def build_daily_watchpool_step(
    candidates: Sequence[Mapping[str, Any]] | None,
    *,
    watchlist_path: str | Path = DEFAULT_PATH,
    lifecycle_store: LifecycleEventStore | str | Path | None = None,
    top: int = 10,
    weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """用综合评分挑选 top 候选写入观察池并复盘，返回可 ``add_step`` 的负载。

    * 用 :func:`compute_composite_scores` 对候选做技术面为主的四维综合评分。
    * 取综合排名 top-N 写入本地观察池（去重，同一标的不重复加入）。
    * 复盘已有观察项（纯纸面，不成交）。
    """
    rows = [dict(row) for row in (candidates or []) if isinstance(row, Mapping)]
    ranked = compute_composite_scores(rows, weights=weights) if rows else []
    limit = max(0, int(top))
    selected = ranked[:limit] if limit else []

    items_to_add: list[dict[str, Any]] = []
    for row in selected:
        items_to_add.append({
            **row,
            "source_task": "daily_composite_watchpool",
            "generated_at": now_iso(),
            "composite_score": row.get("composite_score"),
            "composite_rank": row.get("composite_rank"),
        })

    before = load_paper_watchlist(watchlist_path)
    existing_symbols = _watchlist_symbols(before)

    if items_to_add:
        watchlist = add_watchlist_items(
            items_to_add,
            watchlist_path,
            lifecycle_store=lifecycle_store,
            manual=False,
        )
    else:
        watchlist = before

    # 复盘已有观察项（纯本地纸面，不成交）。
    reviewed = review_watchlist_items(None, watchlist_path)

    final_items = list(reviewed.get("items") or [])
    final_symbols = {str(item.get("symbol")) for item in final_items if isinstance(item, dict)}
    added_symbols = final_symbols - existing_symbols
    added_count = len(added_symbols)

    # 把综合评分回填到新加入的观察项上，便于前端/复盘展示。
    score_by_symbol = {str(row.get("symbol")): row.get("composite_score") for row in selected}
    for item in final_items:
        if isinstance(item, dict) and item.get("symbol") in score_by_symbol and item.get("composite_score") is None:
            item["composite_score"] = score_by_symbol.get(str(item.get("symbol")))

    return {
        "ok": True,
        "status": "passed",
        "task_type": "daily_composite_watchpool",
        "generated_at": now_iso(),
        "network": False,
        "research_only": True,
        "paper_only": True,
        "is_real_order": False,
        "candidate_count": len(rows),
        "scored_count": len(ranked),
        "selected_count": len(selected),
        "added_count": added_count,
        "added_symbols": sorted(added_symbols),
        "reviewed_count": len(final_items),
        "watchlist_count": len(final_items),
        "items": final_items,
        "watchlist_path": str(watchlist_path),
        "warnings": [],
        "errors": [],
        "disclaimer": DISCLAIMER,
    }


__all__ = ["build_daily_watchpool_step", "DISCLAIMER"]
