from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_chunks, read_csv_frame


CANONICAL_ADJUSTMENT_FACTOR_FILE = "adjustment_factors.csv"
ADJUSTMENT_FACTOR_ALIAS_FILES = ("adjustment_factor.csv",)


def _csv_sample_signature(path: Path, *, sample_rows: int) -> dict[str, Any]:
    frame = read_csv_frame(path, dtype=str, nrows=sample_rows)
    warnings = list(frame.attrs.get("warnings", []) or [])
    columns = [str(col) for col in frame.columns]
    sample_text = frame.fillna("").to_csv(index=False)
    digest = hashlib.sha256(sample_text.encode("utf-8")).hexdigest()
    return {
        "name": path.name,
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "columns": columns,
        "sample_rows": int(len(frame)),
        "sample_sha256": digest,
        "read_warnings": warnings,
    }


def audit_adjustment_factor_file_set(
    metadata_dir: Path,
    *,
    canonical_name: str = CANONICAL_ADJUSTMENT_FACTOR_FILE,
    sample_rows: int = 1000,
) -> dict[str, Any]:
    """Audit duplicate adjustment-factor metadata files without loading them fully.

    ``adjustment_factors.csv`` is the authoritative project filename. A singular
    ``adjustment_factor.csv`` may exist as a local alias after manual imports;
    this helper distinguishes a harmless duplicate alias from a divergent file
    that could make downstream audits read different metadata.
    """
    root = Path(metadata_dir)
    canonical = root / canonical_name
    issues: list[str] = []
    alternate_files: list[dict[str, Any]] = []
    if not canonical.exists():
        existing_aliases = [name for name in ADJUSTMENT_FACTOR_ALIAS_FILES if (root / name).exists()]
        return {
            "authoritative_file": canonical_name,
            "authoritative_exists": False,
            "file_set_status": "canonical_missing",
            "alternate_files": [{"name": name, "exists": True} for name in existing_aliases],
            "issues": [f"canonical_adjustment_factor_file_missing:{canonical_name}"],
        }

    canonical_sig = _csv_sample_signature(canonical, sample_rows=sample_rows)
    for alias_name in ADJUSTMENT_FACTOR_ALIAS_FILES:
        alias = root / alias_name
        if alias_name == canonical_name or not alias.exists():
            continue
        alias_sig = _csv_sample_signature(alias, sample_rows=sample_rows)
        matches = (
            alias_sig["size_bytes"] == canonical_sig["size_bytes"]
            and alias_sig["columns"] == canonical_sig["columns"]
            and alias_sig["sample_sha256"] == canonical_sig["sample_sha256"]
        )
        alias_sig["matches_authoritative_sample"] = matches
        alternate_files.append(alias_sig)
        if matches:
            issues.append(f"duplicate_alias_adjustment_factor_file:{alias_name}")
        else:
            issues.append(f"divergent_adjustment_factor_file:{alias_name}")

    if any(str(item).startswith("divergent_adjustment_factor_file:") for item in issues):
        status = "canonical_with_divergent_alias"
    elif alternate_files:
        status = "canonical_with_duplicate_alias"
    else:
        status = "canonical_only"

    return {
        "authoritative_file": canonical_name,
        "authoritative_exists": True,
        "file_set_status": status,
        "authoritative_sample": canonical_sig,
        "alternate_files": alternate_files,
        "issues": issues,
    }


def _iter_adjustment_chunks(path: Path, *, chunksize: int = 250_000):
    """Yield BOM-safe adjustment-factor chunks without loading huge CSVs at once."""

    return read_csv_chunks(
        path,
        dtype={"symbol": str, "trade_date": str},
        chunksize=chunksize,
    )


def audit_adjustment_factors(path: Path, candidate_symbols: set[str], selected_symbols: set[str], drawdown_symbols: set[str]) -> dict[str, Any]:
    file_set = audit_adjustment_factor_file_set(path.parent, canonical_name=path.name)
    if not path.exists():
        return {
            "adjustment_factors_status": "missing",
            "exists": False,
            "rows": 0,
            "symbol_count": 0,
            "candidate_holdings_coverage": 0.0,
            "selected_symbols_coverage": 0.0,
            "drawdown_symbols_coverage": 0.0,
            "metadata_file_set_status": file_set.get("file_set_status"),
            "alternate_adjustment_factor_files": file_set.get("alternate_files", []),
            "issues": [f"{path.name} missing"],
        }
    required = {"trade_date", "symbol", "adj_factor", "source"}
    issues: list[str] = []
    rows = 0
    symbols: set[str] = set()
    columns: set[str] = set()
    min_date: pd.Timestamp | None = None
    max_date: pd.Timestamp | None = None
    null_factor_count = 0
    non_positive_count = 0
    has_source = False
    has_provider = False
    read_warnings: list[str] = []
    # 重复键 (symbol, trade_date)：全局跨 chunk 判重。复权因子出现重复键意味着
    # 复权口径歧义，是典型 P0 数据真实性问题，必须真实计算而非谎报 None。
    seen_keys: set[tuple[str, str]] = set()
    dup_count = 0
    # 相邻复权因子异常跳变：同一 symbol 相邻交易日 adj_factor 比值偏离 1 超过阈值。
    # streaming 下无法保证跨 chunk 的 symbol 顺序，故按 symbol 维护"最后一次
    # (date, factor)"，跨 chunk 也能按日期续接比较；口径标注为 per_symbol_adjacent。
    last_by_symbol: dict[str, tuple[str, float]] = {}
    factor_jumps = 0
    JUMP_RATIO = 3.0  # 相邻比值 >3x 或 <1/3 视为异常跳变（保守阈值）

    def _accumulate(frame: pd.DataFrame) -> None:
        nonlocal dup_count, factor_jumps
        if "symbol" not in frame.columns or "trade_date" not in frame.columns:
            return
        sub = frame[["symbol", "trade_date"]].copy()
        sub["symbol"] = sub["symbol"].astype(str).str.zfill(6)
        sub["trade_date"] = sub["trade_date"].astype(str)
        has_factor = "adj_factor" in frame.columns
        if has_factor:
            sub["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce").to_numpy()
        # 按 (symbol, trade_date) 排序保证同 symbol 相邻比较有序（含跨 chunk 续接）。
        sub = sub.sort_values(["symbol", "trade_date"], kind="stable")
        for record in sub.itertuples(index=False):
            symbol = record.symbol
            date = record.trade_date
            key = (symbol, date)
            if key in seen_keys:
                dup_count += 1
            else:
                seen_keys.add(key)
            if has_factor:
                factor = getattr(record, "adj_factor", float("nan"))
                if factor == factor and factor > 0:  # not NaN and positive
                    prev = last_by_symbol.get(symbol)
                    if prev is not None and prev[1] > 0 and date >= prev[0]:
                        ratio = factor / prev[1]
                        if ratio >= JUMP_RATIO or ratio <= 1.0 / JUMP_RATIO:
                            factor_jumps += 1
                    last_by_symbol[symbol] = (date, float(factor))

    try:
        for chunk in _iter_adjustment_chunks(path):
            columns.update(str(c) for c in chunk.columns)
            rows += int(len(chunk))
            has_source = has_source or "source" in chunk.columns
            has_provider = has_provider or "provider" in chunk.columns
            if "symbol" in chunk.columns:
                symbols.update(chunk["symbol"].dropna().astype(str).str.zfill(6).unique().tolist())
            if "trade_date" in chunk.columns:
                dates = pd.to_datetime(chunk["trade_date"], errors="coerce")
                if len(dates):
                    cmin = dates.min()
                    cmax = dates.max()
                    if pd.notna(cmin):
                        min_date = cmin if min_date is None else min(min_date, cmin)
                    if pd.notna(cmax):
                        max_date = cmax if max_date is None else max(max_date, cmax)
            if "adj_factor" in chunk.columns:
                factors = pd.to_numeric(chunk["adj_factor"], errors="coerce")
                null_factor_count += int(factors.isna().sum())
                non_positive_count += int((factors <= 0).sum())
            _accumulate(chunk)
    except Exception as exc:
        df = read_csv_frame(path, dtype={"symbol": str})
        read_warnings = list(df.attrs.get("warnings", []))
        read_warnings.append(f"chunked_read_fell_back_to_full_read:{type(exc).__name__}")
        columns = set(str(c) for c in df.columns)
        rows = int(len(df))
        if "symbol" in df.columns:
            symbols = set(df["symbol"].dropna().astype(str).str.zfill(6))
        dates = pd.to_datetime(df["trade_date"], errors="coerce") if "trade_date" in df.columns else pd.Series([], dtype="datetime64[ns]")
        if len(dates) and pd.notna(dates.min()):
            min_date = dates.min()
        if len(dates) and pd.notna(dates.max()):
            max_date = dates.max()
        if "adj_factor" in df.columns:
            factors = pd.to_numeric(df["adj_factor"], errors="coerce")
            null_factor_count = int(factors.isna().sum())
            non_positive_count = int((factors <= 0).sum())
        has_source = "source" in df.columns
        has_provider = "provider" in df.columns
        # 全表回退路径：重置累计量后在完整表上重算，保证 dup/jump 口径一致。
        seen_keys.clear()
        last_by_symbol.clear()
        dup_count = 0
        factor_jumps = 0
        _accumulate(df)

    missing = sorted(required - columns)
    if missing:
        issues.append("missing_columns:" + ",".join(missing))
    if read_warnings:
        issues.extend(read_warnings)
    issues.extend(str(item) for item in file_set.get("issues", []) or [])
    if dup_count:
        issues.append(f"duplicate_symbol_date_keys:{dup_count}")
    if factor_jumps:
        issues.append(f"abnormal_adj_factor_jumps:{factor_jumps}")
    candidate_cov = len(symbols & candidate_symbols) / max(1, len(candidate_symbols))
    selected_cov = len(symbols & selected_symbols) / max(1, len(selected_symbols))
    drawdown_cov = len(symbols & drawdown_symbols) / max(1, len(drawdown_symbols))
    if missing or null_factor_count or non_positive_count:
        status = "partial_low_coverage"
    elif candidate_cov < 0.2 or selected_cov < 0.2:
        status = "partial_low_coverage"
    elif candidate_cov < 0.8 or selected_cov < 0.8:
        status = "partial_usable_for_audit"
    else:
        status = "formal_candidate_needs_review"
    return {
        "adjustment_factors_status": status,
        "exists": True,
        "rows": rows,
        "symbol_count": len(symbols),
        "date_min": min_date.date().isoformat() if min_date is not None and pd.notna(min_date) else None,
        "date_max": max_date.date().isoformat() if max_date is not None and pd.notna(max_date) else None,
        "candidate_holdings_coverage": candidate_cov,
        "selected_symbols_coverage": selected_cov,
        "drawdown_symbols_coverage": drawdown_cov,
        "duplicates": dup_count,
        "null_factor_count": null_factor_count,
        "non_positive_factor_count": non_positive_count,
        "abnormal_factor_jump_count": factor_jumps,
        "has_source": has_source,
        "has_provider": has_provider,
        "authoritative_file": file_set.get("authoritative_file"),
        "metadata_file_set_status": file_set.get("file_set_status"),
        "alternate_adjustment_factor_files": file_set.get("alternate_files", []),
        "issues": issues,
        "formal_or_partial_classification": "partial_formal_metadata",
        "read_mode": "chunked_utf8_sig",
    }
