"""Local 1-minute intraday CSV import helpers.

This module only works with user-provided local CSV files. It does not
download intraday data and it never simulates real orders.
"""

from __future__ import annotations

# Compatibility markers: audit_after_import execute_after_import.
import csv
import os
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.canonical_write_guard import (
    ensure_path_outside_canonical,
    path_is_within,
)
from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch
from quantdesk.intraday.intraday_schema import (
    STANDARD_COLUMNS,
    SUPPORTED_ENCODINGS,
    normalize_intraday_frame,
    read_intraday_csv,
)
from quantdesk.intraday.minute_bar_lifecycle import LIFECYCLE_COLUMNS
from quantdesk.paper.position_management_engine import simulate_conditional_plan_file
from quantdesk.shared import normalize_symbol, now_text, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"
INTRADAY_IMPORT_DIR = PROJECT_ROOT / "data" / "intraday" / "import"
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "intraday_1m_template.csv"
IMPORT_TEMPLATE_PATH = INTRADAY_IMPORT_DIR / "intraday_1m_import_template.csv"
INTRADAY_IMPORT_LATEST_JSON = PROJECT_ROOT / "results" / "intraday_import_latest.json"
REQUIRED_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "amount"]
DISCLAIMER = "仅用于本地 1 分钟线导入和纸面模拟，不接券商、不真实下单、不构成投资建议。"


def intraday_1m_path(symbol: str, base_dir: Path | None = None) -> Path:
    return (base_dir or INTRADAY_1M_DIR) / f"{normalize_symbol(symbol)}.csv"


def create_intraday_template(path: Path | None = None) -> Path:
    target = ensure_intraday_template_target_is_noncanonical(path or TEMPLATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "datetime": "2026-05-13 09:31:00",
                "open": "3.21",
                "high": "3.24",
                "low": "3.20",
                "close": "3.23",
                "volume": "10000",
                "amount": "32300",
            }
        )
    return target


def normalize_intraday_1m_frame(df: pd.DataFrame, *, symbol: str = "", target_date: str = "") -> pd.DataFrame:
    payload = normalize_intraday_frame(df, default_symbol=symbol, target_date=target_date)
    frame = payload.get("frame")
    if isinstance(frame, pd.DataFrame):
        legacy_columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        return frame[[col for col in legacy_columns if col in frame.columns]].copy()
    return pd.DataFrame(columns=REQUIRED_COLUMNS)


def check_intraday_1m(symbol: str, base_dir: Path | None = None, *, target_date: str = "") -> dict[str, Any]:
    path = intraday_1m_path(symbol, base_dir)
    payload = read_intraday_csv(path, default_symbol=symbol, target_date=target_date)
    frame = payload.get("frame")
    row_count = int(len(frame)) if isinstance(frame, pd.DataFrame) else 0
    ok = bool(payload.get("ok")) and row_count > 0
    return {
        "ok": ok,
        "exists": bool(payload.get("exists")),
        "symbol": normalize_symbol(symbol),
        "path": str(path),
        "row_count": row_count,
        "warnings": payload.get("warnings") or [],
        "errors": payload.get("errors") or [],
        "required_columns": REQUIRED_COLUMNS,
        "message": "分钟线可读。" if ok else "缺少盘中数据或无法读取目标分钟线。",
        "disclaimer": DISCLAIMER,
    }


def _read_input_csv(path: Path, *, encoding: str = "auto") -> tuple[pd.DataFrame | None, list[str], str]:
    if not path.exists():
        return None, [f"输入文件不存在：{path}"], ""
    encodings = SUPPORTED_ENCODINGS if not encoding or encoding == "auto" else (encoding,)
    errors: list[str] = []
    for enc in encodings:
        frame = read_csv_frame(path, encoding=enc, dtype=str, keep_default_na=False)
        warnings = list(frame.attrs.get("warnings") or [])
        if any(item.startswith("csv_read_failed:") for item in warnings):
            errors.extend(warnings)
            continue
        return frame, [], enc
    return None, [f"输入 CSV 不可读：{'; '.join(errors)}"], ""


def _merge_intraday(target_path: Path, incoming: pd.DataFrame, *, overwrite: bool, append: bool) -> pd.DataFrame:
    if path_is_within(target_path.parent, INTRADAY_1M_DIR):
        raise PermissionError("canonical_intraday_direct_write_forbidden")
    if target_path.exists() and append and not overwrite:
        existing_payload = read_intraday_csv(target_path, default_symbol=str(incoming["symbol"].iloc[0]) if not incoming.empty else "")
        existing = existing_payload.get("frame")
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            incoming = pd.concat([existing, incoming], ignore_index=True)
    incoming = incoming.sort_values("datetime").drop_duplicates(subset=["symbol", "datetime"], keep="last")
    return incoming[STANDARD_COLUMNS].reset_index(drop=True)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def ensure_intraday_template_target_is_noncanonical(path: Path) -> Path:
    """Reject template output anywhere inside the canonical 1m directory."""

    target = Path(path)
    if path_is_within(target, INTRADAY_1M_DIR):
        raise ValueError("canonical_intraday_direct_write_forbidden")
    return target


def import_intraday_1m_file(
    input_path: str | Path,
    *,
    target_date: str = "",
    symbol: str = "",
    source: str = "manual_import",
    append: bool = True,
    overwrite: bool = False,
    validate_only: bool = False,
    base_dir: Path | None = None,
    output_path: Path | None = None,
    encoding: str = "auto",
    input_format: str = "auto",
    audit_after_import: bool = False,
    execute_after_import: bool = False,
    update_portfolio_after_execution: bool = False,
) -> dict[str, Any]:
    output_path = Path(output_path or INTRADAY_IMPORT_LATEST_JSON)
    ensure_path_outside_canonical(
        output_path,
        INTRADAY_1M_DIR,
        role="manual_import_output",
    )
    target_dir = Path(base_dir or INTRADAY_1M_DIR)
    if path_is_within(target_dir, INTRADAY_1M_DIR) and not _same_path(
        target_dir, INTRADAY_1M_DIR
    ):
        raise ValueError("canonical_intraday_direct_write_forbidden:manual_import_target")
    path = Path(input_path)
    raw, read_errors, used_encoding = _read_input_csv(path, encoding=encoding)
    result: dict[str, Any] = {
        "ok": False,
        "input_path": str(path),
        "validate_only": bool(validate_only),
        "append": bool(append),
        "overwrite": bool(overwrite),
        "target_date": target_date,
        "format": input_format,
        "encoding": used_encoding or encoding,
        "imported_symbols": 0,
        "imported_rows": 0,
        "updated_files": [],
        "invalid_rows": [],
        "warnings": [],
        "errors": read_errors,
        "disclaimer": DISCLAIMER,
    }
    canonical_apply = bool(
        not validate_only and _same_path(target_dir, INTRADAY_1M_DIR)
    )
    if canonical_apply and (overwrite or not append):
        result.update(
            {
                "ok": False,
                "reason_codes": ["canonical_replace_semantics_forbidden"],
                "message": (
                    "canonical 分钟数据只允许 append-only 审计导入；"
                    "overwrite/append=false 请求被 fail-close。"
                ),
            }
        )
        write_json(output_path, result)
        return result
    if raw is None:
        write_json(output_path, result)
        return result
    result["warnings"].extend(list(raw.attrs.get("warnings") or []))
    normalized = normalize_intraday_frame(raw, default_symbol=symbol, source=source, target_date=target_date)
    frame = normalized.get("frame")
    result["warnings"].extend(normalized.get("warnings") or [])
    result["errors"].extend(normalized.get("errors") or [])
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        if validate_only:
            result["warnings"].extend(result["errors"])
            result["errors"] = []
            result["ok"] = True
        else:
            result["ok"] = False
        result["message"] = "输入文件没有可导入的分钟线记录；如果这是模板文件，请填入真实 1 分钟线后再导入。"
        write_json(output_path, result)
        return result
    if result["errors"]:
        result["message"] = "输入 CSV 字段不完整，未导入。"
        write_json(output_path, result)
        return result
    if bool(normalized.get("price_only_intraday")):
        result.update(
            {
                "ok": False,
                "reason_codes": ["price_only_intraday"],
                "message": (
                    "输入仅含 price/close 合成 OHLC，已在 staging 写入前 fail-close；"
                    "请提供原始完整分钟 OHLCV。"
                ),
            }
        )
        write_json(output_path, result)
        return result
    if validate_only:
        result.update({
            "ok": True,
            "imported_symbols": int(frame["symbol"].nunique()),
            "imported_rows": int(len(frame)),
            "message": "validate-only 通过；未写入 data/intraday/1m。",
        })
        write_json(output_path, result)
        return result

    write_frame = frame
    if "evidence_hash" in raw.columns:
        # Normalization returns one current row per minute. Keep the original
        # validated chain for writes, including every superseded observation.
        write_frame = raw.copy()
        stamps = pd.to_datetime(write_frame["datetime"], errors="coerce")
        if target_date:
            write_frame = write_frame.loc[stamps.dt.strftime("%Y-%m-%d") == target_date[:10]].copy()
        write_frame["date"] = stamps.loc[write_frame.index].dt.strftime("%Y-%m-%d")
        write_frame["time"] = stamps.loc[write_frame.index].dt.strftime("%H:%M:%S")
        for column in STANDARD_COLUMNS:
            if column not in write_frame.columns:
                # Only compatibility labels may be supplied at import. Missing
                # signed values stay empty so their evidence hash is unchanged.
                write_frame[column] = source if column == "source" else now_text() if column == "updated_at" else ""

    if canonical_apply:
        observed_dates = sorted(
            {
                str(item)[:10]
                for item in frame.get("date", pd.Series(dtype=str)).astype(str).tolist()
                if str(item).strip()
            }
        )
        resolved_target_date = str(target_date or "")[:10]
        if not resolved_target_date and len(observed_dates) == 1:
            resolved_target_date = observed_dates[0]
        if not resolved_target_date or observed_dates != [resolved_target_date]:
            result.update(
                {
                    "ok": False,
                    "reason_codes": ["canonical_import_requires_single_target_date"],
                    "message": "canonical 审计导入一次只接受一个明确目标交易日。",
                }
            )
            write_json(output_path, result)
            return result
        run_id = f"manual-intraday-{uuid.uuid4().hex}"
        staging_dir = (
            PROJECT_ROOT
            / "data"
            / "vendor_staging"
            / "manual_intraday_imports"
            / run_id
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        symbols = [
            normalize_symbol(item)
            for item in frame["symbol"].dropna().astype(str).unique().tolist()
            if normalize_symbol(item)
        ]
        for sym, group in write_frame.groupby("symbol", sort=True):
            normalized = normalize_symbol(sym)
            if normalized:
                columns = STANDARD_COLUMNS + [column for column in LIFECYCLE_COLUMNS if column in group.columns]
                staged_group = group[columns].copy()
                staged_group["price_only_intraday"] = False
                staged_group.to_csv(
                    staging_dir / f"{normalized}.csv",
                    index=False,
                    encoding="utf-8-sig",
                )
        quality = audit_intraday_batch(
            symbols,
            resolved_target_date,
            intraday_dir=staging_dir,
            output_path=staging_dir / "quality_audit.json",
            write=True,
            run_id=run_id,
        )
        canonical_import = apply_audited_intraday_batch(
            staging_dir=staging_dir,
            canonical_dir=target_dir,
            symbols=symbols,
            target_date=resolved_target_date,
            run_id=run_id,
            quality_audit=quality,
            output_path=staging_dir / "canonical_import.json",
            apply=True,
        )
        result.update(
            {
                "target_date": resolved_target_date,
                "staging_dir": str(staging_dir),
                "quality_audit": quality,
                "canonical_import": canonical_import,
                "reason_codes": list(canonical_import.get("reason_codes") or []),
                "ok": canonical_import.get("status") == "completed",
                "imported_symbols": int(canonical_import.get("appended_symbol_count") or 0),
                "imported_rows": int(len(frame))
                if canonical_import.get("status") == "completed"
                else 0,
                "message": (
                    "分钟数据已经过 staging→质量审计→append-only importer。"
                    if canonical_import.get("status") == "completed"
                    else "分钟数据留在 staging；质量审计或 canonical 冲突门未通过。"
                ),
            }
        )
        if execute_after_import and result["ok"]:
            execution = simulate_conditional_plan_file(write=True)
            result["execution_summary"] = execution.get("summary") or {}
        write_json(output_path, result)
        return result

    target_dir.mkdir(parents=True, exist_ok=True)
    updated_files: list[str] = []
    imported_rows = 0
    for sym, group in write_frame.groupby("symbol", sort=True):
        norm = normalize_symbol(sym)
        if not norm:
            continue
        target_path = target_dir / f"{norm}.csv"
        if "evidence_hash" in group.columns:
            from quantdesk.intraday.intraday_update_engine import _merge_and_write
            target_path = _merge_and_write(norm, group.copy(), overwrite=overwrite,
                                           append=append, base_dir=target_dir)
        else:
            merged = _merge_intraday(target_path, group.copy(), overwrite=overwrite, append=append)
            merged.to_csv(target_path, index=False, encoding="utf-8-sig")
        updated_files.append(str(target_path.relative_to(PROJECT_ROOT)) if target_path.is_absolute() and PROJECT_ROOT in target_path.parents else str(target_path))
        imported_rows += int(len(group))
    result.update({
        "ok": True,
        "imported_symbols": len(updated_files),
        "imported_rows": imported_rows,
        "updated_files": updated_files,
        "message": "本地 1 分钟线已导入；系统仅用于纸面模拟，不会真实下单。",
    })
    imported_symbols = [normalize_symbol(sym) for sym in frame["symbol"].dropna().astype(str).unique().tolist()]
    if audit_after_import:
        audit = audit_intraday_batch(imported_symbols, target_date, intraday_dir=target_dir, write=True, encoding=encoding)
        result["quality_summary"] = audit.get("summary") or {}
        result["quality_audit"] = audit
    if execute_after_import:
        execution = simulate_conditional_plan_file(write=True)
        result["execution_summary"] = execution.get("summary") or {}
        result["conditional_execution"] = {
            "ok": bool(execution.get("ok")),
            "trade_date": execution.get("trade_date") or "",
            "plan_id": execution.get("plan_id") or "",
        }
        result["update_portfolio_after_execution"] = bool(update_portfolio_after_execution)
    result["next_actions"] = [
        "导入后请先查看质量审计结果。",
        "审计通过后可执行条件交易计划。",
        "系统不会用日线收盘价、最高价或最低价假装盘中成交。",
    ]
    write_json(output_path, result)
    return result


def import_intraday_1m(symbol: str, source: str | Path, base_dir: Path | None = None) -> dict[str, Any]:
    payload = import_intraday_1m_file(source, symbol=symbol, base_dir=base_dir, source="manual_import")
    status = check_intraday_1m(symbol, base_dir)
    return {**status, "source": str(source), "import_result": payload, "imported_at": now_text()}
