"""Generate a local-cache report for the low-price learning universe.

This script is intentionally local-only:

* read ``data/universe/low_price_learning_symbols.csv``;
* keep only rows with ``included == True``;
* read only the matching ``data/daily/{symbol}.csv`` files;
* write a lightweight Markdown report and JSON payload;
* never connect to AKShare, Sina, Eastmoney, brokers, or any network.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.corporate_action_repair import repair_corporate_actions  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.learning_universe import load_low_price_universe, low_price_universe_path  # noqa: E402
from quantdesk.market_clock import expected_settlement_date  # noqa: E402
from quantdesk.utils import atomic_write_json, safe_float  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION  # noqa: E402

REPORT_TYPE = "low_price_cache_report"
REPORT_DISCLAIMER = "本报告仅供学习观察与信息整理，不构成投资建议。"
DEFAULT_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_RESULT_JSON = PROJECT_ROOT / "results" / "low_price_learning_report_latest.json"


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    if digits:
        return digits.zfill(6)
    return ""


def _normalize_daily_columns(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "date",
        "date": "date",
        "收盘": "close",
        "close": "close",
        "开盘": "open",
        "open": "open",
        "最高": "high",
        "high": "high",
        "最低": "low",
        "low": "low",
        "成交量": "volume",
        "volume": "volume",
        "成交额": "amount",
        "amount": "amount",
    }
    rename = {col: aliases[col] for col in frame.columns if col in aliases}
    return frame.rename(columns=rename)


def _read_daily_csv(symbol: str, daily_dir: Path) -> pd.DataFrame:
    path = daily_dir / f"{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = read_csv_frame(path, encoding="utf-8-sig")
    csv_read_warnings = list(frame.attrs.get("warnings") or [])
    if any(item.startswith("csv_read_failed:") for item in csv_read_warnings):
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty
    if frame.empty:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty

    frame = _normalize_daily_columns(frame)
    if "date" not in frame.columns or "close" not in frame.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty

    keep = [
        col
        for col in ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg")
        if col in frame.columns
    ]
    frame = frame[keep].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[frame["close"] > 0]
    if frame.empty:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty
    out = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    # ma20 here is a trailing mean, so an unadjusted split cliff inside the last
    # 20 sessions corrupts it and therefore close_vs_ma20_pct, which downstream
    # signal gating keys on. The repair is anchored to the newest bar, so
    # latest_close is unchanged; only the trailing history is restated.
    out, ca_info = repair_corporate_actions(out)
    if ca_info.get("unverifiable"):
        csv_read_warnings.append(f"unverifiable_corporate_action:{ca_info.get('reason') or ''}")
    elif ca_info.get("ca_bars"):
        csv_read_warnings.append(f"corporate_actions_repaired:{ca_info['ca_bars']}")
    out.attrs["warnings"] = csv_read_warnings
    return out


def _included_rows(universe_path: Path, limit: int = 0) -> pd.DataFrame:
    frame = load_low_price_universe(universe_path)
    if frame.empty or "included" not in frame.columns:
        return pd.DataFrame()
    included = frame[frame["included"].astype(bool)].copy()
    if included.empty:
        return included
    included["symbol"] = included["symbol"].map(_normalize_symbol)
    included = included[included["symbol"] != ""].drop_duplicates(subset=["symbol"], keep="first")
    if limit and limit > 0:
        included = included.head(limit)
    return included


def _expected_trade_date(value: str | None = None) -> str:
    if value:
        parsed = pd.to_datetime(value, errors="coerce")
        if not pd.isna(parsed):
            return parsed.strftime("%Y-%m-%d")
    expected = expected_settlement_date()
    return expected.isoformat() if expected else ""


def _liquidity_status(amount_ma20: float | None, has_amount: bool, has_volume: bool) -> tuple[str, str]:
    if amount_ma20 is not None:
        if amount_ma20 >= 30_000_000:
            return "ok", "20日均成交额处于可读范围，仅作数据质量观察。"
        if amount_ma20 >= 10_000_000:
            return "low", "20日均成交额偏低，规则检查需谨慎。"
        return "very_low", "20日均成交额很低，流动性观察不足。"
    if has_amount or has_volume:
        return "unknown", "缺少可计算20日均成交额的数据，仅能展示部分字段。"
    return "unknown", "缺少成交额和成交量字段，无法判断成交活跃度。"


def _fmt(value: object, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}"


def analyze_low_price_universe(
    universe_path: Path | None = None,
    daily_dir: Path | None = None,
    *,
    limit: int = 0,
    min_rows: int = 120,
    expected_trade_date: str | None = None,
) -> dict[str, Any]:
    universe_path = universe_path or low_price_universe_path()
    daily_dir = daily_dir or DEFAULT_DAILY_DIR
    generated_at = datetime.now().isoformat(timespec="seconds")
    expected_date = _expected_trade_date(expected_trade_date)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    included = _included_rows(universe_path, limit=limit)
    for _, item in included.iterrows():
        symbol = _normalize_symbol(item.get("symbol"))
        name = str(item.get("name") or symbol)
        price = safe_float(item.get("price"))
        daily = _read_daily_csv(symbol, daily_dir)
        csv_read_warnings = list(daily.attrs.get("warnings") or [])

        row: dict[str, Any] = {
            "symbol": symbol,
            "name": name,
            "universe_price": price,
            "included": True,
            "expected_trade_date": expected_date,
            "latest_date": "",
            "is_stale": bool(expected_date),
            "stale_reason": "缺少本地日线缓存，无法确认最新完整日线。",
            "row_count": 0,
            "latest_close": None,
            "ma20": None,
            "close_vs_ma20_pct": None,
            "amount_ma20": None,
            "volume_ma20": None,
            "liquidity_status": "unknown",
            "liquidity_note": "缺少成交额和成交量字段，无法判断成交活跃度。",
            "data_confidence_status": "blocked",
            "data_confidence_score": 0,
            "data_confidence_note": "缺少本地日线缓存，无法进入自动模拟。",
            "missing_required_fields": ["daily_cache", "amount_ma20", "liquidity_status"],
            "missing_optional_fields": ["volume_ma20"],
            "block_reasons": ["缺少 data/daily 本地日线 CSV"],
            "warning_reasons": [],
            "has_amount_field": False,
            "has_volume_field": False,
            "data_status": "missing_cache",
            "data_status_label": "缺少本地日线",
            "notes": ["未找到本地日线缓存，请先更新低价学习池日线。"],
        }
        row["csv_read_warnings"] = csv_read_warnings
        if daily.empty:
            rows.append(row)
            continue

        close = daily["close"].astype(float)
        row_count = int(len(daily))
        latest = daily.iloc[-1]
        latest_date = latest["date"].strftime("%Y-%m-%d")
        latest_close = safe_float(latest.get("close"))
        ma20 = safe_float(close.tail(20).mean()) if row_count >= 20 else None
        has_amount = "amount" in daily.columns and daily["amount"].notna().any()
        has_volume = "volume" in daily.columns and daily["volume"].notna().any()
        amount_ma20 = safe_float(daily["amount"].tail(20).mean()) if has_amount and row_count >= 20 else None
        volume_ma20 = safe_float(daily["volume"].tail(20).mean()) if has_volume and row_count >= 20 else None
        liquidity_status, liquidity_note = _liquidity_status(amount_ma20, has_amount, has_volume)

        is_stale = bool(expected_date and latest_date < expected_date)
        stale_reason = (
            "本地日线缓存尚未更新到理论交易日。本报告只读本地缓存，不联网补数。"
            if is_stale
            else ""
        )
        row.update(
            {
                "latest_date": latest_date,
                "is_stale": is_stale,
                "stale_reason": stale_reason,
                "row_count": row_count,
                "latest_close": latest_close,
                "ma20": ma20,
                "amount_ma20": amount_ma20,
                "volume_ma20": volume_ma20,
                "liquidity_status": liquidity_status,
                "liquidity_note": liquidity_note,
                "has_amount_field": has_amount,
                "has_volume_field": has_volume,
            }
        )
        if latest_close is not None and ma20:
            row["close_vs_ma20_pct"] = round((latest_close / ma20 - 1) * 100, 2)
        notes: list[str] = []
        if row_count < max(1, int(min_rows)):
            row["data_status"] = "insufficient_history"
            row["data_status_label"] = "历史不足"
            row["data_confidence_status"] = "warning"
            row["data_confidence_score"] = 60
            row["data_confidence_note"] = "本地历史行数不足，不能作为严肃研究样本。"
            notes.append(f"本地日线只有 {row_count} 行，少于 {min_rows} 行；仅适合轻量观察。")
        else:
            row["data_status"] = "cache_ready"
            row["data_status_label"] = "缓存可读"
            row["data_confidence_status"] = "ok"
            row["data_confidence_score"] = 90
            row["data_confidence_note"] = "本地日线历史长度达到当前报告要求。"
            notes.append("本地日线缓存可读；本报告仍只用于学习观察。")
        if is_stale:
            notes.append(stale_reason)
            row["warning_reasons"] = [*row.get("warning_reasons", []), "本地日线滞后于理论交易日"]
        if liquidity_note:
            notes.append(liquidity_note)
        missing_required = []
        if amount_ma20 is None:
            missing_required.append("amount_ma20")
        if liquidity_status == "unknown":
            missing_required.append("liquidity_status")
        row["missing_required_fields"] = missing_required
        row["missing_optional_fields"] = ["volume_ma20"] if volume_ma20 is None else []
        row["block_reasons"] = ["liquidity_status=very_low，成交活跃度过低"] if liquidity_status == "very_low" else []
        row["notes"] = notes
        rows.append(row)

    status_counts: dict[str, int] = {}
    liquidity_counts = {"ok": 0, "low": 0, "very_low": 0, "unknown": 0}
    latest_dates = [str(row.get("latest_date") or "") for row in rows if row.get("latest_date")]
    for row in rows:
        key = str(row.get("data_status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
        liquidity_key = str(row.get("liquidity_status") or "unknown")
        liquidity_counts[liquidity_key if liquidity_key in liquidity_counts else "unknown"] += 1
    if not rows:
        warnings.append("低价学习池没有 included=true 的股票，未生成明细。")
    stale_count = sum(1 for row in rows if row.get("is_stale"))
    missing_cache_count = int(status_counts.get("missing_cache", 0))
    insufficient_history_count = int(status_counts.get("insufficient_history", 0))
    readable_count = int(status_counts.get("cache_ready", 0))
    summary = {
        "expected_trade_date": expected_date,
        "latest_complete_daily_date": max(latest_dates) if latest_dates else "",
        "total_symbols": int(len(included)),
        "included_count": int(len(included)),
        "analyzed_count": int(len(rows)),
        "readable_count": readable_count,
        "missing_cache_count": missing_cache_count,
        "insufficient_history_count": insufficient_history_count,
        "stale_count": stale_count,
        "up_to_date_count": max(0, len(rows) - stale_count - missing_cache_count),
        "liquidity_ok_count": liquidity_counts["ok"],
        "liquidity_low_count": liquidity_counts["low"],
        "liquidity_very_low_count": liquidity_counts["very_low"],
        "liquidity_unknown_count": liquidity_counts["unknown"],
    }

    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": generated_at,
        "report_type": REPORT_TYPE,
        "universe_path": str(universe_path),
        "daily_dir": str(daily_dir),
        "included_count": int(len(included)),
        "analyzed_count": int(len(rows)),
        "min_rows": int(min_rows),
        "expected_trade_date": expected_date,
        "latest_complete_daily_date": summary["latest_complete_daily_date"],
        "status_counts": status_counts,
        "summary": summary,
        "rows": rows,
        "items": rows,
        "warnings": warnings,
        "disclaimer": REPORT_DISCLAIMER,
    }


def build_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    expected = summary.get("expected_trade_date") or payload.get("expected_trade_date") or ""
    latest = summary.get("latest_complete_daily_date") or payload.get("latest_complete_daily_date") or ""
    lines = [
        "# 低价学习池本地缓存报告",
        "",
        REPORT_DISCLAIMER,
        "",
        "## 摘要",
        "",
        f"- 生成时间：{payload.get('generated_at', '')}",
        f"- 低价学习池：{payload.get('universe_path', '')}",
        f"- 本地日线目录：{payload.get('daily_dir', '')}",
        f"- included=true 数量：{payload.get('included_count', 0)}",
        f"- 已分析数量：{payload.get('analyzed_count', 0)}",
        f"- 缓存可读：{summary.get('readable_count', 0)}",
        f"- 缺失缓存：{summary.get('missing_cache_count', 0)}",
        f"- 历史不足：{summary.get('insufficient_history_count', 0)}",
        f"- 日线滞后：{summary.get('stale_count', 0)}",
        f"- 活跃度 OK / 偏低 / 很低 / 未知：{summary.get('liquidity_ok_count', 0)} / {summary.get('liquidity_low_count', 0)} / {summary.get('liquidity_very_low_count', 0)} / {summary.get('liquidity_unknown_count', 0)}",
        f"- 数据状态分布：{json.dumps(payload.get('status_counts', {}), ensure_ascii=False)}",
        "",
        "## 数据日期说明",
        "",
        f"- 理论应有日线日期：{expected or '未知'}",
        f"- 本地最新完整日线：{latest or '暂无'}",
        "- 本报告只读 data/daily 本地缓存，不联网补数。",
        "- 如果本地日线滞后，请运行：`python scripts/update_low_price_universe_daily.py --workers 4`。",
        "",
        "## 成交活跃度说明",
        "",
        "- amount_ma20 表示最近 20 个交易日平均成交额。",
        "- liquidity_status 仅用于数据质量观察，不代表价格方向判断。",
        "- 缺少 amount/volume 字段时无法判断成交活跃度。",
        "",
        "## 明细",
        "",
        "| 代码 | 名称 | 状态 | 行数 | 最新日期 | 最新收盘 | MA20 | 收盘相对MA20 | 20日均成交额 | 活跃度 | 日期状态 | 说明 |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in payload.get("rows", []):
        notes = "；".join(str(item) for item in row.get("notes", []))
        date_status = "滞后" if row.get("is_stale") else "正常"
        lines.append(
            "| {symbol} | {name} | {status} | {rows} | {date} | {close} | {ma20} | {pct}% | {amount_ma20} | {liquidity} | {date_status} | {notes} |".format(
                symbol=row.get("symbol", ""),
                name=row.get("name", ""),
                status=row.get("data_status_label", ""),
                rows=row.get("row_count", 0),
                date=row.get("latest_date", ""),
                close=_fmt(row.get("latest_close")),
                ma20=_fmt(row.get("ma20")),
                pct=_fmt(row.get("close_vs_ma20_pct")),
                amount_ma20=_fmt(row.get("amount_ma20"), 0),
                liquidity=row.get("liquidity_status", ""),
                date_status=date_status,
                notes=notes,
            )
        )
    lines.extend(
        [
            "",
            "## 边界说明",
            "",
            "- 本报告不联网，只读取本地 CSV 缓存。",
            "- 本报告不扫描全 A，只处理低价学习池 included=true 的股票。",
            "- 本报告不改变普通日报、策略、评分或交易路径回放逻辑。",
            "- 本报告不构成投资建议，不预测价格，不承诺收益。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(
    payload: dict[str, Any],
    output_dir: Path | None = None,
    result_json: Path | None = None,
) -> dict[str, str]:
    output_dir = output_dir or DEFAULT_REPORT_DIR
    result_json = result_json or DEFAULT_RESULT_JSON
    output_dir.mkdir(parents=True, exist_ok=True)
    result_json.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = output_dir / f"low_price_learning_report_{stamp}.md"
    markdown_path.write_text(build_markdown(payload), encoding="utf-8")
    payload["report_path"] = str(markdown_path)
    payload["markdown_report"] = str(markdown_path)
    payload["result_path"] = str(result_json)
    atomic_write_json(result_json, payload)
    return {"markdown_report": str(markdown_path), "result_json": str(result_json)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local cache report for the low-price learning universe.")
    parser.add_argument("--universe-csv", default=str(low_price_universe_path()))
    parser.add_argument("--daily-dir", default=str(DEFAULT_DAILY_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--result-json", default=str(DEFAULT_RESULT_JSON))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--expected-trade-date", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    universe_path = Path(args.universe_csv)
    if not universe_path.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "message": "低价学习池不存在，请先构建低价学习池。",
                    "universe_path": str(universe_path),
                    "hint": "python scripts/build_low_price_universe.py --max-price 20 --buffer-price 22 --min-amount 0",
                },
                ensure_ascii=False,
            )
        )
        return 2
    payload = analyze_low_price_universe(
        universe_path=universe_path,
        daily_dir=Path(args.daily_dir),
        limit=max(0, int(args.limit or 0)),
        min_rows=max(1, int(args.min_rows or 120)),
        expected_trade_date=str(args.expected_trade_date or ""),
    )
    files = write_outputs(payload, output_dir=Path(args.output_dir), result_json=Path(args.result_json))
    payload["files"] = files
    print(json.dumps({"ok": True, "message": "低价学习池本地缓存报告已生成。", "files": files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
