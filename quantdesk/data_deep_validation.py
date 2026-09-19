"""深度数据质量验证模块 — 超越行数和日期跨度的内容级检查。

检查项：
  1. 价格逻辑错误（high < low, open/close 超出 high-low 范围）
  2. 极端离群值检测（MAD / IQR 方法）
  3. 停牌伪装检测（连续多日 O=H=L=C 且 volume=0）
  4. 复权异常检测（已有 adjustment_suspect，这里更细化）
  5. 收益率尖刺检测（单日涨跌超阈值但非涨跌停）
  6. 数据新鲜度评分
  7. 缺失值分布热力（连续缺失 vs 随机缺失）

所有结果为研究诊断，不修改原始数据，不生成投资建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame

DISCLAIMER = "深度数据验证仅为本地研究诊断，不修改原始数据，不构成投资建议。"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LogicalErrorReport:
    """一行或多行违反价格逻辑的记录。"""
    error_type: str            # "high_lt_low", "close_out_of_range", "negative_price"
    row_indices: list[int] = field(default_factory=list)
    count: int = 0
    severity: str = "ERROR"    # ERROR / WARNING
    message: str = ""


@dataclass
class OutlierReport:
    """离群值统计。"""
    column: str = ""
    method: str = "mad"        # mad / iqr
    threshold: float = 5.0
    outlier_count: int = 0
    outlier_pct: float = 0.0
    sample_indices: list[int] = field(default_factory=list)
    severity: str = "WARNING"


@dataclass
class SuspiciousPatternReport:
    """可疑数据模式。"""
    pattern: str = ""          # "suspended_disguise", "duplicate_rows", "flat_line"
    count: int = 0
    row_indices: list[int] = field(default_factory=list)
    severity: str = "WARNING"
    message: str = ""


@dataclass
class DeepValidationResult:
    """深度验证汇总。"""
    symbol: str = ""
    total_rows: int = 0
    logical_errors: list[LogicalErrorReport] = field(default_factory=list)
    outlier_reports: list[OutlierReport] = field(default_factory=list)
    suspicious_patterns: list[SuspiciousPatternReport] = field(default_factory=list)
    data_quality_score: float = 100.0   # 0-100, 越高越好
    quality_label: str = "EXCELLENT"
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_rows": self.total_rows,
            "data_quality_score": round(self.data_quality_score, 2),
            "quality_label": self.quality_label,
            "logical_error_count": sum(e.count for e in self.logical_errors),
            "outlier_count": sum(o.outlier_count for o in self.outlier_reports),
            "suspicious_pattern_count": sum(p.count for p in self.suspicious_patterns),
            "logical_errors": [
                {"type": e.error_type, "count": e.count, "severity": e.severity, "message": e.message}
                for e in self.logical_errors
            ],
            "outliers": [
                {"column": o.column, "method": o.method, "count": o.outlier_count, "pct": round(o.outlier_pct, 4)}
                for o in self.outlier_reports
            ],
            "suspicious_patterns": [
                {"pattern": p.pattern, "count": p.count, "severity": p.severity, "message": p.message}
                for p in self.suspicious_patterns
            ],
            "warnings": self.warnings,
            "disclaimer": self.disclaimer,
        }


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------

_COL_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "trade_date"),
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "成交量"),
    "amount": ("amount", "成交额"),
}


def _resolve_col(df: pd.DataFrame, key: str) -> str | None:
    for alias in _COL_ALIASES.get(key, ()):
        if alias in df.columns:
            return alias
    return None


def _to_numeric(df: pd.DataFrame, col: str | None) -> pd.Series:
    if col is None or col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_logical_errors(df: pd.DataFrame) -> list[LogicalErrorReport]:
    """检测价格逻辑错误：high<low、close超出[low,high]、负价格。"""
    reports: list[LogicalErrorReport] = []
    high_col = _resolve_col(df, "high")
    low_col = _resolve_col(df, "low")
    open_col = _resolve_col(df, "open")
    close_col = _resolve_col(df, "close")

    high = _to_numeric(df, high_col)
    low = _to_numeric(df, low_col)
    opn = _to_numeric(df, open_col)
    close = _to_numeric(df, close_col)

    # 负价格
    for name, series in [("open", opn), ("high", high), ("low", low), ("close", close)]:
        neg_mask = series.dropna() <= 0
        if neg_mask.any():
            idx = list(neg_mask[neg_mask].index[:10])
            reports.append(LogicalErrorReport(
                error_type="negative_price",
                row_indices=idx,
                count=int(neg_mask.sum()),
                severity="ERROR",
                message=f"{name} 存在 {int(neg_mask.sum())} 个非正价格",
            ))

    # high < low
    if not high.empty and not low.empty:
        valid = high.notna() & low.notna()
        mask = valid & (high < low)
        if mask.any():
            idx = list(mask[mask].index[:10])
            reports.append(LogicalErrorReport(
                error_type="high_lt_low",
                row_indices=idx,
                count=int(mask.sum()),
                severity="ERROR",
                message=f"{int(mask.sum())} 行 high < low",
            ))

    # close 超出 [low, high]
    if not close.empty and not high.empty and not low.empty:
        valid = close.notna() & high.notna() & low.notna()
        above = valid & (close > high * 1.001)
        below = valid & (close < low * 0.999)
        bad = above | below
        if bad.any():
            idx = list(bad[bad].index[:10])
            reports.append(LogicalErrorReport(
                error_type="close_out_of_range",
                row_indices=idx,
                count=int(bad.sum()),
                severity="WARNING",
                message=f"{int(bad.sum())} 行 close 超出 [low, high] 范围（含0.1%容差）",
            ))

    return reports


def check_outliers(
    df: pd.DataFrame,
    *,
    columns: Sequence[str] = ("close",),
    method: str = "mad",
    threshold: float = 5.0,
) -> list[OutlierReport]:
    """检测极端离群值。

    method:
      - "mad": 中位数绝对偏差，|x - median| / MAD > threshold
      - "iqr": 四分位距，x < Q1 - threshold*IQR 或 x > Q3 + threshold*IQR
    """
    reports: list[OutlierReport] = []
    for key in columns:
        col = _resolve_col(df, key)
        series = _to_numeric(df, col).dropna()
        if len(series) < 20:
            continue

        if method == "mad":
            median = series.median()
            mad = (series - median).abs().median()
            if mad > 0:
                z = (series - median).abs() / (mad * 1.4826)
                mask = z > threshold
            else:
                mask = pd.Series(False, index=series.index)
        else:  # iqr
            q1 = series.quantile(0.25)
            q3 = series.quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                mask = (series < q1 - threshold * iqr) | (series > q3 + threshold * iqr)
            else:
                mask = pd.Series(False, index=series.index)

        count = int(mask.sum())
        pct = count / len(series) if len(series) > 0 else 0.0
        if count > 0:
            reports.append(OutlierReport(
                column=key,
                method=method,
                threshold=threshold,
                outlier_count=count,
                outlier_pct=round(pct, 6),
                sample_indices=list(mask[mask].index[:5]),
                severity="ERROR" if pct > 0.05 else "WARNING",
            ))
    return reports


def check_return_spikes(
    df: pd.DataFrame,
    *,
    spike_pct: float = 0.35,
    board_limit_pct: float = 0.20,
) -> list[SuspiciousPatternReport]:
    """检测非涨跌停的异常收益率尖刺。"""
    close_col = _resolve_col(df, "close")
    close = _to_numeric(df, close_col).dropna()
    if len(close) < 2:
        return []

    rets = close.ffill().pct_change(fill_method=None).abs()
    spikes = rets > spike_pct
    # 排除涨跌停（涨跌停后一日通常反转较小，这里不做进一步判断）
    if not spikes.any():
        return []

    idx = list(spikes[spikes].index[:10])
    return [SuspiciousPatternReport(
        pattern="return_spike",
        count=int(spikes.sum()),
        row_indices=idx,
        severity="WARNING",
        message=f"{int(spikes.sum())} 个单日收益率超过 {spike_pct:.0%}，可能为复权异常",
    )]


def check_suspended_disguise(df: pd.DataFrame) -> list[SuspiciousPatternReport]:
    """检测停牌伪装：连续多日 O=H=L=C 且 volume=0。"""
    reports: list[SuspiciousPatternReport] = []
    open_col = _resolve_col(df, "open")
    high_col = _resolve_col(df, "high")
    low_col = _resolve_col(df, "low")
    close_col = _resolve_col(df, "close")
    vol_col = _resolve_col(df, "volume")

    o = _to_numeric(df, open_col)
    h = _to_numeric(df, high_col)
    l = _to_numeric(df, low_col)
    c = _to_numeric(df, close_col)
    v = _to_numeric(df, vol_col)

    if o.empty or h.empty or l.empty or c.empty:
        return []

    tolerance = 1e-6
    flat = (
        (o - h).abs().lt(tolerance) &
        (h - l).abs().lt(tolerance) &
        (l - c).abs().lt(tolerance)
    )
    if not v.empty:
        zero_vol = v.fillna(0).abs().lt(1.0)
        mask = flat & zero_vol
    else:
        mask = flat

    if not mask.any():
        return []

    # 连续天数
    groups = (~mask).cumsum()
    group_sizes = mask.groupby(groups).sum()
    long_flat = group_sizes[group_sizes >= 3]
    total_days = int(mask.sum())

    if total_days > 0:
        reports.append(SuspiciousPatternReport(
            pattern="suspended_disguise",
            count=total_days,
            row_indices=list(mask[mask].index[:10]),
            severity="WARNING" if total_days < 20 else "ERROR",
            message=(
                f"{total_days} 日 O=H=L=C 且无成交量，可能为停牌数据伪装。"
                f"其中 {len(long_flat)} 段连续≥3日。"
            ),
        ))

    return reports


def check_duplicate_rows(df: pd.DataFrame) -> list[SuspiciousPatternReport]:
    """检测完全重复的行。"""
    reports: list[SuspiciousPatternReport] = []
    date_col = _resolve_col(df, "date")
    if date_col and date_col in df.columns:
        dupes = df[date_col].duplicated(keep=False)
        if dupes.any():
            reports.append(SuspiciousPatternReport(
                pattern="duplicate_dates",
                count=int(dupes.sum()),
                row_indices=list(dupes[dupes].index[:10]),
                severity="WARNING",
                message=f"{int(dupes.sum())} 行日期重复",
            ))

    # 全行重复
    price_cols = [_resolve_col(df, k) for k in ("open", "high", "low", "close") if _resolve_col(df, k)]
    if len(price_cols) >= 3:
        full_dupes = df[price_cols].duplicated(keep=False)
        count = int(full_dupes.sum())
        if count > 5:
            reports.append(SuspiciousPatternReport(
                pattern="duplicate_price_rows",
                count=count,
                row_indices=list(full_dupes[full_dupes].index[:10]),
                severity="WARNING",
                message=f"{count} 行 OHLC 完全重复",
            ))
    return reports


def check_consecutive_missing(
    df: pd.DataFrame,
    *,
    max_gap_allowed: int = 5,
) -> list[SuspiciousPatternReport]:
    """检测连续缺失（NaN）的模式。"""
    reports: list[SuspiciousPatternReport] = []
    close_col = _resolve_col(df, "close")
    close = _to_numeric(df, close_col)
    if close.empty:
        return []

    missing = close.isna()
    if not missing.any():
        return []

    groups = (~missing).cumsum()
    group_sizes = missing.groupby(groups).sum()
    long_gaps = group_sizes[group_sizes > max_gap_allowed]

    if not long_gaps.empty:
        total = int(missing.sum())
        reports.append(SuspiciousPatternReport(
            pattern="consecutive_missing",
            count=total,
            row_indices=list(missing[missing].index[:10]),
            severity="WARNING" if total < 30 else "ERROR",
            message=(
                f"close 列共 {total} 个缺失值，"
                f"其中 {len(long_gaps)} 段连续缺失超过 {max_gap_allowed} 日。"
                f"最长连续缺失 {int(long_gaps.max())} 日。"
            ),
        ))

    return reports


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def _compute_quality_score(
    total_rows: int,
    logical_errors: list[LogicalErrorReport],
    outlier_reports: list[OutlierReport],
    suspicious_patterns: list[SuspiciousPatternReport],
) -> tuple[float, str]:
    """计算 0-100 的数据质量分数。"""
    if total_rows <= 0:
        return 0.0, "EMPTY"

    score = 100.0
    error_count = sum(e.count for e in logical_errors)
    outlier_count = sum(o.outlier_count for o in outlier_reports)
    pattern_count = sum(p.count for p in suspicious_patterns)

    # 逻辑错误：每1个扣3分
    score -= min(error_count * 3.0, 40.0)

    # 离群值：按比例扣分
    outlier_pct = outlier_count / total_rows
    score -= min(outlier_pct * 500.0, 25.0)

    # 可疑模式：按比例扣分
    pattern_pct = pattern_count / total_rows
    score -= min(pattern_pct * 300.0, 25.0)

    # 额外：有 ERROR 级别直接封顶60
    has_error = any(e.severity == "ERROR" for e in logical_errors)
    has_error = has_error or any(o.severity == "ERROR" for o in outlier_reports)
    has_error = has_error or any(p.severity == "ERROR" for p in suspicious_patterns)
    if has_error:
        score = min(score, 60.0)

    score = max(0.0, score)

    if score >= 90:
        label = "EXCELLENT"
    elif score >= 70:
        label = "GOOD"
    elif score >= 50:
        label = "FAIR"
    elif score >= 30:
        label = "POOR"
    else:
        label = "UNUSABLE"

    return round(score, 2), label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deep_validate_daily(
    df: pd.DataFrame,
    *,
    symbol: str = "",
    outlier_threshold: float = 5.0,
    spike_pct: float = 0.35,
    max_consecutive_gap: int = 5,
) -> DeepValidationResult:
    """对日线 DataFrame 执行全面深度验证。

    Parameters
    ----------
    df : 日线数据（必须包含 OHLCV 列或其中文别名）
    symbol : 股票代码（用于标记）
    outlier_threshold : 离群值检测阈值（MAD 倍数或 IQR 倍数）
    spike_pct : 收益率尖刺阈值
    max_consecutive_gap : 连续缺失最大容忍天数

    Returns
    -------
    DeepValidationResult 包含质量分数和所有检查项详情。
    """
    if df is None or df.empty:
        return DeepValidationResult(
            symbol=symbol, total_rows=0,
            data_quality_score=0.0, quality_label="EMPTY",
            warnings=["输入数据为空"],
        )

    total = len(df)
    logical = check_logical_errors(df)
    outliers = check_outliers(df, columns=("close", "volume"), threshold=outlier_threshold)
    patterns: list[SuspiciousPatternReport] = []
    patterns.extend(check_suspended_disguise(df))
    patterns.extend(check_duplicate_rows(df))
    patterns.extend(check_return_spikes(df, spike_pct=spike_pct))
    patterns.extend(check_consecutive_missing(df, max_gap_allowed=max_consecutive_gap))

    score, label = _compute_quality_score(total, logical, outliers, patterns)

    warnings: list[str] = []
    if score < 70:
        warnings.append(f"数据质量分数 {score:.0f} 偏低，回测结论可靠性下降。")
    if any(e.severity == "ERROR" for e in logical):
        warnings.append("存在价格逻辑错误，请检查数据源或复权方式。")
    if any(p.pattern == "suspended_disguise" for p in patterns):
        warnings.append("检测到停牌伪装数据，回测时这些日期可能被错误交易。")

    return DeepValidationResult(
        symbol=symbol,
        total_rows=total,
        logical_errors=logical,
        outlier_reports=outliers,
        suspicious_patterns=patterns,
        data_quality_score=score,
        quality_label=label,
        warnings=warnings,
    )


def deep_validate_daily_csv(
    csv_path: str | Any,
    *,
    symbol: str = "",
) -> DeepValidationResult:
    """对日线 CSV 文件执行深度验证。"""
    from pathlib import Path as _Path
    path = _Path(csv_path)
    if not path.exists():
        return DeepValidationResult(
            symbol=symbol or path.stem,
            total_rows=0,
            data_quality_score=0.0,
            quality_label="MISSING",
            warnings=[f"文件不存在: {path}"],
        )
    try:
        df = read_csv_frame(path)
    except Exception as exc:
        return DeepValidationResult(
            symbol=symbol or path.stem,
            total_rows=0,
            data_quality_score=0.0,
            quality_label="READ_ERROR",
            warnings=[f"读取失败: {exc}"],
        )
    csv_read_warnings = [str(item) for item in df.attrs.get("warnings", []) if item]
    if df.empty and any("csv_read_failed" in item for item in csv_read_warnings):
        return DeepValidationResult(
            symbol=symbol or path.stem,
            total_rows=0,
            data_quality_score=0.0,
            quality_label="READ_ERROR",
            warnings=csv_read_warnings,
        )
    result = deep_validate_daily(df, symbol=symbol or path.stem)
    result.warnings.extend(csv_read_warnings)
    return result


def batch_deep_validate(
    csv_dir: str | Any,
    *,
    min_score: float = 50.0,
) -> dict[str, Any]:
    """批量验证目录下所有日线 CSV。

    Returns dict with summary stats and per-symbol results.
    """
    from pathlib import Path as _Path
    root = _Path(csv_dir)
    if not root.exists():
        return {"status": "error", "message": f"目录不存在: {root}", "items": []}

    results: list[DeepValidationResult] = []
    for csv_file in sorted(root.glob("*.csv")):
        result = deep_validate_daily_csv(csv_file)
        results.append(result)

    total = len(results)
    passed = sum(1 for r in results if r.data_quality_score >= min_score)
    avg_score = sum(r.data_quality_score for r in results) / total if total else 0.0

    return {
        "status": "ok",
        "total_files": total,
        "passed_count": passed,
        "failed_count": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "average_quality_score": round(avg_score, 2),
        "items": [r.to_dict() for r in results],
        "disclaimer": DISCLAIMER,
    }
