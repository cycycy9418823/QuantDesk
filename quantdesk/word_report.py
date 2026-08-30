from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from quantdesk.backtest import BacktestMetrics, default_backtest_assumptions
from quantdesk.reporting import audit_file_name, fmt_pct, fmt_value, risk_summary_dict, risk_summary_note_lines


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), fill)
    tc_pr.append(shading)


def set_cell_text(cell, text: str, bold: bool = False, color: str | None = None) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.size = Pt(9)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def add_note_box(document: Document, title: str, lines: list[str], fill: str = "EEF6FF") -> None:
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    cell = table.cell(0, 0)
    set_cell_shading(cell, fill)
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(4)
    title_run = p.add_run(title)
    title_run.bold = True
    title_run.font.size = Pt(10.5)
    title_run.font.color.rgb = RGBColor(31, 78, 121)
    for line in lines:
        para = cell.add_paragraph()
        para.paragraph_format.space_after = Pt(2)
        para.add_run(line).font.size = Pt(9.5)


def add_indicator_table(document: Document, row: pd.Series, indicator_rows) -> None:
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["指标名", "当前读数", "这只票怎么理解"]
    for idx, header in enumerate(headers):
        set_cell_text(table.rows[0].cells[idx], header, bold=True, color="FFFFFF")
        set_cell_shading(table.rows[0].cells[idx], "2F5597")
    for name, value, meaning in indicator_rows(row):
        cells = table.add_row().cells
        values = [name, value, meaning]
        for idx, text in enumerate(values):
            set_cell_text(cells[idx], text)
            if idx == 2:
                cells[idx].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT


def add_trade_checklist_table(document: Document, row: pd.Series, temp: dict, checklist_rows) -> None:
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["检查项", "结果", "解释"]
    for idx, header in enumerate(headers):
        set_cell_text(table.rows[0].cells[idx], header, bold=True, color="FFFFFF")
        set_cell_shading(table.rows[0].cells[idx], "1F4E79")
    for item, status, detail in checklist_rows(row, temp):
        cells = table.add_row().cells
        values = [item, status, detail]
        for idx, text in enumerate(values):
            color = None
            if idx == 1 and status == "通过":
                color = "05825F"
            elif idx == 1 and status == "谨慎":
                color = "B66A00"
            elif idx == 1 and status == "否决":
                color = "C2413B"
            set_cell_text(cells[idx], text, bold=(idx == 1), color=color)
            if idx == 2:
                cells[idx].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT


def add_doc_heading(document: Document, text: str, level: int = 1) -> None:
    paragraph = document.add_heading(text, level=level)
    for run in paragraph.runs:
        run.font.name = "Microsoft YaHei"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        run.font.color.rgb = RGBColor(31, 78, 121)


def build_word_report(
    features: pd.DataFrame,
    spot: pd.DataFrame,
    metrics: BacktestMetrics,
    args: argparse.Namespace,
    report_date: str,
    backtest_files: dict[str, Path] | None = None,
    report_dir: Path | None = None,
    helpers: dict[str, Callable] | None = None,
) -> Path:
    report_dir = report_dir or Path("reports")
    helpers = helpers or {}

    def h(name: str):
        return helpers[name]

    if "data_invalid" in features.columns:
        features = features[~features["data_invalid"].fillna(False)].copy()
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.55)
    section.right_margin = Inches(0.55)

    styles = document.styles
    styles["Normal"].font.name = "Microsoft YaHei"
    styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    styles["Normal"].font.size = Pt(10)

    summary = h("market_summary")(spot)
    temperature = h("market_temperature")(spot, features)
    watch = h("make_watchlist")(features, args.top_n)
    latest_data_date = features["date"].max() if not features.empty else "N/A"
    assumptions = metrics.assumptions or default_backtest_assumptions()
    backtest_files = backtest_files or {}
    risk_summary = risk_summary_dict(assumptions.get("backtest_risk_summary", {}))
    data_levels = features.get("data_warning_level", pd.Series("OK", index=features.index)).astype(str).str.upper()
    data_risks = features.get("data_risk_flag", pd.Series("", index=features.index)).astype(str)
    data_summary = {
        "total": len(features),
        "error": int(data_levels.eq("ERROR").sum()),
        "warning": int(data_levels.eq("WARNING").sum()),
    }
    today_brief = h("build_today_decision_brief")(temperature, data_summary, risk_summary, watch)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("A股每日量化观察报告")
    run.bold = True
    run.font.size = Pt(20)
    run.font.color.rgb = RGBColor(31, 78, 121)
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.add_run(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}    数据日期：{latest_data_date}    价格上限：{args.max_price} 元").font.size = Pt(10)

    add_note_box(document, "Report Navigation / 报告目录", h("report_toc_lines")(), fill="F7FAFC")
    add_note_box(
        document,
        "今日阅读指南",
        [
            "先检查 Data Quality：是否出现 WARNING / ERROR，以及原因是否只是休市、等待收盘或样例演示。",
            "再看候选股准备状态：是否只是观察、虚拟验证，还是进入深度复核。",
            "留意市场日历提示：今天是否为休市日、盘中估算或等待行情源更新。",
            "可信度评分不代表价格方向预测，准备状态不是买卖建议；本报告仅用于辅助观察和复盘。",
        ],
        fill="F7FAFC",
    )
    add_note_box(
        document,
        "Today Decision Brief / 今日操作简报",
        h("today_decision_brief_lines")(today_brief),
        fill="F0F7FF",
    )

    add_note_box(
        document,
        "先看这几条",
        [
            "这是一份研究报告，不是交易指令；任何行动前都要先核对风险。",
            "长期候选看趋势和风控，打板候选只适合短线观察和虚拟验证。",
            "本版新增：股价位置、最近新闻、反面风险。看不懂或不符合条件时，宁可不买。",
            "新手优先学会：日K均线、MACD、成交量、风险阈值观察、新闻验证。",
        ],
        fill="EAF4F4",
    )

    add_doc_heading(document, "一、值得关注股票总览", 1)
    meta = document.add_paragraph()
    meta.add_run(
        f"本次样本 {len(features)} 只，长期候选 {(features['long_score'] >= args.score_threshold).sum()} 只，"
        f"打板候选 {(features['board_score'] >= args.score_threshold).sum()} 只；市场状态：{summary['regime']}。"
    )
    add_note_box(document, "市场温度仪表盘", h("market_temperature_notes")(temperature), fill="EEF6FF")
    add_note_box(
        document,
        "Data Quality / 数据质量",
        [
            f"Displayed names: {len(features)}",
            f"ERROR rows: {int(data_levels.eq('ERROR').sum())}",
            f"WARNING rows: {int(data_levels.eq('WARNING').sum())}",
            f"INFO/fallback rows: {int(data_levels.eq('INFO').sum())}",
            "Risk flags: " + ("; ".join(sorted(set(flag for flag in data_risks if flag))) or "none"),
        ],
        fill="FFF7E6",
    )
    add_note_box(
        document,
        "Backtest Risk Summary / 回测风险摘要",
        risk_summary_note_lines(risk_summary),
        fill="FFEFEF",
    )

    table = document.add_table(rows=1, cols=9)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["排名", "代码", "名称", "类型", "评分", "位置", "消息", "风险", "操作倾向"]
    for idx, header in enumerate(headers):
        cell = table.rows[0].cells[idx]
        set_cell_text(cell, header, bold=True, color="FFFFFF")
        set_cell_shading(cell, "1F4E79")

    for rank, (_, row) in enumerate(watch.iterrows(), start=1):
        cells = table.add_row().cells
        values = [
            str(rank),
            h("stock_code")(row["code"]),
            str(row["name"]),
            str(row["attention_type"]),
            fmt_value(row["watch_score"], 1),
            h("position_summary")(row),
            str(row.get("news_badge", "-")),
            f"{fmt_value(row['risk_pct'])}%" + (f"；{row.get('data_risk_flag')}" if row.get("data_risk_flag") else ""),
            h("brief_action")(row),
        ]
        for idx, value in enumerate(values):
            set_cell_text(cells[idx], value)
            if idx == 8:
                cells[idx].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
        if rank % 2 == 0:
            for cell in cells:
                set_cell_shading(cell, "F7FAFC")

    document.add_page_break()
    add_doc_heading(document, "二、Top 候选决策卡片", 1)
    watch_rows = list(watch.iterrows())
    max_card_count = int(getattr(args, "decision_card_top_n", 5) or 0)
    max_card_bullets = int(getattr(args, "max_card_bullets", 3) or 3)
    for rank, (_, row) in enumerate(watch_rows, start=1):
        if rank > max_card_count:
            break
        add_doc_heading(document, f"{rank}. {h('stock_code')(row['code'])} {row['name']}：{row['attention_type']}", 2)
        card = h("build_candidate_decision_card")(row)
        p = document.add_paragraph()
        p.add_run("Decision Card / 决策卡片：").bold = True
        p.add_run(f"状态：{card['action_label']}；数据：{card['data_status']}。")
        p = document.add_paragraph()
        p.add_run("可信度与小白准备状态：").bold = True
        p.add_run(
            f"策略原始分 {card.get('raw_score', 'N/A')}；"
            f"可信度 {card.get('trust_score', 'N/A')}；"
            f"等级 {card.get('trust_level', 'N/A')}；"
            f"准备状态 {card.get('action_state', 'N/A')}。"
        )
        p = document.add_paragraph()
        p.add_run("可信度提示：").bold = True
        p.add_run(card.get("trust_disclaimer", "可信度评分不代表价格方向预测，不构成投资建议。"))
        p = document.add_paragraph()
        p.add_run("小白解释：").bold = True
        p.add_run("；".join(str(item) for item in card.get("beginner_explanation", [])[:max_card_bullets]))
        for label, key in [
            ("为什么关注", "why_watch"),
            ("主要风险", "key_risks"),
            ("Review Checklist / 复核前检查清单", "pre_trade_checklist"),
            ("Strategy Invalidation Conditions / 策略失效条件", "invalid_conditions"),
        ]:
            p = document.add_paragraph()
            p.add_run(f"{label}：").bold = True
            p.add_run("；".join(str(item) for item in card[key][:max_card_bullets]))
        p = document.add_paragraph()
        p.add_run("提醒：").bold = True
        p.add_run(card["review_note"])

        detail_table = document.add_table(rows=1, cols=6)
        detail_table.style = "Table Grid"
        for idx, header in enumerate(["长期分", "打板分", "收盘价", "风险阈值", "成交额", "离MA20"]):
            set_cell_text(detail_table.rows[0].cells[idx], header, bold=True, color="FFFFFF")
            set_cell_shading(detail_table.rows[0].cells[idx], "2F5597")
        cells = detail_table.add_row().cells
        values = [
            fmt_value(row.get("long_score"), 1),
            fmt_value(row.get("board_score"), 1),
            fmt_value(row.get("close")),
            fmt_value(row.get("stop")),
            f"{fmt_value(row.get('amount_yi'))} 亿元",
            f"{fmt_value(row.get('dist_ma20_pct'))}%",
        ]
        for idx, value in enumerate(values):
            set_cell_text(cells[idx], value)
        if rank < len(watch_rows):
            document.add_page_break()

    document.add_page_break()
    add_doc_heading(document, "三、固定风控规则", 1)
    for line in [
        "长期：先用低暴露纸面情景观察，符合条件再复核；不要把报告当作真实配置指令。",
        "打板：只做短线纸面观察，次日不强要复核风险；炸板、放巨量、高开低走都进入反证清单。",
        "风险：报告里的风险阈值是观察线，触发后应复核规则，不要用“再等等”替代研究纪律。",
        "新闻：看到利好先问三个问题：是不是公告原文？股价是否已经大涨？有没有负面反证？",
        "学习：每天选1-2只，用同花顺日K复盘均线、MACD、成交量、位置和新闻，不要同时跟太多股票。",
    ]:
        paragraph = document.add_paragraph()
        paragraph.add_run("• ").bold = True
        paragraph.add_run(line)

    if bool(getattr(args, "include_learning_appendix", True)):
        document.add_page_break()
        appendix = h("build_learning_appendix")(
            watch,
            int(getattr(args, "learning_appendix_max_items", 20) or 20),
        )
        for raw_line in appendix.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("## "):
                add_doc_heading(document, line.replace("## ", ""), 1)
            elif line.startswith("### "):
                add_doc_heading(document, line.replace("### ", ""), 2)
            elif line.startswith("- "):
                bullet = document.add_paragraph(style=None)
                bullet.paragraph_format.left_indent = Inches(0.22)
                bullet.paragraph_format.first_line_indent = Inches(-0.12)
                bullet.add_run("• ").bold = True
                bullet.add_run(line[2:])
            else:
                document.add_paragraph(line)

    add_doc_heading(document, "四、Backtest Audit / 回测审计", 1)
    def audit_name(key: str) -> str:
        return audit_file_name(backtest_files, key)

    document.add_paragraph(
        f"简化趋势规则回测：总收益 {fmt_pct(metrics.total_return)}，年化 {fmt_pct(metrics.annual_return)}，"
        f"最大回撤 {fmt_pct(metrics.max_drawdown)}，夏普 {fmt_value(metrics.sharpe)}，"
        f"成交次数 {metrics.number_of_trades}，期末权益 {fmt_value(metrics.final_equity)}。"
        "本轮已计入基础手续费、卖出印花税、滑点和部分无法成交约束，但仍不能等同于真实收益。"
    )
    p = document.add_paragraph()
    p.add_run("Portfolio mode: ").bold = True
    p.add_run(str(assumptions.get("portfolio_mode", "independent average")))
    add_note_box(
        document,
        "Backtest Audit / Portfolio mode / 组合回测设置",
        [
            f"Backtest mode: {assumptions.get('backtest_mode', 'portfolio')}",
            f"Portfolio mode: {assumptions.get('portfolio_mode', 'independent average')}",
            f"Initial cash: {fmt_value(assumptions.get('initial_cash'))}",
            f"Max positions: {assumptions.get('max_positions', 'not applicable')}",
            f"Max position pct: {fmt_pct(assumptions.get('max_position_pct'))}",
            f"Max turnover per rebalance: {assumptions.get('max_turnover_per_rebalance', 'not limited')}; implemented: {assumptions.get('turnover_limit_implemented', False)}",
            f"Rebalance rule: {assumptions.get('rebalance_rule', 'not implemented')}",
            f"Ranking rule: {assumptions.get('ranking_rule', 'not applicable')}",
            f"Signal timing: {assumptions.get('signal_timing', 'close')}; execution timing: {assumptions.get('execution_timing', 'T+1 next trading row')}; execution price: {assumptions.get('execution_price', 'not implemented')}",
            f"Costs: commission {assumptions.get('commission_rate')}, min {assumptions.get('min_commission')}, stamp duty {assumptions.get('stamp_duty_rate')}, slippage bps {assumptions.get('slippage_bps')}",
            f"Limit-up / limit-down rule: {assumptions.get('limit_up_rule', 'not implemented')} / {assumptions.get('limit_down_rule', 'not implemented')}",
            f"Suspension rule: {assumptions.get('suspension_rule', 'not implemented')}",
            f"Final equity: {fmt_value(metrics.final_equity)}; Total return: {fmt_pct(metrics.total_return)}; Max drawdown: {fmt_pct(metrics.max_drawdown)}",
            f"Benchmark return: {fmt_pct(metrics.benchmark_return)}; Excess return: {fmt_pct(metrics.excess_return)}",
            f"Trades file: {audit_name('trades')}; Equity curve file: {audit_name('equity_curve')}",
            f"Positions file: {audit_name('positions')}; Rebalance log file: {audit_name('rebalance_log')}; Blocked trades file: {audit_name('blocked_trades')}",
            f"Risk summary file: {audit_name('risk_summary')}",
            f"Summary file: {audit_name('summary')}; Assumptions file: {audit_name('assumptions')}",
            str(assumptions.get("warning", "历史回测不代表未来收益。")),
            "历史回测不代表未来收益；组合回测仍未模拟盘口排队、部分成交和真实撮合深度。",
        ],
        fill="FFF7E6",
    )

    add_doc_heading(document, "Risk Statement / 风险声明", 1)
    document.add_paragraph("本报告只用于研究和辅助复盘，不构成投资建议，也不会自动下单。")
    document.add_paragraph("评分、新闻、技术指标和历史回测都可能失效；实际操作前应自行核对行情、公告、仓位和止损规则。")
    docx_path = report_dir / f"{report_date}_quant_report.docx"
    document.save(docx_path)
    logging.info("Saved Word report: %s", docx_path)
    return docx_path
