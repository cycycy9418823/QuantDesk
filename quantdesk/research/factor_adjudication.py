"""Round 22 因子裁决（research-only）。

对生产评分体系的五个维度（trend/momentum/activity/position/risk）做
预注册的全 A、PIT 口径周频 rank IC 裁决。判据见
``docs/internal/round22_factor_adjudication_preregistration.md``——阈值
在跑数前定稿，本模块只负责忠实执行。

边界：不改生产评分公式、不接每日推荐、不动账本、不联网。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECURITY_MASTER_CSV = PROJECT_ROOT / "data" / "metadata" / "security_master.csv"
ST_HISTORY_CSV = PROJECT_ROOT / "data" / "metadata" / "st_history.csv"
SUSPENSION_HISTORY_CSV = PROJECT_ROOT / "data" / "metadata" / "suspension_history.csv"
ADJUSTMENT_FACTORS_CSV = PROJECT_ROOT / "data" / "metadata" / "adjustment_factors.csv"
BENCHMARK_CALENDAR_CSV = PROJECT_ROOT / "data" / "benchmark" / "sh000001.csv"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"

DIMENSIONS = ("trend", "momentum", "activity", "position", "risk")
# 预期 IC 方向：position 双向（0 表示不设先验）。
EXPECTED_SIGN = {"trend": 1, "momentum": 1, "activity": 1, "position": 0, "risk": -1}

DISCLAIMER = (
    "Research Only / Paper Only。因子裁决是统计诊断，不构成投资建议，"
    "不改生产评分权重，不接每日推荐。"
)

# Round 22 判决结果（2026-08-13，预注册判据，用户裁定：只披露、不改权重）。
# 详见 docs/internal/round22_factor_adjudication_preregistration.md 与
# results/round22_factor_adjudication/adjudication_verdict.md。
ROUND22_DIMENSION_ADJUDICATION: dict[str, str] = {
    "trend": "kill",
    "momentum": "kill",
    "activity": "kill",
    "position": "kill",
    "risk": "keep",
}
ROUND22_ADJUDICATION_NOTE = (
    "Round 22 预注册裁决（2026-08-13，全 A PIT 周频 rank IC）：trend/momentum/"
    "activity/position 四维在 5 日尺度 IC 显著为负被判无效（kill），仅 risk "
    "罚分有效（keep）。用户裁定生产权重暂不修改，此处仅作醒目披露；"
    "评分结论不得当作已验证信号使用。"
)


def score_dimension_adjudication_disclosure() -> dict[str, Any]:
    """供报告/payload 引用的评分维度裁决披露块（只读，不改任何行为）。"""

    return {
        "adjudication_round": "round22",
        "adjudicated_at": "2026-08-13",
        "verdicts": dict(ROUND22_DIMENSION_ADJUDICATION),
        "note": ROUND22_ADJUDICATION_NOTE,
        "preregistration": "docs/internal/round22_factor_adjudication_preregistration.md",
        "production_weights_changed": False,
    }


@dataclass(frozen=True)
class AdjudicationThresholds:
    """预注册判据（round22 preregistration §4）。定稿后不得因结果修改。"""

    ic_mean_min: float = 0.02
    icir_min: float = 0.30
    direction_hit_min: float = 0.55
    t_stat_min: float = 2.0
    min_cross_sections: int = 150
    min_avg_cross_section_size: int = 300


@dataclass(frozen=True)
class AdjudicationConfig:
    window_start: str = "2020-01-01"
    window_end: str = "2026-06-30"
    holdout_start: str = "2025-01-01"
    min_prior_rows: int = 60
    horizons: tuple[int, ...] = (5, 20)
    primary_horizon: int = 5
    min_pairs_per_cross_section: int = 30
    thresholds: AdjudicationThresholds = field(default_factory=AdjudicationThresholds)


# ---------------------------------------------------------------------------
# 向量化五维评分（与 quantdesk.backtest._score_components_for_row 等值，
# 含 Round 21 的显式 NA 语义；tests/test_round22_factor_adjudication.py
# 有逐行等值守护测试）
# ---------------------------------------------------------------------------


def _fin(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return _fin(df[name])
    return pd.Series(np.nan, index=df.index, dtype=float)


def compute_dimension_scores_frame(df: pd.DataFrame) -> pd.DataFrame:
    """按生产公式向量化计算五维评分，整维度无可用指标时为 NaN。"""

    close = _col(df, "close")
    ma5 = _col(df, "ma5")
    ma10 = _col(df, "ma10")
    ma20 = _col(df, "ma20")
    ma60 = _col(df, "ma60")
    macd = _col(df, "macd_histogram")
    prev_macd = _col(df, "previous_macd_histogram")
    rsi14 = _col(df, "rsi14")
    act_ratio = _col(df, "activity_ratio_20")
    amt_ratio = _col(df, "amount_ratio_20")
    vol_ratio = _col(df, "volume_ratio_20")
    boll_upper = _col(df, "boll_upper")
    boll_lower = _col(df, "boll_lower")
    vol20 = _col(df, "vol20")
    atr14 = _col(df, "atr14")
    missing = (
        pd.to_numeric(_col(df, "entry_missing_count"), errors="coerce").fillna(0)
        + pd.to_numeric(_col(df, "exit_missing_count"), errors="coerce").fillna(0)
    )

    out = pd.DataFrame(index=df.index)

    # trend
    c1 = close.notna() & ma20.notna() & (ma20 != 0)
    c2 = ma20.notna() & ma60.notna() & (ma60 != 0)
    c3 = ma5.notna() & ma10.notna() & (ma10 != 0)
    trend = pd.Series(50.0, index=df.index)
    trend = trend + np.where(c1, ((close / ma20 - 1) * 120).clip(-25, 25).fillna(0), 0.0)
    trend = trend + np.where(c2, ((ma20 / ma60 - 1) * 100).clip(-20, 20).fillna(0), 0.0)
    trend = trend + np.where(c3, ((ma5 / ma10 - 1) * 90).clip(-15, 15).fillna(0), 0.0)
    out["trend"] = trend.clip(0, 100).where(c1 | c2 | c3)

    # momentum
    m_avail = macd.notna() | rsi14.notna()
    momentum = pd.Series(50.0, index=df.index)
    momentum = momentum + np.where(macd.notna(), (macd * 18).clip(-18, 18).fillna(0), 0.0)
    momentum = momentum + np.where(
        macd.notna() & prev_macd.notna(), ((macd - prev_macd) * 10).clip(-12, 12).fillna(0), 0.0
    )
    momentum = momentum + np.where(rsi14.notna(), ((rsi14 - 50) * 0.6).clip(-12, 12).fillna(0), 0.0)
    out["momentum"] = momentum.clip(0, 100).where(m_avail)

    # activity
    a_avail = act_ratio.notna() | amt_ratio.notna() | vol_ratio.notna()
    activity = pd.Series(50.0, index=df.index)
    activity = activity + np.where(act_ratio.notna(), ((act_ratio - 1) * 28).clip(-18, 18).fillna(0), 0.0)
    activity = activity + np.where(amt_ratio.notna(), ((amt_ratio - 1) * 18).clip(-12, 12).fillna(0), 0.0)
    activity = activity + np.where(vol_ratio.notna(), ((vol_ratio - 1) * 18).clip(-12, 12).fillna(0), 0.0)
    out["activity"] = activity.clip(0, 100).where(a_avail)

    # position
    p_boll = close.notna() & boll_upper.notna() & boll_lower.notna() & (boll_upper != boll_lower)
    p_ma = close.notna() & ma20.notna() & (ma20 != 0)
    band_pct = ((close - boll_lower) / (boll_upper - boll_lower)).clip(0, 1) * 100.0
    pos_ma = 50.0 + ((close / ma20 - 1) * 120).clip(-20, 20)
    position = pd.Series(np.nan, index=df.index)
    position = position.mask(p_ma, pos_ma)
    position = position.mask(p_boll, band_pct)
    out["position"] = position.clip(0, 100).where(p_boll | p_ma)

    # risk（penalty，越高越差）
    r_atr = atr14.notna() & close.notna() & (close > 0)
    r_avail = vol20.notna() | r_atr
    risk = pd.Series(0.0, index=df.index)
    risk = risk + np.where(vol20.notna(), ((vol20 - 0.05) * 1000).clip(0, 35).fillna(0), 0.0)
    risk = risk + np.where(r_atr, ((atr14 / close - 0.02) * 800).clip(0, 30).fillna(0), 0.0)
    risk = risk + missing.mul(8.0).clip(upper=32.0)
    risk = risk + np.where(close.notna(), 0.0, 15.0)
    out["risk"] = risk.clip(0, 100).where(r_avail)

    return out


# ---------------------------------------------------------------------------
# PIT 名单 / ST / 停牌
# ---------------------------------------------------------------------------


def _norm_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    return text.replace("/", "-")[:10]


@dataclass
class PitTables:
    list_date: dict[str, str]
    delist_date: dict[str, str]
    st_intervals: dict[str, list[tuple[str, str]]]
    suspended: set[tuple[str, str]]
    warnings: list[str]

    def eligible(self, symbol: str, date: str) -> tuple[bool, str]:
        """T 日 PIT 入选判定；返回 (是否入选, 排除原因)。"""

        listed = self.list_date.get(symbol)
        if listed is None:
            return False, "not_in_security_master"
        if listed and listed > date:
            return False, "not_listed_yet"
        delist = self.delist_date.get(symbol) or ""
        if delist and delist <= date:
            return False, "delisted"
        for start, end in self.st_intervals.get(symbol, ()):  # ST 区间闭区间
            if start and start <= date and (not end or date <= end):
                return False, "st_period"
        if (symbol, date) in self.suspended:
            return False, "suspended"
        return True, ""


def load_pit_tables(
    security_master_csv: Path = SECURITY_MASTER_CSV,
    st_history_csv: Path = ST_HISTORY_CSV,
    suspension_csv: Path = SUSPENSION_HISTORY_CSV,
) -> PitTables:
    warnings: list[str] = []
    master = pd.read_csv(security_master_csv, dtype=str)
    list_date = {}
    delist_date = {}
    for row in master.itertuples(index=False):
        symbol = str(row.symbol).zfill(6)
        list_date[symbol] = _norm_date(getattr(row, "list_date", ""))
        delist_date[symbol] = _norm_date(getattr(row, "delist_date", ""))

    st_intervals: dict[str, list[tuple[str, str]]] = {}
    if st_history_csv.exists():
        st = pd.read_csv(st_history_csv, dtype=str)
        for row in st.itertuples(index=False):
            symbol = str(row.symbol).zfill(6)
            start = _norm_date(getattr(row, "st_start_date", ""))
            end = _norm_date(getattr(row, "st_end_date", ""))
            if start:
                st_intervals.setdefault(symbol, []).append((start, end))
    else:
        warnings.append("st_history_missing: ST periods not excluded")

    suspended: set[tuple[str, str]] = set()
    if suspension_csv.exists():
        for chunk in pd.read_csv(suspension_csv, dtype=str, usecols=["trade_date", "symbol"], chunksize=500_000):
            for row in chunk.itertuples(index=False):
                suspended.add((str(row.symbol).zfill(6), _norm_date(row.trade_date)))
    else:
        warnings.append("suspension_history_missing: suspension days not excluded")

    return PitTables(list_date, delist_date, st_intervals, suspended, warnings)


def load_adjustment_factor_map(path: Path = ADJUSTMENT_FACTORS_CSV) -> dict[str, pd.Series]:
    """symbol -> Series(index=trade_date str, value=adj_factor float)，升序。"""

    factor_map: dict[str, pd.Series] = {}
    parts: dict[str, list[pd.DataFrame]] = {}
    for chunk in pd.read_csv(
        path,
        dtype={"trade_date": str, "symbol": str, "adj_factor": float},
        usecols=["trade_date", "symbol", "adj_factor"],
        chunksize=2_000_000,
    ):
        chunk["symbol"] = chunk["symbol"].str.zfill(6)
        for symbol, group in chunk.groupby("symbol", sort=False):
            parts.setdefault(str(symbol), []).append(group[["trade_date", "adj_factor"]])
    for symbol, groups in parts.items():
        merged = pd.concat(groups, ignore_index=True)
        merged["trade_date"] = merged["trade_date"].map(_norm_date)
        merged = merged.drop_duplicates("trade_date").sort_values("trade_date")
        factor_map[symbol] = pd.Series(
            merged["adj_factor"].to_numpy(dtype=float), index=merged["trade_date"].tolist()
        )
    return factor_map


def weekly_eval_dates(
    calendar_csv: Path = BENCHMARK_CALENDAR_CSV,
    start: str = "2020-01-01",
    end: str = "2026-06-30",
) -> tuple[list[str], list[str]]:
    """返回（周频评估日, 全部交易日≥start 的主日历）。评估日=每 ISO 周最后交易日。"""

    frame = pd.read_csv(calendar_csv, dtype=str)
    date_col = next((c for c in frame.columns if c.lower() in {"date", "trade_date", "日期"}), frame.columns[0])
    days = sorted({_norm_date(v) for v in frame[date_col] if _norm_date(v)})
    master = [d for d in days if d >= start]
    in_window = [d for d in master if start <= d <= end]
    by_week: dict[tuple[int, int], str] = {}
    for d in in_window:
        iso = pd.Timestamp(d).isocalendar()
        by_week[(int(iso.week), int(iso.year))] = d
    return sorted(by_week.values()), master


# ---------------------------------------------------------------------------
# 判决引擎（预注册 §4）
# ---------------------------------------------------------------------------


def _t_stat(values: np.ndarray) -> float:
    n = values.size
    if n < 2:
        return 0.0
    std = float(values.std(ddof=1))
    if std == 0:
        return 0.0
    return float(values.mean() / (std / math.sqrt(n)))


def adjudicate_dimension(
    dimension: str,
    ic_primary: pd.DataFrame,
    ic_secondary: pd.DataFrame,
    holdout_start: str,
    thresholds: AdjudicationThresholds,
    expected_sign: int | None = None,
    verdict_cap: str | None = None,
) -> dict[str, Any]:
    """对单一维度/因子执行预注册判据。

    ``ic_primary`` / ``ic_secondary``: columns=[date, ic, pairs]，主/辅视野。
    ``expected_sign``：非生产五维（如 Round 23 反转因子）显式传入。
    ``verdict_cap``：数据窥探受限轮（Round 23 §0）传 ``provisional_keep``，
    历史判决 keep/weak_keep 被降级为 provisional_keep/weak_provisional。
    """

    expected = EXPECTED_SIGN[dimension] if expected_sign is None else expected_sign
    main = ic_primary[ic_primary["date"] < holdout_start]
    hold = ic_primary[ic_primary["date"] >= holdout_start]

    n = len(main)
    avg_size = float(main["pairs"].mean()) if n else 0.0
    result: dict[str, Any] = {
        "dimension": dimension,
        "expected_sign": expected,
        "cross_sections": n,
        "avg_cross_section_size": round(avg_size, 1),
    }
    if n < thresholds.min_cross_sections or avg_size < thresholds.min_avg_cross_section_size:
        result["verdict"] = "insufficient"
        result["reason"] = "cross_sections_or_size_below_preregistered_minimum"
        return result

    ics = main["ic"].to_numpy(dtype=float)
    ic_mean = float(ics.mean())
    ic_std = float(ics.std(ddof=1)) if n > 1 else 0.0
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    t_stat = _t_stat(ics)
    if expected == 0:
        direction = 1 if ic_mean >= 0 else -1
    else:
        direction = expected
    hit_ratio = float((np.sign(ics) == direction).mean())

    signed_mean = ic_mean * direction
    signed_icir = icir * direction

    core_pass = {
        "ic_mean": signed_mean >= thresholds.ic_mean_min,
        "icir": signed_icir >= thresholds.icir_min,
        "direction_hit": hit_ratio >= thresholds.direction_hit_min,
    }
    aux_pass = {
        "t_stat": abs(t_stat) >= thresholds.t_stat_min,
        "secondary_horizon_sign": bool(
            len(ic_secondary) and np.sign(ic_secondary["ic"].mean()) == direction
        ),
        "holdout_sign": bool(len(hold) and np.sign(hold["ic"].mean()) == direction),
    }

    result.update(
        {
            "ic_mean": round(ic_mean, 5),
            "ic_std": round(ic_std, 5),
            "icir": round(icir, 4),
            "t_stat": round(t_stat, 3),
            "direction_hit_ratio": round(hit_ratio, 4),
            "resolved_direction": direction,
            "holdout_cross_sections": len(hold),
            "holdout_ic_mean": round(float(hold["ic"].mean()), 5) if len(hold) else None,
            "secondary_ic_mean": round(float(ic_secondary["ic"].mean()), 5) if len(ic_secondary) else None,
            "core_pass": core_pass,
            "aux_pass": aux_pass,
        }
    )

    if not all(core_pass.values()):
        result["verdict"] = "kill"
        result["reason"] = "failed_core_criteria: " + ",".join(k for k, v in core_pass.items() if not v)
    elif all(aux_pass.values()):
        result["verdict"] = "keep"
        result["reason"] = "all_core_and_aux_criteria_passed"
    else:
        result["verdict"] = "weak_keep"
        result["reason"] = "core_passed_aux_failed: " + ",".join(k for k, v in aux_pass.items() if not v)
    if verdict_cap == "provisional_keep" and result["verdict"] in {"keep", "weak_keep"}:
        result["verdict"] = "provisional_keep" if result["verdict"] == "keep" else "weak_provisional"
        result["verdict_capped"] = True
        result["verdict_cap_reason"] = "in_sample_hypothesis_data_snooping_cap"
    return result


def compute_weekly_rank_ic(
    panel: pd.DataFrame,
    score_col: str,
    forward_col: str,
    min_pairs: int = 30,
) -> pd.DataFrame:
    """按评估日算 Spearman rank IC；返回 columns=[date, ic, pairs]。"""

    rows: list[dict[str, Any]] = []
    for date, group in panel.groupby("eval_date", sort=True):
        scores = pd.to_numeric(group[score_col], errors="coerce")
        fwd = pd.to_numeric(group[forward_col], errors="coerce")
        mask = scores.notna() & fwd.notna()
        pairs = int(mask.sum())
        if pairs < min_pairs:
            continue
        ic = float(scores[mask].rank().corr(fwd[mask].rank()))
        if math.isfinite(ic):
            rows.append({"date": str(date), "ic": ic, "pairs": pairs})
    return pd.DataFrame(rows, columns=["date", "ic", "pairs"])


def adjudicate_panel(panel: pd.DataFrame, config: AdjudicationConfig) -> dict[str, Any]:
    """对完整面板执行全部维度判决，返回 JSON 可序列化 payload。"""

    primary = f"fwd_{config.primary_horizon}"
    secondary_h = next((h for h in config.horizons if h != config.primary_horizon), None)
    verdicts = []
    ic_tables: dict[str, Any] = {}
    for dim in DIMENSIONS:
        ic_p = compute_weekly_rank_ic(panel, dim, primary, config.min_pairs_per_cross_section)
        ic_s = (
            compute_weekly_rank_ic(panel, dim, f"fwd_{secondary_h}", config.min_pairs_per_cross_section)
            if secondary_h
            else pd.DataFrame(columns=["date", "ic", "pairs"])
        )
        verdicts.append(
            adjudicate_dimension(dim, ic_p, ic_s, config.holdout_start, config.thresholds)
        )
        ic_tables[dim] = {
            "primary_horizon": config.primary_horizon,
            "cross_sections": len(ic_p),
        }
    return {
        "task_type": "factor_adjudication",
        "research_only": True,
        "paper_only": True,
        "network": False,
        "preregistration": "docs/internal/round22_factor_adjudication_preregistration.md",
        "config": {
            "window_start": config.window_start,
            "window_end": config.window_end,
            "holdout_start": config.holdout_start,
            "horizons": list(config.horizons),
            "primary_horizon": config.primary_horizon,
            "min_prior_rows": config.min_prior_rows,
            "thresholds": {
                "ic_mean_min": config.thresholds.ic_mean_min,
                "icir_min": config.thresholds.icir_min,
                "direction_hit_min": config.thresholds.direction_hit_min,
                "t_stat_min": config.thresholds.t_stat_min,
                "min_cross_sections": config.thresholds.min_cross_sections,
                "min_avg_cross_section_size": config.thresholds.min_avg_cross_section_size,
            },
        },
        "multiple_testing_note": "5 dimensions x 2 horizons = 10 nominal tests; no Bonferroni correction applied; t>=2 is nominal significance.",
        "verdicts": verdicts,
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "DIMENSIONS",
    "EXPECTED_SIGN",
    "ROUND22_DIMENSION_ADJUDICATION",
    "ROUND22_ADJUDICATION_NOTE",
    "score_dimension_adjudication_disclosure",
    "AdjudicationConfig",
    "AdjudicationThresholds",
    "PitTables",
    "compute_dimension_scores_frame",
    "load_pit_tables",
    "load_adjustment_factor_map",
    "weekly_eval_dates",
    "compute_weekly_rank_ic",
    "adjudicate_dimension",
    "adjudicate_panel",
]
