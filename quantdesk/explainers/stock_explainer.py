"""
股票研究解释器。

所有文案只用于研究学习：代入真实计算值、按区间动态生成，不暴露策略代号。
"""

from __future__ import annotations

import math
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _fmt(value: Any, digits: int = 2) -> str:
    value_num = _num(value, math.nan)
    if not math.isfinite(value_num):
        return "数据缺失"
    return f"{value_num:.{digits}f}"


def _return_text(value: float, label: str) -> str:
    direction = "上涨" if value > 0 else "下跌" if value < 0 else "基本持平"
    magnitude = abs(value)
    if magnitude >= 8:
        strength = "幅度很明显，说明这段时间资金分歧或关注度都比较高"
    elif magnitude >= 3:
        strength = "幅度中等，值得继续观察趋势是否延续"
    else:
        strength = "幅度不大，更适合结合成交量和均线一起判断"
    if direction == "基本持平":
        return f"{label}价格基本持平，单看涨跌幅还不能说明方向，需要结合量能和指标。"
    return f"{label}{direction}{magnitude:.1f}%，{strength}。"


def _macd_sentence(dif: float, dea: float, macd: float) -> str:
    gap = dif - dea
    if dif > dea and macd > 0:
        return f"MACD 的 DIF 为{dif:.3f}、DEA 为{dea:.3f}，DIF 高出 DEA {abs(gap):.3f}，柱体为{macd:.3f}，短线动能正在转强。"
    if dif < dea and macd < 0:
        return f"MACD 的 DIF 为{dif:.3f}、DEA 为{dea:.3f}，DIF 低于 DEA {abs(gap):.3f}，柱体为{macd:.3f}，短线动能偏弱。"
    if abs(gap) <= 0.02:
        return f"MACD 的 DIF 为{dif:.3f}、DEA 为{dea:.3f}，两条线距离很近，说明方向还在选择中。"
    return f"MACD 的 DIF 为{dif:.3f}、DEA 为{dea:.3f}，柱体为{macd:.3f}，动能信号不极端，需要和价格位置一起看。"


def _kdj_sentence(k: float, d: float, j: float) -> str:
    if k >= 80:
        return f"KDJ 的 K 值为{k:.1f}，超过80属于超买区，说明短期涨得较快，后面更容易震荡或回调。"
    if k <= 20:
        return f"KDJ 的 K 值为{k:.1f}，低于20属于超卖区，说明短期跌得较多，可能出现修复，但需要确认趋势是否止跌。"
    if k > d:
        return f"KDJ 的 K 值为{k:.1f}、D 值为{d:.1f}、J 值为{j:.1f}，K 线在 D 线上方，短线节奏略偏积极。"
    return f"KDJ 的 K 值为{k:.1f}、D 值为{d:.1f}、J 值为{j:.1f}，K 线没有明显强于 D 线，短线节奏偏谨慎。"


def _rsi_sentence(rsi6: float, rsi12: float) -> str:
    if rsi6 >= 70:
        return f"RSI6 为{rsi6:.1f}、RSI12 为{rsi12:.1f}，短期买盘偏热，继续上冲时要防止追高后的回落。"
    if rsi6 <= 30:
        return f"RSI6 为{rsi6:.1f}、RSI12 为{rsi12:.1f}，短期卖压较重，只有价格止跌并放量时才有修复意义。"
    return f"RSI6 为{rsi6:.1f}、RSI12 为{rsi12:.1f}，处在30到70之间，多空力量暂时没有极端倾斜。"


def risk_level_from_indicators(indicators: dict[str, Any]) -> str:
    """把指标状态映射为低/中/高风险。"""
    vol = _num(indicators.get("volatility_20d"))
    k = _num(indicators.get("k"), 50.0)
    rsi6 = _num(indicators.get("rsi6"), 50.0)
    dif = _num(indicators.get("dif"))
    dea = _num(indicators.get("dea"))
    if vol >= 5.0 or k >= 85 or rsi6 >= 75 or (dif < dea and dif < 0):
        return "高"
    if vol >= 2.5 or k >= 75 or rsi6 >= 65 or dif < dea:
        return "中"
    return "低"


def generate_selection_reason(symbol: str, indicators: dict[str, Any], df_len: int) -> str:
    """
    生成候选卡片入选理由。

    结果为 3-5 句人话，包含真实指标值和区间判断。
    """
    if not indicators or df_len < 20:
        return "本地历史数据不足，暂时只能保留为观察样本。请先运行“数据更新”，补齐至少20个交易日后再查看完整理由。当前不会显示策略代号，也不会给出交易动作。"

    return_5d = _num(indicators.get("return_5d")) * 100
    return_20d = _num(indicators.get("return_20d")) * 100
    vol_20d = _num(indicators.get("volatility_20d"))
    dif = _num(indicators.get("dif"))
    dea = _num(indicators.get("dea"))
    macd = _num(indicators.get("macd"))
    k = _num(indicators.get("k"), 50.0)
    d = _num(indicators.get("d"), 50.0)
    j = _num(indicators.get("j"), 50.0)
    rsi6 = _num(indicators.get("rsi6"), 50.0)

    sentences = [
        _return_text(return_5d, "最近5天"),
        _return_text(return_20d, "最近20天"),
        f"20日波动率为{vol_20d:.2f}%，{'价格弹性较高，适合重点复盘节奏' if vol_20d >= 5 else '波动处在可观察区间，重点看趋势和量能是否配合' if vol_20d >= 2 else '波动不大，重点看后续是否放量改变节奏'}。",
        _macd_sentence(dif, dea, macd),
        f"{_kdj_sentence(k, d, j)} 同时 RSI6 为{rsi6:.1f}，可以用来判断短线是否过热。",
    ]
    return "".join(sentences)


def generate_detailed_explanation(symbol: str, indicators: dict[str, Any], df_len: int) -> str:
    """生成详情弹窗里的“为什么选这只”。"""
    if not indicators or df_len < 20:
        return "<p>本地历史数据不足，暂时无法生成完整解释。请运行数据更新，让本地日线至少覆盖20个交易日。</p>"

    return_5d = _num(indicators.get("return_5d")) * 100
    return_20d = _num(indicators.get("return_20d")) * 100
    vol_20d = _num(indicators.get("volatility_20d"))
    dif = _num(indicators.get("dif"))
    dea = _num(indicators.get("dea"))
    macd = _num(indicators.get("macd"))
    k = _num(indicators.get("k"), 50.0)
    d = _num(indicators.get("d"), 50.0)
    j = _num(indicators.get("j"), 50.0)
    rsi6 = _num(indicators.get("rsi6"), 50.0)
    close = _num(indicators.get("close"))
    ma20 = _num(indicators.get("ma20"))

    price_position = "站在20日均线上方" if ma20 and close >= ma20 else "低于20日均线" if ma20 else "20日均线数据不足"
    trend_note = "趋势背景相对顺" if ma20 and close >= ma20 else "趋势背景还需要修复" if ma20 else "需要补数据后再判断趋势背景"

    return (
        f"<p>{_return_text(return_5d, '这只股票最近5天')} {_return_text(return_20d, '最近20天')}</p>"
        f"<p>当前收盘价为{_fmt(close)}元，20日均线为{_fmt(ma20)}元，价格{price_position}，{trend_note}。</p>"
        f"<p>20日波动率为{vol_20d:.2f}%，"
        f"{'属于活跃水平，说明它的涨跌节奏比较快，复盘时要把回撤风险放在前面。' if vol_20d >= 5 else '属于中等水平，既有波动也没有过度失控，适合用来学习趋势与节奏。' if vol_20d >= 2 else '说明近期日波动相对收敛，要观察后续是否有放量突破或跌破均线。'}</p>"
        f"<p>{_macd_sentence(dif, dea, macd)}</p>"
        f"<p>{_kdj_sentence(k, d, j)}</p>"
        f"<p>{_rsi_sentence(rsi6, _num(indicators.get('rsi12'), 50.0))}</p>"
    )


def generate_indicator_explanation(indicators: dict[str, Any]) -> str:
    """生成技术指标详解：数值、含义、当前判断。"""
    if not indicators:
        return "<p>本地数据不足，暂无指标计算结果。请先更新本地日线数据。</p>"

    dif = _num(indicators.get("dif"))
    dea = _num(indicators.get("dea"))
    macd = _num(indicators.get("macd"))
    k = _num(indicators.get("k"), 50.0)
    d = _num(indicators.get("d"), 50.0)
    j = _num(indicators.get("j"), 50.0)
    rsi6 = _num(indicators.get("rsi6"), 50.0)
    rsi12 = _num(indicators.get("rsi12"), 50.0)
    vol_ma5 = _num(indicators.get("vol_ma5"))
    vol_ma20 = _num(indicators.get("vol_ma20"))

    volume_note = "成交量均线数据不足，暂时无法判断量能。"
    if vol_ma5 and vol_ma20:
        ratio = vol_ma5 / vol_ma20 if vol_ma20 else 0.0
        if ratio >= 1.5:
            volume_note = f"5日均量是20日均量的{ratio:.2f}倍，说明近期成交明显放大，价格信号更需要结合量价同步复核。"
        elif ratio <= 0.7:
            volume_note = f"5日均量是20日均量的{ratio:.2f}倍，说明近期成交收缩，趋势信号的可靠性要打折。"
        else:
            volume_note = f"5日均量是20日均量的{ratio:.2f}倍，量能处在正常区间，没有明显放大或萎缩。"

    return (
        "<h4>MACD</h4>"
        f"<p>数值：DIF={dif:.3f}，DEA={dea:.3f}，MACD柱体={macd:.3f}。"
        f"含义：DIF 与 DEA 的距离反映短线动能变化。当前判断：{_macd_sentence(dif, dea, macd)}</p>"
        "<h4>KDJ</h4>"
        f"<p>数值：K={k:.1f}，D={d:.1f}，J={j:.1f}。"
        f"含义：KDJ反应短线节奏，80以上偏热、20以下偏冷。当前判断：{_kdj_sentence(k, d, j)}</p>"
        "<h4>RSI</h4>"
        f"<p>数值：RSI6={rsi6:.1f}，RSI12={rsi12:.1f}。"
        f"含义：RSI看短期上涨和下跌力量的相对强弱。当前判断：{_rsi_sentence(rsi6, rsi12)}</p>"
        "<h4>成交量</h4>"
        f"<p>数值：5日均量={_fmt(vol_ma5, 0)}，20日均量={_fmt(vol_ma20, 0)}。当前判断：{volume_note}</p>"
    )


def generate_risk_warning(indicators: dict[str, Any], df_len: int) -> str:
    """生成具体风险提示，强调研究-only。"""
    if not indicators or df_len < 20:
        return "<p>本地数据不足，无法给出可靠风险区间。请补齐日线后再复盘。</p>"

    close = _num(indicators.get("close"))
    ma5 = _num(indicators.get("ma5"))
    ma20 = _num(indicators.get("ma20"))
    vol_20d = _num(indicators.get("volatility_20d"))
    k = _num(indicators.get("k"), 50.0)
    rsi6 = _num(indicators.get("rsi6"), 50.0)
    dif = _num(indicators.get("dif"))
    dea = _num(indicators.get("dea"))

    notes = [
        f"<p><b>纸面观察风险：</b>以当前价{_fmt(close)}元做学习基准，只记录5%观察线和8%观察线两档回撤幅度；如果只是复盘学习，触发这些幅度要重新检查入选逻辑是否还成立。这里不输出具体未来价格或交易安排。</p>",
        f"<p><b>均线风险：</b>MA5为{_fmt(ma5)}元，MA20为{_fmt(ma20)}元。短线跌破MA5说明惯性减弱；如果进一步跌破MA20，说明中期节奏也转弱。</p>",
    ]
    if vol_20d >= 5:
        notes.append(f"<p><b>波动风险：</b>20日波动率{vol_20d:.2f}%偏高，这类样本涨跌都快，更适合等回调后复盘，而不是在情绪最热时追逐。</p>")
    if k >= 80 or rsi6 >= 70:
        notes.append(f"<p><b>过热风险：</b>KDJ的K值{k:.1f}、RSI6为{rsi6:.1f}，至少一个指标进入偏热区，当前时点要小心短线回落。</p>")
    if dif < dea:
        notes.append(f"<p><b>动能风险：</b>DIF={dif:.3f}低于DEA={dea:.3f}，说明短线动能没有占优，观察时要等待价格和量能重新确认。</p>")
    notes.append("<p><b>边界：</b>本页面只做股票研究学习，不接券商、不下真实订单、不构成投资建议。</p>")
    return "".join(notes)


def generate_market_sentiment(
    hs300_change: float | None,
    sh_change: float | None,
    advancing_count: int | None = None,
    declining_count: int | None = None,
) -> str:
    """基于指数表现和涨跌家数生成市场情绪一句话。"""
    if hs300_change is None and sh_change is None:
        return "本地指数数据缺失，暂时无法判断市场情绪；请更新 data/benchmark 后再查看。"
    valid_changes = [value for value in (hs300_change, sh_change) if value is not None and math.isfinite(value)]
    avg_change = sum(valid_changes) / len(valid_changes)
    breadth = ""
    if advancing_count is not None and declining_count is not None and advancing_count + declining_count > 0:
        if advancing_count > declining_count * 1.2:
            breadth = f"，上涨家数{advancing_count}多于下跌家数{declining_count}"
        elif declining_count > advancing_count * 1.2:
            breadth = f"，下跌家数{declining_count}多于上涨家数{advancing_count}"
        else:
            breadth = f"，上涨{advancing_count}家、下跌{declining_count}家，个股分化"
    if avg_change >= 1.0:
        return f"市场走强：主要指数平均上涨{avg_change:.2f}%{breadth}，适合先看强势板块的持续性。"
    if avg_change <= -1.0:
        return f"市场走弱：主要指数平均下跌{abs(avg_change):.2f}%{breadth}，研究时要把风险控制放在前面。"
    return f"市场震荡：主要指数平均涨跌{avg_change:.2f}%{breadth}，更适合做候选池复盘而不是急着下结论。"
