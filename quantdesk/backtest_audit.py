"""Strategy audit report generation for QuantDesk.

Generates a Markdown report summarising walk-forward results,
data quality, backtest quality, and risk disclaimers.

All reports are for local research reference only and do not
constitute investment advice.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from quantdesk.backtest_metrics import WalkForwardSummary
from quantdesk.backtest_quality import BacktestQuality, evaluate_backtest_quality

DISCLAIMER = (
    "本报告由 QuantDesk 自动生成，仅用于本地研究参考。\n"
    "所有回测结果基于历史数据，不代表未来收益。\n"
    "不构成任何投资建议，不连接券商，不自动下单。"
)


def generate_audit_markdown(
    summary: WalkForwardSummary,
    quality: BacktestQuality | None = None,
    extra_notes: str = "",
) -> str:
    """Generate a Markdown strategy audit report."""
    if quality is None:
        quality = evaluate_backtest_quality(summary)
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines.append(f"# 策略审计报告 — {summary.strategy}")
    lines.append("")
    lines.append(f"生成时间：{now}")
    lines.append("")

    lines.append("## 1. 基本信息")
    lines.append("")
    lines.append(f"- **策略**：{summary.strategy}")
    lines.append(f"- **回测区间**：{summary.start_date} ~ {summary.end_date}")
    lines.append(f"- **训练窗口**：{summary.train_years} 年")
    lines.append(f"- **测试窗口**：{summary.test_years} 年")
    lines.append(f"- **滚动窗口数量**：{summary.total_windows}")
    lines.append(f"- **总交易笔数**：{summary.total_trades}")
    lines.append(f"- **数据门禁**：{summary.data_grade_filter}")
    lines.append(f"- **交易成本**：{summary.cost_bps} bps")
    lines.append(f"- **滑点**：{summary.slippage_bps} bps")
    lines.append("")

    lines.append("## 2. 总体结论")
    lines.append("")
    lines.append(f"- **回测质量等级**：{quality.quality_grade} (得分 {quality.score}/100)")
    lines.append(f"- **中位年化收益**：{summary.median_annual_return:.2%}")
    lines.append(f"- **平均年化收益**：{summary.average_annual_return:.2%}")
    lines.append(f"- **最差窗口收益**：{summary.worst_window_return:.2%}")
    lines.append(f"- **最大回撤**：{summary.worst_max_drawdown:.2%}")
    lines.append(f"- **总体 Sharpe**：{summary.aggregate_sharpe:.2f}")
    lines.append(f"- **总体胜率**：{summary.aggregate_win_rate:.0%}")
    lines.append(f"- **正收益窗口 / 总窗口**：{summary.positive_windows} / {summary.total_windows}")
    lines.append("")

    if quality.reasons:
        lines.append("### 质量评分原因")
        lines.append("")
        for reason in quality.reasons:
            lines.append(f"- {reason}")
        lines.append("")
    if quality.warnings:
        lines.append("### 质量警告")
        lines.append("")
        for warning in quality.warnings:
            lines.append(f"- ⚠ {warning}")
        lines.append("")

    lines.append("## 3. 窗口明细")
    lines.append("")
    lines.append("| # | 测试区间 | 年化收益 | 最大回撤 | Sharpe | 胜率 | 笔数 | 备注 |")
    lines.append("|---|---------|---------|---------|--------|------|------|------|")
    for idx, w in enumerate(summary.windows, 1):
        note = "；".join(w.warnings) if w.warnings else ""
        lines.append(
            f"| {idx} | {w.test_start}~{w.test_end} "
            f"| {w.annual_return:.2%} | {w.max_drawdown:.2%} "
            f"| {w.sharpe:.2f} | {w.win_rate:.0%} "
            f"| {w.trades_count} | {note} |"
        )
    lines.append("")

    lines.append("## 4. 风险声明")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    if summary.warnings:
        lines.append("### 策略级警告")
        lines.append("")
        for w in summary.warnings:
            lines.append(f"- {w}")
        lines.append("")
    if extra_notes:
        lines.append("## 5. 附加说明")
        lines.append("")
        lines.append(extra_notes)
        lines.append("")

    lines.append("---")
    lines.append(f"*{summary.disclaimer}*")
    lines.append("")
    return "\n".join(lines)


def save_audit_report(
    output_dir: Path,
    summary: WalkForwardSummary,
    quality: BacktestQuality | None = None,
    extra_notes: str = "",
) -> tuple[Path, Path]:
    """Save both JSON and Markdown audit reports.

    Returns (json_path, md_path).
    """
    if quality is None:
        quality = evaluate_backtest_quality(summary)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{summary.strategy}_{now}"

    json_path = output_dir / f"{base_name}_audit.json"
    json_data = {
        "walk_forward_summary": summary.to_dict(),
        "backtest_quality": quality.to_dict(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = output_dir / f"{base_name}_audit.md"
    md_content = generate_audit_markdown(summary, quality, extra_notes)
    md_path.write_text(md_content, encoding="utf-8")

    return json_path, md_path
