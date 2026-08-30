"""Structured A-share data-provider capability matrix for QuantDesk.

The matrix is diagnostic metadata. It does not connect to brokers, place
orders, scan the whole market, or treat a failed free data source as success.
"""

from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from quantdesk.shared import now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = PROJECT_ROOT / "results" / "a_stock_data_provider_matrix_latest.json"

RISK_NOTE = "仅用于本地学习研究，不接券商、不真实下单、不构成投资建议。"

CATEGORY_LABELS = {
    "market_quote": "行情数据",
    "daily_kline": "日线 K 线",
    "intraday_kline": "1 分钟线",
    "valuation": "估值 / 市值",
    "fundamentals": "财报 / F10",
    "announcement": "公告",
    "research_report": "研报数据",
    "hot_theme": "热点题材",
    "news": "新闻资讯",
    "concept_map": "板块 / 概念",
    "risk_event": "风险事件",
}


@dataclass(frozen=True)
class AStockProviderSpec:
    provider: str
    provider_id: str
    display_name: str
    category: str
    priority: int
    enabled_by_default: bool
    default_enabled: bool
    requires_network: bool
    network_required: bool
    requires_dependency: bool
    dependency_name: str
    requires_api_key: bool
    api_key_required: bool
    supports_realtime: bool
    supports_daily: bool
    supports_intraday: bool
    supports_current_day: bool
    supports_historical_day: bool
    supports_historical: bool
    supports_full_market: bool
    supports_batch: bool
    supports_single_symbol: bool
    default_scope: str
    status: str
    capability_status: str
    rate_limit_note: str
    terms_note: str
    fallback_next_action: str
    suggested_next_action: str
    risk_note: str = RISK_NOTE


def dependency_available(name: str) -> bool:
    if not name:
        return True
    aliases = {
        "Ashare": ("Ashare", "ashare"),
        "akshare": ("akshare",),
        "mootdx": ("mootdx",),
    }.get(name, (name,))
    return any(importlib.util.find_spec(item) is not None for item in aliases)


def _spec(
    provider: str,
    category: str,
    priority: int,
    *,
    display_name: str | None = None,
    dependency: str = "",
    enabled: bool = True,
    status: str = "available_if_installed",
    network: bool = True,
    api_key: bool = False,
    realtime: bool = False,
    daily: bool = False,
    intraday: bool = False,
    current_day: bool = False,
    historical_day: bool = False,
    full_market: bool = False,
    batch: bool = False,
    single: bool = True,
    scope: str = "current_symbols_only",
    rate_limit_note: str = "免费数据源可能限流、延迟或字段变化。",
    terms_note: str = "请遵守对应数据源网站条款；QuantDesk 不绕过限制。",
    next_action: str = "如果不可用，请尝试下一 provider 或使用本地 CSV 导入。",
) -> AStockProviderSpec:
    return AStockProviderSpec(
        provider=provider,
        provider_id=f"{provider}_{category}",
        display_name=display_name or provider,
        category=category,
        priority=priority,
        enabled_by_default=enabled,
        default_enabled=enabled,
        requires_network=network,
        network_required=network,
        requires_dependency=bool(dependency),
        dependency_name=dependency,
        requires_api_key=api_key,
        api_key_required=api_key,
        supports_realtime=realtime,
        supports_daily=daily,
        supports_intraday=intraday,
        supports_current_day=current_day,
        supports_historical_day=historical_day,
        supports_historical=historical_day,
        supports_full_market=full_market,
        supports_batch=batch,
        supports_single_symbol=single,
        default_scope=scope,
        status=status,
        capability_status=status,
        rate_limit_note=rate_limit_note,
        terms_note=terms_note,
        fallback_next_action=next_action,
        suggested_next_action=next_action,
    )


def provider_specs() -> list[AStockProviderSpec]:
    disabled_note = "默认禁用，不作为核心 provider；保留未来扩展或兼容诊断。"
    return [
        _spec("mootdx", "market_quote", 1, display_name="mootdx 行情", dependency="mootdx", realtime=True, current_day=True, daily=True, intraday=True, historical_day=True, next_action="安装 mootdx 后可作为行情与基础数据优先 provider。"),
        _spec("tencent", "market_quote", 2, display_name="腾讯财经行情", realtime=True, current_day=True, daily=True, next_action="无需 SDK，网络可用时作为轻量行情 fallback。"),
        _spec("local_existing", "daily_kline", 0, display_name="本地日线缓存", network=False, daily=True, historical_day=True, batch=True, next_action="优先读取 data/daily；如果滞后，再由用户触发联网补全。"),
        _spec("mootdx", "daily_kline", 1, display_name="mootdx 日线", dependency="mootdx", daily=True, historical_day=True, next_action="安装 mootdx 后可补强日线 K 线。"),
        _spec("tencent", "daily_kline", 2, display_name="腾讯财经日线", daily=True, historical_day=True, next_action="腾讯接口字段不稳定时记录 failed_reason，并继续 fallback。"),
        _spec("akshare", "daily_kline", 3, display_name="AKShare 日线补充", dependency="akshare", daily=True, historical_day=True, next_action="AKShare 保留为补充 provider，不作为唯一行情源。"),
        _spec("local_existing", "intraday_kline", 0, display_name="本地 1 分钟线", network=False, intraday=True, current_day=True, historical_day=True, next_action="优先使用本地 data/intraday/1m；不把日线当分钟线。"),
        _spec("mootdx", "intraday_kline", 1, display_name="mootdx 分钟线", dependency="mootdx", intraday=True, current_day=True, historical_day=True, next_action="安装 mootdx 后优先尝试当前范围分钟线。"),
        _spec("tencent", "intraday_kline", 2, display_name="腾讯财经分钟线", intraday=True, current_day=True, historical_day=False, next_action="通常更适合当日分钟线；历史日期失败时建议 CSV 导入。"),
        _spec("akshare", "intraday_kline", 3, display_name="AKShare 分钟线补充", dependency="akshare", intraday=True, current_day=True, historical_day=True, next_action="AKShare 请求失败时记录原因，不假装成交。"),
        _spec("tencent", "valuation", 1, display_name="腾讯估值 / 市值", realtime=True, current_day=True, next_action="用于 PE/PB/市值等补充字段，失败不影响主流程。"),
        _spec("mootdx", "valuation", 2, display_name="mootdx 估值补充", dependency="mootdx", current_day=True, historical_day=True, next_action="依赖可用后作为基础数据补充。"),
        _spec("akshare", "fundamentals", 1, display_name="AKShare 基础数据", dependency="akshare", historical_day=True, next_action="用于财务/F10 补充，不作为交易建议。"),
        _spec("mootdx", "fundamentals", 2, display_name="mootdx F10 / 财务", dependency="mootdx", historical_day=True, next_action="mootdx 可用时作为基础数据补充。"),
        _spec("cninfo_via_akshare", "announcement", 1, display_name="巨潮公告（AKShare 封装）", dependency="akshare", historical_day=True, next_action="低频刷新公告，用于风险提示。"),
        _spec("mootdx", "announcement", 2, display_name="mootdx 公告补充", dependency="mootdx", historical_day=True, next_action="若可用可作为公告补充来源。"),
        _spec("akshare", "research_report", 1, display_name="AKShare 研报", dependency="akshare", historical_day=True, next_action="研报仅用于学习参考，不作为买卖建议。"),
        _spec("iwencai", "research_report", 2, display_name="iwencai 研报（可选）", enabled=False, status="planned", api_key=True, historical_day=True, next_action="如需使用 iwencai，请在本地配置 API Key；不要写入仓库。"),
        _spec("ths_hotspot", "hot_theme", 1, display_name="同花顺热点", enabled=False, status="planned", historical_day=True, current_day=True, next_action="热点数据仅用于题材学习和归因观察，不构成投资建议。"),
        _spec("cls_news", "news", 1, display_name="财联社新闻", enabled=False, status="planned", current_day=True, historical_day=False, next_action="只在用户点击刷新资讯或 POST 时请求。"),
        _spec("eastmoney_news", "news", 2, display_name="东方财富资讯", enabled=False, status="planned", current_day=True, historical_day=True, next_action="用于新闻阅读辅助，不参与默认交易决策。"),
        _spec("akshare_theme", "concept_map", 1, display_name="AKShare 板块/概念", dependency="akshare", historical_day=True, next_action="板块题材补充来源，失败后使用导入草稿。"),
        _spec("ths_hotspot", "concept_map", 2, display_name="同花顺概念热点", enabled=False, status="planned", historical_day=True, current_day=True, next_action="用于题材归因观察，不输出买卖建议。"),
        _spec("cninfo_via_akshare", "risk_event", 1, display_name="巨潮风险事件", dependency="akshare", historical_day=True, next_action="用于公告与风险事件提示，不触发真实交易。"),
        _spec("tushare", "daily_kline", 99, display_name="Tushare（默认禁用）", enabled=False, status="deprecated_or_disabled", api_key=True, daily=True, historical_day=True, next_action="积分墙限制、免费数据不足，不作为默认 provider。"),
        _spec("ashare", "daily_kline", 98, display_name="Ashare（默认禁用）", dependency="Ashare", enabled=False, status="deprecated_or_disabled", daily=True, historical_day=True, next_action=disabled_note),
        _spec("ashare", "intraday_kline", 98, display_name="Ashare 分钟线（默认禁用）", dependency="Ashare", enabled=False, status="deprecated_or_disabled", intraday=True, current_day=True, historical_day=True, next_action=disabled_note),
    ]


def provider_to_row(spec: AStockProviderSpec, *, check_network: bool = False) -> dict[str, Any]:
    row = asdict(spec)
    dep_ok = dependency_available(spec.dependency_name)
    row["dependency_available"] = dep_ok
    row["checked_network"] = bool(check_network and spec.requires_network)
    row["category_label"] = CATEGORY_LABELS.get(spec.category, spec.category)
    row["network"] = bool(spec.requires_network and check_network)
    row["is_real_order"] = False
    row["full_scan"] = False
    if spec.provider == "mootdx":
        row["implemented"] = True
        row["api_key_required"] = False
        row["requires_api_key"] = False
        row["failed_reason"] = ""
        row["skipped_reason"] = ""
        row["suggested_next_action"] = "mootdx 是可选 provider；未安装时可继续使用腾讯 provider。"
        if not dep_ok:
            row["status"] = "not_available"
            row["capability_status"] = "dependency_missing"
            row["failed_reason"] = "未安装 mootdx"
            row["skipped_reason"] = "未安装 mootdx"
            row["suggested_next_action"] = "未安装 mootdx，可继续使用腾讯 provider；如需启用 mootdx，请按 docs/provider_setup.md 安装。"
            return row
        if not check_network:
            row["status"] = "skipped"
            row["capability_status"] = "available_not_checked"
            row["skipped_reason"] = "默认未联网检测。"
            return row
        try:
            from quantdesk.providers.mootdx_provider import capability

            probe = capability(network=True)
            row.update({
                "status": probe.get("status", "failed"),
                "capability_status": probe.get("status", "failed"),
                "row_count": probe.get("row_count", 0),
                "failed_reason": probe.get("failed_reason", ""),
                "skipped_reason": probe.get("skipped_reason", ""),
                "suggested_next_action": probe.get("suggested_next_action", row["suggested_next_action"]),
                "warnings": probe.get("warnings", []),
            })
            return row
        except Exception as exc:  # noqa: BLE001
            row["status"] = "failed"
            row["capability_status"] = "failed"
            row["failed_reason"] = str(exc)
            row["suggested_next_action"] = "检查 mootdx 安装、网络和服务器状态；失败时可继续使用腾讯 provider。"
            return row
    if spec.provider == "tencent":
        row["implemented"] = True
        row["api_key_required"] = False
        row["requires_api_key"] = False
        row["failed_reason"] = ""
        row["skipped_reason"] = ""
        row["suggested_next_action"] = "如需真实检查，请使用 --network true 或在 UI 中点击联网检查。"
        if not check_network:
            row["status"] = "skipped"
            row["capability_status"] = "implemented_not_checked"
            row["skipped_reason"] = "默认未联网检测。"
            return row
        try:
            from quantdesk.providers.tencent_provider import capability

            probe = capability(network=True)
            row.update({
                "status": probe.get("status", "failed"),
                "capability_status": probe.get("status", "failed"),
                "row_count": probe.get("row_count", 0),
                "latest_date": probe.get("latest_date", ""),
                "fields": probe.get("fields", []),
                "failed_reason": probe.get("failed_reason", ""),
                "warnings": probe.get("warnings", []),
            })
            if row["status"] == "failed":
                row["suggested_next_action"] = "检查网络、接口变化，或改用本地 CSV / mootdx / AKShare。"
            return row
        except Exception as exc:  # noqa: BLE001
            row["status"] = "failed"
            row["capability_status"] = "failed"
            row["failed_reason"] = str(exc)
            row["suggested_next_action"] = "检查网络、接口变化，或改用本地 CSV / mootdx / AKShare。"
            return row
    if spec.status in {"deprecated_or_disabled", "not_configured", "disabled", "not_implemented", "planned"}:
        row["status"] = spec.status
        if spec.status == "planned":
            row["implemented"] = False
            row["placeholder"] = True
        row["failed_reason"] = ""
    elif spec.requires_dependency and not dep_ok:
        row["status"] = "not_available"
        row["failed_reason"] = f"未安装依赖 {spec.dependency_name}"
    elif spec.requires_api_key:
        row["status"] = "not_configured"
        row["failed_reason"] = "未配置 API Key"
    elif check_network and spec.requires_network:
        row["status"] = "not_checked_network_adapter"
        row["failed_reason"] = "v3.0.0 默认只做能力矩阵；真实请求由专门 POST/CLI adapter 执行。"
    else:
        row["status"] = "available" if spec.enabled_by_default or not spec.requires_dependency else "available_if_installed"
        row["failed_reason"] = ""
    row.setdefault("failed_reason", "")
    return row


def list_provider_matrix(*, categories: list[str] | None = None, check_network: bool = False) -> list[dict[str, Any]]:
    wanted = {item.strip() for item in categories or [] if item.strip()}
    rows = [provider_to_row(spec, check_network=check_network) for spec in provider_specs()]
    if wanted:
        rows = [row for row in rows if row.get("category") in wanted]
    return sorted(rows, key=lambda row: (str(row.get("category")), int(row.get("priority") or 0), str(row.get("provider"))))


def providers_by_category(category: str, *, check_network: bool = False) -> list[dict[str, Any]]:
    return list_provider_matrix(categories=[category], check_network=check_network)


def summarize_provider_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    for row in rows:
        category = str(row.get("category") or "unknown")
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        bucket = by_category.setdefault(category, {"count": 0, "providers": [], "status_counts": {}})
        bucket["count"] += 1
        bucket["providers"].append(row.get("provider"))
        bucket["status_counts"][status] = bucket["status_counts"].get(status, 0) + 1
    return {
        "total": len(rows),
        "status_counts": status_counts,
        "by_category": by_category,
        "category_labels": CATEGORY_LABELS,
    }


def build_provider_matrix_payload(
    *,
    categories: list[str] | None = None,
    symbols: list[str] | None = None,
    target_date: str = "",
    network: bool = False,
    write: bool = False,
    output_path: Path | None = None,
) -> dict[str, Any]:
    rows = list_provider_matrix(categories=categories, check_network=network)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "a_stock_data_provider_matrix",
        "generated_at": now_text(),
        "network": bool(network),
        "full_scan": False,
        "is_real_order": False,
        "default_scope": "current_symbols_only",
        "symbols": symbols or [],
        "target_date": target_date,
        "providers": rows,
        "summary": summarize_provider_matrix(rows),
        "disclaimer": RISK_NOTE,
    }
    if write:
        target = output_path or RESULT_PATH
        write_json(target, payload)
        payload["path"] = str(Path(target).relative_to(PROJECT_ROOT)).replace("\\", "/")
    return payload
