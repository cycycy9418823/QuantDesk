from __future__ import annotations

from pathlib import Path

from quantdesk.data_store.intraday_parquet import read_intraday_parquet

from .akshare_provider import fetch_intraday_min_em


def load_intraday(symbol: str, period: str = "1m", network: bool = False, config: dict | None = None) -> dict:
    cfg = config or {}
    rows, warnings = read_intraday_parquet(symbol, period, config=cfg)
    if rows:
        return {"ok": True, "status": "passed", "source": "local_parquet", "rows": rows, "warnings": warnings, "data_limitations": []}
    csv_path = Path(cfg.get("csv_path") or f"data/intraday/1m/{symbol}.csv")
    if csv_path.exists():
        return {"ok": True, "status": "passed", "source": "local_csv", "rows": [], "warnings": ["local_csv_intraday_available_not_loaded_in_summary"], "data_limitations": []}
    if not network:
        return {"ok": True, "status": "warning", "source": "none", "rows": [], "warnings": warnings + ["network_false_intraday_provider_skipped"], "data_limitations": ["No full-market historical minute guarantee in v3.7.0."]}
    payload = fetch_intraday_min_em(symbol, network=network)
    return {**payload, "data_limitations": ["Provider minute history is limited and may vary by interface."]}
