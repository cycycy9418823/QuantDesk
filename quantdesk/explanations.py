from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quantdesk.reporting import fmt_value
from quantdesk.timing_advisor import advise_timing, timing_advice_to_dict
from quantdesk.trust_engine import assess_stock_trust, trust_assessment_to_dict

DANGEROUS_PHRASES = ["必涨", "稳赚", "保证收益", "无风险", "满仓买入", "强烈买入"]


def stock_code(value: object) -> str:
    text = str(value)
    return text.zfill(6) if text.isdigit() else text


def row_float(row: pd.Series, key: str, default: float = np.nan) -> float:
    try:
        value = row.get(key, default)
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def attention_type(row: pd.Series) -> str:
    long_score = float(row.get("long_score", 0))
    board_score = float(row.get("board_score", 0))
    if long_score >= 80 and board_score >= 80:
        return "长期+强势"
    if long_score >= 80:
        return "长期关注"
    if board_score >= 80:
        return "短线强势"
    if long_score >= 70:
        return "长期观察"
    if board_score >= 70:
        return "打板观察"
    return "普通观察"


def data_risk_flag(row: pd.Series) -> str:
    level = str(row.get("warning_level", "OK")).upper()
    freshness = str(row.get("data_freshness_status", row.get("data_freshness_status", "")) or "")
    expected_reason = str(row.get("expected_reason", row.get("data_expected_reason", "")) or "")
    if freshness == "market_holiday_using_last_trading_day":
        return expected_reason or "当前为 A 股休市日，使用最近交易日完整日线"
    if freshness == "waiting_for_close":
        return expected_reason or "当前未收盘，使用上一交易日完整日线"
    if freshness == "waiting_for_quote_update":
        return expected_reason or "今日已收盘，但行情源尚未更新到今日"
    if level == "ERROR":
        return "数据不可用，不进入候选观察区"
    if level == "WARNING":
        if bool(row.get("served_from_cache", False)):
            return "使用缓存数据，需核对最新行情"
        return "数据可能过期或存在质量警告"
    if level == "INFO" and bool(row.get("fallback_used", False)):
        return "使用备用数据源"
    return ""


def position_summary(row: pd.Series) -> str:
    position = row_float(row, "position250_pct")
    if not np.isfinite(position):
        return "位置未知"
    if position >= 85:
        return f"高位 {fmt_value(position)}%"
    if position >= 60:
        return f"中高位 {fmt_value(position)}%"
    if position >= 35:
        return f"中位 {fmt_value(position)}%"
    return f"低位 {fmt_value(position)}%"


def news_bullets(row: pd.Series) -> list[str]:
    titles = str(row.get("news_titles", "") or "").split("；")
    return [title for title in titles if title.strip()] or [str(row.get("news_risk", "暂无新闻。"))]


def brief_action(row: pd.Series) -> str:
    long_score = float(row.get("long_score", 0))
    board_score = float(row.get("board_score", 0))
    risk_pct = float(row.get("risk_pct", 0))
    change_pct = float(row.get("change_pct", 0))
    position250 = float(row.get("position250_pct", 0) or 0)
    news_badge = str(row.get("news_badge", ""))
    if news_badge == "消息有风险":
        return "先核实消息"
    if position250 >= 88 and change_pct > 3:
        return "高位谨慎"
    if risk_pct > 12:
        return "先观察，等回踩"
    if board_score >= 80 and change_pct >= 9:
        return "仅做纸面短线观察"
    if long_score >= 80:
        return "长期观察，分情景复核"
    if long_score >= 70 or board_score >= 70:
        return "观察确认"
    return "暂不主动参与"


def operation_advice(row: pd.Series) -> str:
    long_score = float(row.get("long_score", 0))
    board_score = float(row.get("board_score", 0))
    risk_pct = float(row.get("risk_pct", 0))
    change_pct = float(row.get("change_pct", 0))
    close = float(row.get("close", 0))
    ma20 = float(row.get("ma20", 0))
    stop = float(row.get("stop", 0))
    position250 = float(row.get("position250_pct", 0) or 0)
    news_badge = str(row.get("news_badge", ""))

    if news_badge == "消息有风险":
        return "先核实消息面。等负面消息被市场消化、股价重新站稳20日线，再重新评分。"
    if position250 >= 88 and change_pct > 3:
        return "位置偏高且当天仍在上涨，新手不适合追。更好的做法是等回踩不破20日线，或者等下一次低风险观察点。"
    if risk_pct > 12:
        return f"先观察，不追高。等股价回踩靠近20日线 {ma20:.2f} 且不跌破，再考虑；若跌破 {stop:.2f}，放弃。"
    if board_score >= 80 and change_pct >= 9:
        return "只适合短线小仓位观察。次日竞价强、板块仍强、回封有量才考虑；炸板或高开低走就放弃。"
    if long_score >= 80 and close > ma20:
        return f"适合放入长期观察池。可等回踩20日线附近确认，分批而不是一次做满；跌破 {stop:.2f} 要执行风控。"
    if long_score >= 70:
        return "趋势还可以，但不是最强。适合观察，不适合冲动追入；等收盘再次站稳20日线后再看。"
    return "信号不够强，暂时只观察，暂不进入计划动作。"


def beginner_reason(row: pd.Series) -> str:
    position = row_float(row, "position250_pct")
    amount_ratio = row_float(row, "amount_ratio")
    rsi14 = row_float(row, "rsi14")
    risk_pct = row_float(row, "risk_pct")
    dist_ma20 = row_float(row, "dist_ma20_pct")
    change_pct = row_float(row, "change_pct")

    if position >= 88:
        main_conflict = "强势和追高风险同时存在"
    elif position <= 35:
        main_conflict = "位置不高，但要确认是不是弱势反弹"
    elif amount_ratio >= 2:
        main_conflict = "资金明显放大，要判断是健康放量还是短线情绪过热"
    elif risk_pct > 12:
        main_conflict = "趋势分数还可以，但参与后的风控空间偏大"
    else:
        main_conflict = "趋势、量能和风险距离是否能继续配合"

    heat_text = ""
    if rsi14 >= 70 or "高位" in str(row.get("kdj_signal", "")) or row.get("news_badge") == "消息过热":
        heat_text = " 但 RSI/KDJ/新闻热度提示它不适合无脑追。"
    elif rsi14 <= 35:
        heat_text = " RSI偏低时要分清是超跌反弹，还是趋势真的转强。"

    return (
        f"这只票当前最该看的矛盾是：{main_conflict}。"
        f"技术上是“{row.get('ma_stack_signal', row.get('ma_signal', '-'))}”，"
        f"距离MA20约 {fmt_value(dist_ma20)}%，20日涨幅 {fmt_value(row.get('ret20'))}%，"
        f"当天涨跌幅 {fmt_value(change_pct)}%。"
        f"消息面为“{row.get('news_status', row.get('news_badge', '消息字段缺失'))}”。{heat_text}"
    )


def tonghuashun_steps(row: pd.Series) -> list[str]:
    return [
        f"打开同花顺，输入 {stock_code(row.get('code'))} {row.get('name')}，切到“日K”。",
        "指标设置里先勾选：MA、VOL成交量、MACD、KDJ、RSI、BOLL。先不用全懂，每天重点看本票下面标出的几项。",
        f"MA均线：看 MA5/MA10/MA20/MA60/MA120。当前为“{row.get('ma_stack_signal', '-')}”，重点观察收盘价是否继续站在 MA20 上方。",
        f"VOL/量比/换手率：当前量比约 {fmt_value(row.get('amount_ratio'))}，换手率约 {fmt_value(row.get('turnover'))}%。上涨放量是确认，放巨量冲高回落是风险。",
        f"MACD：当前为“{row.get('macd_signal', '-')}”。你认识MACD很好，后面要学会把它和量能、位置一起看。",
        f"KDJ/RSI：当前为“{row.get('kdj_signal', '-')}、{row.get('rsi_signal', '-')}”。这两个更敏感，适合提醒短线是否过热或转弱。",
        f"BOLL布林线：当前为“{row.get('boll_signal', '-')}”。靠近上轨说明强，但也容易追高；跌回中轨通常要降温。",
        f"支撑/压力：20日支撑约 {fmt_value(row.get('support20'))}，20日压力约 {fmt_value(row.get('resistance20'))}，风险阈值观察 {fmt_value(row.get('stop'))}。",
    ]


def indicator_table_rows(row: pd.Series) -> list[tuple[str, str, str]]:
    return [
        (
            "MA5/10/20/60/120",
            f"{fmt_value(row.get('ma5'))}/{fmt_value(row.get('ma10'))}/{fmt_value(row.get('ma20'))}/"
            f"{fmt_value(row.get('ma60'))}/{fmt_value(row.get('ma120'))}",
            f"{row.get('ma_stack_signal', '-')}: 看趋势是不是顺着走。短线看MA5/10，中线看MA20/60，长线看MA120。",
        ),
        (
            "VOL成交量 / 量比",
            f"量比 {fmt_value(row.get('amount_ratio'))}，成交额 {fmt_value(row.get('amount_yi'))}亿元",
            f"{row.get('volume_signal', '-')}: 涨时温和放量是确认，突然巨量但价格不稳要防出货。",
        ),
        ("换手率", f"{fmt_value(row.get('turnover'))}%", "换手率代表筹码交换程度。低换手可能没人关注，过高换手说明分歧很大，打板时尤其要看。"),
        (
            "MACD",
            f"DIF {fmt_value(row.get('macd_dif'), 4)} / DEA {fmt_value(row.get('macd_dea'), 4)} / BAR {fmt_value(row.get('macd_bar'), 4)}",
            f"{row.get('macd_signal', '-')}: 它看动能方向，但不能单独决定买卖，要和价格位置、成交量一起验证。",
        ),
        (
            "KDJ",
            f"K {fmt_value(row.get('kdj_k'))} / D {fmt_value(row.get('kdj_d'))} / J {fmt_value(row.get('kdj_j'))}",
            f"{row.get('kdj_signal', '-')}: 比MACD更敏感，短线用来提醒过热、钝化、转弱，不适合长期单独使用。",
        ),
        ("RSI14", fmt_value(row.get("rsi14")), f"{row.get('rsi_signal', '-')}: 50上方偏强，70上方偏热，30下方可能超卖但不等于一定反弹。"),
        (
            "BOLL布林线",
            f"位置 {fmt_value(row.get('boll_position'))}%，上轨 {fmt_value(row.get('boll_upper'))}，中轨 {fmt_value(row.get('boll_mid'))}",
            f"{row.get('boll_signal', '-')}: 看价格离正常波动带有多远，上轨附近要结合量能判断能否继续强。",
        ),
        (
            "支撑 / 压力 / ATR",
            f"支撑 {fmt_value(row.get('support20'))}，压力 {fmt_value(row.get('resistance20'))}，ATR14 {fmt_value(row.get('atr14'))}",
            f"ATR代表平均波动。止损 {fmt_value(row.get('stop'))}，当前到止损约 {fmt_value(row.get('risk_pct'))}%，用来控制亏损。",
        ),
        ("250日位置", position_summary(row), "这是判断高低位的核心。高位强势可以继续强，但新手要防追高；低位要防它只是弱势横盘。"),
    ]


def stock_learning_notes(row: pd.Series) -> list[str]:
    notes: list[str] = []
    position = row_float(row, "position250_pct")
    amount_ratio = row_float(row, "amount_ratio")
    turnover = row_float(row, "turnover")
    rsi14 = row_float(row, "rsi14")
    risk_pct = row_float(row, "risk_pct")
    change_pct = row_float(row, "change_pct")
    dist_ma20 = row_float(row, "dist_ma20_pct")
    board_score = row_float(row, "board_score")

    if position >= 90:
        notes.append("重点学“高位强势股”：它离一年高点很近，能涨说明资金强，但观察点必须等确认，不能因为分数高就追。")
    elif position <= 35:
        notes.append("重点学“低位不等于安全”：位置低只说明离高点远，还要看MA20是否走平向上、成交量是否放大。")
    else:
        notes.append("重点学“中位趋势”：这种票不是绝对低位，也不是极端高位，关键看MA20/MA60是否继续向上。")

    if amount_ratio >= 2:
        notes.append(f"重点学“量比”：量比 {fmt_value(amount_ratio)} 说明今天成交显著放大。你要看放量后价格有没有站稳，而不是只看放量。")
    elif amount_ratio < 0.8:
        notes.append(f"重点学“缩量”：量比 {fmt_value(amount_ratio)} 偏低，说明资金确认不足，突破信号容易失败。")
    if turnover >= 12:
        notes.append(f"重点学“换手率”：换手率 {fmt_value(turnover)}% 很高，筹码分歧大，短线波动可能较大，需谨慎复核，但不适合重仓。")
    elif turnover <= 2:
        notes.append(f"重点学“流动性”：换手率 {fmt_value(turnover)}% 偏低，波动可能慢，适合观察趋势，不适合幻想马上爆发。")
    if rsi14 >= 70 or "高位" in str(row.get("kdj_signal", "")):
        notes.append("重点学“超买不等于马上跌”：RSI/KDJ偏热时，强股还能继续强，但新手要等回踩或二次确认。")
    elif rsi14 <= 35:
        notes.append("重点学“超卖不等于马上涨”：RSI偏低只说明跌得多，必须等价格重新站上关键均线。")
    if "上轨" in str(row.get("boll_signal", "")):
        notes.append("重点学“布林上轨”：贴着上轨走是强势，但一旦跌回中轨，短线情绪可能降温。")
    elif "下轨" in str(row.get("boll_signal", "")):
        notes.append("重点学“布林下轨”：靠近下轨说明偏弱或超跌，要等止跌信号，不要只因为便宜就买。")
    if board_score >= 80 or change_pct >= 9:
        notes.append("重点学“涨停/打板”：要看封板质量、炸板次数、次日竞价、板块强弱。报告只提醒候选，不代表可以直接冲。")
    if risk_pct > 12:
        notes.append(f"重点学“风险距离”：到止损约 {fmt_value(risk_pct)}%，说明参与后可能要承受较大波动，不适合大仓位。")
    elif abs(dist_ma20) <= 3:
        notes.append("重点学“MA20回踩”：价格离20日线不远，后续如果缩量回踩不破，通常比追涨更容易控制风险。")
    if row.get("news_badge") in {"消息过热", "消息有风险", "消息偏利好"}:
        notes.append(f"重点学“新闻验证”：当前是“{row.get('news_badge')}”。新闻只能当线索，你要去看公告原文和股价是否已经提前反映。")
    return notes[:7] or ["重点学“先定义条件再操作”：参与条件、放弃条件、止损线都写清楚，条件没出现就不动。"]


def trade_checklist(row: pd.Series, temp: dict) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    temp_score = row_float(pd.Series(temp), "temp_score", 50)
    close = row_float(row, "close")
    ma20 = row_float(row, "ma20")
    ma60 = row_float(row, "ma60")
    position = row_float(row, "position250_pct")
    amount_ratio = row_float(row, "amount_ratio")
    risk_pct = row_float(row, "risk_pct")
    rsi14 = row_float(row, "rsi14")
    news_badge = str(row.get("news_badge", ""))
    macd_signal = str(row.get("macd_signal", ""))
    kdj_signal = str(row.get("kdj_signal", ""))

    if temp_score >= 55:
        checks.append(("市场环境", "通过", f"市场温度 {fmt_value(temp_score, 1)}，不算太冷。"))
    elif temp_score >= 35:
        checks.append(("市场环境", "谨慎", f"市场温度 {fmt_value(temp_score, 1)}，震荡偏弱，降低预期。"))
    else:
        checks.append(("市场环境", "否决", f"市场温度 {fmt_value(temp_score, 1)}，环境太弱，不适合主动进攻。"))
    if close > ma20 > ma60:
        checks.append(("趋势结构", "通过", "价格在MA20上方，且MA20高于MA60，趋势结构较顺。"))
    elif close > ma20:
        checks.append(("趋势结构", "谨慎", "价格在MA20上方，但MA20/MA60关系还不够顺。"))
    else:
        checks.append(("趋势结构", "否决", "价格未站稳MA20，先等趋势修复。"))
    if 30 <= position <= 85:
        checks.append(("股价位置", "通过", f"一年位置 {fmt_value(position)}%，不算极端追高。"))
    elif position > 85:
        checks.append(("股价位置", "谨慎", f"一年位置 {fmt_value(position)}%，已经偏高，观察条件必须更严格。"))
    else:
        checks.append(("股价位置", "谨慎", f"一年位置 {fmt_value(position)}%，位置低但要确认不是弱势。"))
    if 1.0 <= amount_ratio <= 3.5:
        checks.append(("量能确认", "通过", f"量比 {fmt_value(amount_ratio)}，资金参与度较好。"))
    elif amount_ratio > 3.5:
        checks.append(("量能确认", "谨慎", f"量比 {fmt_value(amount_ratio)} 很高，可能强也可能分歧过大。"))
    else:
        checks.append(("量能确认", "否决", f"量比 {fmt_value(amount_ratio)} 偏弱，资金确认不足。"))
    if "死叉" in macd_signal:
        checks.append(("动能指标", "否决", f"{macd_signal}，短线动能转弱。"))
    elif rsi14 >= 80 or "高位" in kdj_signal:
        checks.append(("动能指标", "谨慎", f"{row.get('rsi_signal', '-')} / {kdj_signal}，偏热时不要追。"))
    else:
        checks.append(("动能指标", "通过", f"{macd_signal} / {kdj_signal}，暂未出现明显动能否决。"))
    if news_badge == "消息有风险":
        checks.append(("消息面", "否决", str(row.get("news_risk", "消息面有风险。"))))
    elif news_badge == "消息过热":
        checks.append(("消息面", "谨慎", "消息热度较高，可能已经被价格提前反映。"))
    else:
        checks.append(("消息面", "通过", f"{news_badge or '消息字段缺失'}，未发现明显消息面否决。"))
    if risk_pct <= 8:
        checks.append(("风险距离", "通过", f"到止损约 {fmt_value(risk_pct)}%，风控距离较容易接受。"))
    elif risk_pct <= 12:
        checks.append(("风险距离", "谨慎", f"到止损约 {fmt_value(risk_pct)}%，仓位要小。"))
    else:
        checks.append(("风险距离", "否决", f"到止损约 {fmt_value(risk_pct)}%，容错空间太大。"))
    return checks


def trade_plan(row: pd.Series, temp: dict) -> str:
    checks = trade_checklist(row, temp)
    fail_count = sum(1 for _, status, _ in checks if status == "否决")
    caution_count = sum(1 for _, status, _ in checks if status == "谨慎")
    if fail_count >= 2:
        return "计划结论：偏回避，只观察不操作。否决项太多，等趋势、消息或风控条件修复后再重新评估。"
    if fail_count == 1:
        return "计划结论：暂不主动参与，先解决否决项；如果后续转为通过，也只进入虚拟验证或人工复核。已有虚拟记录要盯MA20和风险参考位。"
    if caution_count >= 3:
        return "计划结论：可以放进观察池，但只适合等确认，不适合追涨。等回踩不破或放量站稳后再提高信心。"
    return "计划结论：条件相对完整，可作为虚拟验证对象，但仍需人工复核和风险检查。"


def action_script_lines(row: pd.Series, temp: dict) -> list[str]:
    close = row_float(row, "close")
    ma20 = row_float(row, "ma20")
    ma60 = row_float(row, "ma60")
    stop = row_float(row, "stop")
    boll_upper = row_float(row, "boll_upper")
    boll_mid = row_float(row, "boll_mid")
    risk_pct = row_float(row, "risk_pct")
    amount_ratio = row_float(row, "amount_ratio")
    rsi14 = row_float(row, "rsi14")
    position = row_float(row, "position250_pct")
    news_badge = str(row.get("news_status", row.get("news_badge", "消息字段缺失")))
    checks = trade_checklist(row, temp)
    fail_count = sum(1 for _, status, _ in checks if status == "否决")

    if close > ma20 > ma60:
        state = f"当前状态：今天收盘价在20日线和60日线上面，说明走势暂时没有坏；但一年位置约 {fmt_value(position, 1)}%，不是越高越能追。"
    elif close > ma20:
        state = "当前状态：今天收盘价在20日线上面，说明有修复迹象，但60日线还没完全配合，所以先别当成稳稳上涨。"
    else:
        state = "当前状态：今天收盘价还没站上20日线。对新手来说，这种票先看，不急着参与。"
    bias = "我的倾向：偏回避。先别把它当观察点，等否决项减少后再看。" if fail_count >= 2 or news_badge == "消息有风险" else "我的倾向：可以观察，但要等触发条件，不因为一个指标好看就冲。"
    entry = f"观察触发条件：打开同花顺日K，看最近两三天收盘价是不是都在20日线 {fmt_value(ma20)} 上面；再看成交量是不是比前几天多一些，但不是突然大得离谱。满足这些，也只进入虚拟验证或人工复核。"
    exit_line = f"风险处理：如果价格跌到风险参考位 {fmt_value(stop)} 附近，或者连续两天收在20日线 {fmt_value(ma20)} 下面，说明原来的判断可能错了，应回到风险复核。"
    forecast = f"后续复核重点：如果未来几天收盘价一直在20日线上方，并且慢慢往布林线上轨 {fmt_value(boll_upper)} 靠近，说明承接仍在；如果跌回布林中轨 {fmt_value(boll_mid)} 或20日线下面，就放回观察名单。"
    confirm = f"还要结合：量比 {fmt_value(amount_ratio)}、RSI14 {fmt_value(rsi14)}、风险距离 {fmt_value(risk_pct, 1)}%、消息面“{news_badge}”。多项同向才更可信。"
    return [state, bias, entry, exit_line, forecast, confirm]


def risk_flags(row: pd.Series) -> list[str]:
    flags: list[str] = []
    if row_float(row, "risk_pct") > 12:
        flags.append("当前价离止损位太远，说明参与后容错空间小，不适合追。")
    if row_float(row, "position250_pct") >= 88:
        flags.append("一年位置偏高，说明强势已经被市场看到，追高风险增加。")
    if row_float(row, "rsi14") >= 75:
        flags.append("RSI偏热，短线继续上冲和回落都可能很快。")
    if row.get("news_badge") == "消息有风险":
        flags.append(str(row.get("news_risk", "消息面存在风险，需要先核实公告原文。")))
    if row.get("data_risk_flag"):
        flags.append(str(row.get("data_risk_flag")))
    return flags or ["暂未发现特别明显的反面信号，但仍要按止损和仓位纪律执行。"]


def explain_candidate(row: pd.Series) -> list[str]:
    return [beginner_reason(row), operation_advice(row)]


def explain_risk_flags(row: pd.Series) -> list[str]:
    return risk_flags(row)


def explain_data_warning(row: pd.Series) -> list[str]:
    flags = []
    level = str(row.get("data_warning_level", row.get("warning_level", "OK"))).upper()
    freshness = str(row.get("data_freshness_status", "") or "")
    reason = str(row.get("data_expected_reason", "") or "")
    latest = str(row.get("data_latest_date", "") or "")
    expected = str(row.get("data_expected_latest_date", "") or "")
    if reason:
        flags.append(reason)
    if latest or expected:
        flags.append(f"最新数据日期：{latest or '暂无'}；预期数据日期：{expected or '暂无'}。")
    if freshness:
        flags.append(f"数据状态：{freshness}。")
    if level == "ERROR":
        flags.append("数据不可用，不建议作为今日决策依据。")
    elif level == "WARNING":
        flags.append("数据存在 WARNING，需要先核对最新行情。")
    elif level == "INFO" or row.get("data_risk_flag"):
        flags.append(str(row.get("data_risk_flag") or "使用备用数据源，需要知道来源变化。"))
    else:
        flags.append("数据状态未见明显异常。")
    return flags


def build_pre_trade_checklist(row: pd.Series, market_temp: dict | None = None, account_config: dict[str, Any] | None = None) -> list[str]:
    market_temp = market_temp or {}
    account_config = account_config or {}
    max_position_pct = account_config.get("max_position_pct", row.get("max_position_pct", "自定上限"))
    checklist = [
        "数据新鲜度：先确认报告日期和同花顺日K最新交易日一致；不一致就只观察。",
        f"备用数据源：{row.get('data_risk_flag') or '未见 fallback/缓存提示'}。",
        f"数据 WARNING：{row.get('data_warning_level', 'OK')}，如为 WARNING/ERROR，先核对行情。",
        f"涨停风险：当天涨跌幅 {fmt_value(row.get('change_pct'))}%，接近涨停时不要追，等次日确认。",
        f"跌停/流动性：成交额约 {fmt_value(row.get('amount_yi'))} 亿元，量能不足或跌停附近先观察。",
        f"风险距离：距离风险阈值约 {fmt_value(row.get('risk_pct'))}%，过远说明波动风险偏高。",
        f"近期涨幅：20日涨幅 {fmt_value(row.get('ret20'))}%，涨幅过大时等待回踩更稳妥。",
        "成交额检查：成交额应足够支撑进出，低成交额不要硬做。",
        f"市场温度：当前 {fmt_value(market_temp.get('temp_score'), 1)} / 100，市场转弱时降低预期。",
        f"风险阈值：先记录风险阈值观察 {fmt_value(row.get('stop'))}，没有纸面情景就继续观察。",
        f"暴露敏感性：纸面情景上限 {max_position_pct} 仅用于观察组合波动，不是真实配置指令。",
        "条件不满足：继续观察，不因为一两个指标好看就行动。",
    ]
    return checklist


def build_invalid_conditions(row: pd.Series, market_temp: dict | None = None) -> list[str]:
    market_temp = market_temp or {}
    return [
        "以下为辅助检查条件，不构成投资建议。",
        f"跌破风险阈值观察 {fmt_value(row.get('stop'))}，纸面情景需要重新评估。",
        f"收盘跌破 MA20 {fmt_value(row.get('ma20'))}，短期趋势可能降温。",
        f"收盘跌破 MA60 {fmt_value(row.get('ma60'))}，中期结构要更谨慎。",
        "出现放量长阴，说明分歧扩大，先观察承接。",
        "数据过期、缓存或 WARNING 未核实前，不建议作为今日决策依据。",
        f"新闻或公告出现风险：{row.get('news_risk', '需要继续核实公告原文。')}",
        f"市场温度转弱到 {fmt_value(market_temp.get('temp_score'), 1)} 以下时，降低短线预期。",
        "回测假设不适用于当前流动性、涨跌停、停复牌或消息冲击时，不能照搬历史结果。",
    ]


def _first_items(items: list[str], limit: int) -> list[str]:
    return [str(item) for item in items if str(item).strip()][:limit]


def _data_status(row: pd.Series) -> str:
    level = str(row.get("data_warning_level", row.get("warning_level", "OK"))).upper()
    flag = str(row.get("data_risk_flag", "") or "")
    freshness = str(row.get("data_freshness_status", "") or "")
    reason = str(row.get("data_expected_reason", "") or "")
    if freshness == "market_holiday_using_last_trading_day":
        return reason or "当前为 A 股休市日，使用最近交易日完整日线"
    if freshness == "waiting_for_close":
        return reason or "当前未收盘，使用上一交易日完整日线"
    if freshness == "waiting_for_quote_update":
        return reason or "今日已收盘，但行情源尚未更新到今日"
    if freshness == "stale":
        return reason or "数据早于预期交易日，可能过期"
    if level == "ERROR":
        return "数据不足，暂不参考"
    if level == "WARNING":
        return flag or "数据存在 WARNING，先核对行情"
    if level == "INFO" or bool(row.get("fallback_used", False)):
        return flag or "使用备用数据源，先确认来源"
    return "数据状态未见明显异常"


def build_beginner_explanation(assessment: Any, timing_advice: Any | None = None) -> list[str]:
    """Build plain-language notes from trust and timing outputs."""
    action_state = str(getattr(assessment, "action_state", "只观察") or "只观察")
    trust_level = str(getattr(assessment, "trust_level", "谨慎观察") or "谨慎观察")
    trust_score = getattr(assessment, "trust_score", "")
    blocking = [str(item) for item in getattr(assessment, "blocking_issues", []) if str(item).strip()]
    risks = [str(item) for item in getattr(assessment, "risks", []) if str(item).strip()]
    reasons = [str(item) for item in getattr(assessment, "reasons", []) if str(item).strip()]
    timing_label = str(getattr(timing_advice, "timing_label", "") or "")

    notes: list[str] = [
        f"策略原始分和可信度分开看：当前可信度为 {trust_score}，等级为 {trust_level}。",
        "可信度评分不代表价格方向预测，只用于把数据、消息、趋势和风险放在一起检查。",
    ]
    if blocking:
        notes.append(f"当前先处理阻塞问题：{blocking[0]}")
    elif risks:
        notes.append(f"主要先看风险：{risks[0]}")
    elif reasons:
        notes.append(f"当前可关注点：{reasons[0]}")
    else:
        notes.append("信息仍不完整，先按保守方式观察。")
    if action_state in {"暂不参考", "只观察", "虚拟验证"}:
        notes.append(f"当前准备状态为“{action_state}”，更适合先观察或做虚拟验证。")
    else:
        notes.append(f"当前准备状态为“{action_state}”，仍需要完成前置检查后再复核。")
    if timing_label:
        notes.append(f"当前时机提示为“{timing_label}”，请结合市场时段和数据状态复核。")
    notes.append("所有结论仅用于辅助分析，不构成投资建议。")
    return notes


def build_candidate_decision_card(row: pd.Series) -> dict[str, Any]:
    """Return a compact, conservative decision card for one candidate."""
    level = str(row.get("data_warning_level", row.get("warning_level", "OK"))).upper()
    risk_pct = row_float(row, "risk_pct")
    position = row_float(row, "position250_pct")
    change_pct = row_float(row, "change_pct")
    long_score = row_float(row, "long_score", 0)
    board_score = row_float(row, "board_score", 0)
    amount_yi = row_float(row, "amount_yi")

    if level == "ERROR":
        action_label = "数据不足，暂不参考"
    elif risk_pct > 12 or position >= 88 or change_pct >= 9:
        action_label = "风险偏高"
    elif long_score >= 80 or board_score >= 80:
        action_label = "条件满足后再评估"
    elif long_score >= 70 or board_score >= 70:
        action_label = "观察"
    else:
        action_label = "继续等待"

    why_watch: list[str] = []
    if long_score >= 70:
        why_watch.append(f"长期分 {fmt_value(long_score, 1)}，趋势值得跟踪")
    if board_score >= 70:
        why_watch.append(f"打板分 {fmt_value(board_score, 1)}，短线强度进入观察")
    if np.isfinite(amount_yi) and amount_yi >= 1:
        why_watch.append(f"成交额约 {fmt_value(amount_yi)} 亿元，流动性相对可看")
    if row.get("ma_stack_signal"):
        why_watch.append(f"均线状态：{row.get('ma_stack_signal')}")
    if not why_watch:
        why_watch.append("分数或指标尚未形成足够清晰的优势，只适合放入观察")

    risks = _first_items(explain_data_warning(row) + risk_flags(row), 4)
    if bool(row.get("fallback_used", False)) and not any("备用" in item or "fallback" in item.lower() for item in risks):
        risks.insert(0, "使用备用数据源，需要先核对行情")
    if level == "WARNING" and not any("WARNING" in item or "数据" in item for item in risks):
        risks.insert(0, "数据存在 WARNING，需要先核对")

    checklist = _first_items(build_pre_trade_checklist(row), 4)
    invalid = _first_items(build_invalid_conditions(row), 4)
    trust_assessment = assess_stock_trust(row)
    timing_advice = advise_timing(trust_assessment)
    trust_payload = trust_assessment_to_dict(trust_assessment)
    timing_payload = timing_advice_to_dict(timing_advice)
    beginner_notes = build_beginner_explanation(trust_assessment, timing_advice)
    reliability_breakdown = trust_payload.get("reliability_breakdown", {})
    return {
        "code": stock_code(row.get("code", "")),
        "name": str(row.get("name", "")),
        "close": row.get("close", ""),
        "date": row.get("date", ""),
        "news_status": row.get("news_status", row.get("news_badge", "消息字段缺失")),
        "latest_news_title": row.get("latest_news_title", ""),
        "news_warning_level": row.get("news_warning_level", ""),
        "news_warning_message": row.get("news_warning_message", ""),
        "action_label": action_label,
        "raw_score": trust_payload.get("raw_score", row.get("score", "")),
        "trust_score": trust_payload.get("trust_score", ""),
        "trust_level": trust_payload.get("trust_level", ""),
        "action_state": trust_payload.get("action_state", ""),
        "timing_label": timing_payload.get("timing_label", ""),
        "timing_state": timing_payload.get("timing_state", ""),
        "reliability_breakdown": reliability_breakdown,
        "beginner_explanation": beginner_notes,
        "required_checks": trust_payload.get("required_checks", []),
        "blocking_issues": trust_payload.get("blocking_issues", []),
        "trust_disclaimer": trust_payload.get("disclaimer", "可信度评分不代表价格方向预测，仅用于辅助分析，不构成投资建议。"),
        "trust_assessment": trust_payload,
        "timing_advice": timing_payload,
        "raw_score_note": "策略原始分不等于可靠性。",
        "confidence_note": "策略原始分不等于可靠性；可信度评分不代表价格方向预测；进入深度复核状态不表示建议任何仓位，仅说明数据与规则条件相对完整。",
        "why_watch": why_watch[:3],
        "key_risks": risks[:4],
        "data_status": _data_status(row),
        "data_latest_date": row.get("data_latest_date", ""),
        "data_expected_latest_date": row.get("data_expected_latest_date", ""),
        "data_freshness_status": row.get("data_freshness_status", ""),
        "data_expected_reason": row.get("data_expected_reason", ""),
        "data_calendar_note": row.get("data_calendar_note", ""),
        "pre_trade_checklist": checklist,
        "invalid_conditions": invalid,
        "stop_loss_note": f"风险阈值观察 {fmt_value(row.get('stop'))}，距离约 {fmt_value(risk_pct)}%。",
        "position_note": position_summary(row),
        "do_not_trade_if": _first_items(build_invalid_conditions(row), 3),
        "review_note": "以下为辅助检查条件，不构成投资建议。",
    }


def _prioritize_risks(items: list[str]) -> list[str]:
    priority = ["数据", "WARNING", "备用", "fallback", "止损", "涨停", "跌停", "流动性", "成交额", "市场温度"]

    def score(item: str) -> tuple[int, int]:
        text = str(item)
        for index, keyword in enumerate(priority):
            if keyword in text:
                return index, len(text)
        return len(priority), len(text)

    return sorted([str(item) for item in items if str(item).strip()], key=score)


def _compact_items(items: list[str], limit: int = 3, *, prioritize_risk: bool = False) -> list[str]:
    values = _prioritize_risks(items) if prioritize_risk else [str(item) for item in items if str(item).strip()]
    return values[:limit]


def _compact_text(items: list[str], limit: int = 3, *, prioritize_risk: bool = False) -> str:
    values = _compact_items(items, limit, prioritize_risk=prioritize_risk)
    return "；".join(values) if values else "暂无明确项目"


def render_compact_candidate_card(card: dict[str, Any], max_bullets: int = 3) -> str:
    """Render a short decision-first card without changing score logic."""
    return "\n".join(
        [
            f"### {card['code']} {card['name']}",
            f"**可信度与小白准备状态**：{card.get('trust_score', 'N/A')} / {card.get('trust_level', 'N/A')} / {card.get('action_state', 'N/A')}",
            f"**小白提示**：{_compact_text(card.get('beginner_explanation', []), 1)}",
            f"**状态**：{card['action_label']} | **数据**：{card['data_status']}",
            f"**为什么关注**：{_compact_text(card['why_watch'], max_bullets)}",
            f"**主要风险**：{_compact_text(card['key_risks'], max_bullets, prioritize_risk=True)}",
            f"**复核前检查清单**：{_compact_text(card['pre_trade_checklist'], max_bullets, prioritize_risk=True)}",
            f"**策略失效条件**：{_compact_text(card['invalid_conditions'], max_bullets, prioritize_risk=True)}",
            f"**止损/仓位**：{card['stop_loss_note']} {card['position_note']}",
            f"**提醒**：{card['review_note']}",
        ]
    )


def build_candidate_decision_card_markdown(card: dict[str, Any], max_bullets: int = 3) -> str:
    return render_compact_candidate_card(card, max_bullets=max_bullets)


def report_toc_lines() -> list[str]:
    return [
        "今日操作简报",
        "候选决策卡片",
        "回测风险摘要",
        "数据质量",
        "学习附录",
        "风险声明",
    ]


def build_report_toc_markdown() -> str:
    return "## Report Navigation / 快速导航\n\n" + "\n".join(f"- {line}" for line in report_toc_lines())


def build_backtest_risk_summary_markdown(lines: list[str]) -> str:
    return "## Backtest Risk Summary / 回测风险摘要\n\n" + "\n".join(f"- {line}" for line in lines)


def _candidate_frame(candidates: pd.DataFrame | list[pd.DataFrame] | None) -> pd.DataFrame:
    if candidates is None:
        return pd.DataFrame()
    if isinstance(candidates, list):
        frames = [frame for frame in candidates if frame is not None and not frame.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return candidates


def build_today_decision_brief(
    market_temp: dict[str, Any] | None,
    data_quality_summary: dict[str, Any] | None,
    backtest_risk_summary: dict[str, Any] | None,
    candidates: pd.DataFrame | list[pd.DataFrame] | None = None,
) -> dict[str, Any]:
    market_temp = market_temp or {}
    data_quality_summary = data_quality_summary or {}
    backtest_risk_summary = backtest_risk_summary or {}
    candidate_frame = _candidate_frame(candidates)

    total = int(data_quality_summary.get("total", 0) or 0)
    errors = int(data_quality_summary.get("error", 0) or 0)
    warnings = int(data_quality_summary.get("warning", 0) or 0)
    temp_score = row_float(pd.Series(market_temp), "temp_score", 50)
    candidate_count = len(candidate_frame)
    if candidate_frame.empty:
        high_risk_count = 0
    else:
        levels = candidate_frame.get("data_warning_level", pd.Series("OK", index=candidate_frame.index)).astype(str).str.upper()
        risks = pd.to_numeric(candidate_frame.get("risk_pct", pd.Series(0, index=candidate_frame.index)), errors="coerce").fillna(0)
        positions = pd.to_numeric(candidate_frame.get("position250_pct", pd.Series(0, index=candidate_frame.index)), errors="coerce").fillna(0)
        high_risk_count = int(((levels.isin(["WARNING", "ERROR"])) | (risks > 12) | (positions >= 88)).sum())

    data_error_pressure = errors >= max(1, int(total * 0.2)) if total else errors > 0
    if data_error_pressure:
        suggested_mode = "数据不足，暂停参考"
    elif warnings > 0:
        suggested_mode = "只观察"
    elif np.isfinite(temp_score) and temp_score < 35:
        suggested_mode = "市场偏弱，降低频率"
    elif candidate_count and high_risk_count >= max(1, int(candidate_count * 0.5)):
        suggested_mode = "只观察"
    else:
        suggested_mode = "条件满足后再评估"

    blocked = int(float(backtest_risk_summary.get("blocked_trades_total", 0) or 0))
    turnover_triggered = str(backtest_risk_summary.get("turnover_limit_triggered", "")).lower() in {"true", "1", "yes"}
    industry_warning = bool(str(backtest_risk_summary.get("industry_concentration_warning", "") or ""))
    if blocked or turnover_triggered or industry_warning:
        backtest_risk_status = "存在回测约束或风险提示，先看审计区"
    else:
        backtest_risk_status = "未见明显审计异常，但仍是简化模型"

    if errors:
        data_status = f"ERROR {errors} 条，需先核对"
    elif warnings:
        data_status = f"WARNING {warnings} 条，先观察"
    else:
        data_status = "未见明显数据质量异常"

    return {
        "market_temperature": f"{fmt_value(temp_score, 1)} / 100 ({market_temp.get('label', '-')})",
        "data_quality_status": data_status,
        "backtest_risk_status": backtest_risk_status,
        "candidate_count": candidate_count,
        "high_risk_candidate_count": high_risk_count,
        "suggested_mode": suggested_mode,
        "review_note": "以下为辅助检查条件，不构成投资建议。",
    }


def today_decision_brief_lines(brief: dict[str, Any]) -> list[str]:
    return [
        f"Market temperature: {brief.get('market_temperature', 'N/A')}",
        f"Data quality status: {brief.get('data_quality_status', 'N/A')}",
        f"Backtest risk status: {brief.get('backtest_risk_status', 'N/A')}",
        f"Candidate count: {brief.get('candidate_count', 0)}",
        f"High risk candidate count: {brief.get('high_risk_candidate_count', 0)}",
        f"Suggested mode: {brief.get('suggested_mode', '只观察')}",
        str(brief.get("review_note", "以下为辅助检查条件，不构成投资建议。")),
    ]


def build_today_decision_brief_markdown(brief: dict[str, Any]) -> str:
    return "## Today Decision Brief / 今日操作简报\n\n" + "\n".join(
        f"- {line}" for line in today_decision_brief_lines(brief)
    )


def build_learning_appendix_items(candidates: pd.DataFrame | None = None, max_items: int = 20) -> list[dict[str, str]]:
    items = [
        {
            "title": "MA20 / MA60 / MA120",
            "content": "MA20 约等于一个月平均成本；MA60 约等于一个季度平均成本；MA120 更适合观察长期方向。",
        },
        {
            "title": "ATR",
            "content": "ATR 衡量平均波动，用来理解止损距离和仓位压力。",
        },
        {
            "title": "RSI / MACD / BOLL",
            "content": "RSI 看短线冷热，MACD 看动能方向，BOLL 看价格在波动带中的位置；都需要结合价格和成交量。",
        },
        {
            "title": "为什么先看数据质量",
            "content": "数据过期、缓存或备用源变化时，先核对同花顺日K和公告原文；数据 ERROR 的股票不适合作为今日报告依据。",
        },
        {
            "title": "为什么先看回测假设",
            "content": "历史回测不代表未来收益；当前回测仍可能没有完全模拟盘口排队、部分成交、真实撮合深度和精确停复牌公告。",
        },
        {
            "title": "为什么止损和仓位比预测更重要",
            "content": "预测会失效，止损和仓位是出错后仍能控制损失的工具；先练习条件复核、止损执行和复盘记录。",
        },
    ]
    frame = candidates if candidates is not None else pd.DataFrame()
    if not frame.empty:
        for _, row in frame.head(max_items).iterrows():
            items.append(
                {
                    "title": f"{stock_code(row.get('code', ''))} {row.get('name', '')}",
                    "content": "本期重点观察 MA20、成交额、止损距离和数据状态。",
                }
            )
    return items


def build_learning_appendix(candidates: pd.DataFrame | None = None, max_items: int = 20) -> str:
    lines = [
        "## Learning Appendix / 学习附录",
        "",
        "这个附录用于复盘和学习，不属于前面的决策页。",
    ]
    for item in build_learning_appendix_items(candidates, max_items):
        lines.extend(["", f"### {item['title']}", f"- {item['content']}"])
    return "\n".join(lines)


def risk_statement_markdown() -> str:
    return "\n".join(
        [
            "## Risk Statement / 风险声明",
            "",
            "本报告只用于研究和辅助复盘，不构成投资建议，也不会自动下单。",
            "评分、新闻、技术指标和历史回测都可能失效；实际操作前应自行核对行情、公告、仓位和止损规则。",
        ]
    )


def build_position_notes(row: pd.Series, account_config: dict[str, Any] | None = None) -> list[str]:
    return [position_summary(row), operation_advice(row)]


def build_ths_learning_notes(row: pd.Series) -> list[str]:
    return tonghuashun_steps(row)


def build_candidate_review_markdown(
    sections: list[tuple[str, pd.DataFrame]],
    market_temp: dict[str, Any] | None = None,
    *,
    limit_per_section: int = 5,
    max_bullets: int = 3,
) -> str:
    blocks: list[str] = [
        "## 可信度与小白准备状态",
        "",
        "可信度评分不代表价格方向预测，不构成投资建议。策略原始分不等于可靠性。",
        "",
        "## Candidate Decision Cards / Candidate Review Checklist / 复核前检查清单",
        "",
        "每张卡片只保留最关键的观察点。以下为辅助检查条件，不构成投资建议。",
    ]
    wrote_any = False
    for title, frame in sections:
        blocks.extend(["", f"### {title}"])
        if frame is None or frame.empty:
            blocks.append("No candidates in this section.")
            continue
        wrote_any = True
        for _, row in frame.head(limit_per_section).iterrows():
            card = build_candidate_decision_card(row)
            blocks.extend(["", build_candidate_decision_card_markdown(card, max_bullets=max_bullets)])
    if not wrote_any:
        blocks.append("")
        blocks.append("今日没有进入候选区的股票，先观察市场环境和数据质量。")
    return "\n".join(blocks)


def contains_dangerous_phrase(text: str) -> bool:
    return any(phrase in text for phrase in DANGEROUS_PHRASES)
