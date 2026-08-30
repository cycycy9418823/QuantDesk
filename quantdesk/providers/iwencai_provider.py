"""Optional iwencai provider placeholder.

No credentials are embedded or logged. The provider is disabled unless the
user configures a local secrets file outside clean exports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE
from quantdesk.shared import read_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = PROJECT_ROOT / "config" / "provider_secrets.json"
PROVIDER = "iwencai"


def _has_api_key() -> bool:
    payload = read_json(SECRETS_PATH, {}) or {}
    key = str(payload.get("iwencai_api_key") or "").strip()
    return bool(key)


def capability(*, network: bool = False) -> dict[str, Any]:
    configured = _has_api_key()
    return {
        "provider": PROVIDER,
        "status": "available" if configured else "not_configured",
        "implemented": False,
        "placeholder": True,
        "requires_api_key": True,
        "api_key_configured": configured,
        "checked_network": bool(network and configured),
        "supports_historical_day": True,
        "supports_current_day": True,
        "default_enabled": False,
        "suggested_next_action": "如需使用 iwencai，请在本地配置 API Key；不要把 API Key 写入代码、日志或 clean zip。",
        "disclaimer": RISK_NOTE,
    }


def fetch_research_reports(symbol: str = "", start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    if not _has_api_key():
        return {
            "ok": False,
            "provider": PROVIDER,
            "data": [],
            "status": "not_configured",
            "implemented": False,
            "placeholder": True,
            "failed_reason": "未配置 iwencai API Key",
            "suggested_next_action": "如需使用 iwencai，请在本地配置 config/provider_secrets.json；默认流程不会强制启用。",
            "disclaimer": RISK_NOTE,
        }
    if not network:
        return {
            "ok": False,
            "provider": PROVIDER,
            "data": [],
            "status": "skipped",
            "implemented": False,
            "placeholder": True,
            "failed_reason": "network=false，未请求 iwencai",
            "disclaimer": RISK_NOTE,
        }
    return {
        "ok": False,
        "provider": PROVIDER,
        "data": [],
        "status": "planned",
        "implemented": False,
        "placeholder": True,
        "failed_reason": "v3.0.0 仅提供配置检查和占位，不调用外部凭据接口。",
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "disclaimer": RISK_NOTE,
    }
