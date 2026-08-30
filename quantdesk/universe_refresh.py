"""Fail-closed daily refresh for the local A-share spot/universe snapshot.

Network access is explicit.  No canonical file is published until the complete
provider snapshot passes date, uniqueness, page/row and baseline coverage gates.
The module is research-only and has no Flask or broker dependency.  The
production default intentionally exposes provider_unavailable; an application
must inject a provider adapter with explicit status/date/page evidence.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.learning_universe import (
    build_low_price_universe_from_spot,
    save_low_price_universe,
)

from quantdesk.market_clock import (
    expected_settlement_date,
    get_market_session,
    to_shanghai_datetime,
)

Fetcher = Callable[..., Any]
_SYMBOL_COLUMNS = ("symbol", "code", "ts_code", "\u4ee3\u7801", "\u4ee3\u7801\u539f\u59cb")
_DATE_COLUMNS = ("quote_date", "trade_date", "date", "\u65e5\u671f", "quote_time", "updated_at")
_PRICE_COLUMNS = (
    "price", "\u6700\u65b0\u4ef7", "latest_price", "latest_close",
    "close", "current_price", "trade",
)
_COMPLETE_STATUSES = {"ok", "complete", "completed", "success", "passed", "full", "refreshed"}


def _sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        try:
            number = float(text)
            if math.isfinite(number) and number > 1_000_000_000:
                unit = "ms" if number > 100_000_000_000 else "s"
                parsed = pd.to_datetime(number, unit=unit, errors="coerce")
        except (TypeError, ValueError, OverflowError):
            parsed = pd.NaT
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def _symbol(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    lower = text.lower()
    if lower.startswith(("sh", "sz", "bj")):
        text = text[2:]
    if "." in text:
        left, suffix = text.split(".", 1)
        if suffix.upper() in {"SH", "SZ", "BJ"} or re.fullmatch(r"0+", suffix):
            text = left
    if not text.isdigit() or len(text) > 6:
        return ""
    return text.zfill(6)


def _symbol_column(frame: pd.DataFrame) -> str | None:
    return next((item for item in _SYMBOL_COLUMNS if item in frame.columns), None)


def _price_column(frame: pd.DataFrame) -> str | None:
    return next((item for item in _PRICE_COLUMNS if item in frame.columns), None)


def _market_segment(symbol: str) -> str:
    value = _symbol(symbol)
    if value.startswith(("920", "83", "87", "43")):
        return "bse"
    if value.startswith("6"):
        return "sh"
    if value.startswith(("0", "3")):
        return "sz"
    return "other"


def _market_coverage(
    baseline: set[str],
    observed: set[str],
    minimum: float,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    evidence: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for market in ("sh", "sz", "bse", "other"):
        baseline_market = {item for item in baseline if _market_segment(item) == market}
        if not baseline_market:
            continue
        observed_market = {item for item in observed if _market_segment(item) == market}
        matched = baseline_market & observed_market
        ratio = len(matched) / len(baseline_market)
        status = "passed"
        if not observed_market or not matched:
            status = "blocked"
            reasons.append(f"market_segment_missing:{market}")
        elif ratio < minimum:
            status = "blocked"
            reasons.append(f"market_coverage_below_minimum:{market}")
        evidence[market] = {
            "baseline_count": len(baseline_market),
            "observed_count": len(observed_market),
            "matched_count": len(matched),
            "missing_count": max(0, len(baseline_market) - len(matched)),
            "coverage_ratio": float(ratio),
            "min_coverage": float(minimum),
            "status": status,
        }
    return evidence, reasons


def _positive_price_evidence(
    frame: pd.DataFrame,
) -> tuple[str, int, float]:
    column = _price_column(frame)
    if column is None or frame.empty:
        return "", 0, 0.0
    values = pd.to_numeric(frame[column], errors="coerce")
    valid = values.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value)) and float(value) > 0))
    count = int(valid.sum())
    return column, count, float(count / len(frame))


def _active_low_price_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            frame = pd.read_csv(path)
        except Exception:
            return 0
    if frame.empty:
        return 0
    truthy = {"true", "1", "yes", "y", "t"}
    included = (
        frame["included"].astype(str).str.strip().str.lower().isin(truthy)
        if "included" in frame.columns
        else pd.Series(False, index=frame.index)
    )
    buffer = (
        frame["in_buffer"].astype(str).str.strip().str.lower().isin(truthy)
        if "in_buffer" in frame.columns
        else pd.Series(False, index=frame.index)
    )
    return int((included | buffer).sum())


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _output_paths(
    root: Path,
    values: Mapping[str, str | Path] | Sequence[str | Path] | None,
) -> dict[str, Path]:
    paths = {
        "spot_snapshot": root / "data/cache/spot_snapshot_latest.csv",
        "spot_meta": root / "data/cache/spot_snapshot_meta.json",
        "low_price_universe": root / "data/universe/low_price_discovery_symbols.csv",
    }
    if values is None:
        return paths
    if not isinstance(values, Mapping):
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and len(values) == 3:
            return {
                "spot_snapshot": _resolve(root, values[0]),
                "spot_meta": _resolve(root, values[1]),
                "low_price_universe": _resolve(root, values[2]),
            }
        raise TypeError("output_paths must be a mapping or three-item sequence")
    aliases = {
        "spot_snapshot": (
            "spot_snapshot", "spot_snapshot_path", "spot_snapshot_latest",
            "spot_snapshot_latest_path", "snapshot", "snapshot_path", "spot",
        ),
        "spot_meta": (
            "spot_meta", "spot_meta_path", "spot_snapshot_meta",
            "spot_snapshot_meta_path", "meta", "meta_path", "metadata",
        ),
        "low_price_universe": (
            "low_price_universe", "low_price_universe_path",
            "low_price_learning_symbols", "low_price_learning_symbols_path",
            "universe", "universe_path", "learning_universe",
        ),
    }
    for canonical, choices in aliases.items():
        for choice in choices:
            if choice in values and values[choice] is not None:
                paths[canonical] = _resolve(root, values[choice])
                break
    return paths


def _baseline(path: Path) -> tuple[set[str], list[str]]:
    if not path.exists():
        return set(), ["baseline_missing"]
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    except Exception:
        try:
            frame = pd.read_csv(path, dtype=str)
        except Exception:
            return set(), ["baseline_unreadable"]
    if frame.empty:
        return set(), ["baseline_empty"]
    column = _symbol_column(frame)
    if column is None:
        return set(), ["baseline_symbol_column_missing"]
    values = [_symbol(value) for value in frame[column]]
    reasons = ["baseline_invalid_symbol"] if any(not value for value in values) else []
    symbols = {value for value in values if value}
    if not symbols:
        reasons.append("baseline_empty")
    return symbols, reasons


def _fetcher_list(fetchers: Any) -> list[tuple[str, Fetcher | None]]:
    if fetchers is None:
        return [("eastmoney", None), ("sina", None)]
    if callable(fetchers):
        return [(getattr(fetchers, "__name__", "provider"), fetchers)]
    if isinstance(fetchers, Mapping):
        items = [(str(key), value) for key, value in fetchers.items()]
    elif isinstance(fetchers, Sequence) and not isinstance(fetchers, (str, bytes)):
        items = []
        for index, item in enumerate(fetchers):
            if isinstance(item, Mapping):
                name = str(item.get("provider") or item.get("name") or f"provider_{index + 1}")
                candidate = item.get("fetch") or item.get("fetcher") or item.get("call")
                items.append((name, candidate if callable(candidate) else None))
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
                items.append((str(item[0]), item[1]))
            else:
                candidate = item.fetch if hasattr(item, "fetch") and callable(item.fetch) else item
                items.append((getattr(item, "__name__", f"provider_{index + 1}"), candidate if callable(candidate) else None))
    else:
        raise TypeError("fetchers must be a mapping, callable, or sequence")
    indexed = list(enumerate(items))
    def rank(entry: tuple[int, tuple[str, Fetcher | None]]) -> tuple[int, int]:
        index, (name, _) = entry
        lower = name.lower()
        return (0 if "eastmoney" in lower or lower in {"em", "east_money"} else 1 if "sina" in lower else 2, index)
    return [item for _, item in sorted(indexed, key=rank)]


def _call(fetcher: Fetcher, target_date: str) -> Any:
    try:
        signature = inspect.signature(fetcher)
    except (TypeError, ValueError):
        return fetcher(target_date=target_date)
    parameters = list(signature.parameters.values())
    if "target_date" in signature.parameters or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
        return fetcher(target_date=target_date)
    positional = [
        item for item in parameters
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return fetcher(target_date) if positional else fetcher()


def _result(value: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    frame_value = value
    if isinstance(value, Mapping):
        for key in ("metadata", "meta", "attrs"):
            if isinstance(value.get(key), Mapping):
                metadata.update(dict(value[key]))
        for key, item in value.items():
            if key not in {"frame", "data", "rows", "metadata", "meta", "attrs"}:
                metadata.setdefault(str(key), item)
        frame_value = next((value[key] for key in ("frame", "data", "rows") if key in value and value[key] is not None), None)
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray, pd.DataFrame))
        and len(value) == 2
        and isinstance(value[1], Mapping)
    ):
        frame_value, extra = value
        metadata.update(dict(extra))
    if isinstance(frame_value, pd.DataFrame):
        metadata = {**dict(frame_value.attrs), **metadata}
        return frame_value.copy(), metadata
    if frame_value is None:
        return pd.DataFrame(), metadata
    try:
        return pd.DataFrame(frame_value), metadata
    except Exception:
        metadata["coerce_error"] = "provider_result_not_tabular"
        return pd.DataFrame(), metadata


def _meta_int(metadata: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        try:
            value = int(float(metadata[key]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if value >= 0:
            return value
    return None


def _quote_date(frame: pd.DataFrame, metadata: Mapping[str, Any]) -> tuple[str, list[str]]:
    values: set[str] = set()
    reasons: list[str] = []
    for key in ("quote_date", "trade_date", "as_of_date", "snapshot_date", "date", "updated_at"):
        if key not in metadata or metadata[key] in (None, ""):
            continue
        parsed = _date(metadata[key])
        if parsed:
            values.add(parsed)
        else:
            reasons.append("quote_date_unparseable")
        break
    found_column = False
    for column in _DATE_COLUMNS:
        if column not in frame.columns:
            continue
        found_column = True
        raw = [item for item in frame[column] if str(item or "").strip().lower() not in {"", "nan", "none", "nat"}]
        parsed = [_date(item) for item in raw]
        if any(not item for item in parsed):
            reasons.append("quote_date_unparseable")
        values.update(item for item in parsed if item)
        break
    if not values:
        if not found_column:
            reasons.append("quote_date_unparseable")
        return "", list(dict.fromkeys(reasons))
    if len(values) > 1:
        reasons.append("quote_date_inconsistent")
    return sorted(values)[-1], list(dict.fromkeys(reasons))


def _evaluate(
    provider: str,
    frame: pd.DataFrame,
    metadata: Mapping[str, Any],
    target_date: str,
    baseline: set[str],
    minimum: float,
    min_valid_price_ratio: float,
) -> tuple[dict[str, Any], pd.DataFrame | None]:
    reasons: list[str] = []
    status = str(metadata.get("provider_status") or metadata.get("status") or "").strip().lower()
    if frame.empty:
        reasons.append("provider_empty")
    if "partial" in status:
        reasons.append("provider_partial")
    elif status not in _COMPLETE_STATUSES:
        reasons.append("provider_status_not_complete")
    column = _symbol_column(frame)
    normalized: list[str] = []
    if column is None:
        reasons.append("symbol_column_missing")
    else:
        normalized = [_symbol(item) for item in frame[column]]
        if any(not item for item in normalized):
            reasons.append("invalid_symbol")
        usable = [item for item in normalized if item]
        if len(usable) != len(set(usable)):
            reasons.append("duplicate_symbol")
    quoted, date_reasons = _quote_date(frame, metadata)
    reasons.extend(date_reasons)
    if quoted and quoted != target_date:
        reasons.append("quote_date_mismatch")

    expected_pages = _meta_int(metadata, ("expected_pages", "total_pages", "planned_pages", "page_count"))
    observed_pages = _meta_int(metadata, ("observed_pages", "fetched_pages", "completed_pages", "pages"))
    expected_pages = expected_pages if expected_pages is not None else (1 if len(frame) else 0)
    observed_pages = observed_pages if observed_pages is not None else (1 if len(frame) else 0)
    if len(frame) and (expected_pages <= 0 or observed_pages <= 0):
        reasons.append("page_evidence_invalid")
    if expected_pages > observed_pages:
        reasons.append("page_coverage_incomplete")
    explicit_expected_rows = _meta_int(metadata, ("expected_rows", "total_rows", "provider_total", "total"))
    observed_rows = _meta_int(metadata, ("observed_rows", "fetched_rows", "row_count"))
    observed_rows = observed_rows if observed_rows is not None else len(frame)
    expected_rows = explicit_expected_rows if explicit_expected_rows is not None else (len(baseline) or len(frame))
    if len(frame) and (expected_rows <= 0 or observed_rows <= 0):
        reasons.append("row_evidence_invalid")
    if explicit_expected_rows is not None and explicit_expected_rows > observed_rows:
        reasons.append("row_coverage_incomplete")

    symbol_set = {item for item in normalized if item}
    matched = len(symbol_set & baseline) if baseline else 0
    coverage = matched / len(baseline) if baseline else 0.0
    if not baseline:
        reasons.append("baseline_missing_or_empty")
    elif coverage < minimum:
        reasons.append("coverage_below_minimum")
    market_coverage, market_reasons = _market_coverage(
        baseline,
        symbol_set,
        minimum,
    )
    reasons.extend(market_reasons)

    price_column, valid_price_count, valid_price_ratio = _positive_price_evidence(
        frame
    )
    if not price_column:
        reasons.append("price_column_missing")
    elif valid_price_ratio < min_valid_price_ratio:
        reasons.append("valid_price_ratio_below_minimum")

    source = str(metadata.get("provider") or metadata.get("source") or provider).strip()
    if "source" in frame.columns:
        candidates = [str(item).strip() for item in frame["source"].dropna() if str(item).strip()]
        if candidates:
            source = candidates[0]
    reasons = list(dict.fromkeys(reasons))
    canonical = None
    if not reasons:
        canonical = frame.copy()
        canonical["symbol"] = normalized
        if column != "symbol":
            canonical = canonical.drop(columns=[column])
        canonical["quote_date"] = target_date
        if price_column and price_column != "price":
            canonical["price"] = pd.to_numeric(
                canonical[price_column],
                errors="coerce",
            )
        if "source" not in canonical.columns:
            canonical["source"] = source or provider
        canonical = canonical.sort_values("symbol", kind="stable").reset_index(drop=True)
    evidence = {
        "provider": provider,
        "source": source or provider,
        "provider_status": status or "missing",
        "status": "accepted" if not reasons else "rejected",
        "quote_date": quoted,
        "expected_pages": int(expected_pages),
        "observed_pages": int(observed_pages),
        "expected_rows": int(expected_rows),
        "observed_rows": int(observed_rows),
        "baseline_count": len(baseline),
        "observed_count": len(symbol_set),
        "matched_baseline_count": matched,
        "missing_baseline_count": max(0, len(baseline) - matched),
        "coverage_ratio": float(coverage),
        "market_coverage": market_coverage,
        "price_column": price_column,
        "valid_price_count": int(valid_price_count),
        "valid_price_ratio": float(valid_price_ratio),
        "min_valid_price_ratio": float(min_valid_price_ratio),
        "reason_codes": reasons,
    }
    warning = str(metadata.get("provider_warning") or metadata.get("warning") or "").strip()
    if warning:
        evidence["provider_warning"] = warning
    return evidence, canonical


def _csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    with temporary.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8"))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _publish(staged: Mapping[str, Path], targets: Mapping[str, Path], stage: Path) -> tuple[bool, str]:
    backups: dict[str, Path | None] = {}
    replaced: list[str] = []
    try:
        for key, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = stage / f"old_{key}{target.suffix}.backup"
                shutil.copyfile(target, backup)
                backups[key] = backup
            else:
                backups[key] = None
        for key in ("spot_snapshot", "low_price_universe", "spot_meta"):
            os.replace(staged[key], targets[key])
            replaced.append(key)
        return True, ""
    except Exception as exc:
        rollback_errors: list[str] = []
        for key in reversed(replaced):
            target, backup = targets[key], backups.get(key)
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, target)
            except Exception as rollback_exc:
                rollback_errors.append(f"{key}:{type(rollback_exc).__name__}")
        detail = f"{type(exc).__name__}:{exc}"
        if rollback_errors:
            detail += ";rollback_failed:" + ",".join(rollback_errors)
        return False, detail


def _base(
    target: str,
    network: bool,
    minimum: float,
    baseline_path: Path,
    baseline_count: int,
    paths: Mapping[str, Path],
    old: Mapping[str, str | None],
) -> dict[str, Any]:
    return {
        "ok": False, "status": "blocked", "formal_eligible": False,
        "stale": True, "preserved_previous": True,
        "network_authorized": bool(network), "target_date": target,
        "provider": "", "quote_date": "", "expected_pages": 0,
        "observed_pages": 0, "expected_rows": baseline_count,
        "observed_rows": 0, "baseline_path": str(baseline_path),
        "baseline_count": baseline_count, "observed_count": 0,
        "matched_baseline_count": 0, "coverage_ratio": 0.0,
        "min_coverage": minimum, "reason_codes": [], "provider_attempts": [],
        "output_paths": {key: str(value) for key, value in paths.items()},
        "old_sha256": dict(old), "new_sha256": dict(old),
        "old_pool_sha256": old.get(str(paths["low_price_universe"])),
        "new_pool_sha256": old.get(str(paths["low_price_universe"])),
        "candidate_sha256": {},
        "files": {
            key: {
                "path": str(path),
                "old_sha256": old[str(path)],
                "new_sha256": old[str(path)],
                "published": False,
            }
            for key, path in paths.items()
        },
        "research_only": True, "paper_only": True, "no_broker_route": True,
        "not_investment_advice": True,
    }


def _refresh_universe_snapshot_unlocked(
    project_root: str | Path,
    target_date: str | date | datetime,
    network: bool,
    fetchers: Mapping[str, Fetcher | None] | Sequence[Any] | Fetcher | None = None,
    output_paths: Mapping[str, str | Path] | Sequence[str | Path] | None = None,
    min_coverage: float = 0.90,
    baseline_path: str | Path | None = None,
    min_valid_price_ratio: float = 0.90,
) -> dict[str, Any]:
    """Publish a complete spot snapshot and its derivative universe, or block."""
    root = Path(project_root)
    paths = _output_paths(root, output_paths)
    old = {str(path): _sha(path) for path in paths.values()}
    target = _date(target_date)
    baseline_file = (_resolve(root, baseline_path) if baseline_path is not None else root / "data/universe/a_share_symbols.csv")
    baseline, baseline_reasons = _baseline(baseline_file)
    try:
        minimum = float(min_coverage)
    except (TypeError, ValueError):
        minimum = -1.0
    try:
        price_minimum = float(min_valid_price_ratio)
    except (TypeError, ValueError):
        price_minimum = -1.0
    payload = _base(target or str(target_date or ""), network, minimum, baseline_file, len(baseline), paths, old)
    payload.update(
        {
            "market_coverage": {},
            "price_column": "",
            "valid_price_count": 0,
            "valid_price_ratio": 0.0,
            "min_valid_price_ratio": price_minimum,
            "low_price_pool": {},
        }
    )
    if not target:
        payload["reason_codes"] = ["target_date_invalid"]
        return payload
    if not 0.0 <= minimum <= 1.0:
        payload["reason_codes"] = ["min_coverage_invalid"]
        return payload
    if not 0.0 <= price_minimum <= 1.0:
        payload["reason_codes"] = ["min_valid_price_ratio_invalid"]
        return payload
    if not network:
        payload["reason_codes"] = list(dict.fromkeys(["network_disabled", *baseline_reasons]))
        return payload

    attempts: list[dict[str, Any]] = []
    accepted: pd.DataFrame | None = None
    evidence: dict[str, Any] | None = None
    for provider, fetcher in _fetcher_list(fetchers):
        if fetcher is None:
            current = {
                "provider": provider, "status": "unavailable", "quote_date": "",
                "expected_pages": 0, "observed_pages": 0,
                "expected_rows": len(baseline), "observed_rows": 0,
                "baseline_count": len(baseline), "observed_count": 0,
                "coverage_ratio": 0.0, "reason_codes": ["provider_unavailable"],
            }
        else:
            try:
                frame, metadata = _result(_call(fetcher, target))
                current, canonical = _evaluate(
                    provider,
                    frame,
                    metadata,
                    target,
                    baseline,
                    minimum,
                    price_minimum,
                )
            except Exception as exc:
                current, canonical = {
                    "provider": provider, "status": "failed", "quote_date": "",
                    "expected_pages": 0, "observed_pages": 0,
                    "expected_rows": len(baseline), "observed_rows": 0,
                    "baseline_count": len(baseline), "observed_count": 0,
                    "coverage_ratio": 0.0, "reason_codes": ["provider_exception"],
                    "error": f"{type(exc).__name__}:{exc}",
                }, None
            if canonical is not None and baseline_reasons:
                current["reason_codes"] = list(
                    dict.fromkeys([*(current.get("reason_codes") or []), *baseline_reasons])
                )
                current["status"] = "rejected"
                canonical = None
            if canonical is not None:
                accepted, evidence = canonical, current
        attempts.append(current)
        if accepted is not None:
            break
    payload["provider_attempts"] = attempts
    if (accepted is None or evidence is None) and all(
        path.is_file() for path in paths.values()
    ):
        cache_meta: dict[str, Any] = {}
        cache_frame = pd.DataFrame()
        cache_reasons: list[str] = list(baseline_reasons)
        try:
            raw_meta = json.loads(paths["spot_meta"].read_text(encoding="utf-8"))
            if isinstance(raw_meta, Mapping):
                cache_meta = dict(raw_meta)
            else:
                cache_reasons.append("verified_cache_meta_invalid")
        except Exception:
            cache_reasons.append("verified_cache_meta_unreadable")
        try:
            cache_frame = pd.read_csv(paths["spot_snapshot"], encoding="utf-8-sig", dtype={"symbol": str})
        except Exception:
            cache_reasons.append("verified_cache_snapshot_unreadable")
        snapshot_sha = _sha(paths["spot_snapshot"])
        discovery_sha = _sha(paths["low_price_universe"])
        expected_snapshot_sha = str(cache_meta.get("snapshot_sha256") or "")
        expected_discovery_sha = str(cache_meta.get("low_price_universe_sha256") or "")
        if not (re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_sha) and snapshot_sha == expected_snapshot_sha):
            cache_reasons.append("verified_cache_snapshot_hash_mismatch")
        if not (re.fullmatch(r"[0-9a-f]{64}", expected_discovery_sha) and discovery_sha == expected_discovery_sha):
            cache_reasons.append("verified_cache_discovery_hash_mismatch")
        cache_evidence, cache_canonical = _evaluate(
            "local_verified_cache", cache_frame, cache_meta, target,
            baseline, minimum, price_minimum,
        )
        cache_evidence["provider"] = "local_verified_cache"
        cache_evidence["source"] = str(cache_meta.get("source") or "local_verified_cache")
        if cache_reasons:
            cache_evidence["reason_codes"] = list(dict.fromkeys(list(cache_evidence.get("reason_codes") or []) + cache_reasons))
            cache_evidence["status"] = "rejected"
            cache_canonical = None
        attempts.append(cache_evidence)
        payload["provider_attempts"] = attempts
        if cache_canonical is not None:
            for key in (
                "quote_date", "expected_pages", "observed_pages",
                "expected_rows", "observed_rows", "observed_count",
                "matched_baseline_count", "coverage_ratio", "market_coverage",
                "price_column", "valid_price_count", "valid_price_ratio",
                "min_valid_price_ratio",
            ):
                if key in cache_evidence:
                    payload[key] = cache_evidence[key]
            payload.update({
                "ok": True,
                "status": "completed",
                "formal_eligible": True,
                "stale": False,
                "preserved_previous": True,
                "provider": "local_verified_cache",
                "source": cache_evidence.get("source") or "local_verified_cache",
                "reason_codes": [],
                "warnings": ["live_provider_refresh_failed_used_verified_cache"],
                "new_sha256": dict(old),
            })
            return payload
    if accepted is None or evidence is None:
        reasons: list[str] = []
        for attempt in attempts:
            reasons.extend(attempt.get("reason_codes") or [])
        payload["reason_codes"] = list(dict.fromkeys([*reasons, *baseline_reasons] or ["provider_unavailable"]))
        if attempts:
            last = attempts[-1]
            for key in (
                "provider", "quote_date", "expected_pages", "observed_pages",
                "expected_rows", "observed_rows", "observed_count",
                "matched_baseline_count", "coverage_ratio", "market_coverage",
                "price_column", "valid_price_count", "valid_price_ratio",
                "min_valid_price_ratio",
            ):
                if key in last:
                    payload[key] = last[key]
        return payload
    for key in (
        "provider", "quote_date", "expected_pages", "observed_pages",
        "expected_rows", "observed_rows", "observed_count",
        "matched_baseline_count", "coverage_ratio", "market_coverage",
        "price_column", "valid_price_count", "valid_price_ratio",
        "min_valid_price_ratio",
    ):
        payload[key] = evidence[key]

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    low = build_low_price_universe_from_spot(accepted, updated_at=generated)
    included_count = (
        int(low["included"].astype(bool).sum())
        if "included" in low.columns
        else 0
    )
    buffer_count = (
        int(low["in_buffer"].astype(bool).sum())
        if "in_buffer" in low.columns
        else 0
    )
    candidate_active_count = included_count + buffer_count
    previous_active_count = _active_low_price_count(
        paths["low_price_universe"]
    )
    retention_ratio = (
        candidate_active_count / previous_active_count
        if previous_active_count
        else None
    )
    low_price_reasons: list[str] = []
    if candidate_active_count <= 0:
        low_price_reasons.append("low_price_pool_empty")
    if (
        previous_active_count > 0
        and retention_ratio is not None
        and retention_ratio < minimum
    ):
        low_price_reasons.append("low_price_pool_shrink_below_minimum")
    payload["low_price_pool"] = {
        "status": "blocked" if low_price_reasons else "passed",
        "previous_active_count": previous_active_count,
        "candidate_active_count": candidate_active_count,
        "included_count": included_count,
        "buffer_count": buffer_count,
        "retention_ratio": retention_ratio,
        "min_retention_ratio": minimum,
        "reason_codes": low_price_reasons,
    }
    if low_price_reasons:
        payload["reason_codes"] = low_price_reasons
        return payload

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    stage = root / "data/.universe_refresh_runs" / run_id
    staged = {
        "spot_snapshot": stage / "spot_snapshot_latest.csv",
        "spot_meta": stage / "spot_snapshot_meta.json",
        "low_price_universe": stage / "low_price_learning_symbols.csv",
    }
    try:
        stage.mkdir(parents=True, exist_ok=False)
        _csv(accepted, staged["spot_snapshot"])
        save_low_price_universe(low, staged["low_price_universe"])
        metadata = {
            "status": "completed", "run_id": run_id, "updated_at": generated,
            "target_date": target, "quote_date": evidence["quote_date"],
            "provider": evidence["provider"], "source": evidence.get("source") or evidence["provider"],
            "provider_status": evidence["provider_status"],
            "expected_pages": evidence["expected_pages"], "observed_pages": evidence["observed_pages"],
            "expected_rows": evidence["expected_rows"], "observed_rows": evidence["observed_rows"],
            "baseline_path": str(baseline_file), "baseline_count": evidence["baseline_count"],
            "observed_count": evidence["observed_count"], "matched_baseline_count": evidence["matched_baseline_count"],
            "coverage_ratio": evidence["coverage_ratio"], "min_coverage": minimum,
            "market_coverage": evidence["market_coverage"],
            "price_column": evidence["price_column"],
            "valid_price_count": evidence["valid_price_count"],
            "valid_price_ratio": evidence["valid_price_ratio"],
            "min_valid_price_ratio": price_minimum,
            "low_price_pool": payload["low_price_pool"],
            "snapshot_sha256": _sha(staged["spot_snapshot"]),
            "low_price_universe_sha256": _sha(staged["low_price_universe"]),
            "old_sha256": dict(old),
            "provider_attempts": attempts,
            "reason_codes": [],
            "research_only": True, "temporary_quote_only": True,
            "no_broker_route": True, "not_investment_advice": True,
        }
        _json(metadata, staged["spot_meta"])
        payload["candidate_sha256"] = {str(paths[key]): _sha(staged[key]) for key in staged}
        published, error = _publish(staged, paths, stage)
        if not published:
            after = {str(path): _sha(path) for path in paths.values()}
            payload["new_sha256"] = after
            payload["new_pool_sha256"] = after.get(str(paths["low_price_universe"]))
            payload["preserved_previous"] = after == old
            payload["reason_codes"] = ["atomic_publish_failed"]
            payload["publish_error"] = error
            if "rollback_failed" in error:
                payload["reason_codes"].append("atomic_rollback_failed")
            return payload
    except Exception as exc:
        after = {str(path): _sha(path) for path in paths.values()}
        payload["new_sha256"] = after
        payload["new_pool_sha256"] = after.get(str(paths["low_price_universe"]))
        payload["preserved_previous"] = after == old
        payload["reason_codes"] = ["staging_or_publish_failed"]
        payload["publish_error"] = f"{type(exc).__name__}:{exc}"
        return payload
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    new = {str(path): _sha(path) for path in paths.values()}
    payload.update({
        "ok": True, "status": "completed", "formal_eligible": True,
        "stale": False, "preserved_previous": False, "reason_codes": [],
        "run_id": run_id, "updated_at": generated, "new_sha256": new,
        "new_pool_sha256": new.get(str(paths["low_price_universe"])),
        "files": {
            key: {
                "path": str(path), "old_sha256": old[str(path)],
                "new_sha256": new[str(path)], "published": True,
            }
            for key, path in paths.items()
        },
    })
    return payload


def refresh_universe_snapshot(
    project_root: str | Path,
    target_date: str | date | datetime,
    network: bool,
    fetchers: Mapping[str, Fetcher | None] | Sequence[Any] | Fetcher | None = None,
    output_paths: Mapping[str, str | Path] | Sequence[str | Path] | None = None,
    min_coverage: float = 0.90,
    baseline_path: str | Path | None = None,
    min_valid_price_ratio: float = 0.90,
    lock_timeout_seconds: float = 0.0,
) -> dict[str, Any]:
    """Refresh under a process lock; busy callers preserve canonical bytes."""
    if not network:
        return _refresh_universe_snapshot_unlocked(
            project_root,
            target_date,
            network=False,
            fetchers=fetchers,
            output_paths=output_paths,
            min_coverage=min_coverage,
            baseline_path=baseline_path,
            min_valid_price_ratio=min_valid_price_ratio,
        )

    root = Path(project_root)
    lock_path = root / "state" / "universe_refresh.lock"
    try:
        with interprocess_file_lock(
            lock_path,
            timeout_seconds=max(0.0, float(lock_timeout_seconds)),
        ):
            return _refresh_universe_snapshot_unlocked(
                root,
                target_date,
                network=True,
                fetchers=fetchers,
                output_paths=output_paths,
                min_coverage=min_coverage,
                baseline_path=baseline_path,
                min_valid_price_ratio=min_valid_price_ratio,
            )
    except TimeoutError:
        paths = _output_paths(root, output_paths)
        old = {str(path): _sha(path) for path in paths.values()}
        target = _date(target_date)
        baseline_file = (
            _resolve(root, baseline_path)
            if baseline_path is not None
            else root / "data" / "universe" / "a_share_symbols.csv"
        )
        baseline, _ = _baseline(baseline_file)
        try:
            minimum = float(min_coverage)
        except (TypeError, ValueError):
            minimum = -1.0
        payload = _base(
            target or str(target_date or ""),
            True,
            minimum,
            baseline_file,
            len(baseline),
            paths,
            old,
        )
        payload.update(
            {
                "reason_codes": ["universe_refresh_busy"],
                "lock_path": str(lock_path),
                "market_coverage": {},
                "price_column": "",
                "valid_price_count": 0,
                "valid_price_ratio": 0.0,
                "min_valid_price_ratio": float(min_valid_price_ratio),
                "low_price_pool": {},
            }
        )
        return payload


__all__ = ["refresh_universe_snapshot", "build_default_fetchers", "restore_formal_learning_universe"]


def _invoke_request_get(
    request_get: Callable[..., Any],
    url: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> Any:
    """Call an injected requests-like function with small signature tolerance."""
    try:
        return request_get(url, params=params, headers=headers, timeout=timeout)
    except TypeError:
        try:
            return request_get(url, params=params, timeout=timeout)
        except TypeError:
            return request_get(url, params)


def _response_json(response: Any) -> Any:
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    if hasattr(response, "json"):
        return response.json()
    if isinstance(response, (Mapping, list)):
        return response
    raise RuntimeError("provider_response_has_no_json")


def _request_with_retry(
    request_get: Callable[..., Any],
    url: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
    retries: int,
    sleep: Callable[[float], Any],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        try:
            return _response_json(
                _invoke_request_get(request_get, url, params, headers, timeout)
            )
        except Exception as exc:  # noqa: BLE001 - evidence is returned by caller
            last_error = exc
            if attempt < retries:
                sleep(0.1 * (attempt + 1))
    raise last_error or RuntimeError("provider_request_failed")


def _timestamp_date(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(number) or number <= 0:
        return ""
    if number > 10_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _timestamp_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(number) or number <= 0:
        return ""
    if number > 10_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _annotate_provider_frame(
    rows: list[dict[str, Any]],
    *,
    provider: str,
    status: str,
    quote_dates: set[str],
    expected_pages: int,
    observed_pages: int,
    expected_rows: int,
    observed_rows: int,
    warning: str = "",
) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame.attrs.update(
        {
            "provider": provider,
            "provider_status": status,
            "formal_eligible": bool(status == "complete"),
            "quote_date": next(iter(quote_dates)) if len(quote_dates) == 1 else "",
            "expected_pages": int(max(0, expected_pages)),
            "observed_pages": int(max(0, observed_pages)),
            "expected_rows": int(max(0, expected_rows)),
            "observed_rows": int(max(0, observed_rows)),
        }
    )
    if warning:
        frame.attrs["provider_warning"] = warning[:1000]
    return frame


def _build_eastmoney_fetcher(
    *,
    request_get: Callable[..., Any],
    timeout_seconds: float,
    page_size: int,
    max_pages: int,
    retries: int,
    sleep: Callable[[float], Any],
) -> Fetcher:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    headers = {
        "User-Agent": "Mozilla/5.0 QuantDesk research client",
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    }
    fields = "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16,f124"

    def fetch(*, target_date: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        quote_dates: set[str] = set()
        expected_pages = 0
        observed_pages = 0
        expected_rows = 0
        warning = ""
        partial = False
        for page in range(1, max(1, int(max_pages)) + 1):
            params = {
                "pn": page,
                "pz": max(1, int(page_size)),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                # Eastmoney's m:0+t:81 segment carries the BSE 92xxxx
                # symbols; keep it explicit so 920xxx is not silently omitted.
                "fs": "m:0+t:6,m:0+t:80,m:0+t:81,m:1+t:2,m:1+t:23",
                "fields": fields,
            }
            try:
                payload = _request_with_retry(
                    request_get,
                    url,
                    params,
                    headers,
                    timeout_seconds,
                    retries,
                    sleep,
                )
            except Exception as exc:  # noqa: BLE001 - preserve partial evidence
                partial = True
                warning = f"page {page} request failed: {type(exc).__name__}:{exc}"
                expected_pages = max(expected_pages, page)
                break
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                partial = True
                warning = f"page {page} response has no data object"
                expected_pages = max(expected_pages, page)
                break
            diff = data.get("diff") or []
            if isinstance(diff, Mapping):
                diff = list(diff.values())
            if not isinstance(diff, list):
                partial = True
                warning = f"page {page} diff is not a list"
                expected_pages = max(expected_pages, page)
                break
            if page == 1:
                try:
                    expected_rows = int(float(data.get("total") or 0))
                except (TypeError, ValueError, OverflowError):
                    expected_rows = 0
                if expected_rows > 0:
                    expected_pages = max(
                        1,
                        int(math.ceil(expected_rows / max(1, int(page_size)))),
                    )
            if not diff:
                if expected_pages and page <= expected_pages:
                    partial = True
                break
            observed_pages += 1
            for item in diff:
                if not isinstance(item, Mapping):
                    continue
                quote_date = _timestamp_date(item.get("f124"))
                if quote_date:
                    quote_dates.add(quote_date)
                rows.append(
                    {
                        "symbol": item.get("f12"),
                        "name": str(item.get("f14") or "").strip(),
                        "price": item.get("f2"),
                        "pct_chg": item.get("f3"),
                        "change": item.get("f4"),
                        "volume": item.get("f5"),
                        "amount": item.get("f6"),
                        "open": item.get("f17"),
                        "pre_close": item.get("f18"),
                        "high": item.get("f15"),
                        "low": item.get("f16"),
                        "quote_time": (
                            _timestamp_text(item.get("f124"))
                            if _timestamp_date(item.get("f124"))
                            else ""
                        ),
                        "quote_date": quote_date,
                        "source": "eastmoney.spot_direct",
                    }
                )
            if expected_pages and page >= expected_pages:
                break
            if not expected_pages and len(diff) < max(1, int(page_size)):
                expected_pages = observed_pages
                break
        if expected_pages <= 0:
            expected_pages = observed_pages
        if expected_rows <= 0:
            expected_rows = len(rows)
        if expected_pages > observed_pages:
            partial = True
        status = "partial" if partial else ("complete" if rows else "empty")
        if not rows and not warning:
            warning = "eastmoney returned no rows"
        return _annotate_provider_frame(
            rows,
            provider="eastmoney",
            status=status,
            quote_dates=quote_dates,
            expected_pages=expected_pages,
            observed_pages=observed_pages,
            expected_rows=expected_rows,
            observed_rows=len(rows),
            warning=warning,
        )

    return fetch


def _sina_quote_day(value: Any) -> str:
    if isinstance(value, datetime):
        try:
            expected = expected_settlement_date(to_shanghai_datetime(value))
            if expected is not None:
                return expected.isoformat()
        except Exception:  # noqa: BLE001 - fall through to strict parser
            pass
    return _date(value)

def _build_sina_fetcher(
    *,
    request_get: Callable[..., Any],
    clock: Callable[[], Any],
    timeout_seconds: float,
    page_size: int,
    max_pages: int,
    retries: int,
    sleep: Callable[[float], Any],
) -> Fetcher:
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    headers = {"User-Agent": "Mozilla/5.0 QuantDesk research client"}

    def fetch(*, target_date: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        quote_dates: set[str] = set()
        observed_pages = 0
        expected_pages = 0
        observed_at = clock()
        market_session = (
            get_market_session(to_shanghai_datetime(observed_at))
            if isinstance(observed_at, datetime)
            else ""
        )
        intraday_snapshot = market_session in {
            "morning_session", "lunch_break", "afternoon_session"
        }
        warning = (
            "sina intraday snapshot is not settlement-eligible"
            if intraday_snapshot
            else ""
        )
        partial = intraday_snapshot
        terminated = False
        for page in range(1, max(1, int(max_pages)) + 1):
            params = {
                "page": page,
                "num": max(1, int(page_size)),
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "init",
            }
            try:
                payload = _request_with_retry(
                    request_get,
                    url,
                    params,
                    headers,
                    timeout_seconds,
                    retries,
                    sleep,
                )
            except Exception as exc:  # noqa: BLE001
                partial = True
                warning = f"page {page} request failed: {type(exc).__name__}:{exc}"
                expected_pages = max(expected_pages, page)
                terminated = True
                break
            if not isinstance(payload, list):
                partial = True
                warning = f"page {page} response is not a list"
                expected_pages = max(expected_pages, page)
                terminated = True
                break
            if not payload:
                expected_pages = observed_pages
                terminated = True
                break
            observed_pages += 1
            quote_day = _sina_quote_day(observed_at)
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                quote_date = quote_day if str(item.get("ticktime") or "").strip() else ""
                if quote_date:
                    quote_dates.add(quote_date)
                rows.append(
                    {
                        "symbol": item.get("code") or item.get("symbol"),
                        "name": str(item.get("name") or "").strip(),
                        "price": item.get("trade"),
                        "pct_chg": item.get("changepercent"),
                        "change": item.get("pricechange"),
                        "volume": item.get("volume"),
                        "amount": item.get("amount"),
                        "open": item.get("open"),
                        "pre_close": item.get("settlement"),
                        "high": item.get("high"),
                        "low": item.get("low"),
                        "quote_time": (
                            f"{quote_day} {item.get('ticktime')}"
                            if quote_day and item.get("ticktime")
                            else ""
                        ),
                        "quote_date": quote_date,
                        "source": "sina.spot_direct",
                    }
                )
            if len(payload) < max(1, int(page_size)):
                expected_pages = observed_pages
                terminated = True
                break
        if not terminated:
            partial = True
            expected_pages = max(expected_pages, observed_pages + 1)
            warning = warning or "sina max_pages reached before a short page"
        if expected_pages <= 0:
            expected_pages = observed_pages
        if expected_pages > observed_pages:
            partial = True
        status = "partial" if partial else ("complete" if rows else "empty")
        if not rows and not warning:
            warning = "sina returned no rows"
        return _annotate_provider_frame(
            rows,
            provider="sina",
            status=status,
            quote_dates=quote_dates,
            expected_pages=expected_pages,
            observed_pages=observed_pages,
            expected_rows=len(rows) + (max(1, int(page_size)) if partial else 0),
            observed_rows=len(rows),
            warning=warning,
        )

    return fetch


def build_default_fetchers(
    *,
    request_get: Callable[..., Any] | None = None,
    clock: Callable[[], Any] | None = None,
    sleep: Callable[[float], Any] | None = None,
    timeout_seconds: float = 8.0,
    page_size: int = 100,
    max_pages: int = 80,
    retries: int = 1,
) -> dict[str, Fetcher]:
    """Build explicit Eastmoney/Sina direct fetchers with auditable evidence.

    The returned functions do not perform a request until called.  Tests and
    offline callers can inject request_get/clock/sleep.  Callers must still pass
    network=True to refresh_universe_snapshot.
    """
    if request_get is None:
        def _lazy_request_get(*args: Any, **kwargs: Any) -> Any:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("requests is not installed") from exc
            return requests.get(*args, **kwargs)

        request_get = _lazy_request_get
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)
    if sleep is None:
        import time

        sleep = time.sleep
    common = {
        "request_get": request_get,
        "timeout_seconds": float(timeout_seconds),
        "page_size": max(1, int(page_size)),
        "max_pages": max(1, int(max_pages)),
        "retries": max(0, int(retries)),
        "sleep": sleep,
    }
    return {
        "eastmoney": _build_eastmoney_fetcher(**common),
        "sina": _build_sina_fetcher(clock=clock, **common),
    }

def restore_formal_learning_universe(
    project_root: str | Path,
    symbols: Sequence[Any],
    *,
    spot_snapshot_path: str | Path | None = None,
    output_path: str | Path | None = None,
    source: str = "explicit_formal_universe_contract",
) -> dict[str, Any]:
    """Restore a stable formal pool from an explicit, audited membership list.

    This function never derives membership from the current discovery snapshot.
    The spot snapshot only supplies display metadata.  Universe expansion must
    remain a separate, explicitly approved gate.
    """

    root = Path(project_root)
    target = (
        _resolve(root, output_path)
        if output_path is not None
        else root / "data" / "universe" / "low_price_learning_symbols.csv"
    )
    spot_path = (
        _resolve(root, spot_snapshot_path)
        if spot_snapshot_path is not None
        else root / "data" / "cache" / "spot_snapshot_latest.csv"
    )
    normalized = sorted({_symbol(item) for item in symbols if _symbol(item)})
    invalid_count = sum(1 for item in symbols if not _symbol(item))
    before = _sha(target)
    if not normalized:
        return {
            "ok": False,
            "status": "blocked",
            "reason_codes": ["formal_membership_empty"],
            "symbol_count": 0,
            "invalid_symbol_count": invalid_count,
            "output_path": str(target),
            "old_sha256": before,
            "new_sha256": before,
            "formal_pool_preserved": True,
        }

    spot = pd.DataFrame()
    spot_error = ""
    if spot_path.exists():
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                spot = pd.read_csv(spot_path, encoding=encoding, dtype={"symbol": str})
                break
            except Exception as exc:  # noqa: BLE001
                spot_error = f"{type(exc).__name__}:{exc}"[:500]
    else:
        spot_error = "spot_snapshot_missing"
    spot_by_symbol: dict[str, Mapping[str, Any]] = {}
    if not spot.empty:
        column = _symbol_column(spot)
        if column is not None:
            for _, row in spot.iterrows():
                normalized_symbol = _symbol(row.get(column))
                if normalized_symbol and normalized_symbol not in spot_by_symbol:
                    spot_by_symbol[normalized_symbol] = row.to_dict()

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    missing_spot: list[str] = []
    for symbol in normalized:
        metadata = dict(spot_by_symbol.get(symbol) or {})
        if not metadata:
            missing_spot.append(symbol)
        name = str(metadata.get("name") or metadata.get("名称") or "").strip()
        price = pd.to_numeric(pd.Series([metadata.get("price")]), errors="coerce").iloc[0]
        amount = pd.to_numeric(pd.Series([metadata.get("amount")]), errors="coerce").iloc[0]
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "price": price,
                "amount": amount,
                "included": True,
                "in_buffer": False,
                "excluded_reason": "",
                "source": str(source or "explicit_formal_universe_contract"),
                "updated_at": generated_at,
            }
        )
    frame = pd.DataFrame(rows)
    save_low_price_universe(frame, target)
    after = _sha(target)
    return {
        "ok": True,
        "status": "completed",
        "reason_codes": [],
        "symbol_count": len(normalized),
        "symbols": normalized,
        "invalid_symbol_count": invalid_count,
        "missing_spot_count": len(missing_spot),
        "missing_spot_symbols": missing_spot,
        "spot_snapshot_path": str(spot_path),
        "spot_snapshot_sha256": _sha(spot_path),
        "spot_snapshot_error": spot_error,
        "output_path": str(target),
        "old_sha256": before,
        "new_sha256": after,
        "formal_pool_preserved": False,
        "membership_source": str(source or "explicit_formal_universe_contract"),
        "expansion_status": "formal_membership_restored_no_auto_expansion",
        "updated_at": generated_at,
        "research_only": True,
        "no_broker_route": True,
    }
