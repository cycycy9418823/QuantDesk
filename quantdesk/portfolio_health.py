"""Portfolio health check — daily risk monitoring for holdings.

Computes stop-loss distance, position concentration, market-board or
user-provided sector exposure, max-drawdown warnings, and overall risk level
for a portfolio.

All results are for local risk reference only.
They do not constitute investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

HEALTH_DISCLAIMER = (
    "持仓体检仅用于本地风险参考，不构成投资建议，"
    "不自动下单，不预测未来价格。请结合自身情况独立判断。"
)


SECTOR_MAP: dict[str, str] = {
    "60": "沪市主板", "601": "沪市主板", "603": "沪市主板",
    "000": "深市主板", "001": "深市主板", "002": "中小板",
    "300": "创业板", "301": "创业板",
    "688": "科创板",
    "920": "北交所", "83": "北交所", "87": "北交所", "43": "北交所",
}


def _guess_sector(symbol: str) -> str:
    """Rough sector guess from symbol prefix."""
    s = str(symbol).zfill(6)
    for prefix, sector in sorted(SECTOR_MAP.items(), key=lambda x: -len(x[0])):
        if s.startswith(prefix):
            return sector
    return "其他"


@dataclass
class HoldingHealth:
    """Health check result for one holding."""

    symbol: str = ""
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    weight: float = 0.0
    sector: str = ""
    data_grade: str = ""

    # risk metrics
    atr14: float = 0.0
    stop_price: float = 0.0
    stop_distance_pct: float = 0.0
    risk_level: str = ""  # 低风险 / 中风险 / 高风险 / 预警
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "shares": self.shares,
            "avg_cost": round(self.avg_cost, 3),
            "current_price": round(self.current_price, 3),
            "market_value": round(self.market_value, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 4),
            "weight": round(self.weight, 4),
            "sector": self.sector,
            "data_grade": self.data_grade,
            "atr14": round(self.atr14, 4),
            "stop_price": round(self.stop_price, 3),
            "stop_distance_pct": round(self.stop_distance_pct, 4),
            "risk_level": self.risk_level,
            "warnings": self.warnings,
        }


@dataclass
class PortfolioHealthReport:
    """Aggregated portfolio health report."""

    total_market_value: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    cash: float = 0.0
    cash_ratio: float = 0.0
    holdings_count: int = 0
    holdings: list[HoldingHealth] = field(default_factory=list)

    # concentration
    max_single_weight: float = 0.0
    top3_weight: float = 0.0
    sector_exposure: dict[str, float] = field(default_factory=dict)
    concentration_warning: str = ""

    # overall
    portfolio_risk_level: str = ""
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    disclaimer: str = HEALTH_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_market_value": round(self.total_market_value, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "cash": round(self.cash, 2),
            "cash_ratio": round(self.cash_ratio, 4),
            "holdings_count": self.holdings_count,
            "holdings": [h.to_dict() for h in self.holdings],
            "max_single_weight": round(self.max_single_weight, 4),
            "top3_weight": round(self.top3_weight, 4),
            "sector_exposure": {k: round(v, 4) for k, v in self.sector_exposure.items()},
            "concentration_warning": self.concentration_warning,
            "portfolio_risk_level": self.portfolio_risk_level,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "disclaimer": self.disclaimer,
        }


def _compute_atr14(df: pd.DataFrame) -> float:
    """Compute 14-day ATR from daily OHLC data."""
    if df is None or df.empty or len(df) < 2:
        return 0.0
    high = pd.to_numeric(df["high"], errors="coerce") if "high" in df.columns else pd.Series(dtype=float)
    low = pd.to_numeric(df["low"], errors="coerce") if "low" in df.columns else pd.Series(dtype=float)
    close = pd.to_numeric(df["close"], errors="coerce") if "close" in df.columns else pd.Series(dtype=float)
    if high.empty or low.empty or close.empty:
        return 0.0
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=1).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def check_holding_health(
    symbol: str,
    name: str,
    shares: int,
    avg_cost: float,
    current_price: float,
    daily_df: pd.DataFrame | None = None,
    data_grade: str = "",
    sector: str = "",
    atr_stop_multiplier: float = 2.0,
) -> HoldingHealth:
    """Check health of a single holding."""
    market_value = shares * current_price
    cost_value = shares * avg_cost
    pnl = market_value - cost_value
    pnl_pct = (pnl / cost_value) if cost_value > 0 else 0.0

    atr14 = _compute_atr14(daily_df) if daily_df is not None else 0.0
    stop_price = current_price - atr14 * atr_stop_multiplier if atr14 > 0 else 0.0
    stop_distance_pct = (atr14 * atr_stop_multiplier / current_price) if current_price > 0 and atr14 > 0 else 0.0

    warnings: list[str] = []
    risk_level = "低风险"

    if pnl_pct < -0.10:
        risk_level = "预警"
        warnings.append(f"浮亏 {pnl_pct:.1%}，超过 10% 警戒线")
    elif pnl_pct < -0.05:
        risk_level = "高风险"
        warnings.append(f"浮亏 {pnl_pct:.1%}，接近警戒线")
    elif stop_distance_pct > 0.08:
        risk_level = "中风险"
        warnings.append(f"ATR止损距离 {stop_distance_pct:.1%}，波动较大")

    if current_price > 0 and avg_cost > 0 and current_price < stop_price:
        warnings.append("当前价已低于 ATR 风险阈值观察价")
        if risk_level not in ("预警",):
            risk_level = "高风险"

    if data_grade in ("UNAVAILABLE", "OBSERVE_ONLY"):
        warnings.append(f"数据质量 {data_grade}，参考价值有限")

    return HoldingHealth(
        symbol=symbol,
        name=name,
        shares=shares,
        avg_cost=avg_cost,
        current_price=current_price,
        market_value=market_value,
        pnl=pnl,
        pnl_pct=pnl_pct,
        sector=sector.strip() if sector else _guess_sector(symbol),
        data_grade=data_grade,
        atr14=atr14,
        stop_price=stop_price,
        stop_distance_pct=stop_distance_pct,
        risk_level=risk_level,
        warnings=warnings,
    )


def check_portfolio_health(
    holdings: Sequence[HoldingHealth],
    cash: float = 0.0,
    total_account_value: float | None = None,
) -> PortfolioHealthReport:
    """Aggregate individual holding checks into a portfolio report."""
    total_mv = sum(h.market_value for h in holdings)
    account_value = total_account_value or (total_mv + cash)
    if account_value <= 0:
        account_value = total_mv + cash if (total_mv + cash) > 0 else 1.0

    total_pnl = sum(h.pnl for h in holdings)
    total_cost = sum(h.shares * h.avg_cost for h in holdings)
    total_pnl_pct = (total_pnl / total_cost) if total_cost > 0 else 0.0
    cash_ratio = cash / account_value if account_value > 0 else 0.0

    # update weights
    for h in holdings:
        h.weight = h.market_value / account_value if account_value > 0 else 0.0

    # concentration
    weights = sorted([h.weight for h in holdings], reverse=True)
    max_single = weights[0] if weights else 0.0
    top3 = sum(weights[:3])

    sector_exposure: dict[str, float] = {}
    for h in holdings:
        sector_exposure[h.sector] = sector_exposure.get(h.sector, 0) + h.weight

    concentration_warning = ""
    suggestions: list[str] = []
    if max_single > 0.30:
        concentration_warning = f"单只持仓占比 {max_single:.0%} 超过 30%，集中度过高"
        suggestions.append("考虑分散持仓，降低单只集中度")
    elif max_single > 0.20:
        concentration_warning = f"单只持仓占比 {max_single:.0%}，建议关注集中度"

    if top3 > 0.70:
        suggestions.append(f"前3持仓占比 {top3:.0%}，建议适度分散")

    if cash_ratio < 0.10:
        suggestions.append(f"现金比例 {cash_ratio:.0%} 偏低，建议保留 10-20% 现金应对波动")

    for sector, exposure in sector_exposure.items():
        if exposure > 0.40:
            suggestions.append(f"{sector} 暴露 {exposure:.0%}，行业集中度偏高")

    # overall risk
    warnings: list[str] = []
    alert_count = sum(1 for h in holdings if h.risk_level == "预警")
    high_count = sum(1 for h in holdings if h.risk_level == "高风险")

    if alert_count > 0:
        portfolio_risk = "预警"
        warnings.append(f"{alert_count} 只持仓触发预警")
    elif high_count >= len(holdings) // 2 and len(holdings) > 0:
        portfolio_risk = "高风险"
        warnings.append(f"{high_count}/{len(holdings)} 只持仓为高风险")
    elif high_count > 0:
        portfolio_risk = "中风险"
    elif total_pnl_pct < -0.05:
        portfolio_risk = "中风险"
        warnings.append(f"组合整体浮亏 {total_pnl_pct:.1%}")
    else:
        portfolio_risk = "低风险"

    return PortfolioHealthReport(
        total_market_value=total_mv,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        cash=cash,
        cash_ratio=cash_ratio,
        holdings_count=len(holdings),
        holdings=list(holdings),
        max_single_weight=max_single,
        top3_weight=top3,
        sector_exposure=sector_exposure,
        concentration_warning=concentration_warning,
        portfolio_risk_level=portfolio_risk,
        warnings=warnings,
        suggestions=suggestions,
    )


def portfolio_health_to_markdown(report: PortfolioHealthReport) -> str:
    """Render portfolio health report as Markdown."""
    lines: list[str] = []
    lines.append("# 持仓体检报告")
    lines.append("")
    lines.append(f"> {HEALTH_DISCLAIMER}")
    lines.append("")

    risk_emoji = {"低风险": "🟢", "中风险": "🟡", "高风险": "🟠", "预警": "🔴"}.get(report.portfolio_risk_level, "⚪")
    lines.append(f"## 组合概览 {risk_emoji} {report.portfolio_risk_level}")
    lines.append("")
    lines.append(f"- 持仓市值: ¥{report.total_market_value:,.2f}")
    lines.append(f"- 现金: ¥{report.cash:,.2f} ({report.cash_ratio:.1%})")
    lines.append(f"- 持仓数: {report.holdings_count}")
    lines.append(f"- 组合浮盈: ¥{report.total_pnl:,.2f} ({report.total_pnl_pct:+.2%})")
    lines.append(f"- 最大单只占比: {report.max_single_weight:.1%}")
    lines.append(f"- 前3占比: {report.top3_weight:.1%}")
    lines.append("")

    if report.warnings:
        lines.append("### ⚠️ 风险提示")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    if report.suggestions:
        lines.append("### 💡 建议")
        for s in report.suggestions:
            lines.append(f"- {s}")
        lines.append("")

    if report.sector_exposure:
        lines.append("")
        lines.append("> 持仓 CSV 提供 sector/industry 时，本节使用用户提供的行业字段；否则按股票代码前缀推断市场板块，不等同于真实行业分类。")
        lines.append("")
        lines.append("### 行业暴露")
        lines.append("| 板块 | 占比 |")
        lines.append("|:---|---:|")
        for sector, weight in sorted(report.sector_exposure.items(), key=lambda x: -x[1]):
            lines.append(f"| {sector} | {weight:.1%} |")
        lines.append("")

    if report.holdings:
        lines.append("### 个股明细")
        lines.append("| 代码 | 名称 | 市值 | 盈亏 | 占比 | 止损距离 | 风险 |")
        lines.append("|:---|:---|---:|---:|---:|---:|:---|")
        for h in report.holdings:
            risk_mark = {"低风险": "🟢", "中风险": "🟡", "高风险": "🟠", "预警": "🔴"}.get(h.risk_level, "")
            lines.append(
                f"| {h.symbol} | {h.name} | ¥{h.market_value:,.0f} "
                f"| {h.pnl_pct:+.1%} | {h.weight:.1%} "
                f"| {h.stop_distance_pct:.1%} | {risk_mark} {h.risk_level} |"
            )
        lines.append("")

        # detail warnings
        has_warnings = [h for h in report.holdings if h.warnings]
        if has_warnings:
            lines.append("### 个股预警明细")
            for h in has_warnings:
                lines.append(f"**{h.symbol} {h.name}**:")
                for w in h.warnings:
                    lines.append(f"  - {w}")
            lines.append("")

    if report.concentration_warning:
        lines.append(f"⚠️ 集中度: {report.concentration_warning}")
        lines.append("")

    lines.append(f"*{HEALTH_DISCLAIMER}*")
    return "\n".join(lines)
