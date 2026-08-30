"""Market Mainline Radar — research-only sector/theme mainline inference.

This module answers, for local A-share research **only**:

- 当前市场主线可能是什么 (current mainlines)?
- 当前次主线 (secondary lines)?
- 哪些方向正在升温 (potential lines)?
- 哪些方向正在退潮 (cooling lines)?
- 判断依据是什么 (evidence) + 反证 / 确认信号 / 失效信号?
- 数据覆盖是否完整?结论是否降级 (data_quality)?
- 与候选池 / 观察池 / 持仓有无重合 (overlap)?

Hard boundaries (see AGENTS.md / ALIGNMENT.md):

- This is **research inference, not prediction, not investment advice**.
- No buy/sell/target-price/position instructions are ever emitted.
- No network access here; callers pass already-loaded local data in.
- No LLM call here; :func:`build_mainline_research_prompt` only *builds* a
  prompt string and never sends it.
- Missing data degrades to ``partial`` / ``degraded`` / ``missing`` with
  explicit warnings; it never fabricates sector heat, money flow, or quotes.

Design: pure functions + stdlib only (csv/json/statistics), with dependency
injection (:class:`RadarInputs`) so tests are deterministic and offline.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from quantdesk.extended_data_readiness import build_extended_data_readiness
from quantdesk.shared import now_text, write_json
from quantdesk.trading_calendar import resolve_target_complete_trade_date

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Local data sources (read-only). Callers may override paths for tests.
SPOT_SNAPSHOT_CSV = PROJECT_ROOT / "data" / "cache" / "spot_snapshot_latest.csv"
THEME_MAP_CSV = PROJECT_ROOT / "data" / "universe" / "stock_theme_map.csv"
INDUSTRY_BOARD_CSV = PROJECT_ROOT / "data" / "cache" / "industry_board_latest.csv"
BENCHMARK_CSV = PROJECT_ROOT / "data" / "benchmark" / "sh000001.csv"
CANDIDATES_CSV = PROJECT_ROOT / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv"
AUTO_PAPER_STATE_JSON = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"
PAPER_PORTFOLIO_JSON = PROJECT_ROOT / "state" / "paper_portfolio.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "results" / "market_intelligence" / "mainline_radar_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "mainline_radar_latest.md"

RESEARCH_BOUNDARY = (
    "Research Only · Paper Account Only · No Broker Route · Live Execution Locked · "
    "Not Investment Advice · Not Position Advice"
)
DISCLAIMER = (
    "市场主线雷达是基于本地数据的研究推断，用于观察行业/题材相对强弱，"
    "不是预测，不构成投资建议，也不构成仓位或买卖建议。"
)

# Scoring weights (conservative, explainable v1). Sum is normalized internally.
_W_UP_RATIO = 0.22       # 行业内上涨家数占比
_W_AVG_PCT = 0.20        # 当日涨跌幅强度
_W_AMOUNT_SHARE = 0.20   # 成交额占比（资金关注度）
_W_STRONG_RATIO = 0.16   # 强势股占比
_W_CANDIDATE = 0.14      # 候选池集中度
_W_OVERLAP = 0.08        # 观察池/持仓重合度

# Classification thresholds (on the 0..100 composite score).
_CURRENT_MAINLINE_MIN = 62.0
_SECONDARY_MIN = 48.0
_POTENTIAL_MIN = 38.0
# Below _POTENTIAL_MIN with negative momentum -> cooling.

_STRONG_PCT = 5.0        # 强势股阈值（当日涨幅 >= 5%）
_LIMIT_LIKE_PCT = 9.5    # 涨停近似阈值（不区分板块，标注为近似）
_MIN_GROUP_MEMBERS = 3   # 样本不足该数则置信度降级
_MIN_GROUP_COVERAGE = 0.5  # 有效数据成员比例低于此则降级


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace("%", "").replace(",", "")
        if text == "" or text.lower() in {"nan", "none", "null", "-"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _date_contract_value(value: Any) -> tuple[str, str]:
    """Return ``(YYYY-MM-DD-ish text, state)`` for temporal contract fields."""

    text = str(value or "").strip()
    if not text:
        return "", "missing"
    candidate = text[:10]
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return candidate, "invalid"
    return candidate, "valid"


def _mainline_temporal_reasons(
    *,
    as_of: Any,
    target_date: Any,
    data_cutoff: Any,
) -> tuple[str, str, str, list[str]]:
    normalized: dict[str, str] = {}
    states: dict[str, str] = {}
    reasons: list[str] = []
    for field_name, value in (
        ("as_of", as_of),
        ("target_date", target_date),
        ("data_cutoff", data_cutoff),
    ):
        normalized[field_name], states[field_name] = _date_contract_value(value)
        if states[field_name] != "valid":
            reasons.append(f"mainline_{states[field_name]}_{field_name}")

    target = normalized["target_date"]
    cutoff = normalized["data_cutoff"]
    as_of_text = normalized["as_of"]
    if states["as_of"] == states["target_date"] == "valid" and as_of_text != target:
        reasons.append("mainline_as_of_target_mismatch")
    if states["target_date"] == states["data_cutoff"] == "valid":
        if cutoff > target:
            reasons.append("mainline_future_data_cutoff")
        elif cutoff < target:
            reasons.append("mainline_stale_data_cutoff")
    return as_of_text, target, cutoff, reasons


def _norm_symbol(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class RadarInputs:
    """All local data the radar needs, injected for deterministic testing.

    Every field defaults to empty so partial inputs degrade gracefully rather
    than raising.
    """

    as_of: str = ""
    target_date: str = ""
    data_cutoff: str = ""
    # spot rows: each dict with symbol, pct_chg, amount, (optional) name
    spot_rows: list[dict[str, Any]] = field(default_factory=list)
    # theme map: symbol -> {sector, industry, primary_theme, theme_tags, ...}
    theme_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    # industry board rows (Eastmoney): name, pct_chg, leading_stock
    industry_board: list[dict[str, Any]] = field(default_factory=list)
    candidate_symbols: list[str] = field(default_factory=list)
    watchlist_symbols: list[str] = field(default_factory=list)
    holding_symbols: list[str] = field(default_factory=list)
    benchmark_pct: float | None = None
    extended_data_readiness: dict[str, Any] = field(default_factory=dict)


@dataclass
class _GroupAccum:
    name: str
    kind: str  # "industry" | "theme"
    members: list[str] = field(default_factory=list)
    pct_values: list[float] = field(default_factory=list)
    amount_values: list[float] = field(default_factory=list)
    up_count: int = 0
    strong_count: int = 0
    limit_like_count: int = 0
    candidate_count: int = 0
    watchlist_count: int = 0
    holding_count: int = 0


def _resolve_group_keys(theme_row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Map a stock's theme row to (kind, group_name) keys it belongs to.

    A stock contributes to its industry group and its primary_theme group.
    Unknown / blank values are skipped (counted as coverage gaps upstream).
    """

    keys: list[tuple[str, str]] = []
    industry = str(theme_row.get("industry") or theme_row.get("sector") or "").strip()
    if industry and not _is_unknown(industry):
        keys.append(("industry", industry))
    theme = str(theme_row.get("primary_theme") or "").strip()
    if theme and not _is_unknown(theme) and theme != industry:
        keys.append(("theme", theme))
    return keys


def _is_unknown(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "nan", "none", "null", "-", "未知", "未知板块", "未知题材", "未知行业", "未知概念"}


def _normalize(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def build_mainline_radar(inputs: RadarInputs) -> dict[str, Any]:
    """Compute the market mainline radar payload from injected local data.

    Pure, offline, deterministic. Never fabricates: when a dimension is
    unavailable it is omitted from evidence and noted in warnings, and the
    overall ``data_quality.status`` degrades accordingly.
    """

    warnings: list[str] = []
    methodology = _methodology_notes()

    spot_rows = inputs.spot_rows or []
    theme_map = inputs.theme_map or {}
    readiness_quality = _readiness_quality(inputs.extended_data_readiness)
    warnings.extend(_readiness_warnings(readiness_quality))
    candidate_set = {_norm_symbol(s) for s in inputs.candidate_symbols if _norm_symbol(s)}
    watchlist_set = {_norm_symbol(s) for s in inputs.watchlist_symbols if _norm_symbol(s)}
    holding_set = {_norm_symbol(s) for s in inputs.holding_symbols if _norm_symbol(s)}

    # ---- data-quality gates -------------------------------------------------
    if not spot_rows:
        warnings.append("缺少全市场行情快照(spot snapshot)：无法计算行业/题材强度。")
    if not theme_map:
        warnings.append("缺少题材/行业映射(stock_theme_map)：无法将个股归入行业/题材，结论降级。")

    # Total market amount for share computation.
    total_amount = 0.0
    spot_by_symbol: dict[str, dict[str, Any]] = {}
    mapped_members = 0
    unmapped_members = 0
    for row in spot_rows:
        sym = _norm_symbol(row.get("symbol") or row.get("code"))
        if not sym:
            continue
        spot_by_symbol[sym] = row
        amt = _safe_float(row.get("amount"))
        if amt is not None and amt > 0:
            total_amount += amt

    # ---- accumulate per group ----------------------------------------------
    groups: dict[tuple[str, str], _GroupAccum] = {}
    valid_pct_members = 0
    for sym, row in spot_by_symbol.items():
        theme_row = theme_map.get(sym)
        if not theme_row:
            unmapped_members += 1
            continue
        keys = _resolve_group_keys(theme_row)
        if not keys:
            unmapped_members += 1
            continue
        mapped_members += 1
        pct = _safe_float(row.get("pct_chg"))
        amt = _safe_float(row.get("amount"))
        if pct is not None:
            valid_pct_members += 1
        for kind, name in keys:
            acc = groups.get((kind, name))
            if acc is None:
                acc = _GroupAccum(name=name, kind=kind)
                groups[(kind, name)] = acc
            acc.members.append(sym)
            if pct is not None:
                acc.pct_values.append(pct)
                if pct > 0:
                    acc.up_count += 1
                if pct >= _STRONG_PCT:
                    acc.strong_count += 1
                if pct >= _LIMIT_LIKE_PCT:
                    acc.limit_like_count += 1
            if amt is not None and amt > 0:
                acc.amount_values.append(amt)
            if sym in candidate_set:
                acc.candidate_count += 1
            if sym in watchlist_set:
                acc.watchlist_count += 1
            if sym in holding_set:
                acc.holding_count += 1

    if total_amount <= 0:
        warnings.append("成交额字段缺失或全为0：资金关注度维度降级。")
    coverage_ratio = (mapped_members / len(spot_by_symbol)) if spot_by_symbol else 0.0
    if spot_by_symbol and coverage_ratio < 0.6:
        warnings.append(
            f"题材映射覆盖率偏低({coverage_ratio:.0%})：约 {unmapped_members} 只个股未归入行业/题材，结论降级。"
        )

    # ---- score each group ---------------------------------------------------
    scored: list[dict[str, Any]] = []
    candidate_total = max(1, len(candidate_set))
    for (kind, name), acc in groups.items():
        member_count = len(acc.members)
        if member_count == 0:
            continue
        pct_coverage = (len(acc.pct_values) / member_count) if member_count else 0.0
        avg_pct = (sum(acc.pct_values) / len(acc.pct_values)) if acc.pct_values else 0.0
        med_pct = median(acc.pct_values) if acc.pct_values else 0.0
        up_ratio = (acc.up_count / len(acc.pct_values)) if acc.pct_values else 0.0
        strong_ratio = (acc.strong_count / len(acc.pct_values)) if acc.pct_values else 0.0
        group_amount = sum(acc.amount_values)
        amount_share = (group_amount / total_amount) if total_amount > 0 else 0.0
        candidate_concentration = acc.candidate_count / candidate_total

        # Normalize each dimension to 0..1 with conservative bounds.
        n_up = _normalize(up_ratio, 0.3, 0.85)
        n_avg = _normalize(avg_pct, -2.0, 5.0)
        n_amt = _normalize(amount_share, 0.0, 0.08)
        n_strong = _normalize(strong_ratio, 0.0, 0.25)
        n_cand = _normalize(candidate_concentration, 0.0, 0.4)
        overlap_signal = 1.0 if (acc.watchlist_count or acc.holding_count) else 0.0

        raw = (
            _W_UP_RATIO * n_up
            + _W_AVG_PCT * n_avg
            + _W_AMOUNT_SHARE * n_amt
            + _W_STRONG_RATIO * n_strong
            + _W_CANDIDATE * n_cand
            + _W_OVERLAP * overlap_signal
        )
        score = round(raw * 100.0, 2)

        # Confidence: shrinks with small samples / low coverage.
        conf = 1.0
        group_warnings: list[str] = []
        if member_count < _MIN_GROUP_MEMBERS:
            conf *= 0.5
            group_warnings.append(f"成分股样本偏少({member_count}只)，结论置信度降低。")
        if pct_coverage < _MIN_GROUP_COVERAGE:
            conf *= 0.6
            group_warnings.append("行情字段覆盖不足，强度判断降级。")
        if total_amount <= 0:
            conf *= 0.8
            group_warnings.append("缺少成交额，资金关注度无法纳入评分。")
        confidence = round(min(1.0, conf), 2)

        scored.append(
            {
                "name": name,
                "type": kind,
                "score": score,
                "confidence": confidence,
                "member_count": member_count,
                "up_ratio": round(up_ratio, 3),
                "avg_pct": round(avg_pct, 2),
                "median_pct": round(med_pct, 2),
                "strong_count": acc.strong_count,
                "limit_like_count": acc.limit_like_count,
                "amount_share": round(amount_share, 4),
                "candidate_count": acc.candidate_count,
                "watchlist_count": acc.watchlist_count,
                "holding_count": acc.holding_count,
                "_avg_pct_raw": avg_pct,
                "_group_warnings": group_warnings,
            }
        )

    # ---- fuse Eastmoney industry board as an independent real data line -----
    # The per-stock theme_map is often sparse; the cached industry board
    # (data/cache/industry_board_latest.csv) carries 真实行业板块 pct_chg even
    # when stock-level mapping is missing.  We add board-derived industry rows
    # only for industries NOT already produced from stock mapping, so the two
    # sources never double-count.  Board rows are explicitly labelled and lack
    # amount/breadth dimensions (confidence is capped accordingly).
    existing_industry_names = {g["name"] for g in scored if g["type"] == "industry"}
    board_rows_used = 0
    for brow in inputs.industry_board or []:
        bname = str(brow.get("name") or "").strip()
        if not bname or _is_unknown(bname) or bname in existing_industry_names:
            continue
        bpct = _safe_float(brow.get("pct_chg"))
        if bpct is None:
            continue
        existing_industry_names.add(bname)
        board_rows_used += 1
        # Single-dimension proxy score: board-level daily move only.
        n_avg = _normalize(bpct, -2.0, 5.0)
        score = round(n_avg * 100.0, 2)
        leading = str(brow.get("leading_stock") or "").strip()
        scored.append(
            {
                "name": bname,
                "type": "industry",
                "score": score,
                "confidence": 0.4,  # board-only: no breadth/amount/candidate dims
                "member_count": 0,
                "up_ratio": 0.0,
                "avg_pct": round(bpct, 2),
                "median_pct": round(bpct, 2),
                "strong_count": 0,
                "limit_like_count": 0,
                "amount_share": 0.0,
                "candidate_count": 0,
                "watchlist_count": 0,
                "holding_count": 0,
                "_avg_pct_raw": bpct,
                "_source": "industry_board",
                "_leading_stock": leading,
                "_group_warnings": [
                    "该行业评分来自东方财富行业板块当日涨跌（板块级代理），"
                    "缺少成分股广度/成交额/候选集中度维度，置信度受限。"
                ],
            }
        )
    if board_rows_used:
        methodology.append(
            f"已融合 {board_rows_used} 个东方财富行业板块的当日涨跌作为补充行业强弱来源"
            "（仅在个股映射未覆盖该行业时使用，板块级代理、置信度受限）。"
        )

    scored.sort(key=lambda g: (g["score"], g["amount_share"]), reverse=True)

    # ---- classify into stages ----------------------------------------------
    current_mainlines: list[dict[str, Any]] = []
    secondary_lines: list[dict[str, Any]] = []
    potential_lines: list[dict[str, Any]] = []
    cooling_lines: list[dict[str, Any]] = []

    for g in scored:
        item = _build_line_item(g, candidate_set, watchlist_set, holding_set)
        score = g["score"]
        avg_pct = g["_avg_pct_raw"]
        if score >= _CURRENT_MAINLINE_MIN:
            item["stage"] = "current_mainline"
            current_mainlines.append(item)
        elif score >= _SECONDARY_MIN:
            item["stage"] = "secondary"
            secondary_lines.append(item)
        elif score >= _POTENTIAL_MIN and g["candidate_count"] > 0:
            # Rising interest proxy: not yet strong on tape but candidate pool
            # is concentrating here. Explicitly a single-day proxy (degraded).
            item["stage"] = "potential"
            item["warnings"].append("升温判断基于单日快照+候选集中度的代理，缺少多日强度序列，属降级推断。")
            potential_lines.append(item)
        elif avg_pct < -0.5:
            item["stage"] = "cooling"
            item["warnings"].append("退潮判断基于单日弱势，缺少多日序列确认，属降级推断。")
            cooling_lines.append(item)
        # else: uncertain — not surfaced as a line, stays in sector_scores.

    # cap list sizes for a scannable UI
    current_mainlines = current_mainlines[:5]
    secondary_lines = secondary_lines[:5]
    potential_lines = potential_lines[:6]
    cooling_lines = cooling_lines[:6]

    as_of, target_date, data_cutoff, reason_codes = _mainline_temporal_reasons(
        as_of=inputs.as_of,
        target_date=inputs.target_date,
        data_cutoff=inputs.data_cutoff,
    )
    evidence_eligible = not reason_codes
    historical_mainlines: list[dict[str, Any]] = []
    if not evidence_eligible:
        historical_mainlines = [
            *current_mainlines,
            *secondary_lines,
            *potential_lines,
            *cooling_lines,
        ]
        current_mainlines = []
        secondary_lines = []
        potential_lines = []
        cooling_lines = []
        warnings.extend(reason_codes)

    # ---- overlaps -----------------------------------------------------------
    candidate_overlap = _overlap_summary(scored, "candidate_count")
    holding_overlap = _overlap_summary(scored, "holding_count")

    # ---- risk notes ---------------------------------------------------------
    risk_notes = _build_risk_notes(scored, warnings, coverage_ratio, total_amount)

    # ---- data quality status ------------------------------------------------
    calculated_quality_status = _merge_quality_status(
        _data_quality_status(spot_rows, theme_map, total_amount, coverage_ratio, scored, board_rows_used),
        readiness_quality,
    )

    # strip private keys from sector_scores
    sector_scores = []
    for g in scored[:40]:
        public = {k: v for k, v in g.items() if not k.startswith("_")}
        public["group_warnings"] = g.get("_group_warnings", [])
        sector_scores.append(public)
    historical_sector_scores: list[dict[str, Any]] = []
    if not evidence_eligible:
        historical_sector_scores = sector_scores
        sector_scores = []

    response_status = (
        "blocked"
        if not evidence_eligible
        else ("passed" if calculated_quality_status in {"ok", "partial"} else "warning")
    )
    quality_status = "blocked" if not evidence_eligible else calculated_quality_status

    return {
        "task_type": "market_mainline_radar",
        "status": response_status,
        "ok": evidence_eligible,
        "generated_at": now_text(),
        "as_of": as_of,
        "target_date": target_date,
        "data_cutoff": data_cutoff,
        "evidence_eligible": evidence_eligible,
        "reason_codes": reason_codes,
        "source": "local_research",
        "scope": "research_only",
        "boundary": RESEARCH_BOUNDARY,
        "disclaimer": DISCLAIMER,
        "data_quality": {
            "status": quality_status,
            "calculated_status": calculated_quality_status,
            "warnings": warnings,
            "mapped_members": mapped_members,
            "unmapped_members": unmapped_members,
            "coverage_ratio": round(coverage_ratio, 3),
            "group_count": len(scored),
            "extended_data_readiness": readiness_quality,
        },
        "extended_data_readiness": readiness_quality,
        "current_mainlines": current_mainlines,
        "historical_mainlines": historical_mainlines,
        "historical_sector_scores": historical_sector_scores,
        "secondary_lines": secondary_lines,
        "potential_lines": potential_lines,
        "cooling_lines": cooling_lines,
        "risk_notes": risk_notes,
        "sector_scores": sector_scores,
        "candidate_overlap": candidate_overlap,
        "holding_overlap": holding_overlap,
        "methodology": methodology,
        "not_advice": True,
    }


def _build_line_item(
    g: Mapping[str, Any],
    candidate_set: set[str],
    watchlist_set: set[str],
    holding_set: set[str],
) -> dict[str, Any]:
    evidence: list[str] = []
    evidence.append(
        f"成分股 {g['member_count']} 只，上涨家数占比 {g['up_ratio']:.0%}，当日涨跌幅均值 {g['avg_pct']:+.2f}%"
    )
    if g["strong_count"]:
        evidence.append(f"强势股(涨幅≥{_STRONG_PCT:.0f}%) {g['strong_count']} 只")
    if g["amount_share"] > 0:
        evidence.append(f"成交额占全市场 {g['amount_share']:.2%}")
    if g["candidate_count"]:
        evidence.append(f"研究候选池中有 {g['candidate_count']} 只落在该方向")
    if g["limit_like_count"]:
        evidence.append(f"接近涨停(≈≥{_LIMIT_LIKE_PCT:.1f}%，不区分板块的近似) {g['limit_like_count']} 只")

    overlap_notes: list[str] = []
    if g["watchlist_count"]:
        overlap_notes.append(f"与观察池重合 {g['watchlist_count']} 只")
    if g["holding_count"]:
        overlap_notes.append(f"与纸面持仓重合 {g['holding_count']} 只")

    warnings = list(g.get("_group_warnings", []))

    confirmation_signals = [
        "后续2-3个交易日成交额占比继续提升，而不是单日脉冲",
        "上涨家数继续扩散到更多成分股，而非少数个股支撑",
    ]
    failure_signals = [
        "成交额占比回落、强势股数量减少",
        "上涨集中度下降、仅剩个别个股维持",
    ]

    return {
        "name": g["name"],
        "type": g["type"],
        "stage": "uncertain",
        "confidence": g["confidence"],
        "score": g["score"],
        "metrics": {
            "member_count": g["member_count"],
            "up_ratio": g["up_ratio"],
            "avg_pct": g["avg_pct"],
            "median_pct": g["median_pct"],
            "strong_count": g["strong_count"],
            "amount_share": g["amount_share"],
            "candidate_count": g["candidate_count"],
            "watchlist_count": g["watchlist_count"],
            "holding_count": g["holding_count"],
        },
        "evidence": evidence,
        "overlap_notes": overlap_notes,
        "warnings": warnings,
        "confirmation_signals": confirmation_signals,
        "failure_signals": failure_signals,
        "not_advice": True,
    }


def _overlap_summary(scored: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in scored:
        count = int(g.get(key) or 0)
        if count <= 0:
            continue
        out.append(
            {
                "name": g["name"],
                "type": g["type"],
                "count": count,
                "score": g["score"],
                "stage_hint": "该方向与研究池重合，但重合本身不构成仓位或买卖建议。",
            }
        )
    out.sort(key=lambda x: x["count"], reverse=True)
    return out[:10]


def _build_risk_notes(
    scored: list[dict[str, Any]],
    warnings: list[str],
    coverage_ratio: float,
    total_amount: float,
) -> list[str]:
    notes: list[str] = []
    if not scored:
        notes.append("当前没有可评分的行业/题材分组，结论为空，请先补齐行情与题材映射。")
        return notes
    top = scored[0]
    # crowding: top group dominated by a few names while breadth is thin
    if top["member_count"] >= _MIN_GROUP_MEMBERS and top["up_ratio"] < 0.5 and top["score"] >= _CURRENT_MAINLINE_MIN:
        notes.append(f"{top['name']} 评分高但上涨家数占比不足50%，可能是少数个股拉动，注意高位拥挤风险。")
    # single-day burst caveat (always, since v1 is single-day)
    notes.append("本期评分主要基于单日快照，缺少多日强度序列；潜在/退潮判断为代理降级，不能视为确定趋势。")
    if coverage_ratio < 0.6:
        notes.append("题材映射覆盖不足，部分真实主线可能因缺映射而被低估。")
    if total_amount <= 0:
        notes.append("缺少成交额数据，资金关注度维度未纳入，主线判断偏向价格强度。")
    return notes


def _data_quality_status(
    spot_rows: list,
    theme_map: dict,
    total_amount: float,
    coverage_ratio: float,
    scored: list,
    board_rows_used: int = 0,
) -> str:
    # Nothing to work with at all.
    if not scored:
        return "missing"
    # Stock-level mapping basically empty, but the Eastmoney industry board
    # supplied real industry rows: surface results as degraded (board-only proxy)
    # rather than "missing", so the page still shows real industry strength.
    if (not spot_rows or not theme_map) and board_rows_used > 0:
        return "degraded"
    if not spot_rows or not theme_map:
        return "missing"
    degraded = False
    if total_amount <= 0:
        degraded = True
    if coverage_ratio < 0.6:
        degraded = True
    if degraded:
        return "degraded"
    if coverage_ratio < 0.85:
        return "partial"
    return "ok"


def _methodology_notes() -> list[str]:
    return [
        "主线评分综合：上涨家数占比、当日涨跌幅强度、成交额占比、强势股占比、候选池集中度、观察池/持仓重合度。",
        "各维度做保守归一化后加权求和，得到 0-100 的相对评分，仅用于横向比较行业/题材的相对强弱。",
        "当前为 v1 单日快照版本：缺少多日相对强度序列，潜在线/退潮线为代理降级推断。",
        "涨停近似阈值为 9.5%（不区分主板/创业板/科创板/北交所），仅作强势计数的粗略代理。",
        "结论为研究观察，不是预测，不构成投资建议、确定价格结论或真实配置指令。",
    ]


_MAINLINE_READINESS_DOMAINS = (
    "sector_index",
    "sector_membership",
    "concept_theme",
    "moneyflow",
    "events",
    "fundamentals",
)


def _readiness_domain_statuses(readiness: Mapping[str, Any] | None) -> dict[str, str]:
    payload = readiness if isinstance(readiness, Mapping) else {}
    domains = payload.get("domains") if isinstance(payload.get("domains"), Mapping) else {}
    return {
        key: str((domains.get(key) or {}).get("status") or "missing")
        for key in _MAINLINE_READINESS_DOMAINS
    }


def _readiness_quality(readiness: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = readiness if isinstance(readiness, Mapping) else {}
    if not payload:
        return {
            "status": "not_checked",
            "mainline_analysis": "not_checked",
            "moneyflow_context": "not_checked",
            "event_context": "not_checked",
            "fundamental_context": "not_checked",
            "domain_statuses": _readiness_domain_statuses({}),
            "warnings": ["extended_data_readiness_not_checked"],
        }
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), Mapping) else {}
    warnings = [str(item) for item in (payload.get("warnings") or []) if str(item or "").strip()]
    return {
        "status": str(payload.get("status") or "missing"),
        "mainline_analysis": str(capabilities.get("mainline_analysis") or "degraded"),
        "moneyflow_context": str(capabilities.get("moneyflow_context") or "degraded"),
        "event_context": str(capabilities.get("event_context") or "degraded"),
        "fundamental_context": str(capabilities.get("fundamental_context") or "degraded"),
        "domain_statuses": _readiness_domain_statuses(payload),
        "warnings": warnings,
    }


def _merge_quality_status(base_status: str, readiness: Mapping[str, Any]) -> str:
    mainline = str(readiness.get("mainline_analysis") or "not_checked")
    if mainline == "not_checked":
        return base_status
    if mainline == "degraded":
        return "degraded" if base_status in {"ok", "partial"} else base_status
    if mainline == "partial" and base_status == "ok":
        return "partial"
    return base_status


def _readiness_warnings(readiness: Mapping[str, Any]) -> list[str]:
    mainline = str(readiness.get("mainline_analysis") or "not_checked")
    if mainline == "not_checked":
        return []
    warnings: list[str] = []
    if mainline != "available":
        warnings.append(f"extended_mainline_data_{mainline}")
    for domain, status in (readiness.get("domain_statuses") or {}).items():
        if status in {"missing", "staged_not_imported", "partial"}:
            warnings.append(f"extended_data_{domain}_{status}")
    return warnings


# ---------------------------------------------------------------------------
# Production loaders (read local files; never network). Used by the API.
# ---------------------------------------------------------------------------

def _load_theme_map(path: Path) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for row in _read_csv_rows(path):
        sym = _norm_symbol(row.get("symbol") or row.get("code"))
        if sym:
            mapping[sym] = row
    return mapping


def _load_candidate_symbols(path: Path) -> list[str]:
    return [_norm_symbol(r.get("symbol")) for r in _read_csv_rows(path) if _norm_symbol(r.get("symbol"))]


def _load_holding_symbols(*paths: Path) -> list[str]:
    out: list[str] = []
    for path in paths:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        for pos in data.get("positions") or []:
            if isinstance(pos, dict):
                sym = _norm_symbol(pos.get("symbol"))
                if sym:
                    out.append(sym)
    return out


def _load_watchlist_symbols() -> list[str]:
    try:
        from quantdesk.watchlist import list_watchlist

        items = list_watchlist()
    except Exception:
        return []
    out: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            sym = _norm_symbol(item.get("symbol"))
            if sym:
                out.append(sym)
    return out


def _latest_date_from_spot(rows: list[dict[str, Any]]) -> str:
    dates: list[str] = []
    for row in rows:
        for key in ("quote_time", "updated_at", "date"):
            value, state = _date_contract_value(row.get(key))
            if state == "valid":
                dates.append(value)
                break
    return max(dates) if dates else ""


def _resolve_radar_target_date(value: Any = None) -> str:
    explicit, state = _date_contract_value(value)
    if str(value or "").strip():
        return explicit
    try:
        resolved = resolve_target_complete_trade_date()
    except Exception:  # noqa: BLE001 - missing calendar must fail closed downstream
        return ""
    target, state = _date_contract_value(
        resolved.get("target_complete_trade_date") or resolved.get("target_date")
    )
    return target if state == "valid" else ""


def load_radar_inputs(
    *,
    project_root: Path | None = None,
    target_date: str | None = None,
    spot_path: Path | None = None,
    theme_path: Path | None = None,
    industry_board_path: Path | None = None,
    candidates_path: Path | None = None,
) -> RadarInputs:
    """Load all radar inputs from local files. Read-only, never network."""

    root = project_root or PROJECT_ROOT
    spot_p = spot_path or (root / "data" / "cache" / "spot_snapshot_latest.csv")
    theme_p = theme_path or (root / "data" / "universe" / "stock_theme_map.csv")
    board_p = industry_board_path or (root / "data" / "cache" / "industry_board_latest.csv")
    cand_p = candidates_path or (root / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv")

    spot_rows = _read_csv_rows(spot_p)
    resolved_target_date = _resolve_radar_target_date(target_date)
    data_cutoff = _latest_date_from_spot(spot_rows)
    industry_board = _read_csv_rows(board_p)
    theme_map = _load_theme_map(theme_p)
    candidate_symbols = _load_candidate_symbols(cand_p)
    watchlist_symbols = _load_watchlist_symbols()
    holding_symbols = _load_holding_symbols(
        root / "state" / "auto_paper_portfolio.json",
        root / "state" / "paper_portfolio.json",
    )
    try:
        extended_data_readiness = build_extended_data_readiness(project_root=root)
    except Exception as exc:  # noqa: BLE001 - readiness must degrade, not break radar
        extended_data_readiness = {
            "status": "missing",
            "capabilities": {"mainline_analysis": "degraded"},
            "domains": {},
            "warnings": [f"extended_data_readiness_error:{type(exc).__name__}"],
        }

    return RadarInputs(
        as_of=resolved_target_date,
        target_date=resolved_target_date,
        data_cutoff=data_cutoff,
        spot_rows=spot_rows,
        theme_map=theme_map,
        industry_board=industry_board,
        candidate_symbols=candidate_symbols,
        watchlist_symbols=watchlist_symbols,
        holding_symbols=holding_symbols,
        benchmark_pct=None,
        extended_data_readiness=extended_data_readiness,
    )


def build_mainline_radar_payload(
    *,
    project_root: Path | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    """API entry: load local data + compute. Never network, never LLM.

    On any unexpected internal error, returns an explicit degraded payload
    rather than raising, so the endpoint never 500s blank.
    """

    try:
        inputs = load_radar_inputs(project_root=project_root, target_date=target_date)
        payload = build_mainline_radar(inputs)
        return payload
    except Exception as exc:  # noqa: BLE001 - endpoint must stay alive
        resolved_target = _resolve_radar_target_date(target_date)
        return {
            "task_type": "market_mainline_radar",
            "status": "blocked",
            "ok": False,
            "generated_at": now_text(),
            "as_of": resolved_target,
            "target_date": resolved_target,
            "data_cutoff": "",
            "evidence_eligible": False,
            "reason_codes": ["mainline_internal_error"],
            "source": "local_research",
            "scope": "research_only",
            "boundary": RESEARCH_BOUNDARY,
            "disclaimer": DISCLAIMER,
            "data_quality": {
                "status": "blocked",
                "calculated_status": "degraded",
                "warnings": [f"雷达计算内部错误，已降级返回：{type(exc).__name__}: {exc}"],
            },
            "current_mainlines": [],
            "historical_mainlines": [],
            "historical_sector_scores": [],
            "secondary_lines": [],
            "potential_lines": [],
            "cooling_lines": [],
            "risk_notes": ["计算失败，请检查本地数据完整性。"],
            "sector_scores": [],
            "candidate_overlap": [],
            "holding_overlap": [],
            "methodology": _methodology_notes(),
            "not_advice": True,
        }


# ---------------------------------------------------------------------------
# Optional LLM hook — DEFAULT OFF. Builds a prompt only; never sends it.
# ---------------------------------------------------------------------------

def build_mainline_research_prompt(payload: Mapping[str, Any]) -> str:
    """Build a text prompt summarizing the radar payload for an *optional* LLM.

    This function NEVER calls a model. It only returns a prompt string that a
    caller could choose to send after explicit user opt-in. The instructions
    embedded here forbid buy/sell/target-price/position advice.
    """

    lines: list[str] = []
    lines.append("你是一个 A 股市场研究助手。以下是基于本地数据计算的『市场主线雷达』结构化结果。")
    lines.append("请只做研究性总结：归纳证据与反证、确认信号与失效信号。")
    lines.append("严禁输出：买入/卖出/加仓/减仓指令、确定价格结论、真实配置指令、确定性涨跌预测。")
    lines.append("必须保留边界声明：Research Only / Not Investment Advice / Not Position Advice。")
    lines.append("")
    lines.append(f"as_of: {payload.get('as_of')}")
    dq = payload.get("data_quality") or {}
    lines.append(f"data_quality: {dq.get('status')} warnings={dq.get('warnings')}")
    for label, key in (
        ("当前主线", "current_mainlines"),
        ("次主线", "secondary_lines"),
        ("潜在线", "potential_lines"),
        ("退潮线", "cooling_lines"),
    ):
        items = payload.get(key) or []
        names = [f"{it.get('name')}(score={it.get('score')})" for it in items]
        lines.append(f"{label}: {', '.join(names) if names else '（无）'}")
    lines.append("")
    lines.append("请用中文输出 AI Research Summary，标注 Research Only / Not Investment Advice。")
    return "\n".join(lines)


def _line_names(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "-"
    rendered: list[str] = []
    for item in items[:5]:
        if isinstance(item, Mapping):
            rendered.append(f"{item.get('name') or '-'}({item.get('score') or 0})")
    return ", ".join(rendered) or "-"


def build_mainline_radar_report(payload: Mapping[str, Any]) -> str:
    data_quality = payload.get("data_quality") if isinstance(payload.get("data_quality"), Mapping) else {}
    readiness = payload.get("extended_data_readiness") if isinstance(payload.get("extended_data_readiness"), Mapping) else {}
    domain_statuses = readiness.get("domain_statuses") if isinstance(readiness.get("domain_statuses"), Mapping) else {}
    lines = [
        "# Market Mainline Radar",
        "",
        "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice",
        "",
        "This report is a local evidence radar. It does not predict a certain market mainline and does not provide buy/sell, target-price, stop-loss, or position instructions.",
        "",
        "## Summary",
        "",
        f"- status: {payload.get('status') or 'unknown'}",
        f"- as_of: {payload.get('as_of') or '-'}",
        f"- data_quality: {data_quality.get('status') or '-'}",
        f"- mainline_analysis_readiness: {readiness.get('mainline_analysis') or '-'}",
        f"- current_mainlines: {_line_names(payload.get('current_mainlines'))}",
        f"- secondary_lines: {_line_names(payload.get('secondary_lines'))}",
        f"- potential_lines: {_line_names(payload.get('potential_lines'))}",
        f"- cooling_lines: {_line_names(payload.get('cooling_lines'))}",
        "",
        "## Extended Data Domains",
        "",
        "| domain | status |",
        "| --- | --- |",
    ]
    for key in _MAINLINE_READINESS_DOMAINS:
        lines.append(f"| {key} | {domain_statuses.get(key) or 'missing'} |")
    lines.extend(["", "## Warnings", ""])
    warnings = [str(item) for item in (data_quality.get("warnings") or []) if item]
    lines.extend([f"- {warning}" for warning in warnings] or ["- none"])
    lines.extend(["", "## Methodology", ""])
    lines.extend([f"- {item}" for item in (payload.get("methodology") or []) if str(item or "").strip()] or ["- none"])
    return "\n".join(lines).rstrip() + "\n"


def write_mainline_radar_outputs(
    *,
    project_root: Path | None = None,
    target_date: str | None = None,
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
    report_path: Path | str = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    payload = build_mainline_radar_payload(project_root=root, target_date=target_date)
    output = Path(output_path)
    report = Path(report_path)
    if not output.is_absolute():
        output = root / output
    if not report.is_absolute():
        report = root / report
    try:
        payload["output_path"] = output.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        payload["output_path"] = output.as_posix()
    try:
        payload["report_path"] = report.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        payload["report_path"] = report.as_posix()
    write_json(output, payload)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(build_mainline_radar_report(payload), encoding="utf-8")
    return payload


__all__ = [
    "RadarInputs",
    "RESEARCH_BOUNDARY",
    "DISCLAIMER",
    "build_mainline_radar",
    "build_mainline_radar_payload",
    "build_mainline_radar_report",
    "build_mainline_research_prompt",
    "load_radar_inputs",
    "write_mainline_radar_outputs",
]
