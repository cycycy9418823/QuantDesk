"""Trade plan export for QuantDesk.

Trade plans are for manual confirmation ONLY. They do not auto-execute,
do not connect to brokers, and do not constitute investment advice.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary

DISCLAIMER = (
    "本纸面研究情景仅为本地研究复盘参考，不构成操作指令。\n"
    "不构成任何投资建议，不连接券商，不自动下单。\n"
    "risk_observation_line 和 virtual_size_reference 仅为研究观察参考线，不是止损指令或仓位指令。"
)


@dataclass
class TradePlanItem:
    symbol: str = ""
    name: str = ""
    strategy: str = ""
    direction: str = "观察"
    reference_price: float = 0.0
    risk_observation_line: float = 0.0
    virtual_size_reference: float = 0.0
    research_only: bool = True
    is_real_order: bool = False
    data_grade: str = ""
    backtest_quality: str = ""
    risk_note: str = ""
    manual_confirmed: bool = False

    def __post_init__(self) -> None:
        self.research_only = True
        self.is_real_order = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "strategy": self.strategy,
            "direction": self.direction,
            "reference_price": self.reference_price,
            "risk_observation_line": self.risk_observation_line,
            "virtual_size_reference": round(self.virtual_size_reference, 4),
            "research_only": self.research_only,
            "is_real_order": self.is_real_order,
            "data_grade": self.data_grade,
            "backtest_quality": self.backtest_quality,
            "risk_note": self.risk_note,
            "manual_confirmed": self.manual_confirmed,
            "disclaimer": DISCLAIMER,
            **paper_only_boundary(),
        }


@dataclass
class TradePlan:
    generated_at: str = ""
    items: list[TradePlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
            "disclaimer": self.disclaimer,
            **paper_only_boundary(),
        }


def build_trade_plan(
    candidates: list[dict[str, Any]],
    max_items: int = 10,
) -> TradePlan:
    """Build a trade plan from candidate records.

    Each candidate dict should have: symbol, name, strategy,
    reference_price, data_grade, backtest_quality, atr14, etc.
    """
    plan = TradePlan(generated_at=datetime.now().isoformat(timespec="seconds"))

    for cand in candidates[:max_items]:
        symbol = str(cand.get("symbol", ""))
        data_grade = str(cand.get("data_grade", ""))
        bq = str(cand.get("backtest_quality", ""))

        risk_notes: list[str] = []
        if data_grade in ("UNAVAILABLE", "OBSERVE_ONLY"):
            risk_notes.append(f"数据等级 {data_grade}，不适合操作")
        if bq in ("INVALID", ""):
            risk_notes.append(f"回测质量 {bq or '未知'}，可信度不足")

        ref_price = float(cand.get("reference_price", 0) or 0)
        atr = float(cand.get("atr14", 0) or 0)
        stop_ref = ref_price - 2.0 * atr if atr > 0 and ref_price > 0 else 0.0
        position_pct = min(0.10, 0.02 / (atr / ref_price)) if atr > 0 and ref_price > 0 else 0.05

        item = TradePlanItem(
            symbol=symbol,
            name=str(cand.get("name", "")),
            strategy=str(cand.get("strategy", "")),
            direction="观察",
            reference_price=round(ref_price, 2),
            risk_observation_line=round(stop_ref, 2),
            virtual_size_reference=round(min(position_pct, 0.10), 4),
            data_grade=data_grade,
            backtest_quality=bq,
            risk_note="；".join(risk_notes) if risk_notes else "需人工复核",
            manual_confirmed=False,
        )
        plan.items.append(item)

    if not plan.items:
        plan.warnings.append("没有符合条件的候选。")
    plan.warnings.append("所有纸面情景条目仅供人工复核，不得作为执行指令；本工具不自动下单。")
    return plan


def export_trade_plan_csv(plan: TradePlan, output_path: Path) -> Path:
    """Export trade plan to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol", "name", "strategy", "direction",
        "reference_price", "risk_observation_line", "virtual_size_reference",
        "research_only", "is_real_order",
        "data_grade", "backtest_quality", "risk_note", "manual_confirmed",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in plan.items:
            row = item.to_dict()
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return output_path


def export_trade_plan_markdown(plan: TradePlan, output_path: Path) -> Path:
    """Export trade plan to Markdown."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# 纸面研究情景（仅供人工复核）")
    lines.append("")
    lines.append(f"生成时间：{plan.generated_at}")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    lines.append("| 代码 | 名称 | 策略 | 方向 | 参考价 | 风险阈值观察 | 暴露敏感性 | 数据等级 | 回测质量 | 风险备注 | 已确认 |")
    lines.append("|------|------|------|------|--------|---------|---------|---------|---------|---------|--------|")
    for item in plan.items:
        lines.append(
            f"| {item.symbol} | {item.name} | {item.strategy} | {item.direction} "
            f"| {item.reference_price:.2f} | {item.risk_observation_line:.2f} "
            f"| {item.virtual_size_reference:.1%} | {item.data_grade} "
            f"| {item.backtest_quality} | {item.risk_note} "
            f"| {'✓' if item.manual_confirmed else '☐'} |"
        )
    lines.append("")
    if plan.warnings:
        lines.append("## 警告")
        lines.append("")
        for w in plan.warnings:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("---")
    lines.append(f"*{DISCLAIMER}*")
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
