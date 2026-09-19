from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSTITUENTS_PATH = PROJECT_ROOT / "data" / "universe" / "index_constituents.json"


def load_index_membership_history(path: str | Path) -> dict:
    target = Path(path)
    if not target.exists():
        return {"status": "warning", "warnings": ["index_membership_history_missing"], "members": []}
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return {"status": "passed", "warnings": [], "members": list(csv.DictReader(handle))}


def build_index_membership_history_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    data = load_index_membership_history(cfg.get("path") or "data/index_membership/HS300_latest.csv")
    payload = base_payload("index_membership_history", status=data["status"], warnings=data.get("warnings") or [])
    payload.update({"members": data.get("members") or [], "member_count": len(data.get("members") or []), "items": data.get("members") or []})
    return finalize_outputs(payload, "index_membership_history", simple_markdown("Index Membership History", payload, "items"))


def load_constituent_history(index_name: str = "hs300", path: str | Path = CONSTITUENTS_PATH) -> dict[str, list[str]]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    raw = data.get(index_name, {})
    if not isinstance(raw, dict):
        return {}
    return {str(date): [str(symbol) for symbol in symbols] for date, symbols in raw.items() if isinstance(symbols, list)}


def get_constituents_on_date(
    date: str,
    index_name: str = "hs300",
    path: str | Path = CONSTITUENTS_PATH,
) -> set[str]:
    history = load_constituent_history(index_name=index_name, path=path)
    if not history:
        return set()
    target = datetime.strptime(date, "%Y-%m-%d")
    valid_dates = []
    for raw_date in history:
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError:
            continue
        if parsed <= target:
            valid_dates.append(parsed)
    if not valid_dates:
        return set()
    latest = max(valid_dates).strftime("%Y-%m-%d")
    return set(history.get(latest, []))


def filter_by_index_constituents(
    symbols: set[str],
    date: str,
    index_name: str = "hs300",
    path: str | Path = CONSTITUENTS_PATH,
) -> set[str]:
    constituents = get_constituents_on_date(date=date, index_name=index_name, path=path)
    if not constituents:
        return set(symbols)
    return set(symbols) & constituents


def fetch_from_akshare_and_store(*, network: bool = False, path: str | Path = CONSTITUENTS_PATH) -> dict:
    if not network:
        return {"status": "skipped", "reason": "network_disabled"}
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        return {"status": "skipped", "reason": f"akshare_unavailable:{type(exc).__name__}"}
    try:
        df = ak.index_stock_cons_weight_csindex(symbol="000300")
    except Exception as exc:  # pragma: no cover - depends on network/provider
        return {"status": "failed", "reason": f"fetch_failed:{type(exc).__name__}"}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hs300_latest_raw_columns": list(df.columns), "hs300_latest": df.to_dict(orient="records")}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "passed", "path": str(target), "row_count": int(len(df))}
