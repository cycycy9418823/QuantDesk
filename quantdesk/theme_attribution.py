"""Local, evidence-first theme attribution helpers.

The helpers here do not fetch network data. They turn existing local mappings,
provider industry labels, and cached news/announcement titles into a visible
evidence chain for research review.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

UNKNOWN_VALUES = {
    "",
    "-",
    "nan",
    "none",
    "null",
    "未知",
    "未知行业",
    "未知板块",
    "未知题材",
    "未知概念",
    "待分类",
}

KEYWORD_THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AI算力", ("算力", "人工智能", "ai", "大模型", "数据中心", "服务器", "gpu")),
    ("光模块/CPO", ("光模块", "cpo", "硅光", "光通信", "光器件")),
    ("PCB/元件", ("pcb", "印制电路", "覆铜板", "电子元件", "元器件")),
    ("半导体", ("半导体", "芯片", "晶圆", "封测", "存储", "集成电路")),
    ("机器人", ("机器人", "机器视觉", "减速器", "伺服", "人形")),
    ("汽车零部件", ("汽车", "零部件", "智能驾驶", "无人驾驶", "车载", "新能源车")),
    ("新能源", ("光伏", "储能", "锂电", "电池", "风电", "新能源")),
    ("稀土/小金属", ("稀土", "钨", "钼", "锑", "钴", "锂", "小金属")),
    ("贵金属", ("黄金", "白银", "贵金属", "金矿")),
    ("化工涨价", ("涨价", "化工", "原料", "树脂", "pvc", "烧碱")),
    ("消费电子", ("消费电子", "手机", "折叠屏", "耳机", "穿戴")),
    ("文化传媒", ("影视", "传媒", "电影", "电视剧", "游戏", "短剧", "版权")),
    ("低空经济", ("低空", "无人机", "通航", "飞行器")),
    ("军工", ("军工", "航天", "航空", "导弹", "卫星")),
    ("证券金融", ("证券", "券商", "金融", "并购重组", "资本市场")),
    ("银行", ("银行", "息差", "存款", "贷款")),
    ("地产链", ("房地产", "地产", "物业", "城中村", "保障房")),
    ("电力能源", ("电力", "火电", "水电", "核电", "煤电", "电网")),
    ("公告事件", ("收购", "重组", "并购", "股权转让", "实际控制人", "签订合同", "中标")),
    ("经营风险提示", ("减持", "质押", "诉讼", "处罚", "问询函", "风险提示")),
)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in UNKNOWN_VALUES else text


def _is_meaningful(value: Any) -> bool:
    return bool(_clean_text(value))


def _split_tags(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    parts = []
    for chunk in text.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",").split(","):
        item = _clean_text(chunk)
        if item and item not in parts:
            parts.append(item)
    return parts


def _news_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        _clean_text(item.get(key))
        for key in ("title", "content", "summary")
        if _clean_text(item.get(key))
    )


def _keyword_hits(news_items: list[Mapping[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in news_items:
        text = _news_text(item)
        lower = text.lower()
        if not text:
            continue
        for theme, keywords in KEYWORD_THEME_RULES:
            matched = [keyword for keyword in keywords if keyword.lower() in lower]
            if not matched:
                continue
            key = (theme, text[:80])
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                {
                    "source_type": "announcement_keyword",
                    "source_name": _clean_text(item.get("source")) or "本地公告/新闻缓存",
                    "theme": theme,
                    "keywords": matched[:4],
                    "evidence_text": text[:120],
                    "published": _clean_text(item.get("published")),
                    "url": _clean_text(item.get("url")),
                    "confidence": "medium",
                }
            )
            if len(hits) >= limit:
                return hits
    return hits


def _source_label(source_type: str) -> str:
    return {
        "manual_mapping": "本地映射",
        "mapping_import": "导入草稿",
        "provider_industry": "行业线索",
        "announcement_keyword": "公告/新闻",
    }.get(source_type, source_type or "未知来源")


def build_theme_attribution(
    *,
    symbol: str,
    name: str = "",
    map_item: Mapping[str, Any] | None = None,
    raw_industry: str = "",
    news_items: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build traceable theme attribution from local evidence only."""

    map_item = map_item or {}
    news_items = news_items or []
    sources: list[dict[str, Any]] = []

    mapped_primary = _clean_text(map_item.get("primary_theme"))
    mapped_sector = _clean_text(map_item.get("sector"))
    mapped_industry = _clean_text(map_item.get("industry"))
    mapped_concepts = _split_tags(map_item.get("concept_tags"))
    mapped_themes = _split_tags(map_item.get("theme_tags"))
    map_source = _clean_text(map_item.get("source")) or "stock_theme_map.csv"

    if mapped_primary or mapped_sector or mapped_industry or mapped_concepts or mapped_themes:
        source_type = "mapping_import" if "import" in map_source else "manual_mapping"
        sources.append(
            {
                "source_type": source_type,
                "source_label": _source_label(source_type),
                "source_name": map_source,
                "theme": mapped_primary or (mapped_themes[0] if mapped_themes else mapped_industry or mapped_sector),
                "evidence_text": _clean_text(map_item.get("notes")) or "本地题材映射表命中。",
                "published": _clean_text(map_item.get("updated_at")),
                "confidence": "high" if source_type == "manual_mapping" else "medium",
            }
        )

    keyword_sources = _keyword_hits(news_items)
    for source in keyword_sources:
        source["source_label"] = _source_label(str(source.get("source_type") or ""))
    sources.extend(keyword_sources)

    provider_industry = _clean_text(raw_industry)
    if provider_industry:
        sources.append(
            {
                "source_type": "provider_industry",
                "source_label": _source_label("provider_industry"),
                "source_name": "涨停池所属行业字段",
                "theme": provider_industry,
                "evidence_text": f"涨停池返回所属行业：{provider_industry}。",
                "published": "",
                "confidence": "low",
            }
        )

    concept_tags = []
    for value in [*mapped_concepts, *mapped_themes, *(source.get("theme", "") for source in keyword_sources)]:
        clean = _clean_text(value)
        if clean and clean not in concept_tags:
            concept_tags.append(clean)

    primary_theme = mapped_primary
    theme_confidence = "mapped" if primary_theme else ""
    if not primary_theme and keyword_sources:
        primary_theme = _clean_text(keyword_sources[0].get("theme"))
        theme_confidence = "announcement_keyword"
    if not primary_theme and provider_industry:
        primary_theme = provider_industry
        theme_confidence = "provider_industry"
    if not primary_theme:
        primary_theme = "未知题材"
        theme_confidence = "unknown"

    industry = mapped_industry or provider_industry or "未知行业"
    sector = mapped_sector or industry or "未知板块"
    if not concept_tags and primary_theme != "未知题材":
        concept_tags.append(primary_theme)

    summary_parts = []
    for source in sources[:3]:
        label = str(source.get("source_label") or _source_label(str(source.get("source_type") or "")))
        theme = _clean_text(source.get("theme")) or "线索待核验"
        summary_parts.append(f"{label}:{theme}")
    summary = "；".join(summary_parts) if summary_parts else "暂缺可追溯题材线索。"

    return {
        "symbol": symbol,
        "name": name,
        "sector": sector,
        "industry": industry,
        "primary_theme": primary_theme,
        "concept_tags": ",".join(concept_tags[:8]) if concept_tags else "未知概念",
        "theme_tags": ",".join([primary_theme, *concept_tags][:8]) if primary_theme != "未知题材" else "未知题材",
        "theme_confidence": theme_confidence,
        "attribution_sources": sources[:6],
        "attribution_summary": summary,
        "traceable": bool(sources),
        "has_news_evidence": any(source.get("source_type") == "announcement_keyword" for source in sources),
        "has_mapped_evidence": any(source.get("source_type") in {"manual_mapping", "mapping_import"} for source in sources),
        "provider_only": bool(sources) and all(source.get("source_type") == "provider_industry" for source in sources),
    }


def summarize_attribution_sources(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    source_counter: Counter[str] = Counter()
    traceable_count = 0
    news_count = 0
    mapped_count = 0
    provider_only_count = 0
    unknown_count = 0
    for row in rows:
        sources = row.get("attribution_sources") if isinstance(row.get("attribution_sources"), list) else []
        if sources:
            traceable_count += 1
            source_counter.update(str(item.get("source_type") or "unknown") for item in sources if isinstance(item, Mapping))
        else:
            unknown_count += 1
        if row.get("has_news_evidence"):
            news_count += 1
        if row.get("has_mapped_evidence"):
            mapped_count += 1
        if row.get("provider_only"):
            provider_only_count += 1
    return {
        "traceable_count": traceable_count,
        "news_evidence_count": news_count,
        "mapped_count": mapped_count,
        "provider_only_count": provider_only_count,
        "unknown_count": unknown_count,
        "source_counts": dict(source_counter),
        "message": (
            f"{traceable_count} 只具备可追溯线索；"
            f"{news_count} 只命中公告/新闻关键词；"
            f"{provider_only_count} 只仍主要依赖行业线索。"
        ),
    }
