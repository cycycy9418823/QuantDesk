from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT, base_payload


MODES = {"raw": "raw", "forward": "forward", "forward_adjusted": "forward", "backward": "backward", "back": "backward", "back_adjusted": "backward", "default": "forward", "": "forward"}


def normalize_price_adjustment_mode(mode: str | None) -> str:
    return MODES.get(str(mode or "forward").strip().lower(), "forward")


def resolve_daily_price_dir(base_data_dir: str | Path, adjustment: str) -> Path:
    base = Path(base_data_dir)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    mode = normalize_price_adjustment_mode(adjustment)
    if mode == "raw":
        return base / "daily" / "raw"
    if mode == "backward":
        return base / "daily" / "back_adjusted"
    return base / "daily" / "forward_adjusted"


def load_daily_prices(symbol: str, adjustment: str = "forward", config: dict | None = None) -> dict:
    cfg = dict(config or {})
    directory = resolve_daily_price_dir(cfg.get("base_data_dir") or "data", adjustment)
    path = directory / f"{symbol}.csv"
    warnings = []
    rows: list[dict[str, Any]] = []
    if not path.exists():
        warnings.append("daily_price_file_missing")
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    return {"symbol": symbol, "price_adjustment": normalize_price_adjustment_mode(adjustment), "path": str(path), "rows": rows, "warnings": warnings}


def validate_adjusted_price_layout(base_data_dir: str | Path) -> dict:
    base = Path(base_data_dir)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    required = {
        "raw": base / "daily" / "raw",
        "forward": base / "daily" / "forward_adjusted",
        "backward": base / "daily" / "back_adjusted",
        "corporate_actions": base / "corporate_actions",
    }
    missing = [key for key, path in required.items() if not path.exists()]
    return {"ok": not missing, "status": "passed" if not missing else "warning", "paths": {k: str(v) for k, v in required.items()}, "missing": missing}


def build_adjustment_policy_payload(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    mode = normalize_price_adjustment_mode(cfg.get("adjustment") or "forward")
    layout = validate_adjusted_price_layout(cfg.get("base_data_dir") or "data")
    payload = base_payload("price_adjustment_policy", status=layout["status"], warnings=[f"missing_{item}" for item in layout["missing"]])
    payload.update({"price_adjustment": mode, "layout": layout})
    return payload
