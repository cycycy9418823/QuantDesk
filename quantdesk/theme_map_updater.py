"""Auto theme/sector map updater (v2.8.0).

Generates and updates data/universe/stock_theme_map.csv from local sources
and optionally from AKShare.  Does NOT connect to brokers, does NOT place
orders, does NOT constitute investment advice.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import read_json as shared_read_json
from quantdesk.shared import write_csv_rows as shared_write_csv_rows
from quantdesk.shared import write_json as shared_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
UNIVERSE_DIR = DATA_DIR / "universe"
CACHE_DIR = DATA_DIR / "cache"

THEME_MAP_CSV = UNIVERSE_DIR / "stock_theme_map.csv"
THEME_MAP_PENDING_CSV = UNIVERSE_DIR / "stock_theme_map_pending.csv"
LOW_PRICE_SYMBOLS_CSV = UNIVERSE_DIR / "low_price_learning_symbols.csv"
SPOT_CACHE_CSV = CACHE_DIR / "spot_snapshot_latest.csv"

THEME_MAP_COLUMNS = [
    "symbol", "name", "sector", "industry", "concept_tags",
    "theme_tags", "primary_theme", "source", "updated_at",
]

_SECTOR_ALIASES = {"板块": "sector", "行业": "industry", "概念": "concept_tags", "题材": "theme_tags", "主题": "primary_theme"}
_INCLUDED_VALUES = {"true", "1", "yes", "y"}
UNKNOWN_SECTOR = "未知板块"
UNKNOWN_INDUSTRY = "未知行业"
UNKNOWN_THEME = "未知题材"
AKSHARE_MISSING_MESSAGE = "未安装 akshare，无法自动下载板块/概念。可 pip install akshare 后重试，或手动编辑 templates/stock_theme_map_template.csv。"


def _is_included(value: Any) -> bool:
    return str(value or "").strip().lower() in _INCLUDED_VALUES


def _zfill6(symbol: Any) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _merge_tags(*values: Any) -> str:
    tags: list[str] = []
    for value in values:
        text = str(value or "").replace("，", ";").replace(",", ";").replace("|", ";")
        for part in text.split(";"):
            tag = part.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return ";".join(tags)


def _has_mapping(row: dict[str, str]) -> bool:
    return any(str(row.get(key) or "").strip() and str(row.get(key) or "").strip() not in {UNKNOWN_SECTOR, UNKNOWN_INDUSTRY, UNKNOWN_THEME} for key in ("sector", "industry", "concept_tags", "theme_tags", "primary_theme"))


def _needs_pending(row: dict[str, str]) -> bool:
    sector = str(row.get("sector") or "").strip()
    primary = str(row.get("primary_theme") or "").strip()
    return sector in {"", UNKNOWN_SECTOR} or primary in {"", UNKNOWN_THEME}


def _normalize_output_row(row: dict[str, Any]) -> dict[str, str]:
    return {col: str(row.get(col) or "").strip() for col in THEME_MAP_COLUMNS}


def load_low_price_included(
    path: Path | None = None,
    *,
    only_included: bool = True,
    limit: int = 0,
    csv_read_warnings: list[str] | None = None,
) -> list[dict[str, str]]:
    source = path or LOW_PRICE_SYMBOLS_CSV
    if not source.exists():
        return []
    df = read_csv_frame(source, dtype=str)
    if csv_read_warnings is not None:
        csv_read_warnings.extend(str(item) for item in df.attrs.get("warnings", []) or [])
    rows = []
    for _, row in df.iterrows():
        if not only_included or _is_included(row.get("included")):
            rows.append({"symbol": _zfill6(row.get("symbol") or row.get("code")), "name": str(row.get("name") or "")})
        if limit > 0 and len(rows) >= limit:
            break
    return rows


# Round 21 F811 cleanup: the original v2.8.0 implementations of
# load_existing_theme_map / update_theme_map / theme_map_status /
# theme_map_preview and their private helpers were dead code fully shadowed
# by the v2.8.5/v2.8.6 overrides below and have been removed.

# v2.8.5 overrides ---------------------------------------------------------
#
# The original v2.8.0 updater could emit source=local rows whose fields were
# all unknown or even literal "nan".  Keep the public function names for
# backwards compatibility, but route them through stricter counting and
# sanitising rules.

UNKNOWN_CONCEPT = "未知概念"
UNKNOWN_SECTOR = "未知板块"
UNKNOWN_INDUSTRY = "未知行业"
UNKNOWN_THEME = "未知题材"
THEME_MAP_STATUS_JSON = UNIVERSE_DIR / "stock_theme_map_status.json"


def _clean_text_v285(value: Any, *, unknown: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return unknown
    return text


def _split_tags_v285(value: Any) -> list[str]:
    text = _clean_text_v285(value)
    if not text or _is_unknown_text_v285(text):
        return []
    for sep in ("，", "、", ",", "|"):
        text = text.replace(sep, ";")
    tags: list[str] = []
    for part in text.split(";"):
        item = _clean_text_v285(part)
        if item and not _is_unknown_text_v285(item) and item not in tags:
            tags.append(item)
    return tags


def _merge_tags_v285(*values: Any) -> str:
    tags: list[str] = []
    for value in values:
        for tag in _split_tags_v285(value):
            if tag not in tags:
                tags.append(tag)
    return ";".join(tags)


def _is_unknown_text_v285(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    return text.lower() in {"", "nan", "none", "null", "<na>", "unknown", "pending"} or text in {
        "未知",
        "未知板块",
        "未知行业",
        "未知题材",
        "未知概念",
        "待分类",
        "鏈煡鏉垮潡",
        "鏈煡琛屼笟",
        "鏈煡棰樻潗",
    }


def _sanitize_theme_row_v285(row: dict[str, Any]) -> dict[str, str]:
    item = {col: _clean_text_v285(row.get(col)) for col in THEME_MAP_COLUMNS}
    item["symbol"] = _zfill6(item.get("symbol"))
    item["name"] = _clean_text_v285(item.get("name"), unknown=item["symbol"])
    item["sector"] = _clean_text_v285(item.get("sector"), unknown=UNKNOWN_SECTOR)
    item["industry"] = _clean_text_v285(item.get("industry"), unknown=UNKNOWN_INDUSTRY)
    item["concept_tags"] = _merge_tags_v285(item.get("concept_tags")) or UNKNOWN_CONCEPT
    item["theme_tags"] = _merge_tags_v285(item.get("theme_tags")) or UNKNOWN_THEME
    item["primary_theme"] = _clean_text_v285(item.get("primary_theme"), unknown=UNKNOWN_THEME)
    item["source"] = _clean_text_v285(item.get("source"), unknown="pending")
    item["updated_at"] = _clean_text_v285(item.get("updated_at"))
    return item


def _row_has_mapping_v285(row: dict[str, Any]) -> bool:
    item = _sanitize_theme_row_v285(row)
    return any(
        not _is_unknown_text_v285(item.get(key))
        for key in ("sector", "industry", "concept_tags", "theme_tags", "primary_theme")
    )


def _row_is_unknown_v285(row: dict[str, Any]) -> bool:
    return not _row_has_mapping_v285(row)


def _read_theme_rows_v285(path: Path, csv_read_warnings: list[str] | None = None) -> list[dict[str, str]]:
    if not path.exists():
        return []
    frame = read_csv_frame(path, dtype=str)
    if csv_read_warnings is not None:
        csv_read_warnings.extend(str(item) for item in frame.attrs.get("warnings", []) or [])
    if frame.empty:
        return []
    return [_sanitize_theme_row_v285(dict(row)) for _, row in frame.iterrows()]


def _write_theme_rows_v285(path: Path, rows: list[dict[str, Any]]) -> None:
    shared_write_csv_rows(
        path,
        [_sanitize_theme_row_v285(row) for row in rows],
        fieldnames=THEME_MAP_COLUMNS,
        encoding="utf-8-sig",
    )


def _extract_local_fields_v285(symbol: str, spot_df: pd.DataFrame | None, universe_df: pd.DataFrame | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    aliases = {
        "sector": ["sector", "板块", "market_sector"],
        "industry": ["industry", "行业", "所属行业"],
        "concept_tags": ["concept_tags", "concept", "概念", "概念标签"],
        "theme_tags": ["theme_tags", "theme", "题材", "主题"],
        "primary_theme": ["primary_theme", "主线题材", "主题"],
    }
    for frame in (spot_df, universe_df):
        if frame is None or frame.empty:
            continue
        for col in ("symbol", "code", "代码"):
            if col not in frame.columns:
                continue
            matches = frame[frame[col].map(_zfill6) == symbol]
            if matches.empty:
                continue
            row = matches.iloc[0]
            for target, names in aliases.items():
                for name in names:
                    if name in row.index:
                        value = _clean_text_v285(row.get(name))
                        if value and not _is_unknown_text_v285(value):
                            fields[target] = _merge_tags_v285(value) if target in {"concept_tags", "theme_tags"} else value
                            break
            break
    return fields


def _import_akshare_v285() -> tuple[Any | None, str]:
    try:
        import akshare as ak  # type: ignore

        return ak, ""
    except Exception:
        return None, "未安装 akshare，无法联网补全板块/题材。可 pip install akshare 后重试，或手工编辑 stock_theme_map.csv。"


def _try_akshare_industry() -> dict[str, dict[str, str]]:  # type: ignore[override]
    ak, _ = _import_akshare_v285()
    if ak is None:
        print("未安装 akshare，无法自动下载板块/概念。可 pip install akshare 后重试，或手动编辑 templates/stock_theme_map_template.csv。")
        return {}
    data, failed_reason = _fetch_akshare_industry_v285(ak)
    if failed_reason:
        logger.warning("akshare industry name fetch failed: %s", failed_reason)
    return data


def _try_akshare_concept() -> dict[str, dict[str, str]]:  # type: ignore[override]
    ak, _ = _import_akshare_v285()
    if ak is None:
        print("未安装 akshare，无法自动下载板块/概念。可 pip install akshare 后重试，或手动编辑 templates/stock_theme_map_template.csv。")
        return {}
    data, failed_reason = _fetch_akshare_concept_v285(ak)
    if failed_reason:
        logger.warning("akshare concept name fetch failed: %s", failed_reason)
    return data


def _board_name_v285(row: Any) -> str:
    for key in ("板块名称", "概念名称", "name", "板块", "行业"):
        value = _clean_text_v285(row.get(key) if hasattr(row, "get") else "")
        if value:
            return value
    return ""


def _member_code_v285(row: Any) -> str:
    for key in ("代码", "symbol", "code"):
        code = _zfill6(row.get(key) if hasattr(row, "get") else "")
        if code:
            return code
    return ""


def _fetch_akshare_industry_v285(ak: Any) -> tuple[dict[str, dict[str, str]], str]:
    mapped: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    try:
        boards = ak.stock_board_industry_name_em()
    except Exception as exc:
        return {}, f"网络请求失败或 akshare 行业板块接口失败：{exc}"
    if boards is None or getattr(boards, "empty", False):
        return {}, "akshare 行业板块接口返回为空。"
    for _, board_row in boards.iterrows():
        board = _board_name_v285(board_row)
        if not board:
            continue
        try:
            members = ak.stock_board_industry_cons_em(symbol=board)
        except Exception as exc:
            errors.append(f"行业板块成分接口失败 {board}: {exc}")
            continue
        if members is None or getattr(members, "empty", False):
            continue
        for _, stock_row in members.iterrows():
            sym = _member_code_v285(stock_row)
            if not sym:
                continue
            item = mapped.setdefault(sym, {})
            item["industry"] = board
            item["sector"] = board
    return mapped, "; ".join(errors[:3])


def _fetch_akshare_concept_v285(ak: Any) -> tuple[dict[str, dict[str, str]], str]:
    mapped: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    try:
        boards = ak.stock_board_concept_name_em()
    except Exception as exc:
        return {}, f"网络请求失败或 akshare 概念板块接口失败：{exc}"
    if boards is None or getattr(boards, "empty", False):
        return {}, "akshare 概念板块接口返回为空。"
    for _, board_row in boards.iterrows():
        concept = _board_name_v285(board_row)
        if not concept:
            continue
        try:
            members = ak.stock_board_concept_cons_em(symbol=concept)
        except Exception as exc:
            errors.append(f"概念板块成分接口失败 {concept}: {exc}")
            continue
        if members is None or getattr(members, "empty", False):
            continue
        for _, stock_row in members.iterrows():
            sym = _member_code_v285(stock_row)
            if not sym:
                continue
            item = mapped.setdefault(sym, {})
            item["concept_tags"] = _merge_tags_v285(item.get("concept_tags"), concept)
            item["theme_tags"] = _merge_tags_v285(item.get("theme_tags"), concept)
            item["primary_theme"] = item.get("primary_theme") or concept
    return mapped, "; ".join(errors[:3])


def _theme_map_stats_v285(rows: list[dict[str, Any]], pending_rows: list[dict[str, Any]] | None = None) -> dict[str, int]:
    pending_rows = pending_rows if pending_rows is not None else [row for row in rows if _row_is_unknown_v285(row)]
    mapped = [row for row in rows if _row_has_mapping_v285(row)]
    return {
        "total": len(rows),
        "mapped_count": len(mapped),
        "total_mapped": len(mapped),
        "unknown_count": len(rows) - len(mapped),
        "pending_count": len(pending_rows),
        "manual_count": sum(1 for row in rows if str(row.get("source") or "") == "manual"),
        "local_valid_count": sum(1 for row in mapped if str(row.get("source") or "") == "local"),
        "local_count": sum(1 for row in mapped if str(row.get("source") or "") == "local"),
        "akshare_count": sum(1 for row in mapped if str(row.get("source") or "") == "akshare"),
    }


def _write_status_meta_v285(path: Path, payload: dict[str, Any]) -> None:
    try:
        shared_write_json(path, payload)
    except Exception:
        return


def _read_status_meta_v285(path: Path = THEME_MAP_STATUS_JSON) -> dict[str, Any]:
    try:
        data = shared_read_json(path, {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_existing_theme_map(
    path: Path | None = None,
    csv_read_warnings: list[str] | None = None,
) -> dict[str, dict[str, str]]:  # type: ignore[override]
    source = path or THEME_MAP_CSV
    return {row["symbol"]: row for row in _read_theme_rows_v285(source, csv_read_warnings) if row.get("symbol")}


# Round 21 F811 cleanup: the v2.8.5 update_theme_map override was dead code
# fully shadowed by the v2.8.6 provider-chain version below and was removed.


# Round 21 F811 cleanup: the v2.8.5 theme_map_status override was dead code
# fully shadowed by the v2.8.6 version below and was removed.


def theme_map_preview(limit: int = 20) -> list[dict[str, str]]:  # type: ignore[override]
    rows = _read_theme_rows_v285(THEME_MAP_CSV)
    return rows[: max(0, int(limit or 20))]


# v2.8.6 provider fallback overrides ---------------------------------------
#
# Keep the public v2.8.0/v2.8.5 API, but drive updates through a provider
# chain: local -> eastmoney_stock_profile -> akshare_em -> akshare_ths ->
# per_symbol_info, with a separate user_import source for local CSV fallback.
from quantdesk.theme_map_providers import (
    is_unknown as _provider_is_unknown_v286,
)
from quantdesk.theme_map_providers import (
    row_has_mapping as _provider_row_has_mapping_v286,
)
from quantdesk.theme_map_providers import (  # noqa: E402
    run_provider_chain as _run_theme_provider_chain_v286,
)
from quantdesk.theme_map_providers import (
    sanitize_row as _provider_sanitize_row_v286,
)

THEME_MAP_IMPORT_CSV = UNIVERSE_DIR / "stock_theme_map_import.csv"


def _write_status_meta_for_output_v286(output_path: Path, payload: dict[str, Any]) -> None:
    try:
        if output_path.resolve() != THEME_MAP_CSV.resolve():
            return
    except Exception:
        return
    _write_status_meta_v285(THEME_MAP_STATUS_JSON, payload)


def _source_counts_v286(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not _row_has_mapping_v285(row):
            continue
        source_name = str(row.get("source") or "pending")
        counts[source_name] = counts.get(source_name, 0) + 1
    return counts


def _network_provider_count_v286(source_counts: dict[str, int]) -> int:
    return sum(
        int(source_counts.get(name, 0) or 0)
        for name in ("eastmoney_stock_profile", "akshare_em", "akshare_ths", "per_symbol_info")
    )


def _load_import_symbol_rows_v286(path: Path) -> list[dict[str, str]]:
    rows = _read_theme_rows_v285(path)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = _zfill6(row.get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append({"symbol": symbol, "name": _clean_text_v285(row.get("name"), unknown=symbol)})
    return result


def _merge_symbol_rows_v286(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for group in groups:
        for row in group:
            symbol = _zfill6(row.get("symbol"))
            if not symbol:
                continue
            current = merged.setdefault(symbol, {"symbol": symbol, "name": ""})
            if not current.get("name") and _clean_text_v285(row.get("name")):
                current["name"] = _clean_text_v285(row.get("name"))
    return list(merged.values())


def _merge_theme_fields_v286(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    incoming_source = _clean_text_v285(incoming.get("source"))
    base_source = _clean_text_v285(base.get("source"))
    prefer_incoming = incoming_source in {"eastmoney_stock_profile", "akshare_em", "akshare_ths", "per_symbol_info"} and base_source in {
        "",
        "pending",
        "local",
        "seed",
        "manual_import",
        "limit_up_review_cache",
    }
    for key in ("name", "sector", "industry", "concept_tags", "theme_tags", "primary_theme"):
        if prefer_incoming and not _provider_is_unknown_v286(incoming.get(key)):
            merged[key] = incoming.get(key)
        elif _provider_is_unknown_v286(merged.get(key)) and not _provider_is_unknown_v286(incoming.get(key)):
            merged[key] = incoming.get(key)
        elif key in {"concept_tags", "theme_tags"} and not _provider_is_unknown_v286(incoming.get(key)):
            merged[key] = _merge_tags_v285(merged.get(key), incoming.get(key))
    if _provider_row_has_mapping_v286(incoming):
        merged["source"] = incoming.get("source") or merged.get("source") or "local"
    return merged


def update_theme_map(  # type: ignore[override]
    *,
    source: str = "auto",
    overwrite_manual: bool = False,
    only_low_price: bool = True,
    output_path: Path | None = None,
    pending_path: Path | None = None,
    limit: int = 0,
    input_path: Path | None = None,
) -> dict[str, Any]:
    output = output_path or THEME_MAP_CSV
    pending = pending_path or THEME_MAP_PENDING_CSV
    source = str(source or "auto").strip().lower()
    if source not in {"local", "akshare", "auto", "import"}:
        source = "auto"
    import_path = input_path or THEME_MAP_IMPORT_CSV

    input_csv_read_warnings: list[str] = []
    low_price_symbols = load_low_price_included(
        LOW_PRICE_SYMBOLS_CSV,
        only_included=only_low_price,
        limit=limit,
        csv_read_warnings=input_csv_read_warnings,
    )
    import_symbols = _load_import_symbol_rows_v286(import_path) if (source == "import" or input_path is not None) else []
    symbols = _merge_symbol_rows_v286(low_price_symbols, import_symbols)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not symbols:
        reason = "低价学习池不存在或没有 included=true 股票，且导入 CSV 没有可用股票代码。"
        result = {
            "ok": False,
            "status": "failed",
            "source": source,
            "last_update_source": source,
            "message": reason,
            "failed_reason": reason,
            "provider_results": [],
            "total": 0,
            "mapped_count": 0,
            "total_mapped": 0,
            "unknown_count": 0,
            "pending_count": 0,
            "manual_count": 0,
            "manual_import_count": 0,
            "local_valid_count": 0,
            "local_count": 0,
            "akshare_count": 0,
            "path": str(output),
            "pending_path": str(pending),
            "updated_at": now,
            "csv_read_warnings": input_csv_read_warnings,
        }
        _write_status_meta_for_output_v286(output, result)
        return result

    existing = load_existing_theme_map(output, csv_read_warnings=input_csv_read_warnings)
    provider_existing_rows = [] if source == "auto" else [
        row for row in existing.values()
        if not (overwrite_manual and str(row.get("source") or "").strip().lower() == "manual")
    ]
    if source == "akshare":
        ak, provider_reason = _import_akshare_v285()
        provider_rows = {}
        if ak is None:
            provider_results = [{
                "provider": "akshare_em",
                "status": "failed",
                "mapped": 0,
                "failed_reason": provider_reason,
                "message": "akshare provider unavailable",
            }]
        else:
            industry, industry_reason = _fetch_akshare_industry_v285(ak)
            concept, concept_reason = _fetch_akshare_concept_v285(ak)
            for sym, values in industry.items():
                provider_rows.setdefault(sym, {"symbol": sym, "source": "akshare_em"}).update(values)
            for sym, values in concept.items():
                row = provider_rows.setdefault(sym, {"symbol": sym, "source": "akshare_em"})
                row.update({key: value for key, value in values.items() if key not in {"concept_tags", "theme_tags"}})
                row["concept_tags"] = _merge_tags_v285(row.get("concept_tags"), values.get("concept_tags"))
                row["theme_tags"] = _merge_tags_v285(row.get("theme_tags"), values.get("theme_tags"))
            provider_rows = {
                sym: _provider_sanitize_row_v286(row, source="akshare_em")
                for sym, row in provider_rows.items()
                if _provider_row_has_mapping_v286(row)
            }
            provider_reason = "; ".join(part for part in (industry_reason, concept_reason) if part)
            provider_results = [{
                "provider": "akshare_em",
                "status": "ok" if provider_rows else "failed",
                "mapped": len(provider_rows),
                "failed_reason": provider_reason or ("" if provider_rows else "akshare interface returned no usable theme mapping"),
                "message": f"akshare provider mapped {len(provider_rows)} symbols",
            }]
    else:
        provider_rows, provider_results = _run_theme_provider_chain_v286(
            symbols,
            source=source,
            existing_rows=provider_existing_rows,
            spot_path=SPOT_CACHE_CSV,
            low_price_path=LOW_PRICE_SYMBOLS_CSV,
            import_path=import_path,
        )
    affected_symbols = sorted(
        sym for sym, row in provider_rows.items()
        if _provider_row_has_mapping_v286(row)
    )

    rows: list[dict[str, str]] = []
    for item in symbols:
        sym = item["symbol"]
        current = existing.get(sym, {})
        if current.get("source") == "manual" and not overwrite_manual:
            row = _sanitize_theme_row_v285({**current, "symbol": sym, "name": current.get("name") or item.get("name")})
            rows.append(row)
            continue
        current_for_merge = {} if current.get("source") == "manual" and overwrite_manual else current
        base = {col: "" for col in THEME_MAP_COLUMNS}
        base.update(current_for_merge)
        base["symbol"] = sym
        base["name"] = item.get("name") or current.get("name") or sym
        incoming = provider_rows.get(sym, {})
        merged = _merge_theme_fields_v286(base, incoming)
        if not _clean_text_v285(merged.get("primary_theme")) or _is_unknown_text_v285(merged.get("primary_theme")):
            tags = _split_tags_v285(merged.get("theme_tags")) or _split_tags_v285(merged.get("concept_tags"))
            if tags:
                merged["primary_theme"] = tags[0]
        merged["updated_at"] = now
        row = _sanitize_theme_row_v285(_provider_sanitize_row_v286(merged, source=str(merged.get("source") or "pending")))
        if not _row_has_mapping_v285(row):
            row["source"] = "pending"
        rows.append(row)

    pending_rows = [{**row, "source": "pending"} for row in rows if _row_is_unknown_v285(row)]
    _write_theme_rows_v285(output, rows)
    _write_theme_rows_v285(pending, pending_rows)

    stats = _theme_map_stats_v285(rows, pending_rows)
    source_counts = _source_counts_v286(rows)
    eastmoney_count = source_counts.get("eastmoney_stock_profile", 0)
    network_provider_count = _network_provider_count_v286(source_counts)
    provider_failures = [
        str(row.get("failed_reason") or row.get("message") or "")
        for row in provider_results
        if row.get("status") in {"failed", "warning"} and str(row.get("failed_reason") or row.get("message") or "")
    ]
    csv_read_warnings = list(input_csv_read_warnings) + [
        str(warning)
        for row in provider_results
        for warning in (row.get("warnings") or [])
        if warning
    ]
    provider_warnings = "; ".join(provider_failures[:4])
    failed_reason = "" if stats["pending_count"] == 0 else provider_warnings
    if stats["mapped_count"] == 0 and not failed_reason:
        failed_reason = "未能补全任何股票，原因：股票代码未匹配到行业/概念成分。"
    if source == "import" and stats["mapped_count"] == 0 and not import_path.exists():
        failed_reason = f"导入文件不存在：{import_path}"
    if source == "akshare" and stats["mapped_count"] == 0 and "失败" not in failed_reason:
        failed_reason = f"akshare 接口失败：{failed_reason or '外部数据源不可用'}"

    if source == "akshare" and stats["mapped_count"] == 0 and "akshare" not in failed_reason.lower():
        failed_reason = f"akshare 接口失败：{failed_reason or '外部数据源不可用'}"

    status = "ok" if stats["mapped_count"] > 0 else "warning"
    ok = not (source == "akshare" and stats["mapped_count"] == 0)
    if source == "import":
        ok = True
    if source == "auto":
        ok = True
    message = (
        f"已补全 {stats['mapped_count']} 只股票板块/题材，仍有 {stats['pending_count']} 只待补。"
        if stats["mapped_count"] > 0
        else f"未能补全任何股票，原因：{failed_reason or '外部数据源不可用或网络连接被中断'}。"
    )
    if stats["mapped_count"] == 0:
        message += " 可稍后重试，或使用 stock_theme_map_import.csv 手工导入。"

    result = {
        "ok": ok,
        "status": status,
        "source": source,
        "last_update_source": source,
        "akshare_used": any(row.get("provider") in {"akshare_em", "akshare_ths", "per_symbol_info"} and row.get("mapped", 0) for row in provider_results),
        "network_provider_used": network_provider_count > 0,
        "path": str(output),
        "output_path": str(output),
        "pending_path": str(pending),
        "pending_exists": pending.exists(),
        "import_path": str(import_path),
        "updated_at": now,
        "failed_reason": failed_reason,
        "provider_warnings": provider_warnings,
        "csv_read_warnings": csv_read_warnings,
        "provider_results": provider_results,
        "affected_symbols": affected_symbols,
        "affected_count": len(affected_symbols),
        "provider_error": failed_reason,
        "provider_unavailable": bool(failed_reason),
        "message": message,
        "source_counts": source_counts,
        "stats": {
            "total": stats["total"],
            "mapped": stats["mapped_count"],
            "unknown": stats["unknown_count"],
            "manual": stats["manual_count"],
            "manual_import": source_counts.get("manual_import", 0),
            "akshare": source_counts.get("akshare_em", 0) + source_counts.get("akshare_ths", 0) + source_counts.get("per_symbol_info", 0),
            "eastmoney": eastmoney_count,
            "network_provider": network_provider_count,
            "local": stats["local_valid_count"],
        },
        **stats,
        "manual_import_count": source_counts.get("manual_import", 0),
        "eastmoney_count": eastmoney_count,
        "network_provider_count": network_provider_count,
        "akshare_count": source_counts.get("akshare_em", 0) + source_counts.get("akshare_ths", 0) + source_counts.get("per_symbol_info", 0),
        "local_valid_count": source_counts.get("local", stats["local_valid_count"]),
        "local_count": source_counts.get("local", stats["local_valid_count"]),
    }
    _write_status_meta_for_output_v286(output, result)
    return result


def import_theme_map(input_path: Path, **kwargs: Any) -> dict[str, Any]:
    return update_theme_map(source="import", input_path=input_path, **kwargs)


def theme_map_status(path: Path | None = None) -> dict[str, Any]:  # type: ignore[override]
    source = path or THEME_MAP_CSV
    pending = source.with_name("stock_theme_map_pending.csv") if source.name == "stock_theme_map.csv" else THEME_MAP_PENDING_CSV
    rows = _read_theme_rows_v285(source)
    pending_rows = _read_theme_rows_v285(pending)
    stats = _theme_map_stats_v285(rows, pending_rows)
    meta = _read_status_meta_v285()
    source_counts = _source_counts_v286(rows)
    eastmoney_count = source_counts.get("eastmoney_stock_profile", 0)
    network_provider_count = _network_provider_count_v286(source_counts)
    updated_at = max((str(row.get("updated_at") or "") for row in rows), default="")
    failed_reason = str(meta.get("failed_reason") or "")
    if rows and stats["mapped_count"] == 0 and not failed_reason:
        failed_reason = "本地文件未包含真实板块/题材映射。"
    provider_results = meta.get("provider_results") if isinstance(meta.get("provider_results"), list) else []
    return {
        "ok": True,
        "exists": source.exists(),
        "path": str(source),
        "total": stats["total"],
        "total_rows": stats["total"],
        "mapped_count": stats["mapped_count"],
        "total_mapped": stats["mapped_count"],
        "unknown_count": stats["unknown_count"],
        "pending_count": stats["pending_count"],
        "manual_count": stats["manual_count"],
        "manual_import_count": source_counts.get("manual_import", 0),
        "eastmoney_count": eastmoney_count,
        "network_provider_count": network_provider_count,
        "local_valid_count": source_counts.get("local", stats["local_valid_count"]),
        "local_count": source_counts.get("local", stats["local_valid_count"]),
        "akshare_count": source_counts.get("akshare_em", 0) + source_counts.get("akshare_ths", 0) + source_counts.get("per_symbol_info", 0),
        "pending_exists": pending.exists(),
        "pending_path": str(pending),
        "import_path": str(THEME_MAP_IMPORT_CSV),
        "failed_reason": failed_reason,
        "provider_warnings": str(meta.get("provider_warnings") or ""),
        "provider_results": provider_results,
        "last_update_source": str(meta.get("last_update_source") or meta.get("source") or ""),
        "updated_at": updated_at or str(meta.get("updated_at") or ""),
        "message": "板块/题材映射状态只读，不会触发联网。",
    }
