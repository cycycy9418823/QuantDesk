"""题材/新闻/基本面/技术面四维综合评分器。

每日流水线在候选行上留下了多个互不相干的分数：技术面（sandbox 的
``long_score`` / ``board_score``）、题材（``theme_score`` 或字符串
``theme_confidence``）、新闻情绪（``news_score``）、基本面（``fundamental_score``）。
它们量纲不一，本模块用「跨截面百分位归一 + 可配置加权平均」把它们融合成一个
0–100 的 ``composite_score``。

设计约束（用户明确要求）：

* **技术面为主** —— ``technical`` 默认权重严格大于其它任一维度，且严格大于 0.5，
  使得技术面单一维度可以压过其余三维之和。
* **缺失维度自动降权归一** —— 只在存在的维度上归一化权重，缺分量的候选不会被
  不公平地打成 0（除非它一个分量都没有）。
* **本地研究用途** —— 不联网、不下单、不接实盘、不预测未来收益。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from quantdesk.time_utils import now_iso
from quantdesk.version import APP_VERSION

# 技术面为主：technical 权重严格大于其余任一维度，且 > 0.5。
DEFAULT_COMPOSITE_WEIGHTS: dict[str, float] = {
    "technical": 0.55,
    "theme": 0.20,
    "fundamental": 0.15,
    "news": 0.10,
}

# 每个维度在候选行里依次尝试的字段名。
DEFAULT_DIMENSION_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "technical": ("technical_score", "long_score", "board_score", "score", "raw_score"),
    "theme": ("theme_score",),
    "fundamental": ("fundamental_score", "value_score", "quality_score"),
    "news": ("news_score", "sentiment_score"),
}

# theme_confidence 常见为字符串标签，映射到 0–100 数值分量。
THEME_CONFIDENCE_SCORES: dict[str, float] = {
    "high": 80.0,
    "medium": 55.0,
    "mid": 55.0,
    "low": 35.0,
}

DISCLAIMER = (
    "综合评分为本地研究用途，技术面为主，缺失维度显式 NA 且不参与加权（renormalized 披露）；"
    "权重为未经 IC/统计验证的启发式设定（heuristic_unvalidated），"
    "不联网、不下单、不接实盘，也不预测未来收益。"
)

# Round 21 Part D: 权重从未经过 IC/walk-forward 统计验证，禁止在任何输出中
# 使用「最优/推荐」措辞描述该权重组合。
WEIGHTS_PROVENANCE = "heuristic_unvalidated"
WEIGHTS_NOTE = (
    "权重为启发式设定（heuristic_unvalidated），未经统计验证；"
    "仅用于本地研究排序，不构成任何权重组合的有效性结论。"
)
# Round 21 裁定：保留「存在维度上归一」而非固定分母 —— 综合评分是跨截面
# 百分位排序工具，固定分母会把缺维候选系统性压到底部、放大数据缺失惩罚；
# 代价是缺维候选分数被放大，故必须逐行输出 renormalized/coverage 披露，
# 且展示排序按 coverage 分层（低覆盖不得越级压过高覆盖）。
RENORMALIZATION_POLICY = "renormalize_present_dimensions_with_disclosure"


def _dimension_adjudication_disclosure() -> dict[str, Any]:
    """Round 22 裁决披露（延迟导入避免环依赖）。

    综合评分的输入维度大量来自生产五维评分链路，故报告必须携带该五维的
    裁决结论，防止读者把综合排序当作已验证信号。
    """

    from quantdesk.research.factor_adjudication import (
        score_dimension_adjudication_disclosure,
    )

    return score_dimension_adjudication_disclosure()


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _extract(row: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _theme_dimension(row: Mapping[str, Any]) -> float | None:
    numeric = _extract(row, DEFAULT_DIMENSION_FIELD_MAP["theme"])
    if numeric is not None:
        return numeric
    label = str(row.get("theme_confidence") or "").strip().lower()
    return THEME_CONFIDENCE_SCORES.get(label)


def score_candidate_dimensions(
    row: Mapping[str, Any],
    *,
    field_map: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, float | None]:
    """抽取单个候选的四个维度原始分（缺失为 ``None``），不做归一化。"""
    fmap = dict(field_map) if field_map else DEFAULT_DIMENSION_FIELD_MAP
    dims: dict[str, float | None] = {}
    for dimension, keys in fmap.items():
        if dimension == "theme":
            dims[dimension] = _theme_dimension(row)
        else:
            dims[dimension] = _extract(row, keys)
    return dims


def _percentile_ranks(value_by_index: Mapping[int, float | None]) -> dict[int, float]:
    """跨截面百分位 [0, 100]，并列取中点。"""
    items = [(index, value) for index, value in value_by_index.items() if value is not None]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 100.0}
    values = [value for _, value in items]
    ranks: dict[int, float] = {}
    for index, value in items:
        less = sum(1 for other in values if other < value)
        equal = sum(1 for other in values if other == value)
        ranks[index] = (less + 0.5 * equal) / n * 100.0
    return ranks


def compute_composite_scores(
    candidates: Iterable[Mapping[str, Any]],
    *,
    weights: Mapping[str, float] | None = None,
    field_map: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """返回带 ``composite_score`` / ``composite_rank`` 的候选副本列表。

    * ``composite_score``：存在维度的百分位加权平均（0–100）。
    * ``composite_components``：各存在维度的百分位值。
    * ``composite_components_used``：参与计算的维度名列表。
    * ``composite_weights_used``：在存在维度上归一化后的权重（和为 1）。
    """
    merged_weights = {**DEFAULT_COMPOSITE_WEIGHTS, **(dict(weights) if weights else {})}
    dimensions = list(DEFAULT_DIMENSION_FIELD_MAP.keys())
    enriched = [dict(candidate) for candidate in candidates]

    raw_by_dimension: dict[str, dict[int, float | None]] = {dim: {} for dim in dimensions}
    for index, candidate in enumerate(enriched):
        dims = score_candidate_dimensions(candidate, field_map=field_map)
        candidate["composite_dimensions_raw"] = {k: v for k, v in dims.items() if v is not None}
        for dim in dimensions:
            raw_by_dimension[dim][index] = dims.get(dim)

    percentiles = {dim: _percentile_ranks(values) for dim, values in raw_by_dimension.items()}

    total_weight = sum(float(w) for w in merged_weights.values() if float(w) > 0)
    for index, candidate in enumerate(enriched):
        numerator = 0.0
        denominator = 0.0
        parts: dict[str, float] = {}
        weights_used: dict[str, float] = {}
        for dim in dimensions:
            percentile = percentiles[dim].get(index)
            weight = float(merged_weights.get(dim, 0.0))
            if percentile is not None and weight > 0:
                numerator += percentile * weight
                denominator += weight
                parts[dim] = round(percentile, 2)
                weights_used[dim] = weight
        # Round 21 Part D: 缺因子不再打成 0，显式 NA + 状态字段。
        if denominator > 0:
            candidate["composite_score"] = round(numerator / denominator, 4)
            candidate["composite_score_status"] = "scored"
        else:
            candidate["composite_score"] = None
            candidate["composite_score_status"] = "no_dimensions_available"
        candidate["composite_components"] = parts
        candidate["composite_components_used"] = [dim for dim in dimensions if dim in parts]
        candidate["composite_missing_dimensions"] = [dim for dim in dimensions if dim not in parts]
        candidate["composite_coverage"] = (
            round(len(parts) / len(dimensions), 4) if dimensions else 0.0
        )
        candidate["composite_renormalized"] = bool(0 < denominator < total_weight - 1e-9)
        candidate["composite_weights_used"] = (
            {dim: round(weight / denominator, 6) for dim, weight in weights_used.items()}
            if denominator > 0
            else {}
        )

    # Round 21 Part D: 展示排序按 coverage 分层，层内按分数；
    # 低覆盖候选不得凭 renormalize 后的放大分数越级压过高覆盖候选。
    enriched.sort(
        key=lambda item: (
            float(item["composite_coverage"]),
            item["composite_score"] if item["composite_score"] is not None else -1.0,
        ),
        reverse=True,
    )
    for rank, candidate in enumerate(enriched, start=1):
        candidate["composite_rank"] = rank
    return enriched


def build_composite_candidate_scoring_report(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造 JSON 可序列化的综合评分报告负载（不写文件、不联网）。"""
    cfg = dict(config or {})
    raw_candidates = [row for row in (cfg.get("candidates") or []) if isinstance(row, dict)]
    weights = cfg.get("weights") if isinstance(cfg.get("weights"), dict) else None
    ranked = compute_composite_scores(raw_candidates, weights=weights)
    top = cfg.get("top")
    if isinstance(top, int) and top > 0:
        ranked_out = ranked[:top]
    else:
        ranked_out = ranked
    return {
        "ok": True,
        "status": "passed",
        "task_type": "composite_candidate_scoring",
        "generated_at": now_iso(),
        "app_version": APP_VERSION,
        "network": False,
        "research_only": True,
        "paper_only": True,
        "weights": {**DEFAULT_COMPOSITE_WEIGHTS, **(weights or {})},
        "weights_provenance": WEIGHTS_PROVENANCE,
        "dimension_adjudication": _dimension_adjudication_disclosure(),
        "weights_note": WEIGHTS_NOTE,
        "renormalization_policy": RENORMALIZATION_POLICY,
        "technical_primary": True,
        "scored_count": len(ranked),
        "ranked": ranked_out,
        "warnings": [],
        "errors": [],
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "DEFAULT_COMPOSITE_WEIGHTS",
    "DEFAULT_DIMENSION_FIELD_MAP",
    "THEME_CONFIDENCE_SCORES",
    "DISCLAIMER",
    "WEIGHTS_PROVENANCE",
    "WEIGHTS_NOTE",
    "RENORMALIZATION_POLICY",
    "score_candidate_dimensions",
    "compute_composite_scores",
    "build_composite_candidate_scoring_report",
]
