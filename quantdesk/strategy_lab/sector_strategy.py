from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from quantdesk.strategy_lab.research_signal_safety import RESEARCH_ONLY_NOTE, boundary_payload


UNKNOWN_MARKERS = {"", "nan", "none", "null", "unknown", "未知", "未知行业", "未知板块", "未知题材", "未知概念", "待分类"}


def _normalize_symbol(symbol: Any) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _meaningful_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in UNKNOWN_MARKERS or text in UNKNOWN_MARKERS:
        return ""
    return text


def load_sector_theme_map(root: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Read formal/imported theme maps for strategy-lab sector grouping.

    The map is evidence only. Missing or unknown rows deliberately fall back to a
    visible proxy label rather than inventing an industry.
    """
    base = Path(root) if root is not None else Path.cwd()
    universe = base / "data" / "universe"
    rows_by_symbol: dict[str, dict[str, str]] = {}
    for path in (universe / "stock_theme_map.csv", universe / "stock_theme_map_import.csv"):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = _normalize_symbol(row.get("symbol") or row.get("ts_code") or row.get("code"))
                if not symbol or symbol in rows_by_symbol:
                    continue
                industry = _meaningful_text(row.get("industry"))
                sector = _meaningful_text(row.get("sector"))
                primary_theme = _meaningful_text(row.get("primary_theme"))
                concept_tags = _meaningful_text(row.get("concept_tags"))
                theme_tags = _meaningful_text(row.get("theme_tags"))
                sector_name = industry or sector or primary_theme or (concept_tags.split(";")[0] if concept_tags else "") or (theme_tags.split(";")[0] if theme_tags else "")
                if not sector_name:
                    continue
                rows_by_symbol[symbol] = {
                    "sector_name": sector_name,
                    "industry": industry,
                    "sector": sector,
                    "primary_theme": primary_theme,
                    "concept_tags": concept_tags,
                    "theme_tags": theme_tags,
                    "source": _meaningful_text(row.get("source")) or path.name,
                }
    return rows_by_symbol


def sector_name(symbol: str) -> str:
    code = _normalize_symbol(symbol)
    if code.startswith("60"):
        return "sh_main_board_proxy"
    if code.startswith("30"):
        return "growth_board_proxy"
    if code.startswith("00"):
        return "sz_main_board_proxy"
    if code.startswith("68"):
        return "star_board_proxy"
    return "other_board_proxy"


def _sector_for_metric(metric: dict[str, Any], theme_map: dict[str, dict[str, str]]) -> tuple[str, str, str, dict[str, str]]:
    symbol = _normalize_symbol(metric.get("symbol"))
    mapped = theme_map.get(symbol) or {}
    if mapped.get("sector_name"):
        return mapped["sector_name"], "stock_theme_map", "mapped_theme_or_industry", mapped
    return sector_name(symbol), "symbol_prefix_proxy", "sector_proxy_mapping_warning", {}


def load_classic_sector_rotation(root: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    base = Path(root) if root is not None else Path.cwd()
    path = base / "results" / "sector_rotation_latest.json"
    if not path.exists():
        return {}, {"available": False, "status": "missing", "path": str(path).replace("\\", "/")}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {"available": False, "status": "unreadable", "path": str(path).replace("\\", "/"), "error": str(exc)}
    if not isinstance(payload, dict):
        return {}, {"available": False, "status": "invalid", "path": str(path).replace("\\", "/")}
    data_source = str(payload.get("data_source") or "")
    if data_source == "synthetic_test_data":
        return {}, {
            "available": True,
            "used": False,
            "status": "synthetic_ignored",
            "path": str(path).replace("\\", "/"),
            "reason": "classic sector rotation is synthetic_test_data and is not used for strategy-lab ordering",
        }
    rows = payload.get("sector_rankings") if isinstance(payload.get("sector_rankings"), list) else []
    by_sector: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        sector = _meaningful_text(raw.get("sector_name"))
        if not sector:
            continue
        by_sector[sector] = dict(raw)
    return by_sector, {
        "available": bool(by_sector),
        "used": bool(by_sector),
        "status": "loaded" if by_sector else "empty",
        "path": str(path).replace("\\", "/"),
        "sector_count": len(by_sector),
        "target_date": payload.get("target_date") or "",
        "data_source": data_source or "local_sector_rotation",
    }


def _classic_sector_score(row: dict[str, Any]) -> float | None:
    for key in ("heat_score", "composite_strength_score", "rs_20d", "return_20d"):
        value = row.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def generate_sector_rotation(
    metrics: list[dict],
    *,
    root: str | Path | None = None,
    theme_map: dict[str, dict[str, str]] | None = None,
    classic_sector_rotation: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict, list[dict]]:
    resolved_theme_map = theme_map if theme_map is not None else load_sector_theme_map(root)
    if classic_sector_rotation is None:
        classic_sector_rotation, classic_context = load_classic_sector_rotation(root)
    else:
        classic_context = {
            "available": bool(classic_sector_rotation),
            "used": bool(classic_sector_rotation),
            "status": "provided" if classic_sector_rotation else "empty",
            "sector_count": len(classic_sector_rotation),
        }
    grouped = defaultdict(list)
    for m in metrics:
        sec, source, risk_flag, mapped = _sector_for_metric(m, resolved_theme_map)
        item = dict(m)
        item["_sector_name"] = sec
        item["_sector_mapping_source"] = source
        item["_sector_risk_flag"] = risk_flag
        item["_sector_theme"] = mapped.get("primary_theme") or mapped.get("theme_tags") or mapped.get("concept_tags") or ""
        grouped[sec].append(item)
    sector_scores: dict[str, float] = {}
    sector_score_source: dict[str, str] = {}
    for name, rows_for_sector in grouped.items():
        classic = classic_sector_rotation.get(name) if isinstance(classic_sector_rotation, dict) else None
        classic_score = _classic_sector_score(classic or {})
        if classic_score is not None:
            sector_scores[name] = classic_score
            sector_score_source[name] = "classic_sector_rotation_engine"
        else:
            sector_scores[name] = mean([x["return_20d"] for x in rows_for_sector]) - mean([x["volatility"] for x in rows_for_sector]) * 0.1
            sector_score_source[name] = "strategy_lab_metric_proxy"
    ranked_sectors = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
    mapped_count = sum(1 for rows_for_sector in grouped.values() for item in rows_for_sector if item.get("_sector_mapping_source") == "stock_theme_map")
    total_count = sum(len(rows_for_sector) for rows_for_sector in grouped.values())
    mapping_coverage = mapped_count / total_count if total_count else 0.0
    rows = []
    for rank, (sec, score) in enumerate(ranked_sectors[:5], start=1):
        stocks = sorted(grouped[sec], key=lambda x: x["return_20d"], reverse=True)[:8]
        classic = classic_sector_rotation.get(sec) if isinstance(classic_sector_rotation, dict) else {}
        classic = classic if isinstance(classic, dict) else {}
        for m in stocks:
            mapping_source = str(m.get("_sector_mapping_source") or "symbol_prefix_proxy")
            risk_flag = str(m.get("_sector_risk_flag") or "sector_proxy_mapping_warning")
            rows.append(
                {
                    "sector_name": sec,
                    "sector_score": score,
                    "sector_rank": rank,
                    "stock_symbol": m["symbol"],
                    "stock_name": m.get("name", ""),
                    "sector_mapping_source": mapping_source,
                    "sector_score_source": sector_score_source.get(sec, "strategy_lab_metric_proxy"),
                    "classic_sector_rank": classic.get("current_rank"),
                    "classic_sector_heat_score": classic.get("heat_score"),
                    "classic_sector_status": classic.get("sector_status") or classic.get("heat_status") or "",
                    "theme_tags": m.get("_sector_theme", ""),
                    "triggered_strategy_families": "sector_rotation_family",
                    "risk_flags": risk_flag,
                    "research_reason": "classic_sector_rotation_heat" if sector_score_source.get(sec) == "classic_sector_rotation_engine" else "relative_sector_strength_from_theme_map" if mapping_source == "stock_theme_map" else "relative_sector_strength_proxy",
                    "prohibited_usage_note": RESEARCH_ONLY_NOTE,
                }
            )
    if mapping_coverage >= 0.999 and total_count:
        status = "completed_with_theme_mapping"
    elif mapping_coverage > 0:
        status = "completed_with_partial_theme_mapping"
    else:
        status = "completed_with_proxy_mapping"
    summary = {
        "task_id": "v420_sector_rotation",
        "status": status,
        "sector_count": len(grouped),
        "mapped_stock_count": mapped_count,
        "total_stock_count": total_count,
        "mapping_coverage": mapping_coverage,
        "classic_sector_rotation": classic_context,
        "top_sectors": [
            {
                "sector_name": s,
                "sector_score": v,
                "sector_rank": i + 1,
                "sector_mapping_source": "stock_theme_map" if any(item.get("_sector_mapping_source") == "stock_theme_map" for item in grouped[s]) else "symbol_prefix_proxy",
                "sector_score_source": sector_score_source.get(s, "strategy_lab_metric_proxy"),
            }
            for i, (s, v) in enumerate(ranked_sectors[:10])
        ],
        **boundary_payload(),
    }
    return summary, rows
