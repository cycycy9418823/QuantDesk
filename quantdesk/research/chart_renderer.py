from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Static charts are research visuals only. Not investment advice and not a trading signal."


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _load_pyplot() -> tuple[Any | None, list[str]]:
    warnings: list[str] = []
    if str(os.environ.get("QUANTDESK_ENABLE_MATPLOTLIB_CHARTS", "")).lower() not in {"1", "true", "yes"}:
        return None, ["matplotlib_chart_rendering_disabled_by_default: set QUANTDESK_ENABLE_MATPLOTLIB_CHARTS=true to enable local PNG rendering"]
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        try:
            plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Microsoft YaHei", "SimHei", "Arial Unicode MS"]
            plt.rcParams["axes.unicode_minus"] = False
        except Exception as exc:
            warnings.append(f"font_fallback_warning:{exc}")
        return plt, warnings
    except Exception as exc:
        return None, [f"matplotlib_unavailable:{exc}"]


def _save(fig: Any, path: str | Path) -> str:
    target = _project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=150)
    return _display_path(target)


def render_equity_curve(data: Any, output_path: str | Path = "reports/charts/equity_curve.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty or "equity" not in frame.columns:
        return {"ok": False, "path": "", "warnings": warnings + ["equity_curve_empty"]}
    frame["date"] = pd.to_datetime(frame.get("date"), errors="coerce")
    frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
    frame = frame.dropna(subset=["date", "equity"]).sort_values("date")
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(frame["date"], frame["equity"], color="#2563eb", linewidth=1.6)
    ax.set_title("Equity Curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity")
    ax.grid(True, alpha=0.25)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}


def render_monthly_returns(data: Any, output_path: str | Path = "reports/charts/monthly_returns.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty or "equity" not in frame.columns:
        return {"ok": False, "path": "", "warnings": warnings + ["monthly_returns_empty"]}
    frame["date"] = pd.to_datetime(frame.get("date"), errors="coerce")
    frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
    frame = frame.dropna(subset=["date", "equity"]).sort_values("date").set_index("date")
    monthly = frame["equity"].resample("ME").last().ffill().pct_change(fill_method=None).dropna()
    fig, ax = plt.subplots(figsize=(8, 4))
    colors = ["#16a34a" if value >= 0 else "#dc2626" for value in monthly]
    ax.bar(monthly.index.strftime("%Y-%m"), monthly.values, color=colors)
    ax.set_title("Monthly Returns")
    ax.set_xlabel("Month")
    ax.set_ylabel("Return")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(True, axis="y", alpha=0.25)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}


def render_factor_ic_timeseries(data: Any, output_path: str | Path = "reports/charts/factor_ic_timeseries.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty:
        return {"ok": False, "path": "", "warnings": warnings + ["factor_ic_timeseries_empty"]}
    if "date" not in frame.columns:
        frame["date"] = range(len(frame))
    value_col = "rank_ic" if "rank_ic" in frame.columns else ("ic" if "ic" in frame.columns else frame.select_dtypes("number").columns[0])
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(frame["date"], pd.to_numeric(frame[value_col], errors="coerce"), color="#7c3aed", linewidth=1.4)
    ax.axhline(0, color="#666", linewidth=0.8)
    ax.set_title("Factor IC Timeseries")
    ax.grid(True, alpha=0.25)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}


def render_industry_exposure_heatmap(data: Any, output_path: str | Path = "reports/charts/industry_exposure_heatmap.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty:
        return {"ok": False, "path": "", "warnings": warnings + ["industry_exposure_empty"]}
    label_col = "sector" if "sector" in frame.columns else ("industry" if "industry" in frame.columns else frame.columns[0])
    value_col = "weight" if "weight" in frame.columns else ("market_value" if "market_value" in frame.columns else frame.select_dtypes("number").columns[0])
    values = pd.to_numeric(frame[value_col], errors="coerce").fillna(0.0).to_numpy().reshape(1, -1)
    fig, ax = plt.subplots(figsize=(max(6, len(frame) * 0.8), 2.6))
    image = ax.imshow(values, aspect="auto", cmap="Blues")
    ax.set_xticks(range(len(frame)))
    ax.set_xticklabels(frame[label_col].astype(str).tolist(), rotation=45, ha="right")
    ax.set_yticks([0])
    ax.set_yticklabels(["Exposure"])
    ax.set_title("Industry / Sector Exposure")
    fig.colorbar(image, ax=ax, fraction=0.025)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}


def render_quantile_returns(data: Any, output_path: str | Path = "reports/charts/quantile_returns.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty:
        return {"ok": False, "path": "", "warnings": warnings + ["quantile_returns_empty"]}
    label_col = "quantile" if "quantile" in frame.columns else frame.columns[0]
    value_col = "return" if "return" in frame.columns else frame.select_dtypes("number").columns[-1]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(frame[label_col].astype(str), pd.to_numeric(frame[value_col], errors="coerce"), color="#0f766e")
    ax.set_title("Quantile Returns")
    ax.grid(True, axis="y", alpha=0.25)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}


def render_sector_strength_ranking(data: Any, output_path: str | Path = "reports/charts/sector_strength_ranking.png") -> dict[str, Any]:
    plt, warnings = _load_pyplot()
    if plt is None:
        return {"ok": False, "path": "", "warnings": warnings}
    frame = pd.DataFrame(data)
    if frame.empty:
        return {"ok": False, "path": "", "warnings": warnings + ["sector_strength_empty"]}
    label_col = "sector_name" if "sector_name" in frame.columns else ("sector" if "sector" in frame.columns else frame.columns[0])
    value_col = "heat_score" if "heat_score" in frame.columns else ("strength_score" if "strength_score" in frame.columns else frame.select_dtypes("number").columns[-1])
    frame = frame.copy()
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce").fillna(0.0)
    frame = frame.sort_values(value_col).tail(15)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.barh(frame[label_col].astype(str), frame[value_col], color="#f59e0b")
    ax.set_title("Sector Strength Ranking")
    ax.grid(True, axis="x", alpha=0.25)
    path = _save(fig, output_path)
    plt.close(fig)
    return {"ok": True, "path": path, "warnings": warnings}
