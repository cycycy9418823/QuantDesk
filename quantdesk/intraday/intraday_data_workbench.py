"""Real intraday data workbench for current QuantDesk scope.

This module orchestrates existing local-only checks, optional provider fetch,
CSV import, quality audit, and conditional execution. It never scans the whole
market, never connects to brokers, and never turns daily bars into fake fills.
"""

from __future__ import annotations

# Compatibility markers: run_intraday_data_workbench provider_summary next_actions.
from pathlib import Path
from typing import Any

from quantdesk.intraday.intraday_gap_checker import intraday_gap_status
from quantdesk.intraday.intraday_import import import_intraday_1m_file
from quantdesk.intraday.intraday_provider_capability import check_intraday_provider_capability
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.paper.auto_paper_portfolio import AUTO_PAPER_PORTFOLIO_STATE_JSON
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
INTRADAY_DATA_WORKBENCH_LATEST_JSON = RESULTS_DIR / "intraday_data_workbench_latest.json"

DISCLAIMER = "仅用于本地纸面模拟，不接券商、不真实下单、不保存真实账户信息、不构成投资建议。"
NO_FAKE_NOTE = "系统不会用日线收盘价、最高价或最低价假装盘中成交。"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _as_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _symbols_from_gap(payload: dict[str, Any]) -> list[str]:
    return [normalize_symbol(row.get("symbol")) for row in payload.get("items") or [] if normalize_symbol(row.get("symbol"))]


def _quality_counts(audit_payload: dict[str, Any]) -> dict[str, int]:
    summary = audit_payload.get("summary") if isinstance(audit_payload.get("summary"), dict) else {}
    return {
        "quality_usable": int(summary.get("usable") or 0),
        "quality_partial": int(summary.get("partial") or 0),
        "quality_invalid": int(summary.get("invalid") or 0),
        "quality_insufficient": int(summary.get("insufficient") or 0),
        "quality_missing": int(summary.get("missing") or 0),
    }


def _provider_summary(provider_payload: dict[str, Any] | None, update_payload: dict[str, Any] | None) -> dict[str, Any]:
    provider_results = []
    if provider_payload:
        provider_results.extend(provider_payload.get("provider_results") or [])
    if update_payload:
        provider_results.extend(update_payload.get("provider_results") or [])
    failed_or_skipped = [
        row for row in provider_results
        if str(row.get("status") or "") in {"failed", "skipped", "not_available"}
    ]
    ok_rows = [row for row in provider_results if str(row.get("status") or "") == "ok"]
    formal_rows = [
        row for row in ok_rows
        if row.get("formal_execution_eligible") is True
    ]
    reasons = [
        row.get("failed_reason") or row.get("skipped_reason") or row.get("suggested_next_action")
        for row in failed_or_skipped
        if row.get("failed_reason") or row.get("skipped_reason") or row.get("suggested_next_action")
    ]
    main_reason = "当前 provider 未能获取目标历史分钟线。" if failed_or_skipped and not ok_rows else "provider 状态已检查。"
    if reasons:
        main_reason = short_error_text("; ".join(map(str, reasons)), 300)
    return {
        "all_failed_or_skipped": bool(provider_results) and not ok_rows,
        "main_reason": main_reason,
        "suggested_next_action": (
            "请导入目标日期的原始 1 分钟 OHLCV CSV，并重新运行严格审计。"
            if not ok_rows
            else (
                "已有严格完整会话审计证据；仅可进入 Paper Account 后续复核。"
                if formal_rows
                else "not formal: provider 仅返回了数据，仍须通过 raw 与严格完整会话审计；不得据此执行条件计划。"
            )
        ),
        "provider_results": provider_results,
    }


def _write_report(payload: dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"intraday_data_workbench_report_{now_text().replace('-', '').replace(':', '').replace(' ', '_')}.md"
    summary = payload.get("summary") or {}
    provider_summary = payload.get("provider_summary") or {}
    lines = [
        "# 真实分钟线工作台报告",
        "",
        f"- 目标交易日：{payload.get('target_date') or '-'}",
        f"- 处理范围：{payload.get('source') or '-'}",
        f"- 需要分钟线股票数：{summary.get('required_symbols', 0)}",
        f"- 已有可用分钟线：{summary.get('available_after', 0)}",
        f"- 缺少分钟线：{summary.get('missing_after', 0)}",
        f"- 可可靠执行：{summary.get('quality_usable', 0)}",
        f"- 部分可用：{summary.get('quality_partial', 0)}",
        f"- 无效：{summary.get('quality_invalid', 0)}",
        f"- provider 主要原因：{provider_summary.get('main_reason') or '-'}",
        "",
        "## 条件执行结果",
        "",
        f"- 触发事件：{summary.get('execution_event_count', 0)}",
        f"- 未成交/未触发数量：{summary.get('no_fill_count', 0)}",
        f"- 缺分钟线次数：{summary.get('missing_intraday_count', 0)}",
        "",
        "## 下一步建议",
        "",
        *[f"- {item}" for item in payload.get("next_actions") or []],
        "",
        "## 风险提示",
        "",
        "- 本工作台仅用于本地纸面模拟。",
        "- 不接券商。",
        "- 不真实下单。",
        "- 不保存真实账户信息。",
        "- 不构成投资建议。",
        f"- {NO_FAKE_NOTE}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return _display_path(path)


def run_intraday_data_workbench(
    *,
    target_date: str,
    source: str = "current",
    symbols: list[str] | None = None,
    try_provider: bool = False,
    provider_order: list[str] | None = None,
    import_path: str | None = None,
    validate_only: bool = False,
    audit_only: bool = False,
    network: bool = False,
    execute_after_ready: bool = True,
    update_portfolio: bool = False,
    format: str = "auto",
    encoding: str = "auto",
    dry_run: bool = False,
    output_path: Path | None = None,
) -> dict[str, Any]:
    output_path = Path(output_path or INTRADAY_DATA_WORKBENCH_LATEST_JSON)
    symbol_list = [normalize_symbol(item) for item in (symbols or []) if normalize_symbol(item)]
    actual_source = "symbols" if symbol_list else source
    gap_before = intraday_gap_status(target_trade_date=target_date, source=actual_source, symbols=symbol_list, write=False)
    actual_target_date = str(target_date or gap_before.get("target_trade_date") or "")[:10]
    required_symbols = _symbols_from_gap(gap_before)
    provider_payload: dict[str, Any] | None = None
    provider_update_payload: dict[str, Any] | None = None
    import_payload: dict[str, Any] | None = None
    execution_payload: dict[str, Any] | None = None
    portfolio_update: dict[str, Any] = {"skipped": True, "reason": "update_portfolio=false"}
    warnings: list[str] = []
    errors: list[str] = []

    if try_provider:
        order = provider_order or DEFAULT_INTRADAY_PROVIDER_ORDER
        provider_payload = check_intraday_provider_capability(
            symbols=required_symbols or symbol_list,
            dates=actual_target_date,
            provider_order=order,
            network=bool(network),
            write=not dry_run,
        )
        provider_update_payload = update_intraday_1m_for_symbols(
            symbols=required_symbols or symbol_list,
            target_date=actual_target_date,
            provider_order=order,
            source=actual_source,
            dry_run=dry_run,
            network=bool(network),
            execute_after_update=False,
            update_portfolio_after_execution=False,
        )

    if import_path:
        import_payload = import_intraday_1m_file(
            import_path,
            target_date=actual_target_date,
            source="manual_import",
            append=True,
            overwrite=False,
            validate_only=bool(validate_only or dry_run),
            encoding=encoding,
            input_format=format,
        )
        if not import_payload.get("ok"):
            errors.extend(import_payload.get("errors") or [import_payload.get("message") or "导入失败"])

    gap_after = intraday_gap_status(target_trade_date=actual_target_date, source=actual_source, symbols=symbol_list, write=not dry_run)
    audit_symbols = _symbols_from_gap(gap_after) or required_symbols or symbol_list
    audit_payload = audit_intraday_batch(audit_symbols, actual_target_date, write=not dry_run, encoding=encoding)
    quality = _quality_counts(audit_payload)

    can_execute_count = sum(1 for item in audit_payload.get("items") or [] if item.get("can_execute_condition_plan"))
    if execute_after_ready and not audit_only and can_execute_count > 0:
        execution_payload = simulate_conditional_plan_file(write=not dry_run)
        if update_portfolio and execution_payload.get("ok"):
            state = read_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, {})
            applied = apply_conditional_execution_to_state(state if isinstance(state, dict) else {}, execution_payload)
            portfolio_update = {
                "ok": bool(applied.get("ok")),
                "skipped": False,
                "action_count": len(applied.get("actions") or []),
                "already_updated_today": bool(applied.get("already_updated_today")),
            }
            if applied.get("state") and not dry_run:
                write_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, applied["state"])
        elif update_portfolio:
            portfolio_update = {"skipped": True, "reason": "execution failed or unavailable"}

    exec_summary = execution_payload.get("summary") if isinstance(execution_payload, dict) else {}
    provider_summary = _provider_summary(provider_payload, provider_update_payload)
    next_actions = []
    if provider_summary.get("all_failed_or_skipped"):
        next_actions.append(f"provider 未能获取 {actual_target_date} 历史分钟线。")
    if gap_after.get("missing_count", 0):
        next_actions.append("请点击“生成导入模板”，按模板导入真实 1 分钟线 CSV。")
    if import_path and not errors:
        next_actions.append("导入后已完成质量审计；可继续执行条件交易计划。")
    if not can_execute_count:
        next_actions.append("当前没有通过审计的分钟线，暂不能可靠执行条件计划。")
    else:
        next_actions.append("已有分钟线通过审计，可执行条件交易计划。")
    next_actions.append(NO_FAKE_NOTE)

    summary = {
        "required_symbols": int(gap_before.get("total_symbols") or len(required_symbols)),
        "available_before": int(gap_before.get("available_count") or 0),
        "missing_before": int(gap_before.get("missing_count") or 0),
        "provider_updated": int((provider_update_payload or {}).get("updated_symbols") or 0),
        "imported_symbols": int((import_payload or {}).get("imported_symbols") or 0),
        "available_after": int(gap_after.get("available_count") or 0),
        "missing_after": int(gap_after.get("missing_count") or 0),
        **quality,
        "execution_event_count": int((exec_summary or {}).get("event_count") or 0),
        "no_fill_count": int((exec_summary or {}).get("no_fill_count") or 0),
        "missing_intraday_count": int((exec_summary or {}).get("missing_intraday_count") or gap_after.get("missing_count") or 0),
    }

    payload = {
        "ok": not errors,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "intraday_data_workbench",
        "generated_at": now_text(),
        "target_date": actual_target_date,
        "source": actual_source,
        "symbols": audit_symbols,
        "network": bool(network and try_provider),
        "try_provider": bool(try_provider),
        "full_scan": False,
        "is_real_order": False,
        "dry_run": bool(dry_run),
        "import_path": import_path or "",
        "validate_only": bool(validate_only),
        "audit_only": bool(audit_only),
        "execute_after_ready": bool(execute_after_ready),
        "update_portfolio": bool(update_portfolio),
        "summary": summary,
        "gap_before": gap_before,
        "gap_after": gap_after,
        "provider_summary": provider_summary,
        "provider_capability": provider_payload or {},
        "provider_update": provider_update_payload or {},
        "import_result": import_payload or {},
        "quality_audit": audit_payload,
        "conditional_execution_summary": exec_summary or {},
        "portfolio_update": portfolio_update,
        "next_actions": next_actions,
        "warnings": warnings,
        "errors": errors,
        "disclaimer": DISCLAIMER,
        "no_fake_note": NO_FAKE_NOTE,
    }
    report_path = _write_report(payload) if not dry_run else ""
    payload["artifacts"] = {
        "latest_json": _display_path(output_path),
        "report_path": report_path,
    }
    if not dry_run:
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_intraday_data_workbench(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or INTRADAY_DATA_WORKBENCH_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {"ok": True, "exists": False, "path": _display_path(path), "message": "尚未运行真实分钟线工作台。", "disclaimer": DISCLAIMER}
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload


def intraday_data_workbench_report_html() -> str:
    import html

    latest = latest_intraday_data_workbench()
    report_path = Path(PROJECT_ROOT / str((latest.get("artifacts") or {}).get("report_path") or ""))
    if report_path.exists():
        body = report_path.read_text(encoding="utf-8", errors="replace")
    else:
        body = "尚未生成真实分钟线工作台报告。"
    escaped = html.escape(body)
    return f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>真实分钟线工作台报告</title><style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:980px;margin:2em auto;line-height:1.6;padding:0 1em}}pre{{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:1em;border-radius:8px}}</style></head><body><button onclick=\"window.print()\">打印 / 另存为 PDF</button><pre>{escaped}</pre></body></html>"
