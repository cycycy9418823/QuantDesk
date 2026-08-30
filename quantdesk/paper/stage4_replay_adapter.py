"""Offline adapter between Stage 4 tri-account replay and evidence consumers.

The adapter has two deliberately separate modes:

* ``real_canonical`` accepts only local canonical evidence with verified file
  hashes and can be exposed by the maintenance collector.
* ``synthetic_fixture`` exists only for deterministic contract tests and is
  permanently ineligible for maintenance or formal conclusions.

It never performs network I/O, writes a funded ledger, or routes an order.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_truth.pit_sector_status import build_pit_sector_status
from quantdesk.data_truth.pit_st_delist_loader import load_st_delist_data
from quantdesk.data_truth.sector_history_archive import verify_snapshot_self_hash
from quantdesk.intraday.minute_pool_window_audit import (
    audit_minute_pool_window,
    load_window_verified_minute_pool,
)
from quantdesk.paper.stage4_final_evaluator import build_synthetic_replay_fixture
from quantdesk.paper.stage4_tri_account_replay import (
    STRATEGY_BOARD,
    STRATEGY_LONG,
    _read_daily_enriched,
    replay_forward_account,
)
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.research.affordable_leaders_universe import (
    build_affordable_leaders_universe,
)
from quantdesk.shared import normalize_symbol
from quantdesk.trader_profile import (
    ResearchPaperProfile,
    load_research_paper_profile,
    validate_research_paper_profile,
)
from quantdesk.trading_calendar import trade_days_between
from quantdesk.utils import atomic_write_json

STAGE4_REPLAY_ADAPTER_SCHEMA_VERSION = "stage4_tri_account_replay_adapter_v1"
EXPECTED_ACCOUNT_IDS = ("A", "B", "C")
REAL_DATA_MODE = "real_canonical"
FIXTURE_DATA_MODE = "synthetic_fixture"
# The strict A/B/C account contract: pool name and strategy are pinned so a
# replay can never silently run on the wrong universe or signal family. The
# signal name is metadata for the experiment profile, not a trading decision.
ACCOUNT_REPLAY_CONTRACTS: dict[str, dict[str, str]] = {
    "A": {
        "universe_label": "low_price_learning_pool",
        "strategy": STRATEGY_LONG,
        "experiment_signal": "low_price_learning_signal",
    },
    "B": {
        "universe_label": "affordable_leaders_proxy_pool",
        "strategy": STRATEGY_LONG,
        "experiment_signal": "affordable_leaders_signal",
    },
    "C": {
        "universe_label": "board_channel_verified_minute_pool",
        "strategy": STRATEGY_BOARD,
        "experiment_signal": "board_channel_minute_signal",
    },
}
# Gates the maintenance collector requires before exposing real interim
# counts. The adapter owns the closed vocabulary; the maintenance section
# consumes it, so the two can never disagree about which gates exist.
MAINTENANCE_REPLAY_GATE_NAMES: tuple[str, ...] = (
    "shadow_counts",
    "t0_birth_evidence",
    "forward_replay_window",
)
STAGE4_REPLAY_RUN_STATE_SCHEMA_VERSION = "stage4_replay_run_state_v2"
_RUN_STEP_ORDER: tuple[str, ...] = (
    "pit_t0",
    "account_b_universe",
    "minute_window_audit",
    "account_replay_A",
    "account_replay_B",
    "account_replay_C",
    "account_replays",
    "source_sealing",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_manifest"):
        manifest = value.to_manifest()
        return dict(manifest) if isinstance(manifest, Mapping) else {}
    return {
        key: getattr(value, key)
        for key in (
            "account_id",
            "candidate_count",
            "fill_count",
            "stage_counts",
            "outcomes",
            "exit_stage_counts",
            "exit_outcomes",
            "warnings",
            "universe_label",
            "strategy",
            "closed_trades",
            "nav_series",
            "effective_sell_sample",
            "fixture_or_synthetic",
            "research_only",
            "paper_only",
            "no_broker",
            "is_real_order",
        )
        if hasattr(value, key)
    }


def _contains_synthetic_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("fixture_or_synthetic") is True or value.get("synthetic_test_data") is True:
            return True
        if value.get("fixture_kind"):
            return True
        return any(_contains_synthetic_marker(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_synthetic_marker(item) for item in value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered.startswith("synthetic://") or lowered.startswith("fixture://")
    return False


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    if len(text) != 71 or not text.startswith("sha256:"):
        return False
    return all(character in "0123456789abcdef" for character in text[7:].lower())


def _source_manifest_reasons(source_manifest: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if source_manifest.get("data_mode") != REAL_DATA_MODE:
        reasons.append("real_source_manifest_mode_invalid")
    if source_manifest.get("network_used") is not False:
        reasons.append("real_source_manifest_network_boundary_invalid")
    try:
        root = Path(str(source_manifest.get("project_root") or "")).resolve(strict=True)
    except (OSError, RuntimeError):
        return [*reasons, "real_source_project_root_invalid"]

    for key in ("daily_root", "minute_root"):
        try:
            path = Path(str(source_manifest.get(key) or "")).resolve(strict=True)
            path.relative_to(root)
            if not path.is_dir():
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            reasons.append(f"real_source_{key}_invalid")

    profile_paths = source_manifest.get("profile_paths")
    if not isinstance(profile_paths, Mapping) or set(profile_paths) != set(EXPECTED_ACCOUNT_IDS):
        reasons.append("real_source_profile_set_mismatch")

    declared_rows = source_manifest.get("source_files")
    if not isinstance(declared_rows, list) or not declared_rows:
        reasons.append("real_source_files_missing")
        return list(dict.fromkeys(reasons))
    declared_paths: set[Path] = set()
    for row in declared_rows:
        if not isinstance(row, Mapping):
            reasons.append("real_source_file_row_invalid")
            continue
        try:
            path = Path(str(row.get("path") or "")).resolve(strict=True)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            reasons.append("real_source_file_path_invalid")
            continue
        if not path.is_file():
            reasons.append("real_source_file_path_invalid")
            continue
        declared_paths.add(path)
        if str(row.get("sha256") or "") != _sha256(path):
            reasons.append("real_source_file_hash_mismatch")

    required_paths: list[Path] = []
    for value in [source_manifest.get("calendar_path")]:
        try:
            required_paths.append(Path(str(value or "")).resolve(strict=True))
        except (OSError, RuntimeError):
            reasons.append("real_source_calendar_path_invalid")
    if isinstance(profile_paths, Mapping):
        for value in profile_paths.values():
            try:
                required_paths.append(Path(str(value or "")).resolve(strict=True))
            except (OSError, RuntimeError):
                reasons.append("real_source_profile_path_invalid")
    if any(path not in declared_paths for path in required_paths):
        reasons.append("real_source_required_file_not_hashed")
    return list(dict.fromkeys(reasons))


def seal_real_source_manifest(
    manifest: Mapping[str, Any], accounts: Mapping[str, Any]
) -> dict[str, Any]:
    """Seal the manifest with recomputed account evidence-reference digests.

    For each expected account, non-empty ``evidence_ref`` strings are sorted,
    deduplicated, and hashed. Those aggregate digests replace any existing
    ``account_evidence_digests`` field before the resulting projection, except
    ``seal`` itself, is sealed. This does not verify the evidence content behind
    a reference or cover other mutable trade fields.
    """

    sealed = dict(manifest)
    account_evidence_digests: dict[str, str] = {}
    for account_id in EXPECTED_ACCOUNT_IDS:
        raw = _mapping(accounts.get(account_id))
        references: list[str] = []
        for row in (raw.get("closed_trades") or raw.get("trades") or []):
            if not isinstance(row, Mapping):
                continue
            reference = str(row.get("evidence_ref") or "")
            if reference:
                references.append(reference)
        references = sorted(dict.fromkeys(references))
        digest = hashlib.sha256(
            json.dumps(references, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        account_evidence_digests[account_id] = f"sha256:{digest}"
    sealed["account_evidence_digests"] = account_evidence_digests
    projection = {key: value for key, value in sealed.items() if key != "seal"}
    seal_digest = hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    sealed["seal"] = f"sha256:{seal_digest}"
    return sealed


def _failed_adapter_result(
    reason_codes: Sequence[str], *, source_manifest: Mapping[str, Any] | None = None
) -> "Stage4ReplayAdapterResult":
    source = dict(source_manifest or {})
    reasons = list(dict.fromkeys(str(code) for code in reason_codes if str(code)))
    return Stage4ReplayAdapterResult(
        status="quality_gate_failed",
        account_results={},
        replay_product={
            "trades": [],
            "nav_series": [],
            "effective_sell_sample": 0,
            "evidence_eligible": False,
            "fixture_or_synthetic": False,
            "reason_codes": reasons,
        },
        account_replay_products={},
        benchmark_by_date=None,
        quality_gate={"passed": False, "reason_codes": reasons},
        fixture_or_synthetic=False,
        source_manifest=source,
    )


@dataclass
class AdaptedAccountResult:
    account_id: str
    candidate_count: int
    fill_count: int
    stage_counts: dict[str, int]
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    nav_series: list[dict[str, Any]] = field(default_factory=list)
    fixture_or_synthetic: bool = False
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    exit_stage_counts: dict[str, int] = field(default_factory=dict)
    exit_outcomes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    universe_label: str = ""
    strategy: str = ""

    def to_manifest(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "candidate_count": self.candidate_count,
            "fill_count": self.fill_count,
            "stage_counts": dict(self.stage_counts),
            "closed_trades": list(self.closed_trades),
            "nav_series": list(self.nav_series),
            "fixture_or_synthetic": self.fixture_or_synthetic,
            "outcomes": list(self.outcomes),
            "exit_stage_counts": dict(self.exit_stage_counts),
            "exit_outcomes": list(self.exit_outcomes),
            "warnings": list(self.warnings),
            "universe_label": self.universe_label,
            "strategy": self.strategy,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
        }


@dataclass
class Stage4ReplayAdapterResult:
    status: str
    account_results: dict[str, AdaptedAccountResult]
    replay_product: dict[str, Any]
    account_replay_products: dict[str, dict[str, Any]]
    benchmark_by_date: dict[str, Any] | None
    quality_gate: dict[str, Any]
    fixture_or_synthetic: bool
    source_manifest: dict[str, Any]

    @property
    def maintenance_eligible(self) -> bool:
        return bool(
            self.quality_gate.get("passed") is True
            and self.fixture_or_synthetic is False
            and self.source_manifest.get("data_mode") == REAL_DATA_MODE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STAGE4_REPLAY_ADAPTER_SCHEMA_VERSION,
            "task_type": "stage4_tri_account_replay_adapter",
            "status": self.status,
            "quality_gate": dict(self.quality_gate),
            "account_results": {
                key: value.to_manifest() for key, value in self.account_results.items()
            },
            "replay_product": dict(self.replay_product),
            "account_replay_products": {
                key: dict(value) for key, value in self.account_replay_products.items()
            },
            "benchmark_by_date": (
                dict(self.benchmark_by_date) if self.benchmark_by_date is not None else None
            ),
            "fixture_or_synthetic": self.fixture_or_synthetic,
            "maintenance_eligible": self.maintenance_eligible,
            "source_manifest": dict(self.source_manifest),
            "network_used": False,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
        }


def _aggregate_nav(
    accounts: Mapping[str, AdaptedAccountResult], window_dates: Sequence[str]
) -> list[dict[str, Any]]:
    by_account: dict[str, dict[str, float]] = {}
    for account_id, account in accounts.items():
        series: dict[str, float] = {}
        for row in account.nav_series:
            if not isinstance(row, Mapping):
                continue
            day = str(row.get("date") or "")[:10]
            value = _finite(row.get("nav") if "nav" in row else row.get("value"))
            if day and value is not None:
                series[day] = value
        by_account[account_id] = series
    rows: list[dict[str, Any]] = []
    last: dict[str, float] = {}
    for day in window_dates:
        for account_id in accounts:
            if day in by_account[account_id]:
                last[account_id] = by_account[account_id][day]
        if len(last) == len(accounts):
            rows.append({"date": day, "nav": round(sum(last.values()), 8)})
    return rows


def adapt_stage4_replay(
    account_results: Mapping[str, Any],
    *,
    window_dates: Sequence[str],
    source_manifest: Mapping[str, Any],
    benchmark_by_date: Mapping[str, Any] | None = None,
) -> Stage4ReplayAdapterResult:
    """Validate and bridge tri-account outputs into interim/final contracts."""

    source = dict(source_manifest or {})
    data_mode = str(source.get("data_mode") or "")
    fixture_or_synthetic = data_mode == FIXTURE_DATA_MODE
    reasons: list[str] = []
    if set(account_results) != set(EXPECTED_ACCOUNT_IDS):
        reasons.append("tri_account_set_mismatch")
    if data_mode not in {REAL_DATA_MODE, FIXTURE_DATA_MODE}:
        reasons.append("adapter_data_mode_invalid")
    if data_mode == REAL_DATA_MODE:
        reasons.extend(_source_manifest_reasons(source))
        if _contains_synthetic_marker(account_results) or _contains_synthetic_marker(source):
            reasons.append("real_path_contains_synthetic_marker")
        declared_seal = str(source.get("seal") or "")
        declared_digests = source.get("account_evidence_digests")
        if not _is_sha256(declared_seal) or not isinstance(declared_digests, Mapping):
            reasons.append("real_source_manifest_seal_missing")
        else:
            resealed = seal_real_source_manifest(
                {key: value for key, value in source.items() if key != "seal"},
                account_results,
            )
            if resealed.get("seal") != declared_seal:
                reasons.append("real_source_manifest_seal_mismatch")
        profile_paths = source.get("profile_paths")
        if isinstance(profile_paths, Mapping):
            for account_id in EXPECTED_ACCOUNT_IDS:
                profile_path = Path(str(profile_paths.get(account_id) or ""))
                try:
                    raw_profile = json.loads(
                        profile_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    reasons.append(f"profile_{account_id}_content_invalid")
                    continue
                if not isinstance(raw_profile, Mapping) or str(
                    raw_profile.get("experiment_account") or ""
                ) != account_id:
                    # An empty or swapped profile (e.g. ``{}``) cannot be the
                    # account it claims to be; identity is part of the content
                    # contract, so it is a content failure, not a missing file.
                    reasons.append(f"profile_{account_id}_content_invalid")
                    continue
                try:
                    profile = load_research_paper_profile(profile_path)
                    validation = validate_research_paper_profile(profile)
                    if validation.get("ok") is not True:
                        reasons.append(f"profile_{account_id}_content_invalid")
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    reasons.append(f"profile_{account_id}_content_invalid")
        universes = source.get("account_universes")
        if not isinstance(universes, Mapping) or set(universes) != set(EXPECTED_ACCOUNT_IDS):
            reasons.append("real_source_account_universes_invalid")
        else:
            # Price basis is classified on the actual local daily frames the
            # replay consumed, before any filter/sort/dedupe/feature/write: a
            # frame whose basis cannot be classified poisons every candidate.
            daily_root = Path(str(source.get("daily_root") or ""))
            for account_id in EXPECTED_ACCOUNT_IDS:
                for symbol in (universes.get(account_id) or []):
                    normalized = normalize_symbol(symbol)
                    if not normalized:
                        continue
                    frame_path = daily_root / f"{normalized}.csv"
                    try:
                        frame = read_csv_frame(frame_path)
                    except Exception:  # noqa: BLE001 - unreadable means unclassifiable
                        frame = None
                    _basis, reason = classify_frame_price_basis(frame)
                    if reason:
                        reasons.append(reason)

    accounts: dict[str, AdaptedAccountResult] = {}
    account_products: dict[str, dict[str, Any]] = {}
    all_trades: list[dict[str, Any]] = []
    seen_trade_ids: set[str] = set()
    dates = [str(day)[:10] for day in window_dates if str(day)[:10]]
    if not dates or dates != sorted(dict.fromkeys(dates)):
        reasons.append("adapter_window_dates_invalid")

    for account_id in EXPECTED_ACCOUNT_IDS:
        raw = _mapping(account_results.get(account_id))
        if not raw:
            continue
        declared_id = str(raw.get("account_id") or account_id)
        if declared_id != account_id:
            reasons.append("account_identity_mismatch")
        candidate_count = _non_negative_int(raw.get("candidate_count"))
        fill_count = _non_negative_int(raw.get("fill_count"))
        raw_stage_counts = raw.get("stage_counts")
        stage_counts: dict[str, int] = {}
        if not isinstance(raw_stage_counts, Mapping):
            reasons.append("account_stage_counts_invalid")
        else:
            for key, value in raw_stage_counts.items():
                count = _non_negative_int(value)
                if count is None:
                    reasons.append("account_stage_counts_invalid")
                    continue
                stage_counts[str(key)] = count
        if candidate_count is None:
            candidate_count = 0
            reasons.append("account_candidate_count_invalid")
        if fill_count is None:
            fill_count = 0
            reasons.append("account_fill_count_invalid")
        if sum(stage_counts.values()) != candidate_count:
            reasons.append("account_candidate_stage_count_mismatch")
        if int(stage_counts.get("fill", 0)) != fill_count:
            reasons.append("account_fill_stage_count_mismatch")
        if data_mode == REAL_DATA_MODE and not (
            raw.get("fixture_or_synthetic") is False
            and raw.get("research_only") is True
            and raw.get("paper_only") is True
            and raw.get("no_broker") is True
            and raw.get("is_real_order") is False
        ):
            reasons.append("real_account_boundary_invalid")

        trades: list[dict[str, Any]] = []
        raw_trades = raw.get("closed_trades") or raw.get("trades") or []
        if not isinstance(raw_trades, list):
            reasons.append("account_closed_trades_invalid")
            raw_trades = []
        for index, item in enumerate(raw_trades):
            if not isinstance(item, Mapping):
                reasons.append("account_closed_trade_invalid")
                continue
            trade = dict(item)
            trade_id = str(trade.get("trade_id") or "")
            pnl = _finite(trade.get("pnl_pct") if "pnl_pct" in trade else trade.get("return_pct"))
            fees = _finite(trade.get("fees"))
            required = bool(
                trade_id
                and str(trade.get("symbol") or "")
                and str(trade.get("entry_date") or "")[:10]
                and str(trade.get("exit_date") or "")[:10]
                and _non_negative_int(trade.get("hold_days")) is not None
                and pnl is not None
                and fees is not None
                and fees >= 0
                and str(trade.get("evidence_ref") or "").startswith("sha256:")
            )
            if not required:
                reasons.append("account_closed_trade_invalid")
            if trade_id in seen_trade_ids:
                reasons.append("duplicate_trade_id")
            seen_trade_ids.add(trade_id)
            if data_mode == REAL_DATA_MODE and trade.get("evidence_eligible") is not True:
                reasons.append("real_trade_evidence_ineligible")
            if data_mode == REAL_DATA_MODE and not all(
                _is_sha256(trade.get(key))
                for key in (
                    "evidence_ref",
                    "entry_evidence_ref",
                    "exit_evidence_ref",
                    "entry_decision_digest",
                    "exit_decision_digest",
                )
            ):
                reasons.append("real_trade_evidence_chain_invalid")
            entry_day = str(trade.get("entry_date") or "")[:10]
            exit_day = str(trade.get("exit_date") or "")[:10]
            if (
                entry_day not in dates
                or exit_day not in dates
                or not entry_day < exit_day
            ):
                reasons.append("closed_trade_window_invalid")
            if data_mode == REAL_DATA_MODE and trade.get("price_basis") != "raw":
                reasons.append("real_trade_price_basis_invalid")
            trade["account_id"] = account_id
            trades.append(trade)
            all_trades.append(trade)

        raw_nav = raw.get("nav_series") or []
        nav_series = [dict(row) for row in raw_nav if isinstance(row, Mapping)]
        if not nav_series:
            reasons.append("account_nav_series_missing")
        nav_dates: list[str] = []
        for row in nav_series:
            day = str(row.get("date") or "")[:10]
            value = _finite(row.get("nav") if "nav" in row else row.get("value"))
            if day not in dates or value is None or value <= 0:
                reasons.append("account_nav_series_invalid")
            nav_dates.append(day)
        if nav_dates != sorted(dict.fromkeys(nav_dates)):
            reasons.append("account_nav_series_invalid")
        effective_sample = sum(
            _finite(row.get("pnl_pct") if "pnl_pct" in row else row.get("return_pct"))
            is not None
            for row in trades
        )
        declared_effective = raw.get("effective_sell_sample")
        if declared_effective is not None and _non_negative_int(declared_effective) != effective_sample:
            reasons.append("account_effective_sell_sample_mismatch")
        if len(trades) > fill_count:
            reasons.append("closed_trade_count_exceeds_fills")
        if data_mode == REAL_DATA_MODE:
            contract = ACCOUNT_REPLAY_CONTRACTS.get(account_id) or {}
            if str(raw.get("strategy") or "") != str(contract.get("strategy") or ""):
                reasons.append(f"account_strategy_mismatch:{account_id}")
            if str(raw.get("universe_label") or "") != str(
                contract.get("universe_label") or ""
            ):
                reasons.append(f"account_pool_name_mismatch:{account_id}")
        raw_outcomes = raw.get("outcomes") or []
        outcomes = [dict(row) for row in raw_outcomes if isinstance(row, Mapping)]
        if data_mode == REAL_DATA_MODE and len(outcomes) != candidate_count:
            reasons.append("real_candidate_outcome_count_mismatch")
        if data_mode == REAL_DATA_MODE:
            stage_histogram = {
                stage: sum(
                    1
                    for outcome in outcomes
                    if str(outcome.get("stage") or "") == stage
                )
                for stage in dict.fromkeys(
                    str(outcome.get("stage") or "") for outcome in outcomes
                )
            }
            if stage_histogram != stage_counts:
                reasons.append(f"account_outcome_stage_histogram_mismatch:{account_id}")
        for outcome in outcomes:
            if not (
                str(outcome.get("symbol") or "")
                and str(outcome.get("signal_date") or "")[:10]
                and str(outcome.get("stage") or "")
                and str(outcome.get("status") or "")
                and isinstance(outcome.get("reason_codes"), list)
            ):
                reasons.append("candidate_outcome_invalid")
        raw_exit_counts = raw.get("exit_stage_counts") or {}
        exit_stage_counts = (
            {
                str(key): int(value)
                for key, value in raw_exit_counts.items()
                if _non_negative_int(value) is not None
            }
            if isinstance(raw_exit_counts, Mapping)
            else {}
        )
        if not isinstance(raw_exit_counts, Mapping):
            reasons.append("exit_stage_counts_invalid")
        raw_exit_outcomes = raw.get("exit_outcomes") or []
        exit_outcomes = [
            dict(row) for row in raw_exit_outcomes if isinstance(row, Mapping)
        ]
        if sum(exit_stage_counts.values()) != len(exit_outcomes):
            reasons.append("exit_outcome_count_mismatch")
        account = AdaptedAccountResult(
            account_id=account_id,
            candidate_count=candidate_count,
            fill_count=fill_count,
            stage_counts=stage_counts,
            closed_trades=trades,
            nav_series=nav_series,
            fixture_or_synthetic=fixture_or_synthetic,
            outcomes=outcomes,
            exit_stage_counts=exit_stage_counts,
            exit_outcomes=exit_outcomes,
            warnings=[str(item) for item in raw.get("warnings") or []],
            universe_label=str(raw.get("universe_label") or ""),
            strategy=str(raw.get("strategy") or ""),
        )
        accounts[account_id] = account
        account_products[account_id] = {
            "account_id": account_id,
            "window_start": dates[0] if dates else None,
            "window_end": dates[-1] if dates else None,
            "window_dates": list(dates),
            "trades": trades,
            "nav_series": nav_series,
            "effective_sell_sample": effective_sample,
            "evidence_eligible": bool(trades)
            and all(row.get("evidence_eligible") is True for row in trades),
            "fixture_or_synthetic": fixture_or_synthetic,
            "source_path": (
                f"synthetic://stage4-replay-adapter/{account_id}"
                if fixture_or_synthetic
                else str(source.get("artifact_path") or source.get("project_root") or "")
            ),
            "reason_codes": [],
        }

    reasons = list(dict.fromkeys(reasons))
    passed = not reasons and set(accounts) == set(EXPECTED_ACCOUNT_IDS)
    aggregated_nav = _aggregate_nav(accounts, dates)
    replay_product: dict[str, Any] = {
        "window_start": dates[0] if dates else None,
        "window_end": dates[-1] if dates else None,
        "window_dates": list(dates),
        "as_of": dates[-1] if dates else None,
        "trades": all_trades,
        "nav_series": aggregated_nav,
        "effective_sell_sample": sum(
            _finite(row.get("pnl_pct") if "pnl_pct" in row else row.get("return_pct"))
            is not None
            for row in all_trades
        ),
        "evidence_eligible": bool(all_trades)
        and all(row.get("evidence_eligible") is True for row in all_trades)
        and passed,
        "fixture_or_synthetic": fixture_or_synthetic,
        "source_path": (
            "synthetic://stage4-replay-adapter/aggregate"
            if fixture_or_synthetic
            else str(source.get("artifact_path") or source.get("project_root") or "")
        ),
        "reason_codes": [],
    }
    if fixture_or_synthetic:
        replay_product["fixture_kind"] = str(source.get("fixture_kind") or "adapter_fixture")
        for product in account_products.values():
            product["fixture_kind"] = replay_product["fixture_kind"]

    return Stage4ReplayAdapterResult(
        status=(
            "fixture_ready"
            if passed and fixture_or_synthetic
            else "real_replay_ready"
            if passed
            else "quality_gate_failed"
        ),
        account_results=accounts,
        replay_product=replay_product,
        account_replay_products=account_products,
        benchmark_by_date=(
            dict(benchmark_by_date) if benchmark_by_date is not None else None
        ),
        quality_gate={"passed": passed, "reason_codes": reasons},
        fixture_or_synthetic=fixture_or_synthetic,
        source_manifest=source,
    )


def build_stage4_replay_adapter_fixture(
    *, kind: str, trade_count: int | None = None
) -> Stage4ReplayAdapterResult:
    """Build the evaluator's three fixture shapes through the adapter contract."""

    aggregate = build_synthetic_replay_fixture(kind=kind, trade_count=trade_count)
    trades = [dict(row) for row in aggregate.get("trades") or []]
    dates = list(aggregate.get("window_dates") or [])
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in EXPECTED_ACCOUNT_IDS}
    for index, trade in enumerate(trades):
        account_id = EXPECTED_ACCOUNT_IDS[index % len(EXPECTED_ACCOUNT_IDS)]
        trade["account_id"] = account_id
        buckets[account_id].append(trade)
    accounts: dict[str, dict[str, Any]] = {}
    for account_id in EXPECTED_ACCOUNT_IDS:
        account_trades = buckets[account_id]
        nav = 10_000.0
        nav_series = [{"date": dates[0], "nav": nav}] if dates else []
        for trade in account_trades:
            pnl = _finite(trade.get("pnl_pct")) or 0.0
            nav = round(nav * (1.0 + pnl), 8)
        if dates:
            nav_series.append({"date": dates[-1], "nav": nav})
        accounts[account_id] = {
            "account_id": account_id,
            "candidate_count": len(account_trades),
            "fill_count": len(account_trades),
            "stage_counts": {"fill": len(account_trades)},
            "closed_trades": account_trades,
            "nav_series": nav_series,
            "fixture_or_synthetic": True,
        }
    benchmark = None
    if kind != "benchmark_missing":
        benchmark = {day: 100.0 + index for index, day in enumerate(dates)}
    return adapt_stage4_replay(
        accounts,
        window_dates=dates,
        source_manifest={
            "data_mode": FIXTURE_DATA_MODE,
            "fixture_kind": kind,
            "network_used": False,
            "source_path": f"synthetic://stage4-replay-adapter/{kind}",
        },
        benchmark_by_date=benchmark,
    )


def _load_low_price_universe(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.is_file():
        return [], {}
    frame = read_csv_frame(path, dtype=str).fillna("")
    symbols: list[str] = []
    names: dict[str, str] = {}
    for _, row in frame.iterrows():
        if str(row.get("included") or "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "y",
            "t",
        }:
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        symbols.append(symbol)
        names[symbol] = str(row.get("name") or symbol)
    return list(dict.fromkeys(symbols)), names


def _load_profiles(profile_dir: Path) -> tuple[dict[str, Path], list[str]]:
    paths: dict[str, Path] = {}
    reasons: list[str] = []
    expected_universe = {
        "A": "low_price_learning_pool",
        "B": "affordable_leaders_proxy_pool",
        "C": "board_channel_verified_minute_pool",
    }
    for account_id in EXPECTED_ACCOUNT_IDS:
        path = profile_dir / f"exp_{account_id}_10k.json"
        paths[account_id] = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profile = load_research_paper_profile(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            reasons.append(f"profile_{account_id}_missing_or_invalid")
            continue
        validation = validate_research_paper_profile(profile)
        if validation.get("ok") is not True:
            reasons.append(f"profile_{account_id}_quality_gate_failed")
        if not isinstance(raw, Mapping) or raw.get("experiment_account") != account_id:
            reasons.append(f"profile_{account_id}_identity_mismatch")
        if list(profile.research_universe) != [expected_universe[account_id]]:
            reasons.append(f"profile_{account_id}_universe_mismatch")
        if profile.network_enabled or profile.llm_enabled or profile.full_a_scan_enabled:
            reasons.append(f"profile_{account_id}_offline_boundary_failed")
    return paths, list(dict.fromkeys(reasons))


def _load_validated_profile_objects(
    profile_paths: Mapping[str, Path],
) -> tuple[dict[str, ResearchPaperProfile], list[str]]:
    profiles: dict[str, ResearchPaperProfile] = {}
    reasons: list[str] = []
    for account_id in EXPECTED_ACCOUNT_IDS:
        path = profile_paths.get(account_id)
        if path is None:
            reasons.append(f"profile_{account_id}_object_missing")
            continue
        try:
            profile = load_research_paper_profile(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            reasons.append(f"profile_{account_id}_object_invalid")
            continue
        validation = validate_research_paper_profile(profile)
        if validation.get("ok") is not True:
            reasons.append(f"profile_{account_id}_object_invalid")
            continue
        profiles[account_id] = profile
    return profiles, list(dict.fromkeys(reasons))


def _load_sector_snapshots(
    history_root: Path, dates: Sequence[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Path], list[str]]:
    snapshots: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    reasons: list[str] = []
    for day in dates:
        path = history_root / day / "snapshot.json"
        revocation = path.parent / "revocation.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            reasons.append(f"sector_snapshot_missing_or_invalid:{day}")
            continue
        eligible = bool(
            isinstance(payload, Mapping)
            and payload.get("ok") is True
            and payload.get("status") == "passed"
            and payload.get("evidence_eligible") is True
            and payload.get("pit_eligible") is True
            and payload.get("target_date") == day
            and payload.get("market_as_of") == day
            and not payload.get("reason_codes")
            and not payload.get("degraded_reasons")
            and not revocation.exists()
            and verify_snapshot_self_hash(payload)
        )
        provenance = payload.get("provenance") if isinstance(payload, Mapping) else None
        if not (
            eligible
            and isinstance(provenance, Mapping)
            and provenance.get("source")
            and provenance.get("captured_at")
            and str(provenance.get("hash") or "").startswith("sha256:")
        ):
            reasons.append(f"sector_snapshot_quality_gate_failed:{day}")
            continue
        snapshots[day] = dict(payload)
        paths[day] = path
    return snapshots, paths, list(dict.fromkeys(reasons))


def _benchmark_by_date(path: Path, dates: Sequence[str]) -> dict[str, float] | None:
    if not path.is_file():
        return None
    try:
        frame = read_csv_frame(path)
    except Exception:
        return None
    if frame.empty:
        return None
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    date_col = next(
        (column for column in ("date", "trade_date", "datetime") if column in frame.columns),
        None,
    )
    value_col = next(
        (column for column in ("close", "value", "nav") if column in frame.columns),
        None,
    )
    if date_col is None or value_col is None:
        return None
    wanted = set(dates)
    out: dict[str, float] = {}
    for _, row in frame.iterrows():
        day = str(row.get(date_col) or "")[:10]
        value = _finite(row.get(value_col))
        if day in wanted and value is not None:
            out[day] = value
    return out or None


def _source_file_rows(paths: Sequence[Path]) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    reasons: list[str] = []
    seen: set[Path] = set()
    for raw in paths:
        try:
            path = Path(raw).resolve(strict=True)
        except (OSError, RuntimeError):
            reasons.append("adapter_source_file_missing")
            continue
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            reasons.append("adapter_source_file_missing")
            continue
        rows.append({"path": str(path), "sha256": _sha256(path)})
    rows.sort(key=lambda row: row["path"].lower())
    return rows, list(dict.fromkeys(reasons))


def _source_lock_reasons(
    locked_rows: Sequence[Mapping[str, Any]],
    *,
    expected_paths: Sequence[Path] | None = None,
) -> list[str]:
    """Verify that every locked input still has the captured content hash."""

    if not isinstance(locked_rows, Sequence) or not locked_rows:
        return ["adapter_source_lock_invalid"]
    locked_paths: set[str] = set()
    for row in locked_rows:
        path_text = str(row.get("path") or "")
        expected = str(row.get("sha256") or "")
        if not path_text or not _is_sha256(expected):
            return ["adapter_source_lock_invalid"]
        try:
            path = Path(path_text).resolve(strict=True)
        except (OSError, RuntimeError):
            return ["adapter_source_changed_during_run"]
        path_key = str(path).casefold()
        if path_key in locked_paths:
            return ["adapter_source_lock_invalid"]
        locked_paths.add(path_key)
        if not path.is_file() or _sha256(path) != expected:
            return ["adapter_source_changed_during_run"]
    if expected_paths is not None:
        expected_set: set[str] = set()
        for raw_path in expected_paths:
            try:
                expected_set.add(str(Path(raw_path).resolve(strict=True)).casefold())
            except (OSError, RuntimeError):
                return ["adapter_source_lock_invalid"]
        if locked_paths != expected_set:
            return ["adapter_source_lock_invalid"]
    return []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_mapping_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _directory_inventory_digest(path: Path, pattern: str) -> str:
    rows: list[tuple[str, int, int]] = []
    if path.is_dir():
        for item in sorted(path.glob(pattern), key=lambda value: value.name.lower()):
            try:
                stat = item.stat()
            except OSError:
                rows.append((item.name, -1, -1))
                continue
            rows.append((item.name, int(stat.st_size), int(stat.st_mtime_ns)))
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _run_input_fingerprint(
    *,
    root: Path,
    requested_window_start: str,
    requested_window_end: str,
    dates: Sequence[str],
    daily_root: Path,
    minute_root: Path,
    core_paths: Sequence[Path],
) -> tuple[str, dict[str, Any]]:
    files: list[dict[str, str]] = []
    seen: set[Path] = set()
    for raw_path in core_paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append({"path": str(resolved), "sha256": _sha256(resolved)})
    files.sort(key=lambda row: row["path"].lower())
    basis = {
        "project_root": str(root),
        "requested_window_start": str(requested_window_start),
        "requested_window_end": str(requested_window_end),
        "resolved_dates": [str(day)[:10] for day in dates],
        "core_files": files,
        "daily_inventory": _directory_inventory_digest(daily_root, "*.csv"),
        "minute_inventory": _directory_inventory_digest(minute_root, "*.csv"),
    }
    digest = hashlib.sha256(
        json.dumps(
            basis,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}", basis


def _new_or_resumed_run_state(
    *,
    path: Path,
    fingerprint: str,
    fingerprint_basis: Mapping[str, Any],
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    existing = _read_mapping_json(path)
    same_input = bool(existing and existing.get("input_fingerprint") == fingerprint)
    now = _utc_now()
    state: dict[str, Any] = {
        "schema_version": STAGE4_REPLAY_RUN_STATE_SCHEMA_VERSION,
        "run_id": f"stage4-replay-{fingerprint[7:19]}",
        "input_fingerprint": fingerprint,
        "input_fingerprint_basis": dict(fingerprint_basis),
        "window_start": str(window_start),
        "window_end": str(window_end),
        "status": "running",
        "step_status": "pending",
        "current_step": None,
        "last_completed_step": None,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "reason_codes": [],
        "error_type": None,
        "checkpoints": {},
        "resume_count": 0,
    }
    if same_input and existing is not None:
        state["started_at"] = existing.get("started_at") or now
        state["last_completed_step"] = existing.get("last_completed_step")
        checkpoints = existing.get("checkpoints")
        state["checkpoints"] = dict(checkpoints) if isinstance(checkpoints, Mapping) else {}
        if "source_lock" in existing:
            state["source_lock"] = existing.get("source_lock")
        elif any(
            step in state["checkpoints"]
            for step in (
                "account_replay_A",
                "account_replay_B",
                "account_replay_C",
                "account_replays",
                "source_sealing",
            )
        ):
            state["source_lock"] = {"invalid": True}
        try:
            state["resume_count"] = int(existing.get("resume_count") or 0) + 1
        except (TypeError, ValueError):
            state["resume_count"] = 1
        if existing.get("resume_invalidation_reason"):
            state["resume_invalidation_reason"] = existing["resume_invalidation_reason"]
    elif existing is not None:
        state["resume_invalidation_reason"] = "input_fingerprint_mismatch"
    atomic_write_json(path, state)
    return state


def _write_run_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    atomic_write_json(path, state)


def _artifact_rows(paths: Sequence[Path], *, target: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).resolve(strict=True)
        path.relative_to(target)
        if path in seen:
            continue
        seen.add(path)
        rows.append(
            {
                "path": str(path),
                "sha256": _sha256(path),
                "size": int(path.stat().st_size),
            }
        )
    rows.sort(key=lambda row: row["path"].lower())
    return rows


def _checkpoint_valid(state: Mapping[str, Any], step: str, *, target: Path) -> bool:
    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, Mapping):
        return False
    checkpoint = checkpoints.get(step)
    if not isinstance(checkpoint, Mapping) or checkpoint.get("status") != "completed":
        return False
    if checkpoint.get("input_fingerprint") != state.get("input_fingerprint"):
        return False
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for row in artifacts:
        if not isinstance(row, Mapping):
            return False
        try:
            path = Path(str(row.get("path") or "")).resolve(strict=True)
            path.relative_to(target)
        except (OSError, RuntimeError, ValueError):
            return False
        if not path.is_file():
            return False
        if row.get("sha256") != _sha256(path):
            return False
        declared_size = row.get("size")
        try:
            expected_size = int(declared_size)
        except (TypeError, ValueError):
            return False
        if expected_size != int(path.stat().st_size):
            return False
    return True


def _invalidate_checkpoints_from(
    state: dict[str, Any], step: str, *, reason: str
) -> None:
    try:
        start = _RUN_STEP_ORDER.index(step)
    except ValueError:
        start = 0
    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}
        state["checkpoints"] = checkpoints
    for name in _RUN_STEP_ORDER[start:]:
        checkpoints.pop(name, None)
    state["last_completed_step"] = (
        _RUN_STEP_ORDER[start - 1] if start > 0 else None
    )
    state["resume_invalidation_reason"] = reason


def _invalidate_account_replay_checkpoint(
    state: dict[str, Any], step: str, *, reason: str
) -> None:
    """Invalidate one independent account replay and its aggregate dependants."""

    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}
        state["checkpoints"] = checkpoints
    for name in (step, "account_replays", "source_sealing"):
        checkpoints.pop(name, None)
    state["last_completed_step"] = next(
        (
            name
            for name in reversed(_RUN_STEP_ORDER)
            if name in checkpoints
        ),
        None,
    )
    state["resume_invalidation_reason"] = reason


def _begin_step(path: Path, state: dict[str, Any], step: str) -> None:
    state["status"] = "running"
    state["step_status"] = "running"
    state["current_step"] = step
    state["reason_codes"] = []
    state["error_type"] = None
    _write_run_state(path, state)


def _complete_step(
    path: Path,
    state: dict[str, Any],
    step: str,
    *,
    target: Path,
    artifacts: Sequence[Path],
    metadata: Mapping[str, Any] | None = None,
) -> None:
    checkpoints = state.setdefault("checkpoints", {})
    checkpoints[step] = {
        "status": "completed",
        "input_fingerprint": state["input_fingerprint"],
        "completed_at": _utc_now(),
        "artifacts": _artifact_rows(artifacts, target=target),
        "metadata": dict(metadata or {}),
    }
    state["last_completed_step"] = step
    state["current_step"] = step
    state["step_status"] = "completed"
    _write_run_state(path, state)


def _maybe_raise_failpoint(
    path: Path,
    state: dict[str, Any],
    step: str,
    fail_after_step: str | None,
) -> None:
    if fail_after_step != step:
        return
    state["status"] = "interrupted"
    state["step_status"] = "interrupted"
    state["current_step"] = step
    state["error_type"] = "InjectedStage4AdapterInterruption"
    _write_run_state(path, state)
    raise RuntimeError(f"stage4_adapter_failpoint:{step}")


def _persist_terminal_result(
    *,
    target: Path,
    run_state_path: Path,
    state: dict[str, Any],
    result: Stage4ReplayAdapterResult,
    status: str,
    reason_codes: Sequence[str],
    error_type: str | None = None,
) -> Stage4ReplayAdapterResult:
    atomic_write_json(target / "adapter_result.json", result.to_dict())
    state["status"] = status
    state["step_status"] = "completed" if status == "completed" else "failed"
    state["reason_codes"] = list(
        dict.fromkeys(str(code) for code in reason_codes if str(code))
    )
    state["error_type"] = error_type
    state["completed_at"] = _utc_now()
    _write_run_state(run_state_path, state)
    return result


def _persist_failed_adapter(
    *,
    target: Path,
    run_state_path: Path,
    state: dict[str, Any],
    reason_codes: Sequence[str],
    source_manifest: Mapping[str, Any] | None = None,
    error_type: str | None = None,
) -> Stage4ReplayAdapterResult:
    result = _failed_adapter_result(reason_codes, source_manifest=source_manifest)
    return _persist_terminal_result(
        target=target,
        run_state_path=run_state_path,
        state=state,
        result=result,
        status="quality_gate_failed",
        reason_codes=reason_codes,
        error_type=error_type,
    )


def _build_pit_sector_status_artifacts(
    *,
    history_dir: Path,
    dates: Sequence[str],
    output_dir: Path,
) -> dict[str, Path]:
    """Materialize one fail-closed derived PIT status payload per signal day."""

    status_dir = output_dir / "pit_sector_status"
    status_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for raw_day in dates:
        day = str(raw_day)[:10]
        payload = build_pit_sector_status(
            as_of=day,
            history_dir=history_dir,
            windows=(5,),
        )
        path = status_dir / f"{day}.json"
        atomic_write_json(path, payload)
        paths[day] = path
    return paths


def run_real_stage4_replay_adapter(
    project_root: Path,
    *,
    window_start: str,
    window_end: str,
    output_dir: Path | None = None,
    _fail_after_step: str | None = None,
) -> Stage4ReplayAdapterResult:
    """Run the approved A/B/C forward replay from canonical local data only."""

    root = Path(project_root).resolve()
    daily_root = root / "data" / "daily"
    intraday_root = root / "data" / "intraday"
    minute_root = intraday_root / "1m"
    calendar_path = root / "data" / "calendar" / "a_share_trading_calendar.csv"
    profile_dir = root / "config" / "experiment_profiles"
    low_price_path = root / "data" / "universe" / "low_price_learning_symbols.csv"
    st_path = root / "data" / "metadata" / "st_history.csv"
    delist_path = root / "data" / "metadata" / "delist_history.csv"
    benchmark_path = root / "data" / "benchmark" / "sh000001.csv"
    sector_root = root / "data" / "sector_history"
    quantdesk_root = Path(__file__).resolve().parents[1]
    # A resumed replay must never combine account checkpoints produced by a
    # different implementation. Hashing the full QuantDesk Python source tree
    # is deliberately conservative and closes transitive dependency gaps.
    contract_paths = sorted(
        (
            path
            for path in quantdesk_root.rglob("*.py")
            if "__pycache__" not in path.parts
        ),
        key=lambda path: str(path).lower(),
    )
    target = Path(
        output_dir
        or root / "results" / "stage4_replay_adapter" / str(window_end)[:10]
    ).resolve()
    results_root = (root / "results").resolve()
    try:
        target.relative_to(results_root)
    except ValueError:
        return _failed_adapter_result(["adapter_output_path_escape"])

    target.mkdir(parents=True, exist_ok=True)
    run_state_path = target / "run_state.json"
    profile_paths, reasons = _load_profiles(profile_dir)
    research_profiles, profile_object_reasons = _load_validated_profile_objects(
        profile_paths
    )
    reasons.extend(profile_object_reasons)
    try:
        dates = trade_days_between(
            window_start, window_end, calendar_path=calendar_path
        )
    except Exception:
        dates = []
        reasons.append("adapter_trading_calendar_invalid")
    if len(dates) < 2:
        reasons.append("adapter_window_too_short")
    snapshots: dict[str, dict[str, Any]] = {}
    snapshot_paths: dict[str, Path] = {}
    if dates:
        snapshots, snapshot_paths, snapshot_reasons = _load_sector_snapshots(
            sector_root, dates
        )
        reasons.extend(snapshot_reasons)
    a_symbols, a_names = _load_low_price_universe(low_price_path)
    if not a_symbols:
        reasons.append("account_A_universe_empty")

    fingerprint, fingerprint_basis = _run_input_fingerprint(
        root=root,
        requested_window_start=window_start,
        requested_window_end=window_end,
        dates=dates,
        daily_root=daily_root,
        minute_root=minute_root,
        core_paths=[
            calendar_path,
            low_price_path,
            *profile_paths.values(),
            *snapshot_paths.values(),
            *contract_paths,
            *(
                path
                for path in (st_path, delist_path, benchmark_path)
                if path.is_file()
            ),
        ],
    )
    state = _new_or_resumed_run_state(
        path=run_state_path,
        fingerprint=fingerprint,
        fingerprint_basis=fingerprint_basis,
        window_start=window_start,
        window_end=window_end,
    )
    resumed_source_lock = state.get("source_lock")
    if isinstance(resumed_source_lock, Mapping):
        resumed_locked_rows = resumed_source_lock.get("source_files")
        if not isinstance(resumed_locked_rows, list):
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=["adapter_source_lock_invalid"],
            )
        resumed_lock_reasons = _source_lock_reasons(resumed_locked_rows)
        if resumed_lock_reasons:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=resumed_lock_reasons,
            )
    elif resumed_source_lock is not None:
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=["adapter_source_lock_invalid"],
        )
    if reasons:
        _begin_step(run_state_path, state, "preflight")
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=list(dict.fromkeys(reasons)),
        )

    pit_step = "pit_t0"
    t0_theme_map_path = target / "t0_sector_membership.csv"
    t0_security_master_path = target / "t0_security_master.csv"
    sector_status_paths: dict[str, Path] = {}
    pit_reused = _checkpoint_valid(state, pit_step, target=target)
    if pit_reused:
        checkpoint = state["checkpoints"][pit_step]
        metadata = checkpoint.get("metadata") if isinstance(checkpoint, Mapping) else None
        raw_paths = metadata.get("sector_status_paths") if isinstance(metadata, Mapping) else None
        if not isinstance(raw_paths, Mapping) or set(raw_paths) != set(dates):
            pit_reused = False
        else:
            sector_status_paths = {
                str(day): Path(str(path)) for day, path in raw_paths.items()
            }
    if not pit_reused:
        if pit_step in state.get("checkpoints", {}):
            _invalidate_checkpoints_from(
                state,
                pit_step,
                reason="checkpoint_artifact_hash_mismatch:pit_t0",
            )
        _begin_step(run_state_path, state, pit_step)
        try:
            sector_status_paths = _build_pit_sector_status_artifacts(
                history_dir=sector_root,
                dates=dates,
                output_dir=target,
            )
            t0_membership_rows = [
                {
                    "symbol": normalize_symbol(row.get("symbol")),
                    "industry": str(row.get("industry") or row.get("sector") or ""),
                }
                for row in snapshots[dates[0]].get("sector_membership") or []
                if isinstance(row, Mapping)
                and normalize_symbol(row.get("symbol"))
                and str(row.get("industry") or row.get("sector") or "")
            ]
            if not t0_membership_rows:
                return _persist_failed_adapter(
                    target=target,
                    run_state_path=run_state_path,
                    state=state,
                    reason_codes=["t0_sector_membership_empty"],
                )
            pd.DataFrame(t0_membership_rows).drop_duplicates(
                subset=["symbol"], keep="last"
            ).sort_values("symbol").to_csv(
                t0_theme_map_path, index=False, encoding="utf-8"
            )
            t0_name_rows = [
                {
                    "symbol": normalize_symbol(row.get("symbol")),
                    "name": str(row.get("name") or normalize_symbol(row.get("symbol"))),
                }
                for row in snapshots[dates[0]].get("sector_membership") or []
                if isinstance(row, Mapping) and normalize_symbol(row.get("symbol"))
            ]
            pd.DataFrame(t0_name_rows).drop_duplicates(
                subset=["symbol"], keep="last"
            ).sort_values("symbol").to_csv(
                t0_security_master_path, index=False, encoding="utf-8"
            )
        except Exception as exc:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=[f"pit_t0_build_failed:{type(exc).__name__}"],
                error_type=type(exc).__name__,
            )
        _complete_step(
            run_state_path,
            state,
            pit_step,
            target=target,
            artifacts=[
                *sector_status_paths.values(),
                t0_theme_map_path,
                t0_security_master_path,
            ],
            metadata={
                "sector_status_paths": {
                    day: str(path) for day, path in sorted(sector_status_paths.items())
                },
                "t0_theme_map_path": str(t0_theme_map_path),
                "t0_security_master_path": str(t0_security_master_path),
            },
        )
        _maybe_raise_failpoint(
            run_state_path, state, pit_step, _fail_after_step
        )

    b_step = "account_b_universe"
    b_payload_path = target / "checkpoints" / "account_b_universe.json"
    b_symbols: list[str] = []
    b_names: dict[str, str] = {}
    b_reused = _checkpoint_valid(state, b_step, target=target)
    if b_reused:
        payload = _read_mapping_json(b_payload_path)
        symbols = payload.get("symbols") if payload else None
        names = payload.get("names") if payload else None
        if not isinstance(symbols, list) or not isinstance(names, Mapping):
            b_reused = False
        else:
            b_symbols = [normalize_symbol(symbol) for symbol in symbols]
            b_symbols = [symbol for symbol in b_symbols if symbol]
            b_names = {
                normalize_symbol(symbol): str(name)
                for symbol, name in names.items()
                if normalize_symbol(symbol)
            }
            if not b_symbols:
                b_reused = False
    if not b_reused:
        if b_step in state.get("checkpoints", {}):
            _invalidate_checkpoints_from(
                state,
                b_step,
                reason="checkpoint_artifact_hash_mismatch:account_b_universe",
            )
        _begin_step(run_state_path, state, b_step)
        try:
            b_universe = build_affordable_leaders_universe(
                as_of=dates[0],
                daily_dir=daily_root,
                theme_map_path=t0_theme_map_path,
                security_master_path=t0_security_master_path,
            )
        except Exception as exc:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=[f"account_B_universe_build_failed:{type(exc).__name__}"],
                error_type=type(exc).__name__,
            )
        b_symbols = [normalize_symbol(symbol) for symbol in b_universe.symbols]
        b_symbols = [symbol for symbol in b_symbols if symbol]
        b_names = {
            normalize_symbol(row.symbol): str(row.name)
            for row in b_universe.rows
            if normalize_symbol(row.symbol)
        }
        if not b_symbols:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=["account_B_universe_empty"],
            )
        atomic_write_json(
            b_payload_path,
            {"symbols": b_symbols, "names": b_names, "as_of": dates[0]},
        )
        _complete_step(
            run_state_path,
            state,
            b_step,
            target=target,
            artifacts=[b_payload_path],
        )
        _maybe_raise_failpoint(run_state_path, state, b_step, _fail_after_step)

    audit_path = target / "minute_window_audit.json"
    minute_step = "minute_window_audit"
    minute_reused = _checkpoint_valid(state, minute_step, target=target)
    window_pool: dict[str, Any] = {}
    if minute_reused:
        try:
            loaded_pool = load_window_verified_minute_pool(snapshot_path=audit_path)
            window_pool = dict(loaded_pool) if isinstance(loaded_pool, Mapping) else {}
        except Exception:
            minute_reused = False
        if (
            window_pool.get("snapshot_valid") is not True
            or window_pool.get("passed") is not True
        ):
            minute_reused = False
    if not minute_reused:
        if minute_step in state.get("checkpoints", {}):
            _invalidate_checkpoints_from(
                state,
                minute_step,
                reason="checkpoint_artifact_hash_mismatch:minute_window_audit",
            )
        _begin_step(run_state_path, state, minute_step)
        audit_symbols = sorted(
            set(a_symbols)
            | set(b_symbols)
            | {
                normalize_symbol(path.stem)
                for path in minute_root.glob("*.csv")
                if normalize_symbol(path.stem)
            }
        )
        try:
            audit_minute_pool_window(
                window_start=dates[1],
                window_end=dates[-1],
                symbols=audit_symbols,
                intraday_dir=minute_root,
                calendar_path=calendar_path,
                output_path=audit_path,
                write=True,
            )
            loaded_pool = load_window_verified_minute_pool(snapshot_path=audit_path)
            window_pool = dict(loaded_pool) if isinstance(loaded_pool, Mapping) else {}
        except Exception as exc:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=[f"minute_window_audit_failed:{type(exc).__name__}"],
                error_type=type(exc).__name__,
            )
        if (
            window_pool.get("snapshot_valid") is not True
            or window_pool.get("passed") is not True
        ):
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=["minute_window_quality_gate_failed"],
            )
        _complete_step(
            run_state_path,
            state,
            minute_step,
            target=target,
            artifacts=[audit_path],
        )
        _maybe_raise_failpoint(
            run_state_path, state, minute_step, _fail_after_step
        )

    usable = window_pool.get("usable_by_symbol")
    c_symbols = sorted(usable) if isinstance(usable, Mapping) else []
    membership_names: dict[str, str] = {}
    for snapshot in snapshots.values():
        for row in snapshot.get("sector_membership") or []:
            if not isinstance(row, Mapping):
                continue
            symbol = normalize_symbol(row.get("symbol"))
            if symbol:
                membership_names[symbol] = str(row.get("name") or symbol)
    c_names = {symbol: membership_names.get(symbol, a_names.get(symbol, symbol)) for symbol in c_symbols}
    if not c_symbols:
        state["current_step"] = minute_step
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=["account_C_universe_empty"],
        )

    all_symbols = sorted(set(a_symbols) | set(b_symbols) | set(c_symbols))
    source_lock_paths: list[Path] = [
        calendar_path,
        low_price_path,
        audit_path,
        t0_theme_map_path,
        t0_security_master_path,
        *profile_paths.values(),
        *snapshot_paths.values(),
        *sector_status_paths.values(),
    ]
    for optional in (st_path, delist_path, benchmark_path):
        if optional.is_file():
            source_lock_paths.append(optional)
    for symbol in all_symbols:
        daily_path = daily_root / f"{symbol}.csv"
        minute_path = minute_root / f"{symbol}.csv"
        if daily_path.is_file():
            source_lock_paths.append(daily_path)
        if minute_path.is_file():
            source_lock_paths.append(minute_path)
    existing_source_lock = state.get("source_lock")
    if existing_source_lock is not None:
        locked_input_rows = existing_source_lock.get("source_files")
        if not isinstance(locked_input_rows, list):
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=["adapter_source_lock_invalid"],
            )
        source_lock_reasons = _source_lock_reasons(
            locked_input_rows,
            expected_paths=source_lock_paths,
        )
        if source_lock_reasons:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=source_lock_reasons,
            )
    else:
        locked_input_rows, source_lock_reasons = _source_file_rows(source_lock_paths)
        if source_lock_reasons:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=source_lock_reasons,
            )
        state["source_lock"] = {
            "captured_at": _utc_now(),
            "source_files": locked_input_rows,
        }
        _write_run_state(run_state_path, state)
    account_specs = (
        ("A", STRATEGY_LONG, a_symbols, a_names, "low_price_learning_pool"),
        ("B", STRATEGY_LONG, b_symbols, b_names, "affordable_leaders_proxy_pool"),
        ("C", STRATEGY_BOARD, c_symbols, c_names, "board_channel_verified_minute_pool"),
    )
    account_results: dict[str, Any] = {}
    st_delist: Any = None
    st_delist_loaded = False
    frame_cache: dict[str, pd.DataFrame] = {}
    for account_id, strategy, symbols, names, label in account_specs:
        account_step = f"account_replay_{account_id}"
        account_payload_path = target / "checkpoints" / f"{account_step}.json"
        account_reused = _checkpoint_valid(state, account_step, target=target)
        if account_reused:
            payload = _read_mapping_json(account_payload_path)
            raw_result = payload.get("account_result") if payload else None
            if not isinstance(raw_result, Mapping) or str(
                raw_result.get("account_id") or ""
            ) != account_id:
                account_reused = False
            else:
                account_results[account_id] = dict(raw_result)
        if account_reused:
            continue

        checkpoints = state.get("checkpoints")
        if isinstance(checkpoints, Mapping) and any(
            name in checkpoints
            for name in (account_step, "account_replays", "source_sealing")
        ):
            _invalidate_account_replay_checkpoint(
                state,
                account_step,
                reason=f"checkpoint_artifact_hash_mismatch:{account_step}",
            )
        _begin_step(run_state_path, state, account_step)
        try:
            if not st_delist_loaded:
                st_delist = load_st_delist_data(
                    st_path=st_path if st_path.is_file() else None,
                    delist_path=delist_path if delist_path.is_file() else None,
                )
                st_delist_loaded = True
            for symbol in symbols:
                if symbol not in frame_cache:
                    frame_cache[symbol] = _read_daily_enriched(symbol, daily_root)
            result = replay_forward_account(
                account_id=account_id,
                strategy=strategy,
                symbols=symbols,
                names=names,
                universe_label=label,
                window_dates=dates,
                window_pool=window_pool,
                sector_snapshots=snapshots,
                sector_snapshot_paths=snapshot_paths,
                sector_status_paths=sector_status_paths,
                st_delist=st_delist,
                minute_snapshot_path=audit_path,
                daily_dir=daily_root,
                intraday_dir=intraday_root,
                calendar_path=calendar_path,
                initial_capital=float(research_profiles[account_id].paper_capital),
                frame_cache=frame_cache,
                research_profile=research_profiles[account_id],
                data_mode=REAL_DATA_MODE,
                source_cache={},
                lifecycle_source_cache={},
            )
        except Exception as exc:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=[
                    f"tri_account_replay_failed:{account_id}:{type(exc).__name__}"
                ],
                error_type=type(exc).__name__,
            )
        mapped_result = _mapping(result)
        if str(mapped_result.get("account_id") or "") != account_id:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=[f"tri_account_replay_account_id_mismatch:{account_id}"],
            )
        atomic_write_json(
            account_payload_path,
            {"account_id": account_id, "account_result": mapped_result},
        )
        account_results[account_id] = mapped_result
        _complete_step(
            run_state_path,
            state,
            account_step,
            target=target,
            artifacts=[account_payload_path],
        )
        _maybe_raise_failpoint(
            run_state_path, state, account_step, _fail_after_step
        )

    replay_step = "account_replays"
    replay_payload_path = target / "checkpoints" / "account_replays.json"
    replay_reused = _checkpoint_valid(state, replay_step, target=target)
    if replay_reused:
        payload = _read_mapping_json(replay_payload_path)
        raw_accounts = payload.get("account_results") if payload else None
        if (
            not isinstance(raw_accounts, Mapping)
            or set(raw_accounts) != set(EXPECTED_ACCOUNT_IDS)
            or dict(raw_accounts) != account_results
        ):
            replay_reused = False
    if not replay_reused:
        if replay_step in state.get("checkpoints", {}):
            _invalidate_checkpoints_from(
                state,
                replay_step,
                reason="checkpoint_artifact_hash_mismatch:account_replays",
            )
        _begin_step(run_state_path, state, replay_step)
        atomic_write_json(
            replay_payload_path,
            {
                "account_results": {
                    account_id: _mapping(result)
                    for account_id, result in account_results.items()
                }
            },
        )
        _complete_step(
            run_state_path,
            state,
            replay_step,
            target=target,
            artifacts=[replay_payload_path],
        )
        _maybe_raise_failpoint(
            run_state_path, state, replay_step, _fail_after_step
        )

    artifact_path = target / "adapter_result.json"
    source_step = "source_sealing"
    source_payload_path = target / "checkpoints" / "source_sealing.json"
    source_manifest: dict[str, Any] = {}
    benchmark_by_date: dict[str, Any] | None = None
    source_lock_reasons = _source_lock_reasons(
        locked_input_rows,
        expected_paths=source_lock_paths,
    )
    if source_lock_reasons:
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=source_lock_reasons,
        )
    source_reused = _checkpoint_valid(state, source_step, target=target)
    if source_reused:
        payload = _read_mapping_json(source_payload_path)
        raw_manifest = payload.get("source_manifest") if payload else None
        raw_benchmark = payload.get("benchmark_by_date") if payload else None
        if not isinstance(raw_manifest, Mapping):
            source_reused = False
        else:
            source_manifest = dict(raw_manifest)
            benchmark_by_date = (
                dict(raw_benchmark) if isinstance(raw_benchmark, Mapping) else None
            )
            source_reasons = _source_manifest_reasons(source_manifest)
            resealed = seal_real_source_manifest(source_manifest, account_results)
            if source_reasons or resealed.get("seal") != source_manifest.get("seal"):
                source_reused = False
    if not source_reused:
        if source_step in state.get("checkpoints", {}):
            _invalidate_checkpoints_from(
                state,
                source_step,
                reason="checkpoint_artifact_hash_mismatch:source_sealing",
            )
        _begin_step(run_state_path, state, source_step)
        source_lock_reasons = _source_lock_reasons(
            locked_input_rows,
            expected_paths=source_lock_paths,
        )
        if source_lock_reasons:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=source_lock_reasons,
            )
        source_paths: list[Path] = [
            calendar_path,
            low_price_path,
            audit_path,
            t0_theme_map_path,
            t0_security_master_path,
            *profile_paths.values(),
            *snapshot_paths.values(),
            *sector_status_paths.values(),
        ]
        for optional in (st_path, delist_path, benchmark_path):
            if optional.is_file():
                source_paths.append(optional)
        for symbol in all_symbols:
            daily_path = daily_root / f"{symbol}.csv"
            minute_path = minute_root / f"{symbol}.csv"
            if daily_path.is_file():
                source_paths.append(daily_path)
            if minute_path.is_file():
                source_paths.append(minute_path)
        source_files, source_reasons = _source_file_rows(source_paths)
        if source_reasons:
            return _persist_failed_adapter(
                target=target,
                run_state_path=run_state_path,
                state=state,
                reason_codes=source_reasons,
            )
        source_manifest = {
            "data_mode": REAL_DATA_MODE,
            "network_used": False,
            "project_root": str(root),
            "daily_root": str(daily_root),
            "minute_root": str(minute_root),
            "calendar_path": str(calendar_path),
            "profile_paths": {
                key: str(value) for key, value in profile_paths.items()
            },
            "sector_status_paths": {
                day: str(path) for day, path in sorted(sector_status_paths.items())
            },
            "account_universes": {
                "A": list(a_symbols),
                "B": list(b_symbols),
                "C": list(c_symbols),
            },
            "source_files": source_files,
            "input_source_lock_sha256": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    locked_input_rows,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "input_source_lock_captured_at": str(
                _mapping(state.get("source_lock")).get("captured_at") or ""
            ),
            "artifact_path": str(artifact_path),
            "window_start": dates[0],
            "window_end": dates[-1],
        }
        source_manifest = seal_real_source_manifest(source_manifest, account_results)
        benchmark_by_date = _benchmark_by_date(benchmark_path, dates)
        atomic_write_json(
            source_payload_path,
            {
                "source_manifest": source_manifest,
                "benchmark_by_date": benchmark_by_date,
            },
        )
        _complete_step(
            run_state_path,
            state,
            source_step,
            target=target,
            artifacts=[source_payload_path],
        )
        _maybe_raise_failpoint(
            run_state_path, state, source_step, _fail_after_step
        )

    try:
        adapted = adapt_stage4_replay(
            account_results,
            window_dates=dates,
            source_manifest=source_manifest,
            benchmark_by_date=benchmark_by_date,
        )
    except Exception as exc:
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=[f"adapter_adaptation_failed:{type(exc).__name__}"],
            source_manifest=source_manifest,
            error_type=type(exc).__name__,
        )
    if not isinstance(adapted, Stage4ReplayAdapterResult):
        quality = getattr(adapted, "quality_gate", {})
        raw_reasons = quality.get("reason_codes") if isinstance(quality, Mapping) else None
        adapter_reasons = (
            list(raw_reasons)
            if isinstance(raw_reasons, list) and raw_reasons
            else ["adapter_quality_gate_failed"]
        )
        return _persist_failed_adapter(
            target=target,
            run_state_path=run_state_path,
            state=state,
            reason_codes=adapter_reasons,
            source_manifest=source_manifest,
        )
    if adapted.quality_gate.get("passed") is not True:
        raw_reasons = adapted.quality_gate.get("reason_codes")
        adapter_reasons = list(raw_reasons) if isinstance(raw_reasons, list) else []
        return _persist_terminal_result(
            target=target,
            run_state_path=run_state_path,
            state=state,
            result=adapted,
            status="quality_gate_failed",
            reason_codes=adapter_reasons,
        )
    _persist_terminal_result(
        target=target,
        run_state_path=run_state_path,
        state=state,
        result=adapted,
        status="completed",
        reason_codes=[],
    )
    adapted.source_manifest["artifact_sha256"] = _sha256(artifact_path)
    return adapted


__all__ = [
    "ACCOUNT_REPLAY_CONTRACTS",
    "EXPECTED_ACCOUNT_IDS",
    "FIXTURE_DATA_MODE",
    "MAINTENANCE_REPLAY_GATE_NAMES",
    "REAL_DATA_MODE",
    "STAGE4_REPLAY_ADAPTER_SCHEMA_VERSION",
    "STAGE4_REPLAY_RUN_STATE_SCHEMA_VERSION",
    "AdaptedAccountResult",
    "Stage4ReplayAdapterResult",
    "adapt_stage4_replay",
    "build_stage4_replay_adapter_fixture",
    "run_real_stage4_replay_adapter",
    "seal_real_source_manifest",
]
