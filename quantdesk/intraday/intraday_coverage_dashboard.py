"""Intraday coverage summary for local QuantDesk 1m data."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, short_error_text, truthy, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
LOW_PRICE_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "data_health" / "intraday_coverage_summary_latest.json"
DISCLAIMER = "分钟线覆盖率仅用于判断盘中触发可验证性，不构成投资建议。"


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _load_low_price_symbols(max_symbols: int = 50, *, universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV) -> list[str]:
    path = _project_path(universe_path)
    if not path.exists() or not path.is_file():
        return []
    symbols: list[str] = []
    limit = max(0, int(max_symbols or 0)) or 50
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if "included" in row and not truthy(row.get("included")):
                    continue
                symbol = normalize_symbol(row.get("symbol") or row.get("code"))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
                if len(symbols) >= limit:
                    break
    except Exception:
        return []
    return symbols


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] in {"-", "/"}:
        return text[:10].replace("/", "-")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def _candidate_paths(symbol: str, intraday_dir: str | Path, period: str) -> list[Path]:
    base = _project_path(intraday_dir)
    return [
        base / period / f"{symbol}.csv",
        base / f"{symbol}.csv",
        base / f"{symbol}_{period}.csv",
    ]


def _count_target_rows(path: Path, target_date: str) -> tuple[int, int, str]:
    total = 0
    matched = 0
    has_date_column = False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                total += 1
                raw_date = row.get("date") or row.get("trade_date") or row.get("datetime") or row.get("time")
                if raw_date:
                    has_date_column = True
                    if _date_text(raw_date) == target_date:
                        matched += 1
        if target_date and has_date_column:
            return matched, total, "date_filtered"
        return total, total, "date_unknown" if not has_date_column else "all_rows"
    except Exception as exc:  # noqa: BLE001
        return 0, total, f"parse_error:{short_error_text(exc)}"


def inspect_intraday_cache(
    symbol: str,
    *,
    target_date: str,
    intraday_dir: str | Path = DEFAULT_INTRADAY_DIR,
    period: str = "1m",
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    existing = next((path for path in _candidate_paths(normalized, intraday_dir, period) if path.exists()), None)
    if not existing:
        return {
            "has_intraday": False,
            "row_count": 0,
            "total_row_count": 0,
            "status": "missing_intraday",
            "path": "",
            "reason": "intraday_file_missing",
        }
    row_count, total, reason = _count_target_rows(existing, _date_text(target_date))
    covered = row_count > 0
    return {
        "has_intraday": covered,
        "row_count": row_count,
        "total_row_count": total,
        "status": "covered" if covered else "missing_intraday",
        "path": _rel(existing),
        "reason": "" if covered else reason,
    }


def compute_intraday_symbol_coverage(symbols: dict[str, dict[str, Any]]) -> dict[str, Any]:
    covered = [symbol for symbol, row in symbols.items() if row.get("has_intraday")]
    missing = [symbol for symbol, row in symbols.items() if not row.get("has_intraday")]
    total = len(symbols)
    return {
        "covered_symbols": covered,
        "missing_symbols": missing,
        "covered_count": len(covered),
        "missing_count": len(missing),
        "coverage_rate": round(len(covered) / total, 6) if total else 0.0,
    }


def compute_intraday_date_coverage(symbols: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        symbol: {
            "row_count": row.get("row_count", 0),
            "status": row.get("status"),
        }
        for symbol, row in symbols.items()
    }


def summarize_missing_intraday(symbols: dict[str, dict[str, Any]]) -> list[str]:
    return [symbol for symbol, row in symbols.items() if not row.get("has_intraday")]


def build_intraday_coverage_summary(
    symbols: Any = None,
    *,
    target_date: str = "",
    network: bool = False,
    intraday_dir: str | Path = DEFAULT_INTRADAY_DIR,
    period: str = "1m",
    max_symbols: int = 50,
    low_price_universe_path: str | Path = LOW_PRICE_UNIVERSE_CSV,
    write: bool = False,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> dict[str, Any]:
    explicit = _split_symbols(symbols)
    selected = explicit or _load_low_price_symbols(max_symbols=max_symbols, universe_path=low_price_universe_path)
    selected = selected[: max(0, int(max_symbols or 0)) or 50] if not explicit else selected
    target = _date_text(target_date) or now_text()[:10]
    base = _project_path(intraday_dir)
    rows = {
        symbol: inspect_intraday_cache(symbol, target_date=target, intraday_dir=base, period=period)
        for symbol in selected
    }
    coverage = compute_intraday_symbol_coverage(rows)
    warnings: list[str] = []
    if not base.exists():
        warnings.append("未找到本地分钟线目录，无法验证盘中触发；不会用日线冒充分钟线。")
    if coverage["missing_count"]:
        warnings.append("缺少分钟线时不能验证盘中触发，请导入历史 1 分钟线 CSV 或在目标交易日尝试 provider。")
    if not explicit and not selected:
        warnings.append("未传入 symbols，且低价学习池没有可用 included=true 样本；不会默认扫描全 A。")
    status = "passed" if coverage["missing_count"] == 0 and selected else "warning"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "intraday_coverage_summary",
        "status": status,
        "target_date": target,
        "symbol_count": len(rows),
        "covered_count": coverage["covered_count"],
        "missing_count": coverage["missing_count"],
        "coverage_rate": coverage["coverage_rate"],
        "period": period,
        "network": bool(network),
        "full_scan": False,
        "intraday_dir": _rel(base),
        "intraday_dir_exists": base.exists(),
        "symbol_source": "explicit" if explicit else "low_price_included_sample",
        "symbols": rows,
        "date_coverage": compute_intraday_date_coverage(rows),
        "warnings": [short_error_text(item) for item in warnings if item],
        "suggested_next_action": "缺少分钟线时不能验证盘中触发，请导入历史 1 分钟线 CSV 或在目标交易日尝试 provider。",
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_intraday_coverage_summary(payload, output_path)
    return payload


def write_intraday_coverage_summary(payload: dict[str, Any], output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    return write_json(_project_path(output_path), payload)

