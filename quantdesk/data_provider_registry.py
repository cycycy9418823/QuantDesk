from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from quantdesk.a_stock_data_matrix import (
    RESULT_PATH as A_STOCK_PROVIDER_MATRIX_RESULT_PATH,
)
from quantdesk.a_stock_data_matrix import (
    build_provider_matrix_payload,
)
from quantdesk.version import API_SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = PROJECT_ROOT / "results" / "data_provider_status_latest.json"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    type: str
    enabled: bool
    network_required: bool
    default_for_workflow: bool
    description: str
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    implemented: bool = True
    placeholder: bool = False
    local_path: str = ""
    next_step: str = ""


def _provider_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec(
            name="local_existing",
            type="daily_price_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=True,
            description="Read existing data/daily/{symbol}.csv files.",
            local_path="data/daily",
            next_step="If stale, run the existing daily update workflow from a POST action.",
        ),
        ProviderSpec(
            name="current_project_default",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Use QuantDesk's current update_market_data.py Eastmoney/Sina daily fallback updater.",
            local_path="scripts/update_market_data.py",
            next_step="Use only when the user explicitly runs an update.",
        ),
        ProviderSpec(
            name="akshare_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional AKShare daily-price provider.",
            dependencies=("akshare",),
            next_step="Install akshare or keep using local daily cache.",
        ),
        ProviderSpec(
            name="ashare_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional Ashare get_price daily adapter inspired by mpquant/Ashare.",
            dependencies=("Ashare",),
            next_step="Place or install Ashare only if the user chooses this provider.",
        ),
        ProviderSpec(
            name="mootdx_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional mootdx daily/quote adapter inspired by a-stock-data.",
            dependencies=("mootdx",),
            next_step="Install mootdx only if this optional provider is needed.",
        ),
        ProviderSpec(
            name="baostock_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="BaoStock raw daily bars with explicit suspension-status evidence.",
            dependencies=("baostock",),
            implemented=True,
            next_step="Use only with explicit network authorization; suspended dates are evidence, not price bars.",
        ),
        ProviderSpec(
            name="tencent_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Tencent Finance daily HTTP adapter.",
            implemented=True,
            next_step="默认不联网；用户显式触发联网补日线时可作为 mootdx 之后、AKShare 之前的 fallback。",
        ),
        ProviderSpec(
            name="sina_daily",
            type="daily_price_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Sina daily raw-price fallback via AKShare stock_zh_a_daily.",
            implemented=True,
            next_step="Use only after explicit network authorization and after primary daily providers fail.",
        ),
        ProviderSpec(
            name="local_existing",
            type="intraday_1m_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=True,
            description="Read existing data/intraday/1m/{symbol}.csv files.",
            local_path="data/intraday/1m",
            next_step="If missing, user may trigger the intraday provider fallback from POST/CLI.",
        ),
        ProviderSpec(
            name="local_intraday_1m",
            type="intraday_1m_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=True,
            description="Read local data/intraday/1m/{symbol}.csv files.",
            local_path="data/intraday/1m",
            next_step="Missing minute data still means no_fill; do not fake fills.",
        ),
        ProviderSpec(
            name="akshare_intraday",
            type="intraday_1m_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional AKShare 1-minute intraday adapter for current scoped symbols.",
            dependencies=("akshare",),
            next_step="Use only when the user explicitly triggers POST/CLI intraday update.",
        ),
        ProviderSpec(
            name="ashare_intraday",
            type="intraday_1m_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional Ashare minute-line adapter inspired by get_price(frequency='1m').",
            dependencies=("Ashare",),
            next_step="Use only after dependency and network checks pass.",
        ),
        ProviderSpec(
            name="mootdx_intraday",
            type="intraday_1m_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Optional mootdx quote/minute adapter placeholder inspired by a-stock-data.",
            dependencies=("mootdx",),
            next_step="Use only after dependency and network checks pass.",
        ),
        ProviderSpec(
            name="tencent_intraday",
            type="intraday_1m_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Tencent HTTP current-day intraday adapter for scoped symbols.",
            next_step="If unavailable or date scope does not match, continue to the next provider.",
        ),
        ProviderSpec(
            name="sina_intraday",
            type="intraday_1m_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Sina HTTP current-day intraday adapter for scoped symbols.",
            next_step="If unavailable or date scope does not match, missing data remains no_fill.",
        ),
        ProviderSpec(
            name="local",
            type="theme_map_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=True,
            description="Read local theme map, spot snapshot, and low-price pool fields.",
            local_path="data/universe/stock_theme_map.csv",
            next_step="Generate import draft when local fields are still unknown.",
        ),
        ProviderSpec(
            name="import",
            type="theme_map_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=False,
            description="Import data/universe/stock_theme_map_import.csv.",
            local_path="data/universe/stock_theme_map_import.csv",
            next_step="Use the import draft generator before importing.",
        ),
        ProviderSpec(
            name="akshare_em",
            type="theme_map_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="AKShare Eastmoney industry and concept-board provider.",
            dependencies=("akshare",),
            next_step="Retry later or fall back to import draft if RemoteDisconnected occurs.",
        ),
        ProviderSpec(
            name="akshare_ths",
            type="theme_map_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="AKShare THS industry and concept provider when available.",
            dependencies=("akshare",),
            next_step="Continue to per-symbol and seed providers when unavailable.",
        ),
        ProviderSpec(
            name="per_symbol_info",
            type="theme_map_provider",
            enabled=True,
            network_required=True,
            default_for_workflow=False,
            description="Per-symbol industry fallback for low-price included symbols.",
            dependencies=("akshare",),
            next_step="Use as partial fallback; it may not return concepts.",
        ),
        ProviderSpec(
            name="baidu_stock_concept",
            type="theme_map_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Baidu stock concept provider placeholder from the reference-data plan.",
            implemented=False,
            next_step="Adapter not implemented yet; show as planned / placeholder.",
        ),
        ProviderSpec(
            name="ths_hotspot",
            type="theme_map_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="THS hotspot/theme attribution placeholder.",
            implemented=False,
            next_step="Adapter not implemented yet; show as planned / placeholder.",
        ),
        ProviderSpec(
            name="eastmoney_concept",
            type="theme_map_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Eastmoney concept-board placeholder.",
            implemented=False,
            next_step="Adapter not implemented yet; show as planned / placeholder.",
        ),
        ProviderSpec(
            name="seed",
            type="theme_map_provider",
            enabled=True,
            network_required=False,
            default_for_workflow=False,
            description="Small local seed map for current observation samples.",
            local_path="quantdesk/theme_map_seed.py",
            next_step="Use only for known samples; unknown symbols remain pending_manual.",
        ),
        ProviderSpec(
            name="ths_hotspot",
            type="market_hotspot_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="THS hot-stock and reason-tag provider placeholder.",
            implemented=False,
            next_step="Keep as diagnostic-only until implemented.",
        ),
        ProviderSpec(
            name="baidu_stock_concept",
            type="market_hotspot_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Baidu concept heat provider placeholder.",
            implemented=False,
            next_step="Keep as diagnostic-only until implemented.",
        ),
        ProviderSpec(
            name="eastmoney_hotspot",
            type="market_hotspot_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Eastmoney market-hotspot placeholder.",
            implemented=False,
            next_step="Keep as diagnostic-only until implemented.",
        ),
        ProviderSpec(
            name="lhb_provider",
            type="risk_event_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Dragon Tiger Board risk-event provider placeholder.",
            implemented=False,
            next_step="Future adapter can add event warnings without changing strategies.",
        ),
        ProviderSpec(
            name="unlock_provider",
            type="risk_event_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Lockup-expiry risk-event provider placeholder.",
            implemented=False,
            next_step="Future adapter can add risk notes; no trading action is triggered.",
        ),
        ProviderSpec(
            name="announcement_news_provider",
            type="risk_event_provider",
            enabled=False,
            network_required=True,
            default_for_workflow=False,
            description="Announcement and news risk-event provider placeholder.",
            implemented=False,
            next_step="Future adapter can flag missing news or announcements.",
        ),
    ]


def dependency_import_names(dependency: str) -> tuple[str, ...]:
    mapping = {
        "Ashare": ("Ashare", "ashare"),
        "akshare": ("akshare",),
        "mootdx": ("mootdx",),
    }
    return mapping.get(dependency, (dependency,))


def dependency_available(dependency: str) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in dependency_import_names(dependency))


def dependency_status(dependencies: Iterable[str]) -> list[dict[str, object]]:
    rows = []
    for dep in dependencies:
        installed = dependency_available(dep)
        rows.append(
            {
                "name": dep,
                "installed": installed,
                "status": "available" if installed else "unavailable",
                "import_names": list(dependency_import_names(dep)),
            }
        )
    return rows


def _local_path_status(spec: ProviderSpec) -> tuple[str, str]:
    if not spec.local_path:
        return "unknown", ""
    path = PROJECT_ROOT / spec.local_path
    if path.is_dir():
        csv_count = len(list(path.glob("*.csv")))
        detail = f"{spec.local_path} exists; csv_count={csv_count}"
        return ("available" if csv_count > 0 or spec.name == "local_intraday_1m" else "unknown"), detail
    if path.is_file():
        return "available", f"{spec.local_path} exists"
    return "unavailable", f"{spec.local_path} is missing"


def _provider_status(spec: ProviderSpec, *, check_network: bool = False) -> dict[str, object]:
    row = asdict(spec)
    row["dependencies"] = list(spec.dependencies)
    row["dependency_status"] = dependency_status(spec.dependencies)
    row["checked_network"] = bool(check_network and spec.network_required)
    row["last_error"] = ""
    row["status"] = "unknown"
    if not spec.implemented:
        row["placeholder"] = True
        row["status"] = "planned"
        row["planned"] = True
        row["last_error"] = ""
        row["diagnostic_note"] = "planned provider placeholder; no network request is made"
        return row
    missing = [dep for dep in spec.dependencies if not dependency_available(dep)]
    if missing:
        row["status"] = "unavailable"
        row["last_error"] = "missing optional dependency: " + ", ".join(missing)
        return row
    local_status, local_detail = _local_path_status(spec)
    if local_detail:
        row["local_detail"] = local_detail
    if spec.local_path and local_status in {"available", "unavailable"}:
        row["status"] = local_status
        if local_status == "unavailable":
            row["last_error"] = local_detail
        return row
    if spec.network_required:
        row["status"] = "available" if not check_network else "unknown"
        if check_network:
            row["last_error"] = "network smoke probe is not implemented for this adapter; dependency check passed"
        return row
    row["status"] = local_status if local_status != "unknown" else "available"
    return row


def list_data_providers(*, check_network: bool = False) -> list[dict[str, object]]:
    return [_provider_status(spec, check_network=check_network) for spec in _provider_specs()]


def summarize_providers(providers: list[dict[str, object]]) -> dict[str, object]:
    counts: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    dependencies: dict[str, bool] = {}
    placeholder_count = 0
    for provider in providers:
        status = str(provider.get("status") or "unknown")
        ptype = str(provider.get("type") or "unknown")
        if provider.get("placeholder") or status == "planned":
            placeholder_count += 1
        counts[status] = counts.get(status, 0) + 1
        by_type.setdefault(ptype, {})
        by_type[ptype][status] = by_type[ptype].get(status, 0) + 1
        for dep in provider.get("dependency_status") or []:
            if isinstance(dep, dict):
                dependencies[str(dep.get("name"))] = bool(dep.get("installed"))
    return {"counts": counts, "by_type": by_type, "dependencies": dependencies, "placeholder_count": placeholder_count}


def data_provider_status_payload(*, check_network: bool = False) -> dict[str, object]:
    providers = list_data_providers(check_network=check_network)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "check_network": bool(check_network),
        "network": bool(check_network),
        "read_only": not check_network,
        "path": str(RESULT_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "providers": providers,
        "summary": summarize_providers(providers),
        "disclaimer": "数据源诊断仅用于本地可用性检查；不接券商、不真实下单、不构成投资建议。",
    }
    return payload


def save_data_provider_status(*, check_network: bool = False) -> dict[str, object]:
    payload = data_provider_status_payload(check_network=check_network)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def a_stock_data_provider_matrix_payload(
    *,
    categories: list[str] | None = None,
    symbols: list[str] | None = None,
    target_date: str = "",
    network: bool = False,
    write: bool = False,
) -> dict[str, object]:
    """Return the v3.0.0 A-share provider matrix.

    ``network`` only marks whether a user requested network-capability checks.
    The matrix itself never performs broker actions, never scans the full
    market, and does not fabricate data when providers are unavailable.
    """

    return build_provider_matrix_payload(
        categories=categories,
        symbols=symbols,
        target_date=target_date,
        network=network,
        write=write,
        output_path=A_STOCK_PROVIDER_MATRIX_RESULT_PATH,
    )


def save_a_stock_data_provider_matrix(
    *,
    categories: list[str] | None = None,
    symbols: list[str] | None = None,
    target_date: str = "",
    network: bool = False,
) -> dict[str, object]:
    return a_stock_data_provider_matrix_payload(
        categories=categories,
        symbols=symbols,
        target_date=target_date,
        network=network,
        write=True,
    )
