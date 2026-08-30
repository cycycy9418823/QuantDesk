from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.point_in_time_universe import SCHEMA_COLUMNS  # noqa: E402

from quantdesk.data.schema_normalizer import normalize_daily_csv  # noqa: E402
from quantdesk.shared import now_text, truthy  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

OUTPUT_CSV = PROJECT_ROOT / "data" / "universe" / "historical_listings.csv"
RESULT_JSON = PROJECT_ROOT / "results" / "historical_listings_build_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "historical_listings_build_latest.md"
LIST_DATE_MEANING = "first available local price date, not official listing date"
DISCLAIMER = "Runtime price_csv_coverage universe only. It is degraded PIT data, not complete official listing/delist/ST history."


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def _daily_files(data_root: str | Path) -> list[Path]:
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    dirs = [
        root / "daily",
        root / "daily" / "forward_adjusted",
        root / "daily" / "raw",
        root / "daily" / "back_adjusted",
    ]
    seen: set[Path] = set()
    files: list[Path] = []
    for directory in dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.csv")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def _load_name_map(data_root: str | Path) -> dict[str, str]:
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    candidates = [
        root / "universe" / "a_share_symbols.csv",
        root / "a_share_symbols.csv",
        root / "universe" / "symbols.csv",
    ]
    out: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        frame = read_csv_frame(path, dtype=str, encoding="utf-8-sig")
        if any(item.startswith("csv_read_failed:") for item in list(frame.attrs.get("warnings") or [])):
            continue
        cols = {str(c).strip(): c for c in frame.columns}
        symbol_col = cols.get("symbol") or cols.get("code") or cols.get("股票代码") or cols.get("代码")
        name_col = cols.get("name") or cols.get("股票名称") or cols.get("名称")
        if not symbol_col or not name_col:
            continue
        for row in frame[[symbol_col, name_col]].dropna().itertuples(index=False):
            symbol = str(row[0]).strip()
            name = str(row[1]).strip()
            digits = "".join(ch for ch in symbol if ch.isdigit())
            if len(digits) >= 6 and name:
                out[digits[-6:]] = name
    return out


def _is_st_name(name: str) -> bool:
    upper = str(name or "").upper()
    return any(token in upper for token in ("*ST", "ST", "SST", "退"))


def build_historical_listings(
    *,
    data_root: str | Path = "data",
    output_path: str | Path = OUTPUT_CSV,
    write: bool = True,
    network: bool = False,
) -> dict[str, Any]:
    warnings: list[str] = []
    skipped_files: list[dict[str, Any]] = []
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    if network:
        warnings.append("network_requested_ignored: build_historical_listings defaults to offline local CSV coverage")
    name_map = _load_name_map(data_root)
    for path in _daily_files(data_root):
        frame, meta = normalize_daily_csv(path)
        if frame.empty:
            skipped_files.append({"path": _rel(path), "warnings": meta.get("warnings") or [], "errors": meta.get("errors") or []})
            continue
        symbol = str(frame["symbol"].dropna().astype(str).iloc[0]) if "symbol" in frame.columns and not frame.empty else ""
        if not symbol or len(symbol) != 6:
            skipped_files.append({"path": _rel(path), "errors": ["symbol_inference_failed"]})
            continue
        name = name_map.get(symbol, "")
        first_date = str(frame["date"].min())
        last_date = str(frame["date"].max())
        existing = rows_by_symbol.get(symbol)
        if existing:
            first_date = min(str(existing.get("first_price_date") or first_date), first_date)
            last_date = max(str(existing.get("last_price_date") or last_date), last_date)
            name = str(existing.get("name") or name)
        notes = "list_date is inferred from first available local price CSV date, not the official listing date; no_historical_st_period_available; no_delist_history_available"
        row = {column: "" for column in SCHEMA_COLUMNS}
        row.update({
            "symbol": symbol,
            "name": name,
            "list_date": first_date,
            "delist_date": "",
            "is_active": "true",
            "is_st_current": "true" if _is_st_name(name) else "false",
            "st_start_date": "",
            "st_end_date": "",
            "suspend_start_date": "",
            "suspend_end_date": "",
            "suspension_start_date": "",
            "suspension_end_date": "",
            "suspension_periods": "",
            "market": "",
            "board": "",
            "first_price_date": first_date,
            "last_price_date": last_date,
            "source": "price_csv_coverage",
            "confidence": "low_to_medium",
            "survivorship_bias_risk": "high",
            "notes": notes,
            "updated_at": now_text(),
        })
        rows_by_symbol[symbol] = row
        warnings.extend(meta.get("warnings") or [])
    rows = [rows_by_symbol[s] for s in sorted(rows_by_symbol)]
    status = "failed" if not rows else "warning"
    if rows:
        warnings.extend([
            "degraded_pit_price_csv_coverage",
            "list_date_is_first_available_local_price_date_not_official_listing_date",
            "survivorship_bias_risk_high",
            "historical_st_periods_unavailable",
            "delist_history_unavailable",
        ])
    output = Path(output_path)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    if write and rows:
        output.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=list(SCHEMA_COLUMNS)).to_csv(output, index=False, encoding="utf-8")
    payload = {
        "ok": bool(rows),
        "status": status,
        "task_type": "build_historical_listings",
        "generated_at": now_text(),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "output_path": _rel(output),
        "row_count": len(rows),
        "symbol_count": len(rows),
        "source": "price_csv_coverage",
        "confidence": "low_to_medium",
        "pit_quality": "degraded",
        "survivorship_bias_risk": "high",
        "list_date_meaning": LIST_DATE_MEANING,
        "source_distribution": dict(Counter(row["source"] for row in rows)),
        "confidence_distribution": dict(Counter(row["confidence"] for row in rows)),
        "warnings": list(dict.fromkeys(str(w) for w in warnings if w))[:200],
        "skipped_files": skipped_files[:200],
        "network": bool(network),
        "write": bool(write),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_report(payload: dict[str, Any]) -> str:
    return "\n".join([
        "# Historical Listings Build",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- output_path: `{payload.get('output_path')}`",
        f"- row_count: `{payload.get('row_count')}`",
        f"- source: `{payload.get('source')}`",
        f"- confidence: `{payload.get('confidence')}`",
        f"- pit_quality: `{payload.get('pit_quality')}`",
        f"- survivorship_bias_risk: `{payload.get('survivorship_bias_risk')}`",
        f"- list_date_meaning: `{payload.get('list_date_meaning')}`",
        "",
        "## Warnings",
        *(f"- {item}" for item in (payload.get("warnings") or ["none"])[:50]),
        "",
    ])


def write_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    if not result.is_absolute():
        result = PROJECT_ROOT / result
    if not report.is_absolute():
        report = PROJECT_ROOT / report
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report), "csv": payload.get("output_path")}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_report(payload), encoding="utf-8")
    return payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build runtime historical_listings.csv from local daily CSV coverage.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output", default=str(OUTPUT_CSV.relative_to(PROJECT_ROOT)))
    parser.add_argument("--network", default="false")
    parser.add_argument("--write", default="true")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_outputs(build_historical_listings(
        data_root=args.data_root,
        output_path=args.output,
        write=truthy(args.write),
        network=truthy(args.network),
    ))
    wants_json = truthy(args.json) or truthy(args.summary)
    if wants_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(render_report(payload))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
