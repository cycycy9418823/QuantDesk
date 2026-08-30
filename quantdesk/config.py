from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"

BUILTIN_DEFAULT_CONFIG: dict[str, Any] = {
    "backtest": {
        "initial_cash": 100000,
        "commission_rate": 0.00025,
        "min_commission": 5,
        "stamp_duty_rate": 0.0005,
        "slippage_bps": 5,
        "lot_size": 100,
        "max_positions": 5,
        "max_position_pct": 0.2,
        "max_turnover_per_rebalance": None,
        "max_industry_pct": None,
        "industry_map_csv": None,
        "rebalance": "weekly",
        "execution_price": "next_open",
        "signal_timing": "close",
        "limit_up_pct": 9.8,
        "limit_down_pct": -9.8,
        "backtest_mode": "portfolio",
    },
    "report": {
        "top_n": 20,
        "decision_card_top_n": 5,
        "max_card_bullets": 3,
        "include_learning_appendix": True,
        "learning_appendix_max_items": 20,
    },
    "paper": {
        # Default paper-account seed for NEW ledgers only. The live legacy
        # 10k ledger keeps its own recorded initial_cash (capital-profile
        # resolution reads existing state first) and is labeled legacy_10k.
        "initial_cash": 100000,
    },
    "risk": {
        "warning": "Historical backtests do not guarantee future returns.",
    },
    "calendar": {
        "trading_calendar_path": "data/calendar/a_share_trading_calendar.csv",
    },
    "data": {
        "daily_cache_dir": "data/daily",
        "update_status_path": "data/cache/update_status.json",
        "fallback_symbols": ["600000", "000001", "600519", "300750"],
    },
}


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        if any(ch in value for ch in [".", "e", "E"]):
            return float(value)
        return int(value)
    except ValueError:
        return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    current_section: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            raise ValueError(f"invalid config line {line_number}: {raw_line}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if indent == 0:
            if value:
                parsed[key] = _parse_scalar(value)
                current_section = None
            else:
                parsed[key] = {}
                current_section = key
            continue
        if current_section is None:
            raise ValueError(f"nested key without section at line {line_number}: {raw_line}")
        if not isinstance(parsed.get(current_section), dict):
            raise ValueError(f"section {current_section} is not a mapping")
        parsed[current_section][key] = _parse_scalar(value)
    return parsed


def _read_yaml_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        return _parse_simple_yaml(text)
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError("top-level config must be a mapping")
    return loaded


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load QuantDesk configuration with built-in defaults as fallback.

    The default path is ``config/default.yaml``. Missing files return the
    built-in conservative defaults; malformed files raise a clear RuntimeError
    so a bad config does not silently change backtest assumptions.
    """

    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        return deepcopy(BUILTIN_DEFAULT_CONFIG)
    try:
        loaded = _read_yaml_config(config_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to load config from {config_path}: {exc}") from exc
    return _merge_dicts(BUILTIN_DEFAULT_CONFIG, loaded)
