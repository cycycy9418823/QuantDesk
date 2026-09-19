"""Update local 1-minute intraday cache for current QuantDesk scope.

This module is user-triggered only. It can try external data providers, but it
never scans the whole market, never connects to brokers, and never places real
orders.
"""

from __future__ import annotations

# Compatibility markers: update_intraday_1m_for_symbols provider_results full_scan tencent_intraday.
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.intraday.canonical_write_guard import (
    ensure_path_outside_canonical,
    path_is_within,
)

from quantdesk.intraday.intraday_gap_checker import collect_intraday_required_symbols
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER, fetch_intraday_1m
from quantdesk.intraday.intraday_quality_audit import (
    assess_complete_session,
    audit_intraday_batch,
)
from quantdesk.intraday.intraday_schema import STANDARD_COLUMNS, normalize_intraday_frame, read_intraday_csv
from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.auto_paper_portfolio import AUTO_PAPER_PORTFOLIO_STATE_JSON
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"
RESULTS_DIR = PROJECT_ROOT / "results"
INTRADAY_UPDATE_LATEST_JSON = RESULTS_DIR / "intraday_update_latest.json"
STAGING_PRICE_ONLY_PROVENANCE_COLUMN = "price_only_intraday"

DISCLAIMER = "仅用于本地纸面模拟，不接券商、不真实下单、不构成投资建议。"
# v3.1.0 provider order includes tencent_intraday through DEFAULT_INTRADAY_PROVIDER_ORDER.


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _provider_summary(order: list[str]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "provider": name,
            "status": "skipped",
            "attempted": 0,
            "updated": 0,
            "failed": 0,
            "skipped": 0,
            "attempted_symbols": [],
            "updated_symbol_list": [],
            "failed_symbol_list": [],
            "failed_reason": "",
            "skipped_reason": "",
            "suggested_next_action": "",
            "warnings": [],
        }
        for name in order
    }


def _append_failure(row: dict[str, Any], reason: str) -> None:
    text = short_error_text(reason)
    if not text:
        return
    current = str(row.get("failed_reason") or "")
    if text not in current:
        row["failed_reason"] = "; ".join([item for item in [current, text] if item])


def _set_non_formal_next_action(row: dict[str, Any], reason_code: str) -> None:
    row["suggested_next_action"] = (
        f"not formal ({reason_code})：未通过 raw 完整分钟会话门；"
        "请补抓完整数据或改用其他 provider，不得据此执行条件计划。"
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def _secure_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if value:
        return value
    return f"intraday-provider-{uuid.uuid4().hex}"


def _merge_and_write_unlocked(
    symbol: str,
    frame: pd.DataFrame,
    *,
    append: bool,
    overwrite: bool,
    base_dir: Path,
    replace_target_date: str = "",
) -> Path:
    target = base_dir / f"{normalize_symbol(symbol)}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_columns = list(STANDARD_COLUMNS)
    if STAGING_PRICE_ONLY_PROVENANCE_COLUMN in frame.columns:
        write_columns.append(STAGING_PRICE_ONLY_PROVENANCE_COLUMN)
    incoming = frame[write_columns].copy()
    if target.exists() and append and not overwrite:
        existing_payload = read_intraday_csv(target, default_symbol=symbol)
        existing = existing_payload.get("frame")
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            if replace_target_date:
                existing_dates = pd.to_datetime(
                    existing.get("datetime"),
                    errors="coerce",
                ).dt.strftime("%Y-%m-%d")
                existing = existing.loc[
                    existing_dates != str(replace_target_date)[:10]
                ].copy()
            incoming = pd.concat([existing, incoming], ignore_index=True)
    if STAGING_PRICE_ONLY_PROVENANCE_COLUMN in write_columns:
        incoming[STAGING_PRICE_ONLY_PROVENANCE_COLUMN] = incoming[
            STAGING_PRICE_ONLY_PROVENANCE_COLUMN
        ].fillna(False)
    incoming = incoming.sort_values("datetime").drop_duplicates(subset=["symbol", "datetime"], keep="last")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            incoming[write_columns].to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return target


def _merge_and_write(
    symbol: str,
    frame: pd.DataFrame,
    *,
    append: bool,
    overwrite: bool,
    base_dir: Path,
    replace_target_date: str = "",
) -> Path:
    if path_is_within(Path(base_dir), INTRADAY_1M_DIR):
        # Stage 6 task-book sections 14.3 and 16: provider fetchers may only
        # materialize staging evidence. Canonical mutation belongs exclusively
        # to the audited append-only importer.
        raise PermissionError("canonical_intraday_direct_write_forbidden")
    normalized = normalize_symbol(symbol)
    lock_path = Path(base_dir) / ".locks" / f"{normalized}.lock"
    with interprocess_file_lock(lock_path):
        return _merge_and_write_unlocked(
            normalized,
            frame,
            append=append,
            overwrite=overwrite,
            base_dir=base_dir,
            replace_target_date=replace_target_date,
        )


def _resolve_symbols(
    symbols: list[str] | None,
    *,
    source: str,
    target_date: str,
    max_symbols: int,
) -> tuple[list[dict[str, Any]], str]:
    explicit = [normalize_symbol(item) for item in symbols or [] if normalize_symbol(item)]
    collect_source = "symbols" if explicit else source
    collected = collect_intraday_required_symbols(source=collect_source, target_trade_date=target_date, symbols=explicit)
    rows = list(collected.get("items") or [])
    if max_symbols and max_symbols > 0:
        rows = rows[:max_symbols]
    return rows, str(target_date or collected.get("target_trade_date") or "")[:10]


def update_intraday_1m_for_symbols(
    symbols: list[str] | None = None,
    target_date: str = "",
    *,
    provider_order: list[str] | None = None,
    source: str = "current",
    overwrite: bool = False,
    append: bool = True,
    dry_run: bool = False,
    max_symbols: int = 0,
    network: bool = False,
    execute_after_update: bool = False,
    update_portfolio_after_execution: bool = False,
    intraday_dir: Path | None = None,
    existing_intraday_dir: Path | None = None,
    materialize_local_existing: bool = False,
    output_path: Path | None = None,
    run_id: str = "",
    require_raw_price_basis: bool = False,
    require_complete_session: bool = False,
) -> dict[str, Any]:
    requested_intraday_dir = Path(intraday_dir or INTRADAY_1M_DIR)
    canonical_dir = Path(INTRADAY_1M_DIR)
    if path_is_within(requested_intraday_dir, canonical_dir) and not _same_path(
        requested_intraday_dir, canonical_dir
    ):
        raise ValueError(
            "canonical_intraday_direct_write_forbidden:provider_update_target"
        )
    canonical_apply = bool(
        not dry_run and _same_path(requested_intraday_dir, canonical_dir)
    )
    effective_run_id = _secure_run_id(run_id) if canonical_apply else str(run_id or "")
    secure_run_dir: Path | None = None
    if canonical_apply:
        secure_run_dir = RESULTS_DIR / "intraday_staging" / effective_run_id
        intraday_dir = secure_run_dir / "staging"
        existing_intraday_dir = Path(existing_intraday_dir or canonical_dir)
        materialize_local_existing = True
        require_raw_price_basis = True
        require_complete_session = True
        overwrite = False
        append = True
    else:
        intraday_dir = requested_intraday_dir
        existing_intraday_dir = Path(existing_intraday_dir or intraday_dir)
    output_path = Path(output_path or INTRADAY_UPDATE_LATEST_JSON)
    ensure_path_outside_canonical(
        output_path,
        canonical_dir,
        role="provider_update_output",
    )
    order = [name.strip() for name in (provider_order or DEFAULT_INTRADAY_PROVIDER_ORDER) if name and name.strip()]
    rows, trade_date = _resolve_symbols(symbols, source=source, target_date=target_date, max_symbols=max_symbols)
    provider_rows = _provider_summary(order)
    updated_symbols: list[str] = []
    unchanged_symbols: list[str] = []
    failed_symbols: list[str] = []
    failed_symbol_reasons: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []

    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        symbol_failed: list[str] = []
        symbol_done = False
        for provider in order:
            pstat = provider_rows.setdefault(provider, _provider_summary([provider])[provider])
            pstat["attempted"] += 1
            pstat.setdefault("attempted_symbols", []).append(symbol)
            provider_read_dir = (
                existing_intraday_dir if provider == "local_existing" else intraday_dir
            )
            result = fetch_intraday_1m(provider, symbol, trade_date, intraday_dir=provider_read_dir, network=bool(network and not dry_run or provider == "local_existing"))
            pstat["status"] = "ok" if result.get("status") == "ok" else ("warning" if pstat.get("status") == "ok" else result.get("status") or "failed")
            pstat.setdefault("warnings", []).extend(result.get("warnings") or [])
            if result.get("skipped_reason") and not pstat.get("skipped_reason"):
                pstat["skipped_reason"] = result.get("skipped_reason")
            if result.get("suggested_next_action") and not pstat.get("suggested_next_action"):
                pstat["suggested_next_action"] = result.get("suggested_next_action")
            frame = result.get("data")
            if result.get("status") == "ok" and isinstance(frame, pd.DataFrame) and not frame.empty:
                normalized = normalize_intraday_frame(
                    frame,
                    default_symbol=symbol,
                    source=str(result.get("provider") or provider),
                    target_date=trade_date,
                )
                frame = normalized.get("frame")
                pstat.setdefault("warnings", []).extend(normalized.get("warnings") or [])
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    pstat["failed"] += 1
                    pstat.setdefault("failed_symbol_list", []).append(symbol)
                    reason = "provider 返回数据不覆盖目标交易日"
                    _append_failure(pstat, reason)
                    symbol_failed.append(f"{provider}: {reason}")
                    continue
                effective_price_only = bool(
                    result.get("price_only_intraday")
                ) or bool(normalized.get("price_only_intraday"))
                # Preserve the provider's quality-gate fields so the summary keeps
                # the degrade reason and signal capability even after re-normalizing.
                pstat["signal_capability"] = result.get("signal_capability") or pstat.get("signal_capability", "")
                pstat["volume_semantics"] = result.get("volume_semantics") or pstat.get("volume_semantics", "")
                pstat["ohlcv_complete"] = bool(
                    result.get("ohlcv_complete")
                ) and not effective_price_only
                pstat["price_only_intraday"] = effective_price_only
                pstat["signal_eligible"] = bool(
                    result.get("signal_eligible")
                ) and not effective_price_only
                if "source_time_order_valid" in result:
                    pstat["source_time_order_valid"] = result.get("source_time_order_valid")
                # Fail-close on the quality gate. Every provider result MUST carry a
                # quality gate; a result missing ohlcv_complete or signal_eligible is
                # not trustworthy and must not bypass the gate (reason_code=
                # missing_quality_gate). A formal accept (updated OR unchanged) requires
                # BOTH ohlcv_complete AND signal_eligible to be true. Any failure means:
                # no write to data/intraday/1m, no updated, no unchanged — but the chain
                # continues so later providers still get a chance.
                has_quality_gate = "ohlcv_complete" in result and "signal_eligible" in result
                ohlcv_complete = bool(
                    result.get("ohlcv_complete")
                ) and not effective_price_only
                signal_eligible = bool(
                    result.get("signal_eligible")
                ) and not effective_price_only
                if not has_quality_gate:
                    pstat["degraded"] = pstat.get("degraded", 0) + 1
                    pstat.setdefault("degraded_symbol_list", []).append(symbol)
                    pstat["reason_code"] = "missing_quality_gate"
                    _set_non_formal_next_action(pstat, "missing_quality_gate")
                    reason = (
                        f"{provider} 缺少质量门字段（missing_quality_gate：未返回 ohlcv_complete/"
                        "signal_eligible），fail-close，不计 updated/unchanged，继续尝试后续 provider。"
                    )
                    _append_failure(pstat, reason)
                    symbol_failed.append(f"{provider}: {reason}")
                    continue
                price_basis = str(result.get("price_basis") or "unknown").strip().lower()
                pstat["price_basis"] = price_basis
                if require_raw_price_basis and price_basis != "raw":
                    pstat["degraded"] = pstat.get("degraded", 0) + 1
                    pstat.setdefault("degraded_symbol_list", []).append(symbol)
                    pstat["reason_code"] = "price_basis_not_raw"
                    _set_non_formal_next_action(pstat, "price_basis_not_raw")
                    reason = (
                        f"{provider} 分钟价格口径为 {price_basis or 'unknown'}，"
                        "正式每日链要求显式 raw；未写入，继续尝试后续 provider。"
                    )
                    _append_failure(pstat, reason)
                    symbol_failed.append(f"{provider}: {reason}")
                    continue
                is_degraded = not (ohlcv_complete and signal_eligible)
                if is_degraded:
                    pstat["degraded"] = pstat.get("degraded", 0) + 1
                    pstat.setdefault("degraded_symbol_list", []).append(symbol)
                    if effective_price_only:
                        cause = "仅返回 price-only 价格趋势数据（不含真实分钟 OHLCV）"
                        pstat["reason_code"] = "price_only_intraday"
                    elif not ohlcv_complete:
                        cause = "分钟 OHLCV 不完整（缺列/无效价格或成交量/目标日不符/时间倒序或顺序未知/有效行不足）"
                        pstat["reason_code"] = "incomplete_ohlcv"
                    else:
                        cause = "质量门判定不可用作目标日信号（signal_eligible=false）"
                        pstat["reason_code"] = "signal_ineligible"
                    _set_non_formal_next_action(
                        pstat, str(pstat.get("reason_code") or "quality_gate_failed")
                    )
                    reason = (
                        f"{provider} {cause}；signal_capability="
                        f"{result.get('signal_capability') or 'none'}，ohlcv_complete={ohlcv_complete}，"
                        f"signal_eligible={signal_eligible}；未写入正式分钟线，继续尝试后续 provider。"
                    )
                    _append_failure(pstat, reason)
                    symbol_failed.append(f"{provider}: {reason}")
                    continue
                formal_session = assess_complete_session(
                    frame,
                    symbol=symbol,
                    target_date=trade_date,
                    price_basis=price_basis,
                    price_only_intraday=effective_price_only,
                )
                pstat["formal_session_status"] = formal_session.get("status", "")
                pstat["formal_execution_eligible"] = bool(
                    formal_session.get("formal_execution_eligible")
                )
                pstat["unique_session_minutes"] = int(
                    formal_session.get("unique_session_minutes") or 0
                )
                if (
                    require_complete_session
                    and formal_session.get("formal_execution_eligible") is not True
                ):
                    pstat["degraded"] = pstat.get("degraded", 0) + 1
                    pstat.setdefault("degraded_symbol_list", []).append(symbol)
                    pstat["reason_code"] = "formal_session_ineligible"
                    _set_non_formal_next_action(pstat, "formal_session_ineligible")
                    reason = (
                        f"{provider} 严格分钟会话门未通过；status="
                        f"{formal_session.get('status') or 'unknown'}，"
                        f"unique_session_minutes={formal_session.get('unique_session_minutes') or 0}，"
                        "未写入正式分钟 staging，继续尝试后续 provider。"
                    )
                    _append_failure(pstat, reason)
                    symbol_failed.append(f"{provider}: {reason}")
                    continue
                # Passed the gate (ohlcv_complete AND signal_eligible).
                frame = frame.copy()
                frame[STAGING_PRICE_ONLY_PROVENANCE_COLUMN] = (
                    effective_price_only
                )
                if provider == "local_existing":
                    if materialize_local_existing and not dry_run:
                        # Stage 6 task-book section 14.3: canonical is read-only
                        # here; even accepted local evidence is materialized into
                        # the run staging directory before batch audit/import.
                        _merge_and_write(
                            symbol,
                            frame,
                            append=True,
                            overwrite=False,
                            base_dir=intraday_dir,
                        )
                    unchanged_symbols.append(symbol)
                else:
                    if not dry_run:
                        _merge_and_write(
                            symbol,
                            frame,
                            append=append,
                            overwrite=overwrite,
                            base_dir=intraday_dir,
                            replace_target_date=(
                                trade_date
                                if formal_session.get("formal_execution_eligible")
                                else ""
                            ),
                        )
                    updated_symbols.append(symbol)
                    pstat["updated"] += 1
                    pstat.setdefault("updated_symbol_list", []).append(symbol)
                symbol_done = True
                break
            if result.get("status") in {"skipped", "not_available"}:
                pstat["skipped"] += 1
            else:
                pstat["failed"] += 1
                pstat.setdefault("failed_symbol_list", []).append(symbol)
            reason = result.get("failed_reason") or f"{provider} 未返回目标日期分钟线"
            _append_failure(pstat, reason)
            symbol_failed.append(f"{provider}: {reason}")
        if not symbol_done:
            failed_symbols.append(symbol)
            failed_symbol_reasons[symbol] = short_error_text("; ".join(symbol_failed) or "all providers failed")

    updated_unique = list(dict.fromkeys(updated_symbols))
    unchanged_unique = list(dict.fromkeys(unchanged_symbols))
    failed_unique = list(dict.fromkeys(failed_symbols))
    canonical_import: dict[str, Any] = {
        "status": "not_applicable",
        "applied": False,
        "reason_codes": [],
    }
    staging_quality: dict[str, Any] = {
        "status": "not_applicable",
        "items": [],
    }
    if canonical_apply:
        accepted_for_import = list(
            dict.fromkeys(updated_unique + unchanged_unique)
        )
        if accepted_for_import and secure_run_dir is not None:
            staging_quality = audit_intraday_batch(
                accepted_for_import,
                trade_date,
                intraday_dir=intraday_dir,
                output_path=secure_run_dir / "quality_audit.json",
                write=True,
                run_id=effective_run_id,
            )
            canonical_import = apply_audited_intraday_batch(
                staging_dir=intraday_dir,
                canonical_dir=canonical_dir,
                symbols=accepted_for_import,
                target_date=trade_date,
                run_id=effective_run_id,
                quality_audit=staging_quality,
                output_path=secure_run_dir / "canonical_import.json",
                apply=True,
            )
            if canonical_import.get("status") == "completed":
                appended: list[str] = []
                unchanged: list[str] = []
                for item in canonical_import.get("items") or []:
                    symbol = normalize_symbol(item.get("symbol"))
                    operation = str(item.get("operation") or "")
                    if not symbol:
                        continue
                    if operation.startswith("append_"):
                        appended.append(symbol)
                    elif operation == "unchanged_identical_date":
                        unchanged.append(symbol)
                updated_unique = list(dict.fromkeys(appended))
                unchanged_unique = list(dict.fromkeys(unchanged))
                accounted = set(updated_unique) | set(unchanged_unique)
                for symbol in accepted_for_import:
                    if symbol not in accounted:
                        failed_unique.append(symbol)
                        failed_symbol_reasons[symbol] = (
                            "stage6_append_import_missing_terminal_operation"
                        )
            else:
                import_reasons = ";".join(
                    str(item)
                    for item in canonical_import.get("reason_codes") or []
                ) or "stage6_append_import_blocked"
                for symbol in accepted_for_import:
                    failed_unique.append(symbol)
                    failed_symbol_reasons[symbol] = import_reasons
                updated_unique = []
                unchanged_unique = []
        failed_unique = list(dict.fromkeys(failed_unique))
    status = "ok" if not failed_unique else ("partial" if updated_unique or unchanged_unique else "failed")
    if dry_run and status == "failed":
        status = "dry_run"
    payload: dict[str, Any] = {
        "ok": status in {"ok", "partial", "dry_run"},
        "status": status,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "run_id": effective_run_id,
        "target_date": trade_date,
        "scope": "current_candidates_and_positions" if source == "current" else source,
        "network": bool(network),
        "dry_run": bool(dry_run),
        "existing_intraday_dir": _display_path(existing_intraday_dir),
        "materialize_local_existing": bool(materialize_local_existing),
        "require_raw_price_basis": bool(require_raw_price_basis),
        "require_complete_session": bool(require_complete_session),
        "canonical_write_mode": (
            "staging_quality_audit_append_only_import"
            if canonical_apply
            else "noncanonical_or_staging_only"
        ),
        "staging_dir": _display_path(intraday_dir),
        "staging_quality": staging_quality,
        "canonical_import": canonical_import,
        "full_scan": False,
        "is_real_order": False,
        "total_symbols": len(rows),
        "processed_symbols": [normalize_symbol(row.get("symbol")) for row in rows],
        "updated_symbols": len(updated_unique),
        "unchanged_symbols": len(unchanged_unique),
        "failed_symbols": len(failed_unique),
        "updated_symbol_list": updated_unique,
        "unchanged_symbol_list": unchanged_unique,
        "failed_symbol_list": failed_unique,
        "provider_order": order,
        "provider_results": list(provider_rows.values()),
        "failed_symbol_reasons": failed_symbol_reasons,
        "warnings": warnings,
        "errors": errors,
        "message": "分钟线 provider 补全完成；失败股票仍保持 missing_intraday，不会假装成交。",
        "disclaimer": DISCLAIMER,
    }

    if execute_after_update and (
        not canonical_apply or canonical_import.get("status") == "completed"
    ):
        execution = simulate_conditional_plan_file(write=not dry_run)
        payload["conditional_execution_summary"] = execution.get("summary") or {}
        payload["conditional_execution"] = {
            "ok": bool(execution.get("ok")),
            "plan_id": execution.get("plan_id") or "",
            "trade_date": execution.get("trade_date") or "",
        }
        if update_portfolio_after_execution and execution.get("ok"):
            state = read_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, {})
            applied = apply_conditional_execution_to_state(state if isinstance(state, dict) else {}, execution)
            if applied.get("state") and not dry_run:
                write_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, applied["state"])
            payload["portfolio_update"] = {
                "ok": bool(applied.get("ok")),
                "already_updated_today": bool(applied.get("already_updated_today")),
                "action_count": len(applied.get("actions") or []),
            }
    elif execute_after_update:
        payload["conditional_execution"] = {
            "ok": False,
            "skipped": True,
            "reason": "canonical_import_not_completed",
        }
    if not dry_run:
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_intraday_update_status(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or INTRADAY_UPDATE_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未生成分钟线 provider 补全结果。",
            "disclaimer": DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload
