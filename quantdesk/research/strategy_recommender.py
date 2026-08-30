from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DISCLAIMER = "研究观察，不构成买卖建议。No broker. No real orders."
STRATEGIES = ("long_term", "long_swing", "board", "short_swing", "observe", "avoid")
REQUIRED_RECOMMENDER_FEATURES: dict[str, tuple[str, ...]] = {
    "close": ("close", "latest_close", "price"),
    "ma20": ("ma20", "MA20"),
    "ma60": ("ma60", "MA60"),
    "liquidity_score": ("liquidity_score",),
    "volatility": ("volatility", "vol20"),
}
# Below this many rows the daily feature table is almost certainly shrunk/stale
# (e.g. clobbered by a demo bootstrap) rather than a real universe. Warn loudly
# instead of silently emitting recommendations off a single stale row — this is
# the P0 rule "stale/empty data must warn, never be treated as real".
MIN_CREDIBLE_CANDIDATE_ROWS = 5


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _num(data: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = safe_float(data.get(key), None)
        if value is not None:
            return float(value)
    return default


def _truthy_feature(data: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value:
            return True
        if str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "limit_up"}:
            return True
    return False


def _missing_required_features(features: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for canonical, aliases in REQUIRED_RECOMMENDER_FEATURES.items():
        value = next((safe_float(features.get(key), None) for key in aliases if safe_float(features.get(key), None) is not None), None)
        if value is None:
            missing.append(canonical)
            continue
        if canonical in {"close", "ma20", "ma60"} and float(value) <= 0:
            missing.append(canonical)
        elif canonical in {"liquidity_score", "volatility"} and float(value) < 0:
            missing.append(canonical)
    return missing


def _sector_context(sector_status: dict[str, Any] | None, sector: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    status = dict(sector_status or {})
    if not status:
        warnings.append("missing_sector_status")
        return (
            {
                "sector": sector,
                "sector_status": "unknown",
                "rank_5d": None,
                "rank_change_5d": 0.0,
                "heat_score": 0.0,
                "breadth": None,
            },
            warnings,
        )
    label = str(status.get("sector_status") or status.get("status") or "").strip().lower()
    rank_5d = _num(status, "rank_5d", "current_rank", "rank", default=999.0)
    heat = _num(status, "heat_score", "score", "strength_score", default=0.0)
    rank_change = _num(status, "rank_change_5d", "rank_improvement_5d", default=0.0)
    if not label:
        if rank_5d <= 3 or heat >= 80:
            label = "leading"
        elif rank_change >= 5 or heat >= 60:
            label = "strengthening"
        elif rank_change <= -5 or heat <= 25:
            label = "weakening"
        else:
            label = "neutral" if sector_status else "unknown"
    if label not in {"leading", "strengthening", "neutral", "weakening", "unknown"}:
        label = "unknown"
        warnings.append("unknown_sector_status")
    return (
        {
            "sector": sector,
            "sector_status": label,
            "rank_5d": None if rank_5d == 999.0 else rank_5d,
            "rank_change_5d": rank_change,
            "heat_score": heat,
            "breadth": status.get("breadth"),
        },
        warnings,
    )


def _risk_flags(features: dict[str, Any], risk_flags: list[str] | None, sector_label: str) -> list[str]:
    flags = [str(item) for item in (risk_flags or []) if item]
    name = str(features.get("name") or "")
    if _truthy_feature(features, "st", "is_st") or "ST" in name.upper():
        flags.append("st")
    if _truthy_feature(features, "suspended", "is_suspended"):
        flags.append("suspended")
    if _num(features, "liquidity_score", default=60) < 25 or _num(features, "turnover", "amount", default=1) <= 0:
        flags.append("low_liquidity")
    if _num(features, "volatility", "vol20", default=0) >= 0.08:
        flags.append("high_volatility")
    if _truthy_feature(features, "limit_up_like") and _num(features, "pct_chg", "pct_change", default=0) >= 8:
        flags.append("limit_up_chase_risk")
    if sector_label == "weakening":
        flags.append("sector_weakening")
    return list(dict.fromkeys(flags))


def recommend_strategy_for_symbol(
    symbol: str,
    daily_features: dict[str, Any],
    sector_status: dict[str, Any] | None = None,
    risk_flags: list[str] | None = None,
    *,
    rule_config: dict | None = None,
) -> dict[str, Any]:
    features = dict(daily_features or {})
    warnings: list[str] = []
    normalized_symbol = normalize_symbol(symbol or features.get("symbol"))
    if not normalized_symbol:
        warnings.append("missing_symbol")
    if not features:
        warnings.append("missing_daily_features")
    missing_features = _missing_required_features(features)
    if missing_features:
        warnings.append("missing_required_features:" + ",".join(missing_features))
    sector = str(features.get("sector") or (sector_status or {}).get("sector") or (sector_status or {}).get("sector_name") or "unknown")
    sector_ctx, sector_warnings = _sector_context(sector_status, sector)
    warnings.extend(sector_warnings)
    sector_label = str(sector_ctx["sector_status"])

    close = _num(features, "close", "latest_close", "price", default=0.0)
    ma20 = _num(features, "ma20", "MA20", default=0.0)
    ma60 = _num(features, "ma60", "MA60", default=0.0)
    pct = _num(features, "pct_chg", "pct_change", default=0.0)
    volume_ratio = _num(features, "volume_ratio", "vol_ratio", default=1.0)
    liquidity = _num(features, "liquidity_score", default=60.0)
    volatility = _num(features, "volatility", "vol20", default=0.03)
    long_raw = _num(features, "long_score", "long_suitability_score", "score", default=50.0)
    board_raw = _num(features, "board_score", "board_suitability_score", default=30.0)

    above_ma20 = bool(ma20 and close and close >= ma20) or _truthy_feature(features, "breakout_ma20")
    above_ma60 = bool(ma60 and close and close >= ma60) or _truthy_feature(features, "breakout_ma60")
    limit_up_like = _truthy_feature(features, "limit_up_like") or pct >= 9.5

    sector_bonus = {"leading": 18, "strengthening": 10, "neutral": 0, "unknown": -4, "weakening": -18}[sector_label]
    long_score = long_raw + sector_bonus + (10 if above_ma20 else -8) + (8 if above_ma60 else 0) + min(volume_ratio, 3) * 2
    board_score = board_raw + (16 if sector_label == "leading" else 6 if sector_label == "strengthening" else -8) + (26 if limit_up_like else 0) + min(volume_ratio, 5) * 4 + max(pct, 0) * 0.8
    swing_score = 48 + (12 if above_ma20 else -4) + (8 if sector_label in {"leading", "strengthening"} else 0) + min(volume_ratio, 3) * 3 - volatility * 80
    avoid_score = 10 + (32 if sector_label == "weakening" else 0) + max(volatility - 0.05, 0) * 400 + (20 if liquidity < 30 else 0)

    flags = _risk_flags(features, risk_flags, sector_label)
    severe_flags = {"st", "suspended", "low_liquidity"}
    if severe_flags.intersection(flags):
        avoid_score = max(avoid_score, 82)
        long_score -= 28
        board_score -= 22
        swing_score -= 20
        warnings.append("severe_risk_flag_forces_avoid")

    scores = {
        "long_term": _clamp(long_score - (0 if above_ma60 else 12)),
        "long_swing": _clamp(long_score + (8 if above_ma20 and sector_label in {"leading", "strengthening"} else 0)),
        "board": _clamp(board_score),
        "short_swing": _clamp(swing_score),
        "observe": _clamp(52 + (8 if sector_label == "neutral" else 0) - (12 if severe_flags.intersection(flags) else 0)),
        "avoid": _clamp(avoid_score),
    }
    if sector_label == "weakening" and not severe_flags.intersection(flags):
        scores["observe"] = max(scores["observe"], 62)
        scores["avoid"] = max(scores["avoid"], 58)

    if limit_up_like:
        scores["board"] = min(100.0, max(scores["board"], scores["long_swing"] + 1))
    if above_ma20 and sector_label in {"leading", "strengthening"} and not limit_up_like:
        scores["long_swing"] = min(100.0, max(scores["long_swing"], scores["long_term"] + 1))
    priority = {"board": 5, "long_swing": 4, "long_term": 3, "short_swing": 2, "observe": 1, "avoid": 0}
    ranked = sorted(scores.items(), key=lambda item: (item[1], priority.get(item[0], 0)), reverse=True)
    evidence_eligible = not missing_features and bool(normalized_symbol)
    if evidence_eligible:
        primary = ranked[0][0]
        secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] >= 45 else ""
        confidence = int(round(_clamp(ranked[0][1])))
    else:
        primary = "avoid" if severe_flags.intersection(flags) else "observe"
        secondary = ""
        confidence = 0
    stars = int(max(1, min(5, round(confidence / 20)))) if evidence_eligible else 0
    labels = {
        "long_term": "长期观察",
        "long_swing": "长线趋势",
        "board": "打板观察",
        "short_swing": "短线波段",
        "observe": "继续观察",
        "avoid": "不建议参与",
    }
    rationale: list[str] = []
    if sector_label in {"leading", "strengthening"}:
        rationale.append(f"板块「{sector}」状态为 {sector_label}，提供研究观察加分。")
    if above_ma20:
        rationale.append("个股位于 MA20 上方或出现 MA20 突破迹象。")
    if above_ma60:
        rationale.append("个股位于 MA60 上方，中期趋势条件较完整。")
    if limit_up_like:
        rationale.append("个股存在涨停或量价爆发特征，仅适合高风险观察。")
    if sector_label == "weakening":
        rationale.append("板块状态走弱，优先降低参与意愿。")
    if flags:
        rationale.append("存在风险标签，需要降低研究置信度或转为回避。")
    if not rationale:
        rationale.append("可用字段有限，建议补齐本地日线特征和板块状态后再评估。")

    return {
        "symbol": normalized_symbol,
        "name": str(features.get("name") or ""),
        "sector": sector,
        "primary_strategy": primary,
        "secondary_strategy": secondary,
        "recommendation_label": labels[primary],
        "confidence": confidence,
        "stars": stars,
        "action_tone": "observe_or_research" if primary != "avoid" else "avoid_or_wait",
        "score_breakdown": {key: round(value, 4) for key, value in scores.items()},
        "sector_context": sector_ctx,
        "rationale": rationale,
        "warnings": list(dict.fromkeys(warnings)),
        "risk_flags": flags,
        "evidence_eligible": evidence_eligible,
        "decision_status": "research_observation" if evidence_eligible else "insufficient_evidence",
        "missing_features": missing_features,
        "research_only": True,
        "disclaimer": DISCLAIMER,
    }


def _sector_lookup(sector_rotation: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = sector_rotation or {}
    rows: list[Any] = []
    for key in ("sectors", "sector_rankings", "top_sectors", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(value)
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("sector") or row.get("sector_name") or row.get("name") or "")
        if name:
            lookup[name] = row
    return lookup


def recommend_strategies_for_universe(
    candidates: list[dict],
    sector_rotation: dict | None = None,
    *,
    rule_config: dict | None = None,
    top: int = 20,
    sector: str | None = None,
) -> dict:
    warnings: list[str] = []
    lookup = _sector_lookup(sector_rotation)
    rows = [dict(item) for item in candidates if isinstance(item, dict)]
    if sector:
        rows = [row for row in rows if str(row.get("sector") or "") == sector]
    if not rows:
        warnings.append("no_candidates_available")
    recs = [
        recommend_strategy_for_symbol(
            str(row.get("symbol") or ""),
            row,
            lookup.get(str(row.get("sector") or ""), None),
            list(row.get("risk_flags") or []),
            rule_config=rule_config,
        )
        for row in rows
    ]
    eligible_recs = [item for item in recs if item.get("evidence_eligible") is True]
    excluded = [item for item in recs if item.get("evidence_eligible") is not True]
    eligible_recs.sort(key=lambda item: (int(item.get("confidence") or 0), float((item.get("sector_context") or {}).get("heat_score") or 0)), reverse=True)
    limited = eligible_recs[: max(0, int(top or 20))]
    reason_codes: list[str] = []
    if excluded:
        reason_codes.append("insufficient_candidate_evidence")
        warnings.append("insufficient_candidate_evidence")
    counts = {key: 0 for key in STRATEGIES}
    counts.update(Counter(str(item.get("primary_strategy") or "observe") for item in limited))
    top_sectors = [
        {"sector": name, "count": count}
        for name, count in Counter(str(item.get("sector") or "unknown") for item in limited).most_common(5)
    ]
    return {
        "status": "passed" if limited else "warning",
        "recommendations": limited,
        "excluded_observations": excluded,
        "candidate_count": len(recs),
        "eligible_count": len(eligible_recs),
        "excluded_count": len(excluded),
        "strategy_counts": counts,
        "top_sectors": top_sectors,
        "reason_codes": reason_codes,
        "warnings": list(dict.fromkeys(warnings)),
    }


def synthetic_strategy_recommender_inputs() -> dict[str, Any]:
    candidates = [
        {"symbol": "600744", "name": "华银电力", "sector": "电力", "close": 8.8, "ma20": 8.1, "ma60": 7.6, "pct_chg": 3.2, "volume_ratio": 1.8, "liquidity_score": 78, "volatility": 0.035, "long_score": 72},
        {"symbol": "002115", "name": "三维通信", "sector": "通信", "close": 9.9, "ma20": 8.7, "ma60": 8.2, "pct_chg": 10.0, "volume_ratio": 4.2, "limit_up_like": True, "liquidity_score": 70, "volatility": 0.045, "board_score": 68},
        {"symbol": "300323", "name": "华灿光电", "sector": "电子", "close": 5.2, "ma20": 5.4, "ma60": 5.7, "pct_chg": -2.0, "volume_ratio": 0.7, "liquidity_score": 62, "volatility": 0.04},
        {"symbol": "600001", "name": "ST样例", "sector": "地产", "close": 2.1, "ma20": 2.3, "ma60": 2.8, "pct_chg": -1.0, "volume_ratio": 0.2, "liquidity_score": 12, "volatility": 0.09, "st": True},
    ]
    sector_rotation = {
        "sectors": [
            {"sector": "电力", "sector_status": "leading", "rank_5d": 3, "rank_change_5d": 15, "heat_score": 86},
            {"sector": "通信", "sector_status": "leading", "rank_5d": 2, "rank_change_5d": 8, "heat_score": 88},
            {"sector": "电子", "sector_status": "weakening", "rank_5d": 18, "rank_change_5d": -9, "heat_score": 28},
            {"sector": "地产", "sector_status": "weakening", "rank_5d": 26, "rank_change_5d": -12, "heat_score": 18},
        ]
    }
    return {"candidates": candidates, "sector_rotation": sector_rotation}


def _load_json_list(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    payload = read_json(path, {})
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("candidates", "standard_candidates", "recommendations", "long_candidates", "board_candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _load_csv_candidates(path: str | Path | None) -> list[dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def load_strategy_recommender_inputs(config: dict | None = None) -> tuple[dict, list[str]]:
    cfg = dict(config or {})
    warnings: list[str] = []
    if cfg.get("synthetic_test_data"):
        return synthetic_strategy_recommender_inputs(), warnings
    candidates = _load_json_list(cfg.get("candidates_json"))
    loaded_from_features = False
    if not candidates:
        candidates = _load_csv_candidates(PROJECT_ROOT / "data" / "features" / "daily_features_latest.csv")
        loaded_from_features = bool(candidates)
    if not candidates:
        warnings.append("missing_candidates_json_or_daily_features")
    elif loaded_from_features and len(candidates) < MIN_CREDIBLE_CANDIDATE_ROWS:
        # A real universe has hundreds/thousands of rows. A handful of rows from
        # the daily feature table almost always means the source was shrunk or
        # stale (e.g. clobbered by a demo bootstrap). Surface it instead of
        # silently recommending off it.
        warnings.append(
            f"daily_features_too_few_rows: only {len(candidates)} candidate row(s) "
            f"loaded from data/features/daily_features_latest.csv (expected at least "
            f"{MIN_CREDIBLE_CANDIDATE_ROWS}); the feature table looks shrunk or stale. "
            "Rebuild features from a full universe before trusting these recommendations."
        )
    sector_rotation = read_json(cfg.get("sector_rotation_json") or PROJECT_ROOT / "results" / "sector_rotation_latest.json", {})
    if not isinstance(sector_rotation, dict) or not sector_rotation:
        sector_rotation = {}
        warnings.append("missing_sector_rotation_json")
    return {"candidates": candidates, "sector_rotation": sector_rotation}, warnings


def build_strategy_recommendation_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    generated_at = now_text()
    inputs, warnings = load_strategy_recommender_inputs(cfg)
    candidates = inputs.get("candidates") if isinstance(inputs.get("candidates"), list) else []
    mode = "synthetic" if cfg.get("synthetic_test_data") else "universe"
    symbol = normalize_symbol(cfg.get("symbol"))
    sector = str(cfg.get("sector") or "")
    top = int(cfg.get("top") or 20)
    if symbol:
        mode = "symbol"
        candidates = [row for row in candidates if normalize_symbol(row.get("symbol")) == symbol] or [{"symbol": symbol}]
    elif sector:
        mode = "sector"
    result = recommend_strategies_for_universe(candidates, inputs.get("sector_rotation"), top=top, sector=sector or None)
    warnings.extend(result.get("warnings") or [])
    recs = result.get("recommendations") or []
    status = "passed" if recs and not warnings else "warning"
    if not recs and not warnings:
        warnings.append("no_recommendations")
        status = "warning"
    return {
        "ok": bool(recs),
        "status": status,
        "task_type": "strategy_recommendation",
        "schema_version": API_SCHEMA_VERSION,
        "generated_at": generated_at,
        "app_version": APP_VERSION,
        "network": False,
        "research_only": True,
        "price_adjustment": str(cfg.get("price_adjustment") or "forward"),
        "mode": mode,
        "symbol": symbol or None,
        "sector": sector or None,
        "top": top,
        "input_status": {
            "candidate_count": len(candidates),
            "sector_rotation_available": bool(inputs.get("sector_rotation")),
            "synthetic_test_data": bool(cfg.get("synthetic_test_data")),
        },
        "recommendations": recs,
        "recommendation_count": len(recs),
        "eligible_count": int(result.get("eligible_count") or 0),
        "excluded_count": int(result.get("excluded_count") or 0),
        "excluded_observations": result.get("excluded_observations") or [],
        "reason_codes": result.get("reason_codes") or [],
        "strategy_counts": result.get("strategy_counts") or {key: 0 for key in STRATEGIES},
        "top_sectors": result.get("top_sectors") or [],
        "risk_flags": list(dict.fromkeys(flag for row in recs for flag in (row.get("risk_flags") or []))),
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "errors": [],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }


def render_strategy_recommendation_markdown(payload: dict) -> str:
    lines = [
        "# Strategy Recommendation Snapshot",
        "",
        f"- status: `{payload.get('status')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- mode: `{payload.get('mode')}`",
        f"- recommendation_count: `{payload.get('recommendation_count', 0)}`",
        "",
        "Research-only strategy recommendation. It is not a buy/sell/order instruction.",
        "",
        "## Recommendations",
        "",
    ]
    for row in payload.get("recommendations") or []:
        lines += [
            f"### {row.get('symbol')} {row.get('name') or ''}",
            f"- sector: `{row.get('sector') or '-'}`",
            f"- primary_strategy: `{row.get('primary_strategy')}`",
            f"- secondary_strategy: `{row.get('secondary_strategy') or '-'}`",
            f"- confidence: `{row.get('confidence')}`",
            f"- stars: `{row.get('stars')}`",
            f"- risk_flags: `{', '.join(row.get('risk_flags') or []) or '-'}`",
            "- rationale:",
            *[f"  - {item}" for item in (row.get("rationale") or [])],
            "",
        ]
    if not payload.get("recommendations"):
        lines.append("No recommendations available. Provide local candidates or run with synthetic test data.")
    lines += ["", "## Warnings", ""]
    lines += [f"- {item}" for item in (payload.get("warnings") or [])] or ["- none"]
    lines += ["", payload.get("disclaimer") or DISCLAIMER]
    return "\n".join(lines)


def write_strategy_recommendation_outputs(payload: dict, *, output_dir: str | Path = "results", report_dir: str | Path = "reports") -> dict:
    out_dir = PROJECT_ROOT / output_dir if not Path(output_dir).is_absolute() else Path(output_dir)
    rep_dir = PROJECT_ROOT / report_dir if not Path(report_dir).is_absolute() else Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_json = out_dir / "strategy_recommendation_latest.json"
    timestamp_json = out_dir / f"strategy_recommendation_{stamp}.json"
    latest_report = rep_dir / "strategy_recommendation_latest.md"
    timestamp_report = rep_dir / f"strategy_recommendation_{stamp}.md"
    output_paths = {
        "latest_json": str(latest_json.relative_to(PROJECT_ROOT)),
        "timestamp_json": str(timestamp_json.relative_to(PROJECT_ROOT)),
        "latest_report": str(latest_report.relative_to(PROJECT_ROOT)),
        "timestamp_report": str(timestamp_report.relative_to(PROJECT_ROOT)),
    }
    payload = dict(payload)
    payload["output_paths"] = output_paths
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    md = render_strategy_recommendation_markdown(payload)
    latest_report.write_text(md, encoding="utf-8")
    timestamp_report.write_text(md, encoding="utf-8")
    return payload
