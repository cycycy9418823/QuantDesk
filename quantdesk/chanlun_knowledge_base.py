"""Chanlun learning knowledge base for QuantDesk.

This module deliberately stores only structured learning metadata: lesson
titles, keywords, concept summaries, dependency hints, implementation roadmap,
and risk notes.  It never persists full PDF text and never emits trading
recommendations.  The goal is to prepare future rule-research modules without
turning Chanlun observations into an unverified strategy.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from quantdesk.shared import now_text, read_json, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
CHANLUN_SOURCES_DIR = DATA_KNOWLEDGE_DIR / "sources"
CHANLUN_LESSONS_INDEX_JSON = DATA_KNOWLEDGE_DIR / "chanlun_lessons_index.json"
CHANLUN_CONCEPTS_JSON = DATA_KNOWLEDGE_DIR / "chanlun_concepts.json"
CHANLUN_RULE_ROADMAP_JSON = DATA_KNOWLEDGE_DIR / "chanlun_rule_roadmap.json"
REPORT_DIR = PROJECT_ROOT / "reports"
DISCLAIMER = "缠论模块仅用于学习和结构化研究，不构成投资建议，不接券商，不真实下单。"


LESSON_TOPIC_RULES: list[tuple[range, str, list[str], bool, str, str]] = [
    (range(1, 15), "理念与风险边界", ["学习", "风险", "市场观察"], False, "low", "reference"),
    (range(15, 25), "走势与级别", ["走势", "级别", "趋势"], True, "medium", "planned"),
    (range(25, 45), "中枢与走势类型", ["中枢", "走势类型", "连接"], True, "high", "planned"),
    (range(45, 62), "背驰与观察信号", ["背驰", "MACD", "观察信号"], True, "medium", "planned"),
    (range(62, 72), "结构识别", ["分型", "笔", "线段"], True, "high", "planned"),
    (range(72, 90), "中枢演化", ["中枢延伸", "扩张", "离开", "回抽"], True, "high", "planned"),
    (range(90, 109), "复盘与综合观察", ["复盘", "结构观察", "级别联动"], True, "medium", "planned"),
]

SPECIAL_LESSONS: dict[int, dict[str, Any]] = {
    17: {"title": "走势终完美", "keywords": ["走势", "走势终完美", "级别"], "topic_group": "走势结构"},
    29: {"title": "中枢、走势类型与买卖点的结构关系", "keywords": ["中枢", "走势类型", "观察信号"], "topic_group": "中枢"},
    30: {"title": "第一类、第二类、第三类买卖点的观察框架", "keywords": ["第一类买点", "第二类买点", "第三类买点", "观察信号"], "topic_group": "观察信号"},
    37: {"title": "背驰的再分辨", "keywords": ["趋势背驰", "盘整背驰", "MACD"], "topic_group": "背驰"},
    62: {"title": "分型、笔与线段", "keywords": ["分型", "笔", "线段"], "topic_group": "结构识别"},
    63: {"title": "替各位理理基本概念", "keywords": ["K线", "包含关系", "分型", "笔"], "topic_group": "基础结构"},
    65: {"title": "再说说分型、笔、线段", "keywords": ["顶分型", "底分型", "笔", "线段"], "topic_group": "结构识别"},
    67: {"title": "线段的划分标准", "keywords": ["线段", "特征序列", "破坏"], "topic_group": "结构识别"},
    71: {"title": "线段划分再研究", "keywords": ["线段", "笔", "结构确认"], "topic_group": "结构识别"},
    76: {"title": "逗庄家玩的一些杂史", "keywords": ["风险", "观察", "纪律"], "topic_group": "理念与风险边界"},
    83: {"title": "笔、线段与中枢的综合关系", "keywords": ["笔", "线段", "中枢"], "topic_group": "中枢"},
    92: {"title": "中枢震荡与操作级别观察", "keywords": ["中枢震荡", "级别", "观察信号"], "topic_group": "中枢演化"},
    108: {"title": "何谓底部？从月线看中期走势演化", "keywords": ["底部", "级别", "走势观察"], "topic_group": "复盘与综合观察"},
}


def _lesson_template(lesson_id: int) -> dict[str, Any]:
    topic_group = "综合学习"
    keywords = ["缠论", "结构观察"]
    programmable = False
    priority = "low"
    status = "reference"
    for lesson_range, group, group_keywords, group_programmable, group_priority, group_status in LESSON_TOPIC_RULES:
        if lesson_id in lesson_range:
            topic_group = group
            keywords = group_keywords
            programmable = group_programmable
            priority = group_priority
            status = group_status
            break
    title = f"教你炒股票第 {lesson_id:03d} 课"
    if lesson_id in SPECIAL_LESSONS:
        special = SPECIAL_LESSONS[lesson_id]
        title = special.get("title", title)
        keywords = special.get("keywords", keywords)
        topic_group = special.get("topic_group", topic_group)
        programmable = True if topic_group in {"基础结构", "结构识别", "中枢", "中枢演化", "背驰", "观察信号", "走势结构"} else programmable
        priority = "high" if topic_group in {"基础结构", "结构识别", "中枢", "中枢演化"} else priority
        status = "planned"
    return {
        "lesson_id": lesson_id,
        "title": title,
        "topic_group": topic_group,
        "keywords": keywords,
        "programmable": bool(programmable),
        "implementation_priority": priority,
        "status": status,
        "notes": "仅保存章节索引、关键词和程序化研究提示，不保存 PDF 原文。",
    }


def default_lessons_index() -> list[dict[str, Any]]:
    """Return a conservative 108-lesson metadata template."""

    return [_lesson_template(idx) for idx in range(1, 109)]


def default_concepts() -> list[dict[str, Any]]:
    """Return Chanlun concepts grouped for future rule research."""

    rows = [
        ("K线", "基础结构", True, [], ["open", "high", "low", "close"], "planned", "OHLC 是后续分型、笔、线段识别的基础输入。"),
        ("包含关系", "基础结构", True, ["K线", "OHLC"], ["open", "high", "low", "close"], "planned", "包含关系处理口径会影响分型和笔的确认。"),
        ("顶分型", "基础结构", True, ["K线", "包含关系"], ["high", "low", "close"], "planned", "分型确认存在不同严格程度，需要在回放中标记不确定区域。"),
        ("底分型", "基础结构", True, ["K线", "包含关系"], ["high", "low", "close"], "planned", "分型确认存在不同严格程度，需要在回放中标记不确定区域。"),
        ("分型", "基础结构", True, ["K线", "包含关系"], ["open", "high", "low", "close"], "planned", "不同分型定义会影响后续笔、线段和中枢识别。"),
        ("笔", "基础结构", True, ["顶分型", "底分型"], ["datetime", "high", "low"], "planned", "笔识别要处理最小间隔、包含关系和未确认结构。"),
        ("线段", "基础结构", True, ["笔"], ["笔序列"], "planned", "线段划分存在复杂边界，初版只做结构观察。"),
        ("上涨", "走势结构", True, ["笔", "线段"], ["结构方向"], "planned", "仅作为走势方向分类，不代表收益判断。"),
        ("下跌", "走势结构", True, ["笔", "线段"], ["结构方向"], "planned", "仅作为走势方向分类，不代表收益判断。"),
        ("盘整", "走势结构", True, ["笔", "线段", "中枢"], ["结构区间"], "planned", "盘整结构需要结合级别与中枢定义。"),
        ("走势类型", "走势结构", True, ["上涨", "下跌", "盘整"], ["结构序列"], "planned", "用于历史结构标注，不直接生成交易动作。"),
        ("级别", "走势结构", True, ["K线周期", "笔", "线段"], ["period", "datetime"], "planned", "级别联动容易产生未来函数，必须 walk-forward。"),
        ("走势连接", "走势结构", True, ["走势类型", "级别"], ["结构序列"], "planned", "只做连接关系记录。"),
        ("走势终完美", "走势结构", False, ["走势类型"], ["结构序列"], "research", "偏理论化，需要先完成基础结构后再研究。"),
        ("中枢", "中枢", True, ["笔", "线段"], ["结构重叠区间"], "planned", "中枢是后续延伸、扩张、离开、回抽观察的核心结构。"),
        ("中枢定义", "中枢", True, ["笔", "线段"], ["结构重叠区间"], "planned", "中枢定义口径必须可配置并可回放验证。"),
        ("中枢区间", "中枢", True, ["中枢定义"], ["high", "low"], "planned", "输出上沿、下沿和中轴。"),
        ("中枢上沿", "中枢", True, ["中枢区间"], ["price"], "planned", "用于结构观察边界，不是交易建议。"),
        ("中枢下沿", "中枢", True, ["中枢区间"], ["price"], "planned", "用于结构观察边界，不是交易建议。"),
        ("中枢中轴", "中枢", True, ["中枢区间"], ["price"], "planned", "用于震荡观察。"),
        ("中枢形成", "中枢", True, ["笔", "线段", "中枢定义"], ["结构序列"], "planned", "需要在历史回放中验证确认时点。"),
        ("中枢延伸", "中枢", True, ["中枢形成"], ["结构序列"], "planned", "延伸判断必须避免使用未来确认。"),
        ("中枢扩张", "中枢", True, ["中枢形成"], ["结构序列"], "planned", "扩张判断需要严格时间顺序。"),
        ("中枢震荡", "中枢", True, ["中枢区间"], ["price", "datetime"], "planned", "仅做区间观察。"),
        ("中枢破坏", "中枢", True, ["中枢区间", "离开"], ["price", "datetime"], "planned", "只标记结构破坏，不生成买卖建议。"),
        ("离开", "中枢", True, ["中枢区间"], ["price", "datetime"], "planned", "离开需记录发生时点。"),
        ("回抽", "中枢", True, ["离开", "中枢区间"], ["price", "datetime"], "planned", "回抽只作为结构观察。"),
        ("背驰", "背驰", True, ["走势类型", "MACD 辅助判断"], ["close", "macd", "结构力度"], "planned", "背驰只作为结构观察，需要结合级别和历史回放验证。"),
        ("趋势背驰", "背驰", True, ["走势类型", "MACD 辅助判断"], ["close", "macd", "结构力度"], "planned", "背驰观察需要区分级别，不能直接当作买卖建议。"),
        ("盘整背驰", "背驰", True, ["盘整", "MACD 辅助判断"], ["close", "macd", "结构力度"], "planned", "仅作为观察信号候选。"),
        ("MACD 辅助判断", "背驰", True, ["close"], ["dif", "dea", "macd"], "planned", "MACD 只能辅助结构观察，不能单独定性。"),
        ("小级别背驰", "背驰", True, ["级别", "趋势背驰"], ["period", "macd"], "research", "多级别联动需要防未来函数。"),
        ("级别联动", "背驰", True, ["级别", "走势类型"], ["period", "结构序列"], "research", "多级别联动需要单独回放验证。"),
        ("第一类买点", "观察信号", True, ["趋势背驰", "走势类型"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
        ("第二类买点", "观察信号", True, ["第一类买点", "回抽"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
        ("第三类买点", "观察信号", True, ["中枢区间", "离开", "回抽"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
        ("第一类卖点", "观察信号", True, ["趋势背驰", "走势类型"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
        ("第二类卖点", "观察信号", True, ["第一类卖点", "回抽"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
        ("第三类卖点", "观察信号", True, ["中枢区间", "离开", "回抽"], ["结构观察"], "planned", "在 QuantDesk 中仅标记为观察信号，不代表买卖建议。"),
    ]
    return [
        {
            "concept": concept,
            "category": category,
            "programmable": programmable,
            "depends_on": depends_on,
            "possible_inputs": possible_inputs,
            "implementation_status": status,
            "risk_note": risk_note,
        }
        for concept, category, programmable, depends_on, possible_inputs, status, risk_note in rows
    ]


def default_rule_roadmap() -> list[dict[str, Any]]:
    return [
        {
            "stage": 1,
            "version": "v3.0.1-chanlun-knowledge-base-and-rule-roadmap",
            "title": "知识库与规则索引",
            "goal": "整理章节、概念和可程序化路线，不生成交易信号。",
            "outputs": ["chanlun_lessons_index.json", "chanlun_concepts.json", "chanlun_rule_roadmap.json"],
            "status": "implemented",
        },
        {
            "stage": 2,
            "version": "v3.0.2-chanlun-fractal-engine",
            "title": "分型识别引擎",
            "goal": "输入 OHLC，识别顶分型 / 底分型，处理包含关系，输出结构点，不做交易判断。",
            "outputs": ["fractal_points"],
            "status": "planned",
        },
        {
            "stage": 3,
            "version": "v3.0.3-chanlun-bi-engine",
            "title": "笔识别引擎",
            "goal": "基于分型识别笔，输出起点、终点、方向、力度，标记不确定区域。",
            "outputs": ["bi_segments"],
            "status": "planned",
        },
        {
            "stage": 4,
            "version": "v3.0.4-chanlun-segment-engine",
            "title": "线段识别引擎",
            "goal": "基于笔识别线段，输出方向、起止点和破坏状态，不直接交易。",
            "outputs": ["segments"],
            "status": "planned",
        },
        {
            "stage": 5,
            "version": "v3.0.5-chanlun-zhongshu-engine",
            "title": "中枢识别引擎",
            "goal": "基于笔或线段识别中枢，输出上沿、下沿、中轴，识别延伸、扩张、离开、回抽。",
            "outputs": ["zhongshu_ranges"],
            "status": "planned",
        },
        {
            "stage": 6,
            "version": "v3.0.6-chanlun-divergence-observation",
            "title": "背驰观察",
            "goal": "结合走势结构和 MACD，输出背驰观察，不作为买卖建议。",
            "outputs": ["divergence_observations"],
            "status": "planned",
        },
        {
            "stage": 7,
            "version": "v3.0.7-chanlun-replay-validation",
            "title": "历史回放验证",
            "goal": "将观察信号放入历史回放，统计信号后走势表现，不承诺收益，不真实下单。",
            "outputs": ["replay_validation_report"],
            "status": "planned",
        },
    ]


def _pdf_metadata(source_pdf: Path | None) -> dict[str, Any]:
    if not source_pdf:
        return {"exists": False, "path": ""}
    target = Path(source_pdf)
    payload: dict[str, Any] = {"exists": target.exists(), "path": str(target)}
    if not target.exists():
        payload["message"] = "PDF 不存在，使用内置 108 课模板生成知识库。"
        return payload
    payload["message"] = "PDF 已登记；默认不保存全文，仅尝试读取元数据。"
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(target))
        payload["page_count"] = len(reader.pages)
        metadata = reader.metadata or {}
        payload["title"] = str(metadata.get("/Title") or target.stem)
        outline = getattr(reader, "outline", None) or []
        payload["outline_count"] = len(outline) if isinstance(outline, list) else 0
    except Exception as exc:  # noqa: BLE001
        payload["warning"] = f"未读取 PDF 元数据：{exc}"
        payload["title"] = target.stem
    return payload


def build_chanlun_knowledge_base(
    *,
    source_pdf: str | Path | None = None,
    output_dir: str | Path = DATA_KNOWLEDGE_DIR,
    report: bool = True,
    no_fulltext: bool = True,
    write: bool = True,
) -> dict[str, Any]:
    """Build the structured Chanlun learning knowledge base."""

    output = Path(output_dir)
    lessons_path = output / "chanlun_lessons_index.json"
    concepts_path = output / "chanlun_concepts.json"
    roadmap_path = output / "chanlun_rule_roadmap.json"
    generated_at = now_text()
    lessons = default_lessons_index()
    concepts = default_concepts()
    roadmap = default_rule_roadmap()
    pdf_info = _pdf_metadata(Path(source_pdf) if source_pdf else None)

    payload = {
        "ok": True,
        "schema_version": "3.0.1",
        "task_type": "chanlun_knowledge_base",
        "generated_at": generated_at,
        "source_pdf": pdf_info,
        "no_fulltext": bool(no_fulltext),
        "lesson_count": len(lessons),
        "concept_count": len(concepts),
        "programmable_count": sum(1 for row in concepts if row.get("programmable")),
        "implementation_status": "knowledge_index_ready",
        "paths": {
            "lessons_index": str(lessons_path),
            "concepts": str(concepts_path),
            "rule_roadmap": str(roadmap_path),
        },
        "lessons_index": lessons,
        "concepts": concepts,
        "rule_roadmap": roadmap,
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_json(lessons_path, {
            "schema_version": "3.0.1",
            "generated_at": generated_at,
            "source_pdf": pdf_info,
            "no_fulltext": bool(no_fulltext),
            "lessons": lessons,
            "disclaimer": DISCLAIMER,
        })
        write_json(concepts_path, {
            "schema_version": "3.0.1",
            "generated_at": generated_at,
            "concepts": concepts,
            "disclaimer": DISCLAIMER,
        })
        write_json(roadmap_path, {
            "schema_version": "3.0.1",
            "generated_at": generated_at,
            "roadmap": roadmap,
            "disclaimer": DISCLAIMER,
        })
        if report:
            report_path = write_chanlun_report(payload)
            payload["paths"]["report"] = str(report_path)
    return payload


def latest_chanlun_knowledge() -> dict[str, Any]:
    lessons = read_json(CHANLUN_LESSONS_INDEX_JSON, default={}) or {}
    concepts = read_json(CHANLUN_CONCEPTS_JSON, default={}) or {}
    roadmap = read_json(CHANLUN_RULE_ROADMAP_JSON, default={}) or {}
    lesson_rows = lessons.get("lessons") if isinstance(lessons, dict) else []
    concept_rows = concepts.get("concepts") if isinstance(concepts, dict) else []
    roadmap_rows = roadmap.get("roadmap") if isinstance(roadmap, dict) else []
    exists = CHANLUN_LESSONS_INDEX_JSON.exists() and CHANLUN_CONCEPTS_JSON.exists() and CHANLUN_RULE_ROADMAP_JSON.exists()
    return {
        "ok": True,
        "exists": exists,
        "schema_version": "3.0.1",
        "lesson_count": len(lesson_rows or []),
        "concept_count": len(concept_rows or []),
        "programmable_count": sum(1 for row in (concept_rows or []) if isinstance(row, dict) and row.get("programmable")),
        "implementation_status": "knowledge_index_ready" if exists else "not_built",
        "lessons_index": lesson_rows or [],
        "concepts": concept_rows or [],
        "rule_roadmap": roadmap_rows or [],
        "paths": {
            "lessons_index": str(CHANLUN_LESSONS_INDEX_JSON),
            "concepts": str(CHANLUN_CONCEPTS_JSON),
            "rule_roadmap": str(CHANLUN_RULE_ROADMAP_JSON),
        },
        "disclaimer": DISCLAIMER,
    }


def chanlun_knowledge_status() -> dict[str, Any]:
    latest = latest_chanlun_knowledge()
    source_default = CHANLUN_SOURCES_DIR / "chanlun_108.pdf"
    latest.update({
        "source_pdf_default_path": str(source_default),
        "source_pdf_exists": source_default.exists(),
        "message": "缠论知识库已生成。" if latest.get("exists") else "缠论知识库尚未生成；GET 只读，不解析 PDF。请使用 POST /api/chanlun/knowledge/build 或 CLI 生成。",
    })
    return latest


def write_chanlun_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(payload.get("generated_at") or now_text()).replace(":", "").replace("-", "").replace("T", "_")
    report_path = REPORT_DIR / f"chanlun_knowledge_summary_{stamp}.md"
    lessons = payload.get("lessons_index") or []
    concepts = payload.get("concepts") or []
    roadmap = payload.get("rule_roadmap") or []
    high_lessons = [row for row in lessons if row.get("implementation_priority") == "high"][:12]
    lines = [
        "# 缠论知识库摘要",
        "",
        "本报告使用本地结构化模板和可选 PDF 元数据生成；不保存 PDF 全文，不展示大段原文。",
        "",
        DISCLAIMER,
        "",
        "买卖点在 QuantDesk 中仅作为观察信号，不代表买卖建议。",
        "",
        "## 摘要",
        "",
        f"- 章节索引：{payload.get('lesson_count', 0)} 课",
        f"- 核心概念：{payload.get('concept_count', 0)} 个",
        f"- 可程序化概念：{payload.get('programmable_count', 0)} 个",
        "- 当前阶段：知识库与规则索引；不生成交易信号。",
        "",
        "## 高优先级章节线索",
        "",
    ]
    for row in high_lessons:
        lines.append(f"- 第 {row.get('lesson_id')} 课：{row.get('title')}；关键词：{', '.join(row.get('keywords') or [])}")
    lines.extend(["", "## 概念分层", ""])
    for category in ["基础结构", "走势结构", "中枢", "背驰", "观察信号"]:
        names = [row.get("concept") for row in concepts if row.get("category") == category]
        lines.append(f"- {category}：{', '.join(str(name) for name in names)}")
    lines.extend(["", "## 实现路线图", ""])
    for row in roadmap:
        lines.append(f"- 阶段 {row.get('stage')}：{row.get('title')}（{row.get('version')}）- {row.get('goal')}")
    lines.extend([
        "",
        "## 风险提示",
        "",
        "- 本模块只做学习资料登记、概念索引和规则研究路线图。",
        "- 后续任何结构识别都需要 walk-forward 历史验证。",
        "- 不接券商、不真实下单、不保存真实账户信息、不构成投资建议。",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def latest_chanlun_report_html() -> str:
    files = sorted(REPORT_DIR.glob("chanlun_knowledge_summary_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "<!doctype html><meta charset='utf-8'><p>尚未生成缠论学习报告。</p>"
    text = files[0].read_text(encoding="utf-8", errors="ignore")
    return "<!doctype html><meta charset='utf-8'><pre style='white-space:pre-wrap;font-family:system-ui'>" + html.escape(text) + "</pre>"
