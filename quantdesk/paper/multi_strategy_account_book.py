from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.config import load_config
from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.shared import now_text
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = "config/multi_strategy_account_book.yaml"
DEFAULT_LATEST_JSON = "results/multi_strategy_account_book_latest.json"
DEFAULT_LATEST_REPORT = "reports/multi_strategy_account_book_latest.md"
DISCLAIMER = "Research-only virtual account book. Not investment advice. No broker connection. No real orders."

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "portfolio_name": "core_research_book",
    "initial_cash": 1000000.0,
    "currency": "CNY",
    "rebalance_mode": "manual",
    "allow_cross_subaccount_cash": False,
    "allow_short": False,
    "research_only": True,
    "position_sources": {},
    "sub_accounts": [
        {
            "name": "long_term",
            "display_name": "Long-term Research",
            "weight": 0.60,
            "strategy_tag": "long_term_value",
            "description": "Long horizon research sleeve",
        },
        {
            "name": "swing",
            "display_name": "Swing Research",
            "weight": 0.30,
            "strategy_tag": "medium_term_swing",
            "description": "Medium horizon research sleeve",
        },
        {
            "name": "tactical",
            "display_name": "Tactical Research",
            "weight": 0.10,
            "strategy_tag": "short_term_tactical",
            "description": "Short horizon research sleeve",
        },
    ],
}


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PyYAML is required for multi-strategy YAML files with sub-account lists") from exc
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise ValueError("top-level YAML must be a mapping")
    return loaded


def load_multi_strategy_config(path: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Load multi-strategy account book config without enabling any broker action."""

    warnings: list[str] = []
    if path:
        target = _project_path(path)
        if not target.exists():
            warnings.append(f"missing_config: {_display_path(target)}; using default multi_strategy_account_book config")
            return dict(DEFAULT_CONFIG), warnings
        try:
            loaded = _read_yaml(target)
        except Exception as exc:  # noqa: BLE001
            invalid = dict(DEFAULT_CONFIG)
            invalid["enabled"] = False
            invalid["_invalid_config"] = True
            warnings.append(f"invalid_config_yaml: {_display_path(target)}: {exc}")
            return invalid, warnings
        config = loaded.get("multi_strategy_account_book") if isinstance(loaded.get("multi_strategy_account_book"), dict) else loaded
        return dict(config), warnings

    target = _project_path(DEFAULT_CONFIG_PATH)
    if target.exists():
        try:
            loaded = _read_yaml(target)
            config = loaded.get("multi_strategy_account_book") if isinstance(loaded.get("multi_strategy_account_book"), dict) else loaded
            return dict(config), warnings
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"invalid_config_yaml: {_display_path(target)}: {exc}; using config/default.yaml fallback")

    try:
        default_yaml = load_config()
        config = _as_dict(default_yaml.get("multi_strategy_account_book"))
        if config:
            return dict(config), warnings
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"default_config_unavailable: {exc}")
    warnings.append("missing_config: using built-in default multi_strategy_account_book config")
    return dict(DEFAULT_CONFIG), warnings


def validate_multi_strategy_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    raw = dict(DEFAULT_CONFIG)
    raw.update(_as_dict(config))

    if raw.get("_invalid_config"):
        errors.append("invalid_config_yaml")

    enabled = _safe_bool(raw.get("enabled"), True)
    raw["enabled"] = enabled
    raw["research_only"] = _safe_bool(raw.get("research_only"), True)
    if not raw["research_only"]:
        raw["research_only"] = True
        warnings.append("research_only_false_forced_true")

    initial_cash = _safe_float(raw.get("initial_cash"), None)
    if initial_cash is None or initial_cash <= 0:
        errors.append("initial_cash_must_be_positive")
        initial_cash = max(0.0, float(initial_cash or 0.0))
    raw["initial_cash"] = float(initial_cash)

    raw["portfolio_name"] = str(raw.get("portfolio_name") or DEFAULT_CONFIG["portfolio_name"])
    raw["currency"] = str(raw.get("currency") or "CNY")
    raw["rebalance_mode"] = str(raw.get("rebalance_mode") or "manual")
    raw["allow_cross_subaccount_cash"] = _safe_bool(raw.get("allow_cross_subaccount_cash"), False)
    raw["allow_short"] = _safe_bool(raw.get("allow_short"), False)
    if raw["allow_short"]:
        warnings.append("allow_short_true: short selling is research-only and no broker orders are generated")

    sub_accounts = _as_list(raw.get("sub_accounts"))
    if enabled and not sub_accounts:
        errors.append("sub_accounts_required_when_enabled")
    if not enabled and not sub_accounts:
        sub_accounts = _as_list(DEFAULT_CONFIG["sub_accounts"])
        warnings.append("account_book_disabled: using default sleeves for preview only")

    normalized_accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_weight = 0.0
    for index, item in enumerate(sub_accounts):
        row = _as_dict(item)
        name = str(row.get("name") or "").strip()
        if not name:
            name = f"sub_account_{index + 1}"
            warnings.append(f"missing_sub_account_name: assigned {name}")
        if name in seen:
            errors.append(f"duplicate_sub_account_name: {name}")
        seen.add(name)
        weight = _safe_float(row.get("weight"), None)
        if weight is None:
            weight = 0.0
            warnings.append(f"missing_weight: {name}")
        if weight < 0:
            errors.append(f"negative_weight: {name}")
        total_weight += max(0.0, float(weight))
        normalized_accounts.append({
            "name": name,
            "display_name": str(row.get("display_name") or name),
            "weight": max(0.0, float(weight)),
            "strategy_tag": str(row.get("strategy_tag") or name),
            "description": str(row.get("description") or ""),
            "research_only": True,
        })

    if normalized_accounts:
        if total_weight <= 0:
            errors.append("total_weight_must_be_positive")
        elif abs(total_weight - 1.0) > 0.000001:
            warnings.append(f"weight_sum_normalized: {total_weight:.6f}")
            for row in normalized_accounts:
                row["weight"] = float(row["weight"]) / total_weight

    raw["sub_accounts"] = normalized_accounts
    raw["position_sources"] = _as_dict(raw.get("position_sources"))
    raw["warnings"] = list(dict.fromkeys(str(item) for item in _as_list(raw.get("warnings")) + warnings if item))
    return _json_safe(raw), raw["warnings"], list(dict.fromkeys(errors))


def build_sub_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = _as_list(config.get("sub_accounts"))
    total_cash = float(_safe_float(config.get("initial_cash"), 0.0) or 0.0)
    allocated = 0.0
    sub_accounts: list[dict[str, Any]] = []
    for index, row_value in enumerate(accounts):
        row = _as_dict(row_value)
        weight = float(_safe_float(row.get("weight"), 0.0) or 0.0)
        if index == len(accounts) - 1:
            initial_cash = round(total_cash - allocated, 2)
        else:
            initial_cash = round(total_cash * weight, 2)
            allocated += initial_cash
        sub_accounts.append({
            "name": str(row.get("name") or f"sub_account_{index + 1}"),
            "display_name": str(row.get("display_name") or row.get("name") or f"Sub Account {index + 1}"),
            "strategy_tag": str(row.get("strategy_tag") or row.get("name") or ""),
            "description": str(row.get("description") or ""),
            "weight": weight,
            "initial_cash": float(initial_cash),
            "cash": float(initial_cash),
            "market_value": 0.0,
            "equity": float(initial_cash),
            "positions": [],
            "trades": [],
            "status": "ready",
            "warnings": [],
            **paper_only_boundary(),
        })
    return _json_safe(sub_accounts)


def load_sub_account_positions(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    target = _project_path(path)
    warnings: list[str] = []
    if not target.exists():
        return [], [f"missing_position_source: {_display_path(target)}"]
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as exc:  # noqa: BLE001
        return [], [f"invalid_position_source: {_display_path(target)}: {exc}"]

    required = {"symbol", "quantity"}
    fieldnames = set(rows[0].keys()) if rows else set()
    missing = sorted(required - fieldnames)
    if missing:
        warnings.append(f"position_source_missing_fields: {_display_path(target)}: {','.join(missing)}")
    positions: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            warnings.append(f"position_row_missing_symbol: {_display_path(target)}")
            continue
        quantity = _safe_float(row.get("quantity"), 0.0) or 0.0
        last_price = _safe_float(row.get("last_price"), None)
        market_value = _safe_float(row.get("market_value"), None)
        if market_value is None:
            if last_price is not None:
                market_value = quantity * last_price
            else:
                market_value = 0.0
                warnings.append(f"position_market_value_unavailable: {symbol}")
        positions.append({
            "symbol": symbol,
            "quantity": float(quantity),
            "last_price": last_price,
            "cost_basis": _safe_float(row.get("cost_basis"), None),
            "market_value": float(market_value),
            "unrealized_pnl": _safe_float(row.get("unrealized_pnl"), None),
            **paper_only_boundary(),
        })
    return _json_safe(positions), list(dict.fromkeys(warnings))


def attach_positions_to_sub_accounts(
    sub_accounts: list[dict[str, Any]],
    position_sources: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    by_name = {str(row.get("name")): dict(row) for row in sub_accounts}
    for name, source in _as_dict(position_sources).items():
        if name not in by_name:
            warnings.append(f"position_source_unknown_sub_account: {name}")
            continue
        positions, load_warnings = load_sub_account_positions(str(source))
        warnings.extend(load_warnings)
        account = by_name[name]
        market_value = round(sum(float(_safe_float(row.get("market_value"), 0.0) or 0.0) for row in positions), 2)
        account["positions"] = positions
        account["market_value"] = float(market_value)
        account["equity"] = float(round(float(account.get("cash") or 0.0) + market_value, 2))
        account["warnings"] = list(dict.fromkeys(_as_list(account.get("warnings")) + load_warnings))
        by_name[name] = account
    ordered = [by_name.get(str(row.get("name")), row) for row in sub_accounts]
    return _json_safe(ordered), list(dict.fromkeys(warnings))


def _risk_flags(config: dict[str, Any], warnings: list[str]) -> list[str]:
    flags: list[str] = []
    if config.get("allow_short"):
        flags.append("short_selling_research_only")
    if config.get("rebalance_mode") != "manual":
        flags.append("non_manual_rebalance_mode_not_executed")
    if warnings:
        flags.append("account_book_has_warnings")
    return list(dict.fromkeys(flags))


def build_multi_strategy_account_book(config: dict[str, Any]) -> dict[str, Any]:
    normalized, warnings, errors = validate_multi_strategy_config(config)
    sub_accounts = build_sub_accounts(normalized)
    sub_accounts, position_warnings = attach_positions_to_sub_accounts(sub_accounts, _as_dict(normalized.get("position_sources")))
    warnings = list(dict.fromkeys(warnings + position_warnings))

    cash = round(sum(float(_safe_float(row.get("cash"), 0.0) or 0.0) for row in sub_accounts), 2)
    market_value = round(sum(float(_safe_float(row.get("market_value"), 0.0) or 0.0) for row in sub_accounts), 2)
    equity = round(sum(float(_safe_float(row.get("equity"), 0.0) or 0.0) for row in sub_accounts), 2)
    status = "failed" if errors else ("warning" if warnings else "passed")
    allocation = {str(row.get("name")): float(_safe_float(row.get("weight"), 0.0) or 0.0) for row in sub_accounts}
    allocation_cash = {str(row.get("name")): float(_safe_float(row.get("initial_cash"), 0.0) or 0.0) for row in sub_accounts}
    assumptions = [
        "Research-only virtual account book; no broker connection and no real orders.",
        "Sub-account allocations are paper bookkeeping sleeves, not automatic rebalancing instructions.",
        "Positions are optional local CSV snapshots only; missing prices are not filled from the network.",
        "This module does not create buy, sell, or position-sizing advice.",
    ]
    payload = {
        "ok": not errors,
        "status": status,
        "task_type": "multi_strategy_account_book",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "network": False,
        "portfolio_name": normalized.get("portfolio_name") or DEFAULT_CONFIG["portfolio_name"],
        "currency": normalized.get("currency") or "CNY",
        "initial_cash": float(_safe_float(normalized.get("initial_cash"), 0.0) or 0.0),
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "sub_account_count": len(sub_accounts),
        "sub_accounts": sub_accounts,
        "allocation": allocation,
        "allocation_cash": allocation_cash,
        "risk_flags": _risk_flags(normalized, warnings),
        "warnings": warnings,
        "errors": errors,
        "assumptions": assumptions,
        "output_paths": {},
        "disclaimer": DISCLAIMER,
        "research_only": True,
        "rebalance_mode": normalized.get("rebalance_mode") or "manual",
        "allow_cross_subaccount_cash": bool(normalized.get("allow_cross_subaccount_cash", False)),
        "allow_short": bool(normalized.get("allow_short", False)),
        **paper_only_boundary(),
    }
    return _json_safe(payload)


def render_multi_strategy_account_book_markdown(payload: dict[str, Any]) -> str:
    payload = _as_dict(payload)
    lines = [
        "# Multi-Strategy Account Book",
        "",
        f"- Portfolio name: `{payload.get('portfolio_name') or '-'}`",
        f"- Status: `{payload.get('status') or 'unknown'}`",
        f"- Currency: `{payload.get('currency') or 'CNY'}`",
        f"- Initial cash: {payload.get('initial_cash')}",
        f"- Equity: {payload.get('equity')}",
        f"- Sub-accounts: {payload.get('sub_account_count', 0)}",
        "",
        "## Allocation",
        "",
        "| Sub-account | Strategy tag | Weight | Initial cash | Equity | Positions |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row_value in _as_list(payload.get("sub_accounts")):
        row = _as_dict(row_value)
        lines.append(
            f"| {row.get('display_name') or row.get('name') or '-'} | {row.get('strategy_tag') or '-'} | "
            f"{float(_safe_float(row.get('weight'), 0.0) or 0.0):.4f} | {row.get('initial_cash')} | "
            f"{row.get('equity')} | {len(_as_list(row.get('positions')))} |"
        )
    lines += ["", "## Position Source Status", ""]
    position_warnings = [item for item in _as_list(payload.get("warnings")) if "position" in str(item)]
    if position_warnings:
        lines.extend(f"- {item}" for item in position_warnings[:8])
    else:
        lines.append("- No external position CSV source was required for the default empty book.")
    lines += ["", "## Risk Flags", ""]
    risks = _as_list(payload.get("risk_flags"))
    lines.extend(f"- {item}" for item in risks) if risks else lines.append("- none")
    lines += ["", "## Warnings", ""]
    warnings = _as_list(payload.get("warnings"))
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    lines += ["", "## Assumptions", ""]
    assumptions = _as_list(payload.get("assumptions"))
    lines.extend(f"- {item}" for item in assumptions) if assumptions else lines.append("- Research-only virtual account book.")
    lines += ["", "## Output Paths", ""]
    paths = _as_dict(payload.get("output_paths"))
    if paths:
        lines.extend(f"- {key}: `{value}`" for key, value in paths.items())
    else:
        lines.append("- not written")
    lines += ["", "## Disclaimer", "", DISCLAIMER, ""]
    return "\n".join(lines)


def write_multi_strategy_account_book_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = _project_path(output_dir)
    rep_dir = _project_path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    latest_json = out_dir / "multi_strategy_account_book_latest.json"
    timestamp_json = out_dir / f"multi_strategy_account_book_{timestamp}.json"
    latest_report = rep_dir / "multi_strategy_account_book_latest.md"
    timestamp_report = rep_dir / f"multi_strategy_account_book_{timestamp}.md"
    payload = dict(payload)
    payload["output_paths"] = {
        "latest_json": _display_path(latest_json),
        "timestamp_json": _display_path(timestamp_json),
        "latest_report": _display_path(latest_report),
        "timestamp_report": _display_path(timestamp_report),
    }
    safe_payload = _json_safe(payload)
    text = json.dumps(safe_payload, ensure_ascii=False, indent=2, allow_nan=False)
    latest_json.write_text(text, encoding="utf-8")
    timestamp_json.write_text(text, encoding="utf-8")
    report = render_multi_strategy_account_book_markdown(safe_payload)
    latest_report.write_text(report, encoding="utf-8")
    timestamp_report.write_text(report, encoding="utf-8")
    try:
        from quantdesk.experiment_tracker import log_experiment_non_blocking

        tracking = log_experiment_non_blocking(
            "multi_strategy_account_book",
            {"portfolio_name": safe_payload.get("portfolio_name"), "sub_account_count": safe_payload.get("sub_account_count")},
            {
                "initial_cash": safe_payload.get("initial_cash"),
                "equity": safe_payload.get("equity"),
                "portfolio_pnl": (safe_payload.get("equity") or 0) - (safe_payload.get("initial_cash") or 0),
                "sub_account_count": safe_payload.get("sub_account_count"),
            },
            output_paths=safe_payload.get("output_paths") if isinstance(safe_payload.get("output_paths"), dict) else {},
            tags=["auto_logged"],
        )
        if tracking.get("experiment_id"):
            safe_payload["experiment_id"] = tracking.get("experiment_id")
        if tracking.get("warning") and not tracking.get("ok"):
            safe_payload.setdefault("warnings", []).append(tracking["warning"])
    except Exception as exc:
        safe_payload.setdefault("warnings", []).append(f"experiment_tracker_failed:{exc}")
    return safe_payload


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d_%H%M%S"):
        try:
            sample_len = len(datetime.now().strftime(fmt))
            return datetime.strptime(text[:sample_len], fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def load_multi_strategy_account_book_snapshot(
    path: str | Path = DEFAULT_LATEST_JSON,
    *,
    stale_hours: int = 48,
) -> dict[str, Any]:
    target = _project_path(path)
    latest_json = _display_path(target)
    if not target.exists():
        return {
            "enabled": True,
            "status": "missing",
            "portfolio_name": "",
            "sub_account_count": 0,
            "allocation_preview": {},
            "equity": None,
            "warning_count": 1,
            "warnings": [f"missing_multi_strategy_account_book: {latest_json}"],
            "risk_flags_preview": [],
            "latest_json": latest_json,
            "latest_report": DEFAULT_LATEST_REPORT,
            "stale": False,
            "disclaimer": DISCLAIMER,
            **paper_only_boundary(),
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "status": "invalid",
            "portfolio_name": "",
            "sub_account_count": 0,
            "allocation_preview": {},
            "equity": None,
            "warning_count": 1,
            "warnings": [f"invalid_multi_strategy_account_book_json: {latest_json}: {exc}"],
            "risk_flags_preview": [],
            "latest_json": latest_json,
            "latest_report": DEFAULT_LATEST_REPORT,
            "stale": False,
            "disclaimer": DISCLAIMER,
            **paper_only_boundary(),
        }
    payload = _as_dict(payload)
    warnings = [str(item) for item in _as_list(payload.get("warnings")) if item]
    if payload.get("task_type") != "multi_strategy_account_book":
        warnings.append(f"task_type_mismatch: {payload.get('task_type') or 'missing'}")
    generated_at = payload.get("generated_at")
    stale = False
    dt = _parse_dt(generated_at)
    if dt is not None and datetime.now() - dt > timedelta(hours=max(1, int(stale_hours))):
        stale = True
        warnings.append(f"stale_multi_strategy_account_book: {generated_at}")
    output_paths = _as_dict(payload.get("output_paths"))
    status = "stale" if stale else str(payload.get("status") or "warning")
    allocation = _as_dict(payload.get("allocation"))
    snapshot = {
        "enabled": True,
        "status": status,
        "portfolio_name": payload.get("portfolio_name") or "",
        "sub_account_count": int(payload.get("sub_account_count") or len(_as_list(payload.get("sub_accounts")))),
        "allocation_preview": {str(k): v for k, v in list(allocation.items())[:5]},
        "equity": _safe_float(payload.get("equity"), None),
        "warning_count": len(list(dict.fromkeys(warnings))),
        "warnings": list(dict.fromkeys(warnings)),
        "risk_flags_preview": [str(item) for item in _as_list(payload.get("risk_flags"))[:5]],
        "latest_json": str(output_paths.get("latest_json") or latest_json).replace("\\", "/"),
        "latest_report": str(output_paths.get("latest_report") or output_paths.get("report") or DEFAULT_LATEST_REPORT).replace("\\", "/"),
        "generated_at": str(generated_at or ""),
        "stale": stale,
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }
    return _json_safe(snapshot)


def render_daily_multi_strategy_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    snapshot = _as_dict(snapshot)
    lines = [
        "## Multi-Strategy Account Book Snapshot",
        "",
        DISCLAIMER,
        "",
        f"- Status: `{snapshot.get('status') or 'missing'}`",
        f"- Portfolio: `{snapshot.get('portfolio_name') or '-'}`",
        f"- Sub-accounts: {snapshot.get('sub_account_count', 0)}",
        f"- Equity: {snapshot.get('equity')}",
        f"- Latest report: {snapshot.get('latest_report') or DEFAULT_LATEST_REPORT}",
        f"- Latest JSON: {snapshot.get('latest_json') or DEFAULT_LATEST_JSON}",
        "",
        "### Allocation Preview",
        "",
    ]
    allocation = _as_dict(snapshot.get("allocation_preview"))
    if allocation:
        lines.extend(f"- {name}: {weight}" for name, weight in allocation.items())
    else:
        lines.append("- unavailable")
    risks = _as_list(snapshot.get("risk_flags_preview"))
    lines += ["", "### Risk Flags", ""]
    lines.extend(f"- {item}" for item in risks) if risks else lines.append("- none")
    warnings = _as_list(snapshot.get("warnings"))
    lines += ["", "### Warnings", ""]
    lines.extend(f"- {item}" for item in warnings[:5]) if warnings else lines.append("- none")
    lines.append("")
    return "\n".join(lines)
