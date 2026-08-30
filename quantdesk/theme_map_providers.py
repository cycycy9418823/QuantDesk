"""Provider fallbacks for stock theme/sector mapping.

All providers are optional and best-effort. Network providers are only called
from user-triggered CLI/API flows; importing this module never connects to the
network.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from quantdesk.csv_io import read_csv_frame

THEME_MAP_COLUMNS = [
    "symbol",
    "name",
    "sector",
    "industry",
    "concept_tags",
    "theme_tags",
    "primary_theme",
    "source",
    "updated_at",
]

UNKNOWN_SECTOR = "未知板块"
UNKNOWN_INDUSTRY = "未知行业"
UNKNOWN_CONCEPT = "未知概念"
UNKNOWN_THEME = "未知题材"
PENDING_THEME = "待分类"

FIELD_ALIASES = {
    "symbol": ["symbol", "code", "代码", "股票代码", "证券代码"],
    "name": ["name", "名称", "股票名称", "证券简称"],
    "sector": ["sector", "板块", "大板块", "market_sector"],
    "industry": ["industry", "行业", "所属行业", "申万行业", "行业板块"],
    "concept_tags": ["concept_tags", "concept", "概念", "概念标签", "概念板块"],
    "theme_tags": ["theme_tags", "theme", "题材", "题材标签", "主题"],
    "primary_theme": ["primary_theme", "主线题材", "主题", "主題", "主线"],
}


FIELD_ALIASES["symbol"] = ["symbol", "code", "代码", "股票代码", "证券代码"] + FIELD_ALIASES["symbol"]
FIELD_ALIASES["name"] = ["name", "名称", "股票名称", "证券简称"] + FIELD_ALIASES["name"]
FIELD_ALIASES["sector"] = ["sector", "板块", "大板块", "market_sector"] + FIELD_ALIASES["sector"]
FIELD_ALIASES["industry"] = ["industry", "行业", "所属行业", "申万行业", "行业板块"] + FIELD_ALIASES["industry"]
FIELD_ALIASES["concept_tags"] = ["concept_tags", "concept", "概念", "概念标签", "概念板块"] + FIELD_ALIASES["concept_tags"]
FIELD_ALIASES["theme_tags"] = ["theme_tags", "theme", "题材", "题材标签", "主题"] + FIELD_ALIASES["theme_tags"]
FIELD_ALIASES["primary_theme"] = ["primary_theme", "主线题材", "主题", "主线"] + FIELD_ALIASES["primary_theme"]


@dataclass
class ProviderResult:
    provider: str
    status: str
    mapped: int = 0
    failed_reason: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_symbol(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def clean_text(value: Any, unknown: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return unknown
    return text


def is_unknown(value: Any) -> bool:
    text = clean_text(value)
    return text.lower() in {"", "nan", "none", "null", "<na>", "unknown", "pending"} or text in {
        UNKNOWN_SECTOR,
        UNKNOWN_INDUSTRY,
        UNKNOWN_CONCEPT,
        UNKNOWN_THEME,
        PENDING_THEME,
        "未知",
    }


def split_tags(value: Any) -> list[str]:
    text = clean_text(value)
    if not text or is_unknown(text):
        return []
    for sep in ("；", "，", "、", ",", "|", "/"):
        text = text.replace(sep, ";")
    tags: list[str] = []
    for part in text.split(";"):
        tag = clean_text(part)
        if tag and not is_unknown(tag) and tag not in tags:
            tags.append(tag)
    return tags


def merge_tags(*values: Any) -> str:
    tags: list[str] = []
    for value in values:
        for tag in split_tags(value):
            if tag not in tags:
                tags.append(tag)
    return ";".join(tags)


def row_has_mapping(row: dict[str, Any]) -> bool:
    return any(
        not is_unknown(row.get(key))
        for key in ("sector", "industry", "concept_tags", "theme_tags", "primary_theme")
    )


def sanitize_row(row: dict[str, Any], *, source: str = "") -> dict[str, str]:
    item = {col: clean_text(row.get(col)) for col in THEME_MAP_COLUMNS}
    item["symbol"] = normalize_symbol(item.get("symbol"))
    item["name"] = clean_text(item.get("name"), item["symbol"])
    item["sector"] = clean_text(item.get("sector"), UNKNOWN_SECTOR)
    item["industry"] = clean_text(item.get("industry"), UNKNOWN_INDUSTRY)
    item["concept_tags"] = merge_tags(item.get("concept_tags")) or UNKNOWN_CONCEPT
    item["theme_tags"] = merge_tags(item.get("theme_tags")) or UNKNOWN_THEME
    item["primary_theme"] = clean_text(item.get("primary_theme"), UNKNOWN_THEME)
    item["source"] = clean_text(item.get("source"), source or "pending")
    return item


def _find_value(row: Any, aliases: list[str]) -> str:
    if not hasattr(row, "get"):
        return ""
    for alias in aliases:
        value = clean_text(row.get(alias))
        if value and not is_unknown(value):
            return value
    return ""


def _read_csv(path: Path | None) -> pd.DataFrame | None:
    if not path or not path.exists():
        return None
    return read_csv_frame(path, dtype=str)


def _extract_mapping_from_frame(frame: pd.DataFrame | None, requested: set[str], source: str) -> dict[str, dict[str, str]]:
    if frame is None or frame.empty:
        return {}
    symbol_col = next((col for col in FIELD_ALIASES["symbol"] if col in frame.columns), "")
    if not symbol_col:
        return {}
    mapping: dict[str, dict[str, str]] = {}
    for _, raw in frame.iterrows():
        symbol = normalize_symbol(raw.get(symbol_col))
        if not symbol or symbol not in requested:
            continue
        row: dict[str, str] = {"symbol": symbol, "source": source}
        for field_name in ("name", "sector", "industry", "concept_tags", "theme_tags", "primary_theme"):
            value = _find_value(raw, FIELD_ALIASES[field_name])
            if field_name in {"concept_tags", "theme_tags"}:
                value = merge_tags(value)
            if value:
                row[field_name] = value
        if row_has_mapping(row):
            mapping[symbol] = sanitize_row(row, source=source)
    return mapping


def local_provider(
    symbols: list[dict[str, str]],
    *,
    spot_path: Path | None,
    low_price_path: Path | None,
    existing_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    requested = {normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol"))}
    mapping: dict[str, dict[str, str]] = {}
    csv_read_warnings: list[str] = []
    for row in existing_rows or []:
        symbol = normalize_symbol(row.get("symbol"))
        if symbol in requested and row_has_mapping(row):
            mapping[symbol] = sanitize_row(row, source=clean_text(row.get("source"), "local"))
    for source_path in (spot_path, low_price_path):
        frame = _read_csv(source_path)
        if frame is not None:
            csv_read_warnings.extend(str(item) for item in frame.attrs.get("warnings", []) or [])
        extracted = _extract_mapping_from_frame(frame, requested, "local")
        for symbol, row in extracted.items():
            current = mapping.setdefault(symbol, {"symbol": symbol, "source": "local"})
            for key in ("name", "sector", "industry", "concept_tags", "theme_tags", "primary_theme"):
                if is_unknown(current.get(key)) and not is_unknown(row.get(key)):
                    current[key] = row[key]
            current["source"] = "local"
    mapped = sum(1 for row in mapping.values() if row_has_mapping(row))
    status = "ok" if mapped else "warning"
    reason = "" if mapped else "本地文件未包含行业/题材字段"
    return mapping, ProviderResult("local", status, mapped, reason, "本地字段提取完成。", csv_read_warnings)


def _import_akshare() -> tuple[Any | None, str]:
    try:
        import akshare as ak  # type: ignore

        return ak, ""
    except Exception:
        return None, "未安装 akshare"


def _board_name(row: Any) -> str:
    for key in ("板块名称", "概念名称", "行业名称", "name", "板块", "行业"):
        value = clean_text(row.get(key) if hasattr(row, "get") else "")
        if value:
            return value
    return ""


def _member_symbol(row: Any) -> str:
    for key in ("代码", "股票代码", "symbol", "code"):
        symbol = normalize_symbol(row.get(key) if hasattr(row, "get") else "")
        if symbol:
            return symbol
    return ""


def _call_board_provider(
    ak: Any,
    *,
    provider: str,
    requested: set[str],
    industry_name_func: str,
    industry_cons_func: str,
    concept_name_func: str,
    concept_cons_func: str,
) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    mapping: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    any_interface = False

    def read_boards(name_func: str, cons_func: str, kind: str) -> None:
        nonlocal any_interface
        name_callable = getattr(ak, name_func, None)
        cons_callable = getattr(ak, cons_func, None)
        if not callable(name_callable) or not callable(cons_callable):
            errors.append(f"{kind}接口不可用")
            return
        any_interface = True
        try:
            boards = name_callable()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kind}列表接口失败：{exc}")
            return
        if boards is None or getattr(boards, "empty", False):
            errors.append(f"{kind}列表接口返回为空")
            return
        for _, board_row in boards.iterrows():
            board = _board_name(board_row)
            if not board:
                continue
            try:
                members = cons_callable(symbol=board)
            except TypeError:
                try:
                    members = cons_callable(board)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{kind}成分接口失败 {board}: {exc}")
                    continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind}成分接口失败 {board}: {exc}")
                continue
            if members is None or getattr(members, "empty", False):
                continue
            for _, stock_row in members.iterrows():
                symbol = _member_symbol(stock_row)
                if symbol not in requested:
                    continue
                item = mapping.setdefault(symbol, {"symbol": symbol, "source": provider})
                if kind == "行业":
                    item["industry"] = board
                    item["sector"] = board
                else:
                    item["concept_tags"] = merge_tags(item.get("concept_tags"), board)
                    item["theme_tags"] = merge_tags(item.get("theme_tags"), board)
                    item["primary_theme"] = item.get("primary_theme") or board

    read_boards(industry_name_func, industry_cons_func, "行业")
    read_boards(concept_name_func, concept_cons_func, "概念")
    mapping = {symbol: sanitize_row(row, source=provider) for symbol, row in mapping.items() if row_has_mapping(row)}
    mapped = len(mapping)
    if mapped:
        return mapping, ProviderResult(provider, "ok", mapped, "; ".join(errors[:3]), f"{provider} 补全 {mapped} 只。")
    reason = "; ".join(errors[:3]) if errors else ("接口不可用" if not any_interface else "股票代码未匹配到行业/概念成分")
    return mapping, ProviderResult(provider, "failed", 0, reason, f"{provider} 未补全股票。")


def akshare_em_provider(symbols: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    ak, reason = _import_akshare()
    if ak is None:
        return {}, ProviderResult("akshare_em", "failed", 0, reason, "东方财富接口未执行。")
    requested = {normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol"))}
    return _call_board_provider(
        ak,
        provider="akshare_em",
        requested=requested,
        industry_name_func="stock_board_industry_name_em",
        industry_cons_func="stock_board_industry_cons_em",
        concept_name_func="stock_board_concept_name_em",
        concept_cons_func="stock_board_concept_cons_em",
    )


def _eastmoney_secid(symbol: str) -> str:
    market = "1" if symbol.startswith(("5", "6", "9")) else "0"
    return f"{market}.{symbol}"


def eastmoney_stock_profile_provider(symbols: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    """Fetch per-symbol industry/concepts from Eastmoney's lightweight quote API."""
    mapping: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    fields = "f57,f58,f127,f129"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    for row in symbols:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        try:
            response = requests.get(
                url,
                params={"secid": _eastmoney_secid(symbol), "fields": fields},
                headers=headers,
                timeout=6,
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
            continue
        industry = clean_text(data.get("f127"))
        concepts = merge_tags(data.get("f129"))
        name = clean_text(data.get("f58"), clean_text(row.get("name")))
        values = {
            "symbol": symbol,
            "name": name,
            "sector": industry,
            "industry": industry,
            "concept_tags": concepts or industry,
            "theme_tags": concepts or industry,
            "primary_theme": industry or (split_tags(concepts)[0] if concepts else ""),
            "source": "eastmoney_stock_profile",
        }
        if row_has_mapping(values):
            mapping[symbol] = sanitize_row(values, source="eastmoney_stock_profile")
    mapped = len(mapping)
    if mapped:
        return mapping, ProviderResult(
            "eastmoney_stock_profile",
            "ok",
            mapped,
            "; ".join(errors[:3]),
            f"东方财富个股资料补全 {mapped} 只。",
        )
    reason = "; ".join(errors[:3]) if errors else "东方财富个股资料未返回行业/概念"
    return mapping, ProviderResult("eastmoney_stock_profile", "failed", 0, reason, "东方财富个股资料未补全股票。")


def akshare_ths_provider(symbols: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    ak, reason = _import_akshare()
    if ak is None:
        return {}, ProviderResult("akshare_ths", "failed", 0, reason, "同花顺接口未执行。")
    requested = {normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol"))}
    return _call_board_provider(
        ak,
        provider="akshare_ths",
        requested=requested,
        industry_name_func="stock_board_industry_name_ths",
        industry_cons_func="stock_board_industry_cons_ths",
        concept_name_func="stock_board_concept_name_ths",
        concept_cons_func="stock_board_concept_cons_ths",
    )


def per_symbol_info_provider(symbols: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    ak, reason = _import_akshare()
    if ak is None:
        return {}, ProviderResult("per_symbol_info", "failed", 0, reason, "逐股基础信息未执行。")
    info_callable = getattr(ak, "stock_individual_info_em", None)
    if not callable(info_callable):
        return {}, ProviderResult("per_symbol_info", "failed", 0, "stock_individual_info_em 接口不可用", "逐股基础信息接口不可用。")
    mapping: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for row in symbols:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        try:
            frame = info_callable(symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
            continue
        if frame is None or getattr(frame, "empty", False):
            continue
        values: dict[str, str] = {"symbol": symbol, "name": clean_text(row.get("name")), "source": "per_symbol_info"}
        for _, info_row in frame.iterrows():
            item = clean_text(info_row.get("item") if hasattr(info_row, "get") else "")
            value = clean_text(info_row.get("value") if hasattr(info_row, "get") else "")
            if not value:
                continue
            if item in {"行业", "所属行业", "行业板块"}:
                values["industry"] = value
                values["sector"] = value
            elif item in {"概念", "概念板块", "题材"}:
                values["concept_tags"] = merge_tags(values.get("concept_tags"), value)
                values["theme_tags"] = merge_tags(values.get("theme_tags"), value)
                values["primary_theme"] = values.get("primary_theme") or split_tags(value)[0] if split_tags(value) else value
        if row_has_mapping(values):
            mapping[symbol] = sanitize_row(values, source="per_symbol_info")
    mapped = len(mapping)
    if mapped:
        return mapping, ProviderResult("per_symbol_info", "ok", mapped, "; ".join(errors[:3]), f"逐股基础信息补全 {mapped} 只。")
    reason = "; ".join(errors[:3]) if errors else "逐股基础信息未返回行业/题材"
    return mapping, ProviderResult("per_symbol_info", "failed", 0, reason, "逐股基础信息未补全股票。")


def user_import_provider(symbols: list[dict[str, str]], input_path: Path | None) -> tuple[dict[str, dict[str, str]], ProviderResult]:
    if input_path is None:
        return {}, ProviderResult("user_import", "failed", 0, "未提供导入文件路径", "未导入。")
    if not input_path.exists():
        return {}, ProviderResult("user_import", "failed", 0, f"导入文件不存在：{input_path}", "未导入。")
    requested = {normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol"))}
    frame = _read_csv(input_path)
    csv_read_warnings = list(frame.attrs.get("warnings", []) or []) if frame is not None else []
    mapping = _extract_mapping_from_frame(frame, requested, "manual_import")
    mapped = len(mapping)
    status = "ok" if mapped else "failed"
    reason = "" if mapped else "导入文件未匹配到低价池股票或未包含真实板块/题材字段"
    return mapping, ProviderResult("user_import", status, mapped, reason, f"手工导入 {mapped} 只。", csv_read_warnings)


def merge_provider_rows(base: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    for key in ("name", "sector", "industry", "concept_tags", "theme_tags", "primary_theme"):
        if is_unknown(merged.get(key)) and not is_unknown(incoming.get(key)):
            merged[key] = incoming[key]
        elif key in {"concept_tags", "theme_tags"} and not is_unknown(incoming.get(key)):
            merged[key] = merge_tags(merged.get(key), incoming.get(key))
    if row_has_mapping(incoming):
        merged["source"] = clean_text(incoming.get("source"), merged.get("source") or "local")
    return sanitize_row(merged, source=merged.get("source") or "pending")


def run_provider_chain(
    symbols: list[dict[str, str]],
    *,
    source: str,
    existing_rows: list[dict[str, Any]] | None,
    spot_path: Path | None,
    low_price_path: Path | None,
    import_path: Path | None = None,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    source = (source or "auto").strip().lower()
    if source == "akshare":
        provider_names = ["akshare_em", "akshare_ths", "per_symbol_info"]
    elif source == "import":
        provider_names = ["user_import"]
    elif source == "local":
        provider_names = ["local"]
    else:
        provider_names = ["local", "eastmoney_stock_profile", "akshare_em", "akshare_ths", "per_symbol_info"]

    final: dict[str, dict[str, str]] = {}
    results: list[dict[str, Any]] = []
    for name in provider_names:
        remaining_symbols = [
            row for row in symbols
            if not row_has_mapping(final.get(normalize_symbol(row.get("symbol")), {}))
        ]
        if name != "local" and not remaining_symbols:
            results.append(
                ProviderResult(
                    name,
                    "skipped",
                    0,
                    "前序 provider 已覆盖本次请求股票。",
                    f"{name} 已跳过。",
                ).to_dict()
            )
            continue
        provider_symbols = symbols if name == "local" else remaining_symbols
        if name == "local":
            mapping, result = local_provider(provider_symbols, spot_path=spot_path, low_price_path=low_price_path, existing_rows=existing_rows)
        elif name == "eastmoney_stock_profile":
            mapping, result = eastmoney_stock_profile_provider(provider_symbols)
        elif name == "akshare_em":
            mapping, result = akshare_em_provider(provider_symbols)
        elif name == "akshare_ths":
            mapping, result = akshare_ths_provider(provider_symbols)
        elif name == "per_symbol_info":
            mapping, result = per_symbol_info_provider(provider_symbols)
        elif name == "user_import":
            mapping, result = user_import_provider(provider_symbols, import_path)
        else:
            mapping, result = {}, ProviderResult(name, "failed", 0, "未知 provider")
        for symbol, row in mapping.items():
            final[symbol] = merge_provider_rows(final.get(symbol, {"symbol": symbol, "source": "pending"}), row)
        results.append(result.to_dict())
    return final, results
