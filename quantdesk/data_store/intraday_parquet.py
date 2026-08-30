from __future__ import annotations

from pathlib import Path


def is_parquet_available() -> bool:
    try:
        import pandas  # noqa: F401
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


def build_intraday_parquet_path(symbol: str, date_or_month: str, period: str, root: str | Path = "data/intraday_parquet", partition_mode: str = "symbol_month") -> Path:
    month = str(date_or_month)[:7]
    base = Path(root) / month
    if partition_mode == "market_month":
        market = "cn"
        return base / f"{market}_{period}.parquet"
    return base / f"{symbol}_{period}.parquet"


def write_intraday_parquet(rows: list[dict], symbol: str, period: str, month: str, config: dict | None = None) -> dict:
    cfg = config or {}
    path = build_intraday_parquet_path(symbol, month, period, cfg.get("root", "data/intraday_parquet"), cfg.get("partition_mode", "symbol_month"))
    if not is_parquet_available():
        return {"ok": True, "status": "warning", "warnings": ["pyarrow_or_pandas_unavailable"], "storage_path": str(path)}
    import pandas as pd
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return {"ok": True, "status": "passed", "row_count": len(rows), "storage_path": str(path)}


def read_intraday_parquet(symbol: str, period: str, start_datetime: str | None = None, end_datetime: str | None = None, config: dict | None = None) -> tuple[list[dict], list[str]]:
    cfg = config or {}
    if not is_parquet_available():
        return [], ["pyarrow_or_pandas_unavailable"]
    return [], ["month_required_for_direct_read"]


def update_intraday_manifest(db, manifest_row: dict) -> dict:
    return db.upsert_many("intraday_bars_manifest", [manifest_row], ["symbol", "period", "month", "storage_path"])
