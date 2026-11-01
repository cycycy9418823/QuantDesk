"""Summarize recorded paper-account observations beside local index points.

No transaction history is reconstructed. Output is printed unless an explicit
output path is supplied. All dates in the account series come from the saved
valuation_date field, not file or report generation timestamps.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper.performance_report import (  # noqa: E402
    compare_observed_endpoints, finite_number, iso_date, observed_history, read_index_series,
)

BENCHMARKS = (("hs300", "沪深300"), ("sh000001", "上证指数"), ("zz500", "中证500"))


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value, hashlib.sha256(data).hexdigest()


def _calendar_gaps(path: Path | None, points: list[Any]) -> dict[str, Any]:
    if path is None or len(points) < 2:
        return {"status": "unavailable", "missing_dates": [], "sha256": ""}
    try:
        data = path.read_bytes()
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        lookup: dict[str, bool] = {}
        conflicts: set[str] = set()
        for row in rows:
            day = iso_date(row.get("date"))
            value = str(row.get("is_open") or "").strip().lower()
            if not day or value not in {"true", "false", "1", "0"}:
                continue
            flag = value in {"true", "1"}
            if day in lookup and lookup[day] != flag:
                conflicts.add(day)
            lookup[day] = flag
        first, last = date.fromisoformat(points[0].date), date.fromisoformat(points[-1].date)
        required = [(first + timedelta(days=offset)).isoformat()
                    for offset in range((last - first).days + 1)]
        checksum = hashlib.sha256(data).hexdigest()
        if any(day not in lookup or day in conflicts for day in required):
            return {"status": "calendar_incomplete_or_conflicting", "missing_dates": [], "sha256": checksum}
        observed = {point.date for point in points}
        missing = [day for day in required if lookup[day] and day not in observed]
        return {"status": "available", "missing_dates": missing, "sha256": checksum}
    except (OSError, UnicodeError, csv.Error):
        return {"status": "unavailable", "missing_dates": [], "sha256": ""}


def make_payload(history_path: Path, portfolio_path: Path, benchmark_dir: Path,
                 calendar_path: Path | None = None) -> dict[str, Any]:
    history, history_sha = _read_json(history_path)
    portfolio, portfolio_sha = _read_json(portfolio_path)
    observed = observed_history(history)
    comparisons: dict[str, dict[str, Any]] = {}
    for name, label in BENCHMARKS:
        path = benchmark_dir / f"{name}.csv"
        index = read_index_series(path)
        comparison = ({"available": False, "reason": "history_conflicting_or_invalid"}
                      if observed.conflicting_dates or observed.invalid_count
                      else compare_observed_endpoints(observed.points, index))
        comparisons[name] = {"label": label, "source": str(path), "sha256": index.sha256,
                             **comparison}
    reconciliation = portfolio.get("accounting_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    identity = reconciliation.get("pnl_identity")
    identity = identity if isinstance(identity, dict) else {}
    rows = portfolio.get("positions")
    positions = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    valuation_date = iso_date(portfolio.get("valuation_date"))
    stale_marks = sum(1 for row in positions if not valuation_date
                      or iso_date(row.get("valuation_price_date")) != valuation_date)
    stale_factors = sum(1 for row in positions
                        if str(row.get("price_adjustment_status") or "") == "stale_adj_factor")
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "history_source": str(history_path), "history_sha256": history_sha,
        "portfolio_source": str(portfolio_path), "portfolio_sha256": portfolio_sha,
        "points": [asdict(point) for point in observed.points],
        "undated_count": observed.undated_count, "invalid_count": observed.invalid_count,
        "conflicting_dates": observed.conflicting_dates, "invalid_dates": observed.invalid_dates,
        "duplicate_count": observed.duplicate_count,
        "calendar": _calendar_gaps(calendar_path, observed.points),
        "comparisons": comparisons,
        "portfolio": {
            "valuation_date": valuation_date,
            "updated_at": portfolio.get("updated_at"),
            "initial_cash": finite_number(portfolio.get("initial_cash")),
            "total_equity": finite_number(portfolio.get("total_equity")),
            "cash": finite_number(portfolio.get("cash")),
            "market_value": finite_number(portfolio.get("market_value")),
            "reconciliation_status": reconciliation.get("status") or "unknown",
            "identity_difference": finite_number(identity.get("difference")),
            "position_count": len(positions), "stale_or_missing_marks": stale_marks,
            "stale_adjustment_factors": stale_factors,
        },
        "boundary": "paper research; recorded values not independently reconciled; no broker",
    }


def build_report(payload: dict[str, Any]) -> str:
    points = payload["points"]
    book = payload["portfolio"]
    lines = [
        "# 策略账与指数：已有记录对照",
        "",
        f"> 生成时间 {payload['generated_at']}。纸面 · 未验证，仅供学习和研究。",
        "",
        "## 先看结论",
        "",
    ]
    if len(points) >= 2:
        first, last = points[0], points[-1]
        change = (last["equity"] / first["equity"] - 1) * 100
        lines.append(f"在有明确估值日期的记录中，{first['date']} 的账面权益为 {first['equity']:,.2f} 元，"
                     f"{last['date']} 为 {last['equity']:,.2f} 元，账面变化 {change:+.2f}%。"
                     "这些是保存过的数值，不能视为经过完整对账的投资收益率。")
    else:
        lines.append("明确估值日期且金额能对上的记录不足两条，不能计算区间变化。")
    difference = book["identity_difference"]
    diff_text = f"{difference:+.2f} 元" if difference is not None else "未记录"
    lines.append(f"最新账本记录估值日 {book['valuation_date'] or '未记录'}；"
                 f"权益与盈亏的对账差额 {diff_text}，对账状态 {book['reconciliation_status']}。"
                 "金额和原因需继续核验，不能通过改账来消除差额。")
    lines.append(f"相对该账本估值日，{book['position_count']} 个持仓中，估值日期旧或缺失 "
                 f"{book['stale_or_missing_marks']} 个，复权因子明确标记过期 "
                 f"{book['stale_adjustment_factors']} 个。是否已更新到最近交易日需要另行核对。")
    lines.extend([
        "", "## 相同起止日期的对照", "",
        "| 记录 | 起始日期 | 结束日期 | 账面权益变化 | 指数点位变化 |",
        "|---|---|---|---:|---:|",
    ])
    for item in payload["comparisons"].values():
        if item["available"]:
            lines.append(f"| {item['label']} | {item['start']} | {item['end']} | "
                         f"{item['recorded_equity_change_pct']:+.2f}% | {item['index_change_pct']:+.2f}% |")
        else:
            reason = {
                "endpoint_missing": "指数缺少同日端点",
                "endpoint_conflict": "指数端点有冲突记录",
                "history_conflicting_or_invalid": "账本历史有冲突或异常记录",
                "insufficient_dated_observations": "有日期的账本记录不足",
                "missing_file": "指数文件缺失",
                "missing_columns": "指数文件缺少日期或收盘点位",
                "unreadable_file": "指数文件无法读取",
            }.get(item["reason"], "数据尚未核实")
            lines.append(f"| {item['label']} | — | — | — | {reason} |")
    lines.extend([
        "", "指数这里只是本地文件的收盘点位变化；成分、分红、基金费用及跟踪误差没有在这里核对。"
        "它与账面权益并列展示，不能单凭差异认定策略有或没有稳定优势。",
        "", "## 记录缺口", "",
        f"- {payload['undated_count']} 条历史记录没有明确估值日期，未用生成时间补日期；生成时间不等于估值日期。",
        f"- {payload['invalid_count']} 条金额或结构不合格；冲突日期 "
        + ("、".join(payload["conflicting_dates"]) or "无") + "；未把冲突值合并为一个事实。",
    ])
    calendar = payload["calendar"]
    if calendar["status"] == "available":
        missing = calendar["missing_dates"]
        lines.append(f"- 按本地交易日历，区间内缺少 {len(missing)} 个交易日的合格日期记录："
                     + ("、".join(missing) if missing else "无") + "。")
    else:
        lines.append("- 本地交易日历未完整核实，缺少多少交易日暂不能判断。")
    partial = [point["date"] for point in points if point["review_valid"] is not True]
    lines.append("- 完整复盘未确认的已记录日期：" + ("、".join(partial) if partial else "无") + "。"
                 "即便标记复盘完成，也不代表资金流水与公司行动已经核实。")
    lines.extend([
        "", "## 证据和限制", "",
        f"- 原历史记录：`{payload['history_source']}`，SHA-256 `{payload['history_sha256']}`。",
        f"- 原账本：`{payload['portfolio_source']}`，SHA-256 `{payload['portfolio_sha256']}`。",
        "- 不根据当前持仓或已平仓摘要倒推逐日买卖、现金流、胜率或最大回撤；"
        "缺失日期没有插值。入出金、完整成交、费用和分红送转仍需核实。",
        "- 当前材料不能证明哪种策略能赚钱；要更改策略，需先固定比较方案、独立样本外区间和回滚条件。",
        "", "Research Only / Paper Account Only / No Broker Route.", "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=PROJECT_ROOT / "results" / "auto_paper_portfolio_history.json")
    parser.add_argument("--portfolio", type=Path, default=PROJECT_ROOT / "state" / "auto_paper_portfolio.json")
    parser.add_argument("--benchmark-dir", type=Path, default=PROJECT_ROOT / "data" / "benchmark")
    parser.add_argument("--calendar", type=Path, default=PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv")
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args(argv)
    outputs = [path for path in (args.out_md, args.out_json) if path is not None]
    inputs = [args.history, args.portfolio, args.calendar]
    inputs.extend(args.benchmark_dir / f"{name}.csv" for name, _ in BENCHMARKS)
    def aliases(first: Path, second: Path) -> bool:
        if first.resolve() == second.resolve():
            return True
        return first.exists() and second.exists() and first.samefile(second)
    try:
        conflicts = (len(outputs) > 1 and aliases(outputs[0], outputs[1]))
        conflicts = conflicts or any(aliases(output, source) for output in outputs for source in inputs)
    except OSError:
        print("无法核实报告输出和输入文件是否相同。", file=sys.stderr)
        return 2
    if conflicts:
        print("报告输出不能覆盖账本、历史、日历、指数原件或彼此覆盖。", file=sys.stderr)
        return 2
    try:
        payload = make_payload(args.history, args.portfolio, args.benchmark_dir, args.calendar)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法读取账本记录：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report = build_report(payload)
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(report, encoding="utf-8")
    else:
        print(report)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
