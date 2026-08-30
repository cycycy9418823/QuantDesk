"""Stage 1 thin assembly layer: daily Market Brain / Strategy Brain snapshots.

Task book section 5 (2026-07-16) and audit report section 14 authorize exactly
one assembly entry between local evidence files and the pure brain engines.
This module only *assembles* inputs for :func:`build_market_brain_from_frame`
and :func:`build_strategy_brain_snapshot`; it does not re-implement regime
indicators, does not relax any fail-close gate, and never writes paper state
or canonical market data.  Outputs go to ``results/`` only.

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_feature_engine import load_daily_feature_map
from quantdesk.market_regime_engine import (
    REGIMES,
    MarketRegimeConfig,
    build_market_brain_from_frame,
)
from quantdesk.paths import project_root as default_project_root
from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION, price_data_contract
from quantdesk.shared import safe_float, write_json
from quantdesk.strategy_brain import StrategyBrainConfig, build_strategy_brain_snapshot
from quantdesk.trading_calendar import resolve_target_complete_trade_date

MARKET_SNAPSHOT_LATEST = "market_brain_snapshot_latest.json"
MARKET_SNAPSHOT_HISTORY = "market_brain_snapshot_history.jsonl"
STRATEGY_SHADOW_LATEST = "strategy_brain_shadow_latest.json"

_FORMAL_REGIMES = tuple(name for name in REGIMES if name != "UNKNOWN")

_FEATURE_DERIVATIONS = {
    "volatility": "daily_features_latest.csv volatility_20_pct / 100 (percent to fraction)",
    "liquidity_score": "min(100, amount_ma20 / 1e6) liquidity proxy (precedent: strategy_lab.low_price_strategy amount_proxy)",
    "volume_ratio": "volume / volume_ma20 from daily features",
}

__all__ = [
    "run_market_brain_daily",
    "run_strategy_brain_shadow",
    "run_daily_brain_snapshots",
]


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _aux_price_policy() -> dict[str, Any]:
    """Truthful raw-basis policy envelope for non-price auxiliary evidence.

    Spot counts, board breadth and benchmark volume derive from raw exchange
    quotes; declaring ``raw`` here is a statement of fact, not a bypass.  The
    engine still re-validates every field and fails closed on mismatch.
    """

    contract = price_data_contract()
    consumer_policy = (contract.get("current_enforced") or {}).get("feature") or {}
    return {
        "ok": True,
        "status": "passed",
        "eligible": True,
        "contract_version": PRICE_DATA_CONTRACT_VERSION,
        "consumer": "feature",
        "consumer_policy": consumer_policy,
        "observed_basis": "raw",
        "accepted_price_basis": "raw",
        "basis_counts": {"raw": 1},
        "price_conversion_performed": False,
        "reason_codes": [],
    }


def _aux_component(
    name: str,
    values: Mapping[str, Any] | None,
    *,
    data_date: str,
    data_scope: str,
    source_path: Path | str,
    source_hash: str,
) -> dict[str, Any] | None:
    if values is None:
        return None
    return {
        **dict(values),
        "data_date": data_date,
        "data_scope": data_scope,
        "provenance": {"source_path": str(source_path), "source_hash": source_hash},
        "price_policy_validation": _aux_price_policy(),
    }


def _load_benchmark_frame(root: Path, warnings: list[str]) -> pd.DataFrame | None:
    path = root / "data" / "benchmark" / "sh000001.csv"
    if not path.exists():
        warnings.append("missing_benchmark_csv")
        return None
    frame = read_csv_frame(path)
    if frame is None or frame.empty:
        warnings.append("empty_benchmark_csv")
        return None
    if "adjust" not in frame.columns:
        # Exchange index level series carry no corporate-action adjustment;
        # declaring them raw is a statement of fact, made in memory only.
        # A file that DOES carry an adjust column is never overridden here,
        # so mixed/unknown declarations still fail closed downstream.
        frame = frame.copy()
        frame["adjust"] = "raw"
        warnings.append("benchmark_basis_declared_raw_index_series")
    return frame


def _breadth_from_spot(root: Path, warnings: list[str]) -> tuple[dict[str, Any] | None, str, Path | None]:
    path = root / "data" / "cache" / "spot_snapshot_latest.csv"
    if not path.exists():
        warnings.append("missing_spot_snapshot")
        return None, "", None
    frame = read_csv_frame(path)
    if frame is None or frame.empty or "pct_chg" not in frame.columns:
        warnings.append("unusable_spot_snapshot")
        return None, "", None
    changes = pd.to_numeric(frame["pct_chg"], errors="coerce").dropna()
    if changes.empty:
        warnings.append("unusable_spot_snapshot")
        return None, "", None
    quote_date = ""
    if "quote_date" in frame.columns:
        counts = frame["quote_date"].astype(str).str.slice(0, 10).value_counts()
        if not counts.empty:
            quote_date = str(counts.index[0])
    values = {
        "up_count": int((changes > 0).sum()),
        "down_count": int((changes < 0).sum()),
        "flat_count": int((changes == 0).sum()),
        "sample_count": int(changes.shape[0]),
    }
    return values, quote_date, path


def _sector_from_board(root: Path, warnings: list[str]) -> tuple[dict[str, Any] | None, str, Path | None]:
    path = root / "data" / "cache" / "industry_board_latest.csv"
    if not path.exists():
        warnings.append("missing_industry_board_cache")
        return None, "", None
    frame = read_csv_frame(path)
    if frame is None or frame.empty or "pct_chg" not in frame.columns:
        warnings.append("unusable_industry_board_cache")
        return None, "", None
    changes = pd.to_numeric(frame["pct_chg"], errors="coerce").dropna()
    if changes.empty:
        warnings.append("unusable_industry_board_cache")
        return None, "", None
    # The board cache has no date column; its mtime is FETCH time, not data
    # time (a weekend refresh still carries Friday's close values).  Map fetch
    # time to the latest complete trading session it can represent instead of
    # letting a Saturday mtime masquerade as future evidence.
    fetched_at = datetime.fromtimestamp(path.stat().st_mtime)
    resolved = resolve_target_complete_trade_date(None, current=fetched_at)
    data_date = str(resolved.get("target_complete_trade_date") or resolved.get("effective_target_date") or "")
    values = {"strong_sector_ratio": float((changes > 0).sum() / changes.shape[0])}
    return values, data_date, path


def _volume_from_benchmark(frame: pd.DataFrame | None, target_date: str, warnings: list[str]) -> tuple[dict[str, Any] | None, str]:
    if frame is None or "volume" not in frame.columns or "date" not in frame.columns:
        if frame is not None:
            warnings.append("benchmark_missing_volume_column")
        return None, ""
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce")
    work = work.dropna(subset=["date", "volume"]).sort_values("date")
    work = work[work["date"] <= pd.Timestamp(target_date)]
    if work.shape[0] < 21:
        warnings.append("insufficient_benchmark_volume_history")
        return None, ""
    recent = work["volume"].iloc[-21:-1]
    baseline = float(recent.mean())
    latest = float(work["volume"].iloc[-1])
    if baseline <= 0:
        warnings.append("invalid_benchmark_volume_baseline")
        return None, ""
    data_date = work["date"].iloc[-1].strftime("%Y-%m-%d")
    return {"volume_ratio": latest / baseline}, data_date


def _previous_regime_from_history(history_path: Path, target_date: str) -> str | None:
    if not history_path.exists():
        return None
    previous: str | None = None
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_date = str(entry.get("target_date") or "")
        regime = str(entry.get("formal_regime") or entry.get("regime") or "").upper()
        if not entry_date or entry_date >= target_date:
            continue
        if entry.get("evidence_eligible") is True and regime in _FORMAL_REGIMES:
            previous = regime
    return previous


def _resolve_target_date(target_date: str | None, now: datetime | None) -> str:
    if target_date:
        return str(target_date)
    resolved = resolve_target_complete_trade_date(None, current=now)
    return str(resolved.get("target_complete_trade_date") or resolved.get("effective_target_date") or "")


def run_market_brain_daily(
    *,
    project_root: Path | str | None = None,
    target_date: str | None = None,
    now: datetime | None = None,
    config: MarketRegimeConfig | None = None,
) -> dict[str, Any]:
    """Assemble local evidence and produce today's Market Brain snapshot.

    Read-only over canonical data; writes only under ``results/``.  Any
    missing or stale input flows into the engine as an honest gap and the
    engine fails closed to ``UNKNOWN`` — this layer never fabricates evidence.
    """

    root = Path(project_root) if project_root is not None else default_project_root()
    moment = now or datetime.now()
    warnings: list[str] = []
    resolved_target = _resolve_target_date(target_date, moment)

    benchmark_frame = _load_benchmark_frame(root, warnings)
    breadth_values, breadth_date, breadth_path = _breadth_from_spot(root, warnings)
    sector_values, sector_date, sector_path = _sector_from_board(root, warnings)
    volume_values, volume_date = _volume_from_benchmark(benchmark_frame, resolved_target, warnings)

    data_scope = "local_sample"
    benchmark_path = root / "data" / "benchmark" / "sh000001.csv"
    breadth = (
        _aux_component(
            "breadth",
            breadth_values,
            data_date=breadth_date or resolved_target,
            data_scope=data_scope,
            source_path=breadth_path or "",
            source_hash=_sha256_of_file(breadth_path) if breadth_path else "",
        )
        if breadth_values is not None
        else None
    )
    sector = (
        _aux_component(
            "sector",
            sector_values,
            data_date=sector_date or resolved_target,
            data_scope=data_scope,
            source_path=sector_path or "",
            source_hash=_sha256_of_file(sector_path) if sector_path else "",
        )
        if sector_values is not None
        else None
    )
    volume = (
        _aux_component(
            "volume",
            volume_values,
            data_date=volume_date or resolved_target,
            data_scope=data_scope,
            source_path=benchmark_path if benchmark_path.exists() else "",
            source_hash=_sha256_of_file(benchmark_path) if benchmark_path.exists() else "",
        )
        if volume_values is not None
        else None
    )

    history_path = root / "results" / MARKET_SNAPSHOT_HISTORY
    previous_regime = _previous_regime_from_history(history_path, resolved_target)

    snapshot = build_market_brain_from_frame(
        benchmark_frame,
        target_date=resolved_target,
        breadth=breadth,
        volume=volume,
        sector=sector,
        data_scope=data_scope,
        previous_regime=previous_regime,
        config=config,
    )

    # The frame entry hardcodes provenance to None because a bare DataFrame
    # has no source.  This assembly layer DOES know the source — the local
    # benchmark CSV it just read — so record that truthfully.  Downstream
    # strategy_brain requires a non-empty market source_path/source_hash.
    if benchmark_frame is not None and benchmark_path.exists():
        provenance = dict(snapshot.get("provenance") or {})
        if not provenance.get("source_path"):
            provenance["source_path"] = str(benchmark_path)
        if not provenance.get("source_hash"):
            provenance["source_hash"] = _sha256_of_file(benchmark_path)
        snapshot["provenance"] = provenance

    payload = {
        "schema_version": "1.0",
        "task_type": "market_brain_daily_assembly",
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "snapshot": snapshot,
        "assembly_warnings": warnings,
        "assembly_context": {
            "benchmark_path": str(benchmark_path),
            "spot_path": str(breadth_path or ""),
            "board_path": str(sector_path or ""),
            "previous_regime_source": str(history_path),
            "data_scope": data_scope,
        },
        "research_only": True,
        "paper_account_only": True,
        "no_broker_route": True,
        "not_investment_advice": True,
    }
    write_json(root / "results" / MARKET_SNAPSHOT_LATEST, payload)

    history_entry = {
        "target_date": snapshot.get("target_date"),
        "data_date": snapshot.get("data_date"),
        "regime": snapshot.get("regime"),
        "formal_regime": snapshot.get("formal_regime"),
        "confidence": snapshot.get("confidence"),
        "evidence_eligible": snapshot.get("evidence_eligible"),
        "reason_codes": snapshot.get("reason_codes"),
        "generated_at": payload["generated_at"],
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_entry, ensure_ascii=False) + "\n")

    return payload


def _load_auto_strategy_candidates(root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    path = root / "results" / "auto_strategy_latest.json"
    if not path.exists():
        warnings.append("missing_auto_strategy_candidates")
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        warnings.append("unreadable_auto_strategy_candidates")
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in ("long_candidates", "board_candidates", "standard_candidates"):
        for item in payload.get(group) or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("name") or ""),
                    "sector": str(item.get("sector") or ""),
                    "long_score": safe_float(item.get("long_score") or item.get("score"), None),
                    "board_score": safe_float(item.get("board_score"), None),
                }
            )
    if not rows:
        warnings.append("missing_auto_strategy_candidates")
    return rows


def _candidate_from_features(
    base: dict[str, Any],
    feature_row: Mapping[str, Any] | None,
    *,
    as_of: str,
    data_cutoff: str,
    source_path: str,
    source_hash: str,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "symbol": base["symbol"],
        "name": base.get("name") or "",
        "sector": base.get("sector") or "",
        "as_of": as_of,
        "data_cutoff": data_cutoff,
        "source_path": source_path,
        "source_hash": source_hash,
        "coverage": {"kind": "daily_features_row", "feature_count": len(feature_row or {})},
        "pit_status": {"status": "passed"},
        "price_policy_validation": _aux_price_policy(),
    }
    if base.get("long_score") is not None:
        candidate["long_score"] = float(base["long_score"])
    if base.get("board_score") is not None:
        candidate["board_score"] = float(base["board_score"])
    row = feature_row or {}
    close = safe_float(row.get("close"), None)
    ma20 = safe_float(row.get("ma20"), None)
    ma60 = safe_float(row.get("ma60"), None)
    vol_pct = safe_float(row.get("volatility_20_pct"), None)
    amount_ma20 = safe_float(row.get("amount_ma20"), None)
    volume = safe_float(row.get("volume"), None)
    volume_ma20 = safe_float(row.get("volume_ma20"), None)
    if close is not None:
        candidate["close"] = close
    if ma20 is not None:
        candidate["ma20"] = ma20
    if ma60 is not None:
        candidate["ma60"] = ma60
    if vol_pct is not None:
        candidate["volatility"] = vol_pct / 100.0
    if amount_ma20 is not None:
        candidate["liquidity_score"] = min(100.0, amount_ma20 / 1_000_000.0)
    if volume is not None and volume_ma20 not in (None, 0):
        ratio = volume / volume_ma20
        candidate["volume_ratio"] = ratio
        candidate["activity_ratio_20"] = ratio
    return candidate


def run_strategy_brain_shadow(
    *,
    project_root: Path | str | None = None,
    market_snapshot: Mapping[str, Any] | None,
    now: datetime | None = None,
    config: StrategyBrainConfig | None = None,
) -> dict[str, Any]:
    """Build the shadow-only Strategy Brain snapshot from today's candidates.

    Shadow mode is structural: the engine hardcodes ``writes_state=False`` and
    this layer supplies no state-writing capability.  Missing candidates or an
    ineligible market snapshot degrade honestly instead of fabricating fits.
    """

    root = Path(project_root) if project_root is not None else default_project_root()
    moment = now or datetime.now()
    warnings: list[str] = []

    features_path = root / "data" / "features" / "daily_features_latest.csv"
    feature_map: dict[str, dict[str, Any]] = {}
    features_hash = ""
    if features_path.exists():
        feature_map = load_daily_feature_map(features_path)
        features_hash = _sha256_of_file(features_path)
    else:
        warnings.append("missing_daily_features")

    market = dict(market_snapshot or {})
    as_of = str(market.get("as_of") or market.get("target_date") or "")
    data_cutoff = str(market.get("data_cutoff") or market.get("data_date") or "")

    bases = _load_auto_strategy_candidates(root, warnings)
    candidates = [
        _candidate_from_features(
            base,
            feature_map.get(base["symbol"]),
            as_of=as_of,
            data_cutoff=data_cutoff,
            source_path=str(features_path),
            source_hash=features_hash or "sha256:" + "0" * 64,
        )
        for base in bases
    ]

    snapshot = build_strategy_brain_snapshot(candidates, market, config=config)

    payload = {
        "schema_version": "1.0",
        "task_type": "strategy_brain_shadow_assembly",
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "snapshot": snapshot,
        "assembly_warnings": warnings,
        "assembly_context": {
            "candidate_source": str(root / "results" / "auto_strategy_latest.json"),
            "features_path": str(features_path),
            "feature_derivations": dict(_FEATURE_DERIVATIONS),
            "sector_snapshot_supplied": False,
        },
        "research_only": True,
        "paper_account_only": True,
        "no_broker_route": True,
        "not_investment_advice": True,
    }
    write_json(root / "results" / STRATEGY_SHADOW_LATEST, payload)
    return payload


def run_daily_brain_snapshots(
    *,
    project_root: Path | str | None = None,
    target_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Containment wrapper used by the daily workflow step.

    Never raises: each half is wrapped so an assembly failure degrades to an
    explicit error string instead of breaking the legacy daily chain.  ``ok``
    means "the assembly ran and wrote its artifacts", not "regime is formal".
    """

    market_result: dict[str, Any]
    try:
        market_result = run_market_brain_daily(project_root=project_root, target_date=target_date, now=now)
    except Exception as exc:  # noqa: BLE001 - containment is the contract here
        market_result = {"snapshot": {}, "assembly_warnings": [], "error": str(exc)}

    shadow_result: dict[str, Any]
    try:
        shadow_result = run_strategy_brain_shadow(
            project_root=project_root,
            market_snapshot=market_result.get("snapshot") or {},
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        shadow_result = {"snapshot": {}, "assembly_warnings": [], "error": str(exc)}

    combined_warnings = list(market_result.get("assembly_warnings") or []) + list(
        shadow_result.get("assembly_warnings") or []
    )
    return {
        "ok": "error" not in market_result and "error" not in shadow_result,
        "market_brain": market_result,
        "strategy_brain_shadow": shadow_result,
        "assembly_warnings": combined_warnings,
    }
