from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.config import load_config  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.utils import atomic_write_json  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_UNIVERSE_PATH = DATA_DIR / "universe" / "a_share_symbols.csv"
DEFAULT_STATUS_PATH = DATA_DIR / "cache" / "a_share_universe_status.json"
DEFAULT_FALLBACK_SYMBOLS = ["600000", "000001", "600519", "300750"]


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    if digits:
        return digits.zfill(6)
    return ""


def _add_symbol(rows: list[dict[str, str]], symbol: object, name: object = "", source: str = "") -> None:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return
    if any(row["symbol"] == normalized for row in rows):
        return
    rows.append({"symbol": normalized, "name": str(name or "").strip(), "source": source or "unknown"})


def normalize_universe_frame(df: pd.DataFrame, source: str) -> list[dict[str, str]]:
    if df is None or df.empty:
        return []
    code_candidates = ["symbol", "code", "代码", "股票代码", "证券代码"]
    name_candidates = ["name", "名称", "股票简称", "证券简称"]
    code_col = next((column for column in code_candidates if column in df.columns), "")
    name_col = next((column for column in name_candidates if column in df.columns), "")
    if not code_col:
        return []
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        _add_symbol(rows, row.get(code_col), row.get(name_col) if name_col else "", source)
    return rows


def fetch_universe_from_akshare() -> tuple[list[dict[str, str]], str, dict[str, str]]:
    provider_errors: dict[str, str] = {}
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        provider_errors["akshare_import"] = str(exc)
        return [], "", provider_errors

    providers = [
        ("stock_info_a_code_name", lambda: ak.stock_info_a_code_name()),
        ("stock_zh_a_spot_em", lambda: ak.stock_zh_a_spot_em()),
        ("stock_zh_a_spot", lambda: ak.stock_zh_a_spot()),
    ]
    for provider_name, fetcher in providers:
        try:
            rows = normalize_universe_frame(fetcher(), provider_name)
            if rows:
                return rows, provider_name, provider_errors
            provider_errors[provider_name] = "provider returned empty universe"
        except Exception as exc:
            provider_errors[provider_name] = str(exc)
    return [], "", provider_errors


def load_local_fallback_universe(config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    rows: list[dict[str, str]] = []

    daily_dir = PROJECT_ROOT / str(data_cfg.get("daily_cache_dir") or "data/daily")
    if not daily_dir.is_absolute():
        daily_dir = PROJECT_ROOT / daily_dir
    if daily_dir.exists():
        for path in sorted(daily_dir.glob("*.csv")):
            _add_symbol(rows, path.stem, "", "local_daily_cache")

    for status_name in ["update_status.json", "spot_snapshot_meta.json"]:
        status_path = PROJECT_ROOT / "data" / "cache" / status_name
        if not status_path.exists():
            continue
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in raw.get("symbols", []) or raw.get("results", []) or []:
            if isinstance(item, dict):
                _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name", ""), f"cache_{status_name}")

    for pattern in ["reports/*_candidates.csv", "reports/*_long_candidates.csv", "reports/*_watchlist.csv"]:
        for path in sorted(PROJECT_ROOT.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)[:5]:
            df = read_csv_frame(path, dtype=str)
            if any(item.startswith("csv_read_failed:") for item in list(df.attrs.get("warnings") or [])):
                continue
            for _, row in df.iterrows():
                _add_symbol(rows, row.get("symbol") or row.get("code") or row.get("股票代码"), row.get("name") or row.get("名称"), f"report_{path.name}")

    for symbol in data_cfg.get("fallback_symbols") or DEFAULT_FALLBACK_SYMBOLS:
        _add_symbol(rows, symbol, "", "config_fallback_symbols")
    if not rows:
        for symbol in DEFAULT_FALLBACK_SYMBOLS:
            _add_symbol(rows, symbol, "", "default_fallback_symbols")
    return rows


def resolve_output_paths(config: dict[str, Any] | None, output: str = "", status_path: str = "") -> tuple[Path, Path]:
    cfg = config if config is not None else load_config()
    data_cfg = cfg.get("data", {}) if isinstance(cfg.get("data"), dict) else {}
    output_path = Path(output or str(data_cfg.get("a_share_universe_path") or DEFAULT_UNIVERSE_PATH))
    status = Path(status_path or str(data_cfg.get("a_share_universe_status_path") or DEFAULT_STATUS_PATH))
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    if not status.is_absolute():
        status = PROJECT_ROOT / status
    return output_path, status


def write_universe_csv(path: Path, rows: list[dict[str, str]], *, limit: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output_rows = sorted(rows, key=lambda item: item["symbol"])
    if limit and limit > 0:
        output_rows = output_rows[:limit]
    df = pd.DataFrame(output_rows, columns=["symbol", "name", "source"])
    df["updated_at"] = now_text()
    df.to_csv(path, index=False, encoding="utf-8-sig")


def run_update_universe(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config or None)
    output_path, status_path = resolve_output_paths(config, args.output, args.status_path)
    started_at = now_text()
    rows, provider_used, provider_errors = fetch_universe_from_akshare()
    warnings: list[str] = []
    if not rows:
        rows = load_local_fallback_universe(config)
        provider_used = "local_fallback_universe"
        warnings.append("AKShare universe providers unavailable; used local cache/report/config symbols.")
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    if not args.dry_run:
        write_universe_csv(output_path, rows)
    status = {
        "started_at": started_at,
        "finished_at": now_text(),
        "provider_used": provider_used,
        "provider_errors": provider_errors,
        "row_count": len(rows),
        "output_path": str(output_path),
        "status_path": str(status_path),
        "dry_run": bool(args.dry_run),
        "warnings": warnings,
        "ok": bool(rows),
    }
    if not args.dry_run:
        atomic_write_json(status_path, status)
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update local A-share universe CSV for QuantDesk.")
    parser.add_argument("--config", default="", help="Optional config path.")
    parser.add_argument("--output", default="", help="Output CSV path, default data/universe/a_share_symbols.csv.")
    parser.add_argument("--status-path", default="", help="Universe update status JSON path.")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows written, useful for smoke tests.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve universe without writing files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = run_update_universe(args)
    print(
        f"update_a_share_universe finished: ok={status['ok']} provider={status['provider_used']} "
        f"rows={status['row_count']} output={status['output_path']}"
    )
    if status.get("warnings"):
        for warning in status["warnings"]:
            print(f"WARN {warning}")
    return 0 if status.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
