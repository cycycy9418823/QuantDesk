from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pandas as pd

RESEARCH_METRICS_DISCLAIMER = "历史样本统计不代表未来收益或概率，仅用于本地研究复盘。"


@dataclass
class ResearchMetricsSummary:
    sample_count: int
    forward_return_5d_mean: float | None
    forward_return_20d_mean: float | None
    median_return: float | None
    positive_sample_ratio: float | None
    excess_return_mean: float | None
    max_drawdown: float | None
    calmar_ratio: float | None
    sortino_ratio: float | None
    return_distribution: dict[str, int]
    monthly_return_table: list[dict[str, Any]]
    annual_return_table: list[dict[str, Any]]
    rank_ic: float | None
    rank_ic_sample_count: int
    disclaimer: str


def _frame(rows: Any) -> pd.DataFrame:
    if rows is None:
        return pd.DataFrame()
    if isinstance(rows, pd.DataFrame):
        return rows.copy()
    if isinstance(rows, pd.Series):
        return rows.to_frame().T
    if isinstance(rows, Mapping):
        return pd.DataFrame([dict(rows)])
    if isinstance(rows, list):
        clean_rows = [row for row in rows if isinstance(row, Mapping)]
        return pd.DataFrame(clean_rows) if clean_rows else pd.DataFrame()
    return pd.DataFrame()


def _series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def _mean(frame: pd.DataFrame, column: str) -> float | None:
    values = _series(frame, column)
    if values.empty:
        return None
    return float(values.mean())


def _median(frame: pd.DataFrame, column: str) -> float | None:
    values = _series(frame, column)
    if values.empty:
        return None
    return float(values.median())


def _positive_ratio(frame: pd.DataFrame, column: str) -> float | None:
    values = _series(frame, column)
    if values.empty:
        return None
    return float((values > 0).mean())


def _distribution(values: pd.Series) -> dict[str, int]:
    if values.empty:
        return {}
    bins = {
        "<=-10%": int((values <= -0.10).sum()),
        "-10%~-5%": int(((values > -0.10) & (values <= -0.05)).sum()),
        "-5%~0%": int(((values > -0.05) & (values <= 0)).sum()),
        "0%~5%": int(((values > 0) & (values <= 0.05)).sum()),
        ">5%": int((values > 0.05).sum()),
    }
    return bins


def _downside_deviation(values: pd.Series) -> float | None:
    if values.empty:
        return None
    downside = values[values < 0]
    if downside.empty:
        return None
    return float(downside.std(ddof=0))


def _calmar_ratio(mean_return: float | None, max_drawdown: float | None) -> float | None:
    if mean_return is None or max_drawdown in {None, 0}:
        return None
    return float(mean_return / abs(max_drawdown))


def _sortino_ratio(values: pd.Series) -> float | None:
    if values.empty:
        return None
    mean_return = float(values.mean())
    downside = _downside_deviation(values)
    if downside in {None, 0}:
        return None
    return float(mean_return / downside)


def _monthly_table(frame: pd.DataFrame, return_column: str) -> list[dict[str, Any]]:
    if "date" not in frame.columns or return_column not in frame.columns:
        return []
    temp = frame.copy()
    temp["date"] = pd.to_datetime(temp["date"], errors="coerce")
    temp = temp.dropna(subset=["date", return_column])
    if temp.empty:
        return []
    temp["period"] = temp["date"].dt.to_period("M").astype(str)
    grouped = temp.groupby("period")[return_column].agg(["count", "mean"]).reset_index()
    return [
        {"period": row["period"], "sample_count": int(row["count"]), "mean_return": float(row["mean"])}
        for _, row in grouped.iterrows()
    ]


def _annual_table(frame: pd.DataFrame, return_column: str) -> list[dict[str, Any]]:
    if "date" not in frame.columns or return_column not in frame.columns:
        return []
    temp = frame.copy()
    temp["date"] = pd.to_datetime(temp["date"], errors="coerce")
    temp = temp.dropna(subset=["date", return_column])
    if temp.empty:
        return []
    temp["year"] = temp["date"].dt.year
    grouped = temp.groupby("year")[return_column].agg(["count", "mean"]).reset_index()
    return [
        {"period": int(row["year"]), "sample_count": int(row["count"]), "mean_return": float(row["mean"])}
        for _, row in grouped.iterrows()
    ]


def _rank_ic(frame: pd.DataFrame, score_column: str = "rank", return_column: str = "forward_return_5d") -> tuple[float | None, int]:
    if score_column not in frame.columns or return_column not in frame.columns:
        return None, 0
    temp = frame[[score_column, return_column]].copy()
    temp[score_column] = pd.to_numeric(temp[score_column], errors="coerce")
    temp[return_column] = pd.to_numeric(temp[return_column], errors="coerce")
    temp = temp.dropna()
    if len(temp) < 2:
        return None, len(temp)
    ic = temp[score_column].corr(temp[return_column], method="spearman")
    return (float(ic) if ic == ic else None), len(temp)


def summarize_research_metrics(rows: Any) -> ResearchMetricsSummary:
    frame = _frame(rows)
    if frame.empty:
        return ResearchMetricsSummary(
            sample_count=0,
            forward_return_5d_mean=None,
            forward_return_20d_mean=None,
            median_return=None,
            positive_sample_ratio=None,
            excess_return_mean=None,
            max_drawdown=None,
            calmar_ratio=None,
            sortino_ratio=None,
            return_distribution={},
            monthly_return_table=[],
            annual_return_table=[],
            rank_ic=None,
            rank_ic_sample_count=0,
            disclaimer=RESEARCH_METRICS_DISCLAIMER,
        )

    return_5d = _series(frame, "forward_return_5d")
    return_20d = _series(frame, "forward_return_20d")
    excess = _series(frame, "excess_return_5d")
    if excess.empty:
        excess = _series(frame, "excess_return")
    drawdown = _series(frame, "drawdown")
    if drawdown.empty:
        drawdown = _series(frame, "max_drawdown")
    base = return_5d if not return_5d.empty else (return_20d if not return_20d.empty else excess)
    max_drawdown = float(drawdown.min()) if not drawdown.empty else None
    mean_5d = float(return_5d.mean()) if not return_5d.empty else None
    mean_20d = float(return_20d.mean()) if not return_20d.empty else None
    median_return = float(base.median()) if not base.empty else None
    positive_ratio = float((base > 0).mean()) if not base.empty else None
    excess_mean = float(excess.mean()) if not excess.empty else None
    monthly = _monthly_table(frame, "forward_return_5d" if not return_5d.empty else ("forward_return_20d" if not return_20d.empty else "excess_return_5d"))
    annual = _annual_table(frame, "forward_return_5d" if not return_5d.empty else ("forward_return_20d" if not return_20d.empty else "excess_return_5d"))
    rank_ic, rank_ic_count = _rank_ic(frame)
    return ResearchMetricsSummary(
        sample_count=int(len(frame)),
        forward_return_5d_mean=mean_5d,
        forward_return_20d_mean=mean_20d,
        median_return=median_return,
        positive_sample_ratio=positive_ratio,
        excess_return_mean=excess_mean,
        max_drawdown=max_drawdown,
        calmar_ratio=_calmar_ratio(excess_mean if excess_mean is not None else median_return, max_drawdown),
        sortino_ratio=_sortino_ratio(base),
        return_distribution=_distribution(base) if not base.empty else {},
        monthly_return_table=monthly,
        annual_return_table=annual,
        rank_ic=rank_ic,
        rank_ic_sample_count=rank_ic_count,
        disclaimer=RESEARCH_METRICS_DISCLAIMER,
    )


def research_metrics_to_dict(summary: ResearchMetricsSummary) -> dict[str, Any]:
    return asdict(summary)


__all__ = [
    "RESEARCH_METRICS_DISCLAIMER",
    "ResearchMetricsSummary",
    "research_metrics_to_dict",
    "summarize_research_metrics",
]
