"""Round 9S Part B — Stage 4 terminal evaluator (fixture-driven, plug-in ready).

Consumes forward-runner replay products and emits an evaluation report:
total return / max drawdown / total fees / win rate / hold-day distribution /
per-trade evidence refs / insufficient_sample when effective sample < 30.

Benchmark alignment uses only local data; missing days are marked
``benchmark_unavailable`` without extrapolation (W1 honesty principle).

Produces no real evaluation conclusions — real data waits for T0+18.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.paper.stage4_forward_replay import MIN_EFFECTIVE_SAMPLE
from quantdesk.utils import atomic_write_json
from quantdesk.version import API_SCHEMA_VERSION

EVALUATOR_SCHEMA_VERSION = "round9s_stage4_final_evaluator_v1"
FINAL_ARTIFACT_SCHEMA_VERSION = "stage4_final_evaluation_v1"
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice. Fixture-driven Stage 4 final evaluator; not a live conclusion."
)


def _finite(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _max_drawdown(nav_values: Sequence[float]) -> float | None:
    if not nav_values:
        return None
    peak = nav_values[0]
    worst = 0.0
    for value in nav_values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return round(worst, 8)


@dataclass(frozen=True)
class TradeEvidenceRef:
    trade_id: str
    symbol: str
    entry_date: str
    exit_date: str
    hold_days: int
    pnl_pct: float | None
    fees: float
    evidence_eligible: bool
    evidence_ref: str
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "hold_days": self.hold_days,
            "pnl_pct": self.pnl_pct,
            "fees": self.fees,
            "evidence_eligible": self.evidence_eligible,
            "evidence_ref": self.evidence_ref,
            "reason_codes": list(self.reason_codes),
        }


@dataclass
class Stage4FinalEvaluation:
    as_of: str | None
    window_start: str | None
    window_end: str | None
    total_return: float | None
    max_drawdown: float | None
    total_fees: float
    win_rate: float | None
    hold_days_distribution: dict[str, int]
    effective_sample: int
    sample_sufficient: bool
    insufficient_sample: bool
    evidence_eligible: bool
    verdict: str
    fixture_or_synthetic: bool = True
    trade_evidence: list[dict[str, Any]] = field(default_factory=list)
    benchmark: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": "stage4_final_evaluation_report",
            "schema_version": EVALUATOR_SCHEMA_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "as_of": self.as_of,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "metrics": {
                "total_return": self.total_return,
                "max_drawdown": self.max_drawdown,
                "total_fees": self.total_fees,
                "win_rate": self.win_rate,
                "hold_days_distribution": dict(self.hold_days_distribution),
                "effective_sample": self.effective_sample,
                "min_effective_sample": MIN_EFFECTIVE_SAMPLE,
            },
            "sample_sufficient": self.sample_sufficient,
            "insufficient_sample": self.insufficient_sample,
            "evidence_eligible": self.evidence_eligible,
            "verdict": self.verdict,
            "trade_evidence": list(self.trade_evidence),
            "benchmark": dict(self.benchmark),
            "reason_codes": list(self.reason_codes),
            "risk_gate_enforced": False,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "fixture_or_synthetic": self.fixture_or_synthetic,
            "disclaimer": DISCLAIMER,
        }


def _parse_trades(replay_product: Mapping[str, Any]) -> list[TradeEvidenceRef]:
    raw_trades = replay_product.get("trades") or replay_product.get("closed_trades") or []
    trades: list[TradeEvidenceRef] = []
    for index, item in enumerate(raw_trades):
        if not isinstance(item, Mapping):
            continue
        hold = int(item.get("hold_days") or item.get("holding_days") or 0)
        pnl = _finite(item.get("pnl_pct") if "pnl_pct" in item else item.get("return_pct"))
        fees = float(_finite(item.get("fees"), 0.0) or 0.0)
        eligible = item.get("evidence_eligible") is True
        reasons = tuple(
            str(code) for code in (item.get("reason_codes") or []) if str(code).strip()
        )
        trades.append(
            TradeEvidenceRef(
                trade_id=str(item.get("trade_id") or f"trade_{index + 1}"),
                symbol=str(item.get("symbol") or ""),
                entry_date=str(item.get("entry_date") or item.get("buy_date") or "")[:10],
                exit_date=str(item.get("exit_date") or item.get("sell_date") or "")[:10],
                hold_days=hold,
                pnl_pct=pnl,
                fees=fees,
                evidence_eligible=eligible,
                evidence_ref=str(
                    item.get("evidence_ref")
                    or item.get("event_id")
                    or item.get("source_hash")
                    or f"ref:{index + 1}"
                ),
                reason_codes=reasons,
            )
        )
    return trades


def _align_benchmark(
    window_dates: Sequence[str],
    benchmark_by_date: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Align local benchmark series; missing days → benchmark_unavailable."""

    series: list[dict[str, Any]] = []
    missing: list[str] = []
    available_values: list[float] = []
    source = "local_benchmark"
    if benchmark_by_date is None:
        for day in window_dates:
            series.append({"date": day, "value": None, "status": "benchmark_unavailable"})
            missing.append(day)
        return {
            "source": source,
            "status": "benchmark_unavailable",
            "available_days": 0,
            "missing_days": missing,
            "missing_day_count": len(missing),
            "benchmark_return": None,
            "series": series,
            "extrapolated": False,
            "reason_codes": ["benchmark_source_missing"],
        }

    for day in window_dates:
        raw = benchmark_by_date.get(day)
        value = _finite(raw)
        if value is None:
            series.append({"date": day, "value": None, "status": "benchmark_unavailable"})
            missing.append(day)
        else:
            series.append({"date": day, "value": value, "status": "available"})
            available_values.append(value)

    bench_return = None
    if len(available_values) >= 2 and available_values[0] != 0:
        bench_return = round(available_values[-1] / available_values[0] - 1.0, 8)

    if not window_dates:
        status = "no_window"
    elif missing and available_values:
        status = "partial_benchmark_unavailable"
    elif missing:
        status = "benchmark_unavailable"
    else:
        status = "aligned"

    reason_codes: list[str] = []
    if missing:
        reason_codes.append("benchmark_unavailable")
        if available_values:
            reason_codes.append("benchmark_partial_coverage")

    return {
        "source": source,
        "status": status,
        "available_days": len(available_values),
        "missing_days": missing,
        "missing_day_count": len(missing),
        "benchmark_return": bench_return,
        "series": series,
        "extrapolated": False,
        "reason_codes": reason_codes,
    }


def evaluate_stage4_final(
    replay_product: Mapping[str, Any],
    *,
    benchmark_by_date: Mapping[str, Any] | None = None,
    window_dates: Sequence[str] | None = None,
    as_of: str | None = None,
    min_sample: int = MIN_EFFECTIVE_SAMPLE,
) -> Stage4FinalEvaluation:
    """Evaluate a forward-runner replay product (fixture or real) honestly."""

    trades = _parse_trades(replay_product)
    # Effective sample = closed trades with usable return (same bar as runner).
    usable = [t for t in trades if t.pnl_pct is not None]
    effective = int(
        replay_product.get("effective_sell_sample")
        if replay_product.get("effective_sell_sample") is not None
        else len(usable)
    )
    sample_sufficient = effective >= int(min_sample)
    insufficient = not sample_sufficient

    total_fees = round(sum(t.fees for t in trades), 8)
    wins = [t for t in usable if (t.pnl_pct or 0.0) > 0]
    win_rate = round(len(wins) / len(usable), 8) if usable else None

    hold_dist = Counter(str(t.hold_days) for t in trades)
    hold_days_distribution = dict(sorted(hold_dist.items(), key=lambda kv: int(kv[0])))

    nav = replay_product.get("nav_series") or replay_product.get("equity_curve")
    nav_values: list[float] = []
    if isinstance(nav, Sequence) and not isinstance(nav, (str, bytes)):
        for point in nav:
            if isinstance(point, Mapping):
                val = _finite(point.get("nav") if "nav" in point else point.get("value"))
            else:
                val = _finite(point)
            if val is not None:
                nav_values.append(val)

    total_return = _finite(replay_product.get("total_return"))
    if total_return is None and len(nav_values) >= 2 and nav_values[0] != 0:
        total_return = round(nav_values[-1] / nav_values[0] - 1.0, 8)
    max_dd = _finite(replay_product.get("max_drawdown"))
    if max_dd is None:
        max_dd = _max_drawdown(nav_values)

    dates = list(window_dates or replay_product.get("window_dates") or [])
    if not dates:
        start = str(replay_product.get("window_start") or "")[:10]
        end = str(replay_product.get("window_end") or "")[:10]
        if start:
            dates.append(start)
        if end and end != start:
            dates.append(end)
    benchmark = _align_benchmark(dates, benchmark_by_date)

    # Synthetic-source gate (acceptor invariant, acceptance.py:149): any product
    # that is a fixture / synthetic schedule NEVER becomes formal strategy
    # evidence, regardless of sample size. A synthetic product surfaced as
    # "return_conclusion_eligible" would be exactly the beautification the
    # protocol forbids. Detection is deliberately conservative — any positive
    # signal marks the product synthetic and forces the honest downgrade.
    product_reason_codes = [str(c) for c in (replay_product.get("reason_codes") or []) if str(c)]
    is_synthetic = bool(
        replay_product.get("fixture_kind")
        or replay_product.get("fixture_or_synthetic") is True
        or str(replay_product.get("source_path") or "").startswith("synthetic://")
        or "acceptance_fixture_not_strategy_evidence" in product_reason_codes
    )

    # evidence_eligible matches acceptance semantics: synthetic / fixture /
    # thin sample = false; a real product must clear every gate to earn True.
    all_trade_eligible = bool(trades) and all(t.evidence_eligible for t in trades)
    explicit = replay_product.get("evidence_eligible")
    if is_synthetic:
        evidence_eligible = False
    elif explicit is False:
        evidence_eligible = False
    elif insufficient:
        evidence_eligible = False
    elif explicit is True and all_trade_eligible:
        evidence_eligible = True
    else:
        evidence_eligible = False

    reason_codes: list[str] = []
    if is_synthetic:
        reason_codes.append("synthetic_source_not_formal_evidence")
    if insufficient:
        reason_codes.append("insufficient_sample")
    if not evidence_eligible:
        reason_codes.append("evidence_not_eligible_for_formal_conclusion")
    reason_codes.extend(str(c) for c in (benchmark.get("reason_codes") or []) if str(c))
    for text in product_reason_codes:
        if text and text not in reason_codes:
            reason_codes.append(text)

    if is_synthetic:
        verdict = "synthetic_fixture_not_conclusion"
    elif insufficient:
        verdict = "insufficient_sample"
    elif not evidence_eligible:
        verdict = "not_evidence_eligible"
    else:
        verdict = "return_conclusion_eligible"

    return Stage4FinalEvaluation(
        as_of=as_of or str(replay_product.get("as_of") or "")[:10] or None,
        window_start=str(replay_product.get("window_start") or (dates[0] if dates else ""))[:10]
        or None,
        window_end=str(replay_product.get("window_end") or (dates[-1] if dates else ""))[:10]
        or None,
        total_return=total_return,
        max_drawdown=max_dd,
        total_fees=total_fees,
        win_rate=win_rate,
        hold_days_distribution=hold_days_distribution,
        effective_sample=effective,
        sample_sufficient=sample_sufficient,
        insufficient_sample=insufficient,
        evidence_eligible=evidence_eligible,
        verdict=verdict,
        fixture_or_synthetic=is_synthetic,
        trade_evidence=[t.to_dict() for t in trades],
        benchmark=benchmark,
        reason_codes=reason_codes,
    )


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def build_stage4_final_evaluation_artifact(
    *,
    adapter_path: Path,
    project_root: Path,
    maintenance_evidence: Mapping[str, Any],
    as_of: str,
) -> dict[str, Any]:
    """Build a dated terminal evaluation from one exact adapter artifact."""

    root = Path(project_root).resolve()
    adapter = Path(adapter_path).resolve()
    as_of_day = str(as_of)[:10]
    try:
        from datetime import date

        if date.fromisoformat(as_of_day).isoformat() != as_of_day:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stage4_final_as_of_invalid") from exc
    if not adapter.is_file():
        raise RuntimeError("stage4_final_adapter_missing")
    try:
        adapter.relative_to(root / "results" / "stage4_replay_adapter")
    except ValueError as exc:
        raise RuntimeError("stage4_final_adapter_path_invalid") from exc
    try:
        raw = adapter.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stage4_final_adapter_unreadable") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("stage4_final_adapter_invalid")
    if str(payload.get("schema_version") or "").strip() == "":
        raise RuntimeError("stage4_final_adapter_schema_invalid")
    if payload.get("fixture_or_synthetic") is not False:
        raise RuntimeError("stage4_final_adapter_synthetic_boundary")
    quality_gate = payload.get("quality_gate")
    if not isinstance(quality_gate, Mapping) or not isinstance(
        quality_gate.get("passed"), bool
    ):
        raise RuntimeError("stage4_final_adapter_quality_gate_invalid")
    maintenance_eligible = payload.get("maintenance_eligible")
    if not isinstance(maintenance_eligible, bool):
        if quality_gate.get("passed") is False:
            maintenance_eligible = False
        else:
            raise RuntimeError("stage4_final_adapter_eligibility_invalid")
    source_manifest = payload.get("source_manifest")
    if not isinstance(source_manifest, Mapping):
        if quality_gate.get("passed") is False:
            source_manifest = {}
        else:
            raise RuntimeError("stage4_final_adapter_source_boundary")
    if (
        quality_gate.get("passed") is True
        and source_manifest.get("data_mode") != "real_canonical"
    ):
        raise RuntimeError("stage4_final_adapter_source_boundary")
    replay_product = payload.get("replay_product")
    if not isinstance(replay_product, Mapping):
        raise RuntimeError("stage4_final_adapter_replay_product_missing")
    replay_as_of = str(replay_product.get("as_of") or "")[:10]
    replay_window_end = str(replay_product.get("window_end") or "")[:10]
    if quality_gate.get("passed") is True and (
        replay_as_of != as_of_day or replay_window_end != as_of_day
    ):
        raise RuntimeError("stage4_final_adapter_date_identity_mismatch")
    benchmark = payload.get("benchmark_by_date")
    if benchmark is not None and not isinstance(benchmark, Mapping):
        raise RuntimeError("stage4_final_adapter_benchmark_invalid")

    evaluation = evaluate_stage4_final(
        replay_product,
        benchmark_by_date=(dict(benchmark) if benchmark is not None else None),
        as_of=as_of_day,
    ).to_dict()
    adapter_reason_codes = [
        str(code)
        for code in (quality_gate.get("reason_codes") or [])
        if str(code).strip()
    ]
    adapter_eligible = quality_gate.get("passed") is True and maintenance_eligible
    if not adapter_eligible:
        adapter_reason_codes.append("stage4_adapter_not_formally_evaluable")
        evaluation["evidence_eligible"] = False
        if evaluation.get("verdict") == "return_conclusion_eligible":
            evaluation["verdict"] = "not_evidence_eligible"
    metrics = dict(evaluation.get("metrics") or {})
    if bool(evaluation.get("insufficient_sample")):
        sample_disposition = "insufficient_sample"
    elif adapter_eligible and evaluation.get("evidence_eligible") is True:
        sample_disposition = "sufficient"
    else:
        sample_disposition = "not_evaluable"
    maintenance = dict(maintenance_evidence)
    maintenance_reasons = [
        str(code)
        for code in (maintenance.get("reason_codes") or [])
        if str(code).strip()
    ]
    reasons = list(dict.fromkeys(
        [str(code) for code in (evaluation.get("reason_codes") or []) if str(code)]
        + adapter_reason_codes
        + maintenance_reasons
    ))
    quality_gate_passed = (
        quality_gate.get("passed") is True
        if isinstance(quality_gate, Mapping)
        else payload.get("quality_gate_passed") is True
    )
    source_manifest_seal = (
        source_manifest.get("seal")
        if isinstance(source_manifest, Mapping)
        else None
    )
    artifact = {
        "schema_version": FINAL_ARTIFACT_SCHEMA_VERSION,
        "task_type": "stage4_final_evaluation",
        "as_of": as_of_day,
        "terminal_status": str(
            maintenance.get("status") or maintenance.get("lifecycle_status") or ""
        ),
        "maintenance_disposition": str(
            maintenance.get("maintenance_disposition") or ""
        ),
        "sample_disposition": sample_disposition,
        "evaluator_verdict": str(evaluation.get("verdict") or ""),
        "reason_codes": reasons,
        "source_adapter": {
            "path": _display_path(root, adapter),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "schema_version": str(payload.get("schema_version") or ""),
            "status": str(payload.get("status") or ""),
            "quality_gate_passed": quality_gate_passed,
            "source_manifest_seal": source_manifest_seal,
        },
        "evaluation": evaluation,
        "metrics": metrics,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    return artifact


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _json_bytes(payload)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(path), flags)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def write_stage4_final_evaluation_artifact(
    payload: Mapping[str, Any],
    *,
    output_dir: Path,
    results_root: Path,
    as_of: str,
) -> dict[str, Path]:
    """Publish an immutable dated terminal artifact and a latest pointer."""

    as_of_day = str(as_of)[:10]
    if len(as_of_day) != 10:
        raise ValueError("as_of must be YYYY-MM-DD")
    snapshot = dict(payload)
    if snapshot.get("schema_version") != FINAL_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("stage4_final_evaluation_schema_invalid")
    if snapshot.get("as_of") != as_of_day:
        raise ValueError("stage4_final_evaluation_identity_invalid")
    directory = Path(output_dir).resolve()
    allowed_root = Path(results_root).resolve()
    try:
        directory.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("Stage 4 final output must stay under results_root") from exc
    dated = directory / f"stage4_final_evaluation_{as_of_day}.json"
    latest = directory / "stage4_final_evaluation_latest.json"
    if dated.exists():
        try:
            existing = json.loads(dated.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stage4_final_evaluation_existing_unreadable") from exc
        if existing != snapshot:
            raise RuntimeError("stage4_final_evaluation_conflict")
    else:
        try:
            _write_exclusive_json(dated, snapshot)
        except FileExistsError as exc:
            existing = json.loads(dated.read_text(encoding="utf-8"))
            if existing != snapshot:
                raise RuntimeError("stage4_final_evaluation_conflict") from exc
    with interprocess_file_lock(
        allowed_root / ".stage4_final_evaluation.lock", timeout_seconds=10
    ):
        if latest.exists():
            try:
                latest_existing = json.loads(latest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("stage4_final_evaluation_latest_unreadable") from exc
            existing_as_of = str(latest_existing.get("as_of") or "")
            if existing_as_of == as_of_day and latest_existing != snapshot:
                raise RuntimeError("stage4_final_evaluation_conflict")
            if existing_as_of > as_of_day:
                return {"dated": dated, "latest": latest}
        # Keep the pointer recoverable if a process crashed after the dated write.
        atomic_write_json(latest, snapshot)
    return {"dated": dated, "latest": latest}


def validate_stage4_final_evaluation_artifact(
    payload: Mapping[str, Any], target_date: str
) -> tuple[bool, str]:
    if payload.get("schema_version") != FINAL_ARTIFACT_SCHEMA_VERSION:
        return False, "stage4_final_evaluation_schema_invalid"
    if payload.get("task_type") != "stage4_final_evaluation":
        return False, "stage4_final_evaluation_identity_invalid"
    if payload.get("as_of") != target_date:
        return False, "stage4_final_evaluation_identity_invalid"
    if payload.get("research_only") is not True or payload.get("paper_only") is not True:
        return False, "stage4_final_evaluation_boundary_invalid"
    if payload.get("no_broker") is not True or payload.get("is_real_order") is not False:
        return False, "stage4_final_evaluation_boundary_invalid"
    if not isinstance(payload.get("source_adapter"), Mapping):
        return False, "stage4_final_evaluation_source_invalid"
    source = payload["source_adapter"]
    source_path = str(source.get("path") or "").strip()
    source_hash = str(source.get("sha256") or "").lower()
    if not source_path or len(source_hash) != 71 or not source_hash.startswith("sha256:"):
        return False, "stage4_final_evaluation_source_invalid"
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, Mapping):
        return False, "stage4_final_evaluation_report_missing"
    if evaluation.get("schema_version") != EVALUATOR_SCHEMA_VERSION:
        return False, "stage4_final_evaluation_report_invalid"
    if str(evaluation.get("as_of") or "")[:10] != str(target_date)[:10]:
        return False, "stage4_final_evaluation_report_invalid"
    if not isinstance(payload.get("reason_codes"), list):
        return False, "stage4_final_evaluation_reason_codes_invalid"
    verdict = str(payload.get("evaluator_verdict") or "")
    if verdict not in {
        "insufficient_sample",
        "not_evidence_eligible",
        "return_conclusion_eligible",
        "synthetic_fixture_not_conclusion",
    }:
        return False, "stage4_final_evaluation_verdict_invalid"
    if str(evaluation.get("verdict") or "") != verdict:
        return False, "stage4_final_evaluation_report_invalid"
    if str(payload.get("terminal_status") or "") not in {
        "completed",
        "terminal_replay_blocked",
        "terminal_replay_ready",
        "closed_inconclusive",
    }:
        return False, "stage4_final_evaluation_terminal_status_invalid"
    if str(payload.get("maintenance_disposition") or "") not in {
        "complete",
        "inconclusive",
        "pending",
    }:
        return False, "stage4_final_evaluation_maintenance_disposition_invalid"
    if str(payload.get("sample_disposition") or "") not in {
        "sufficient",
        "insufficient_sample",
        "not_evaluable",
    }:
        return False, "stage4_final_evaluation_sample_disposition_invalid"
    return True, ""


def validate_stage4_final_evaluation_pair(
    project_root: Path, target_date: str
) -> tuple[bool, str]:
    """Validate the dated terminal artifact, its pointer, and raw provenance."""

    root = Path(project_root).resolve()
    base = root / "results" / "data_health"
    dated = base / f"stage4_final_evaluation_{target_date}.json"
    latest = base / "stage4_final_evaluation_latest.json"
    try:
        dated_payload = json.loads(dated.read_text(encoding="utf-8-sig"))
        latest_payload = json.loads(latest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "stage4_final_evaluation_missing_or_unreadable"
    if not isinstance(dated_payload, Mapping) or not isinstance(latest_payload, Mapping):
        return False, "stage4_final_evaluation_invalid"
    if dict(dated_payload) != dict(latest_payload):
        return False, "stage4_final_evaluation_latest_mismatch"
    valid, reason = validate_stage4_final_evaluation_artifact(
        dated_payload, target_date
    )
    if not valid:
        return False, reason
    source = dated_payload.get("source_adapter")
    if not isinstance(source, Mapping):
        return False, "stage4_final_evaluation_source_invalid"
    declared = str(source.get("sha256") or "").lower()
    if len(declared) != 71 or not declared.startswith("sha256:"):
        return False, "stage4_final_evaluation_source_invalid"
    source_path = Path(str(source.get("path") or ""))
    if not source_path.is_absolute():
        source_path = root / source_path
    adapter_root = (root / "results" / "stage4_replay_adapter").resolve()
    try:
        source_path = source_path.resolve()
        source_path.relative_to(adapter_root)
        raw = source_path.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return False, "stage4_final_evaluation_source_missing_or_invalid"
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual != declared:
        return False, "stage4_final_evaluation_source_hash_mismatch"
    try:
        adapter_payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return False, "stage4_final_evaluation_source_invalid"
    if not isinstance(adapter_payload, Mapping):
        return False, "stage4_final_evaluation_source_invalid"
    if (
        str(adapter_payload.get("schema_version") or "").strip() == ""
        or adapter_payload.get("task_type") != "stage4_tri_account_replay_adapter"
        or adapter_payload.get("fixture_or_synthetic") is not False
    ):
        return False, "stage4_final_evaluation_source_invalid"
    replay_product = adapter_payload.get("replay_product")
    quality_gate = adapter_payload.get("quality_gate")
    if not isinstance(replay_product, Mapping) or not isinstance(quality_gate, Mapping):
        return False, "stage4_final_evaluation_source_invalid"
    if quality_gate.get("passed") is True and (
        str(replay_product.get("as_of") or "")[:10] != target_date
        or str(replay_product.get("window_end") or "")[:10] != target_date
    ):
        return False, "stage4_final_evaluation_source_identity_mismatch"
    return True, ""


def build_synthetic_replay_fixture(
    *,
    kind: str,
    trade_count: int | None = None,
    window_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build deterministic synthetic forward-runner products for three shapes."""

    dates = list(window_dates or [f"2026-08-{i:02d}" for i in range(1, 20)])
    if kind == "normal_sample":
        n = trade_count if trade_count is not None else 32
        trades = []
        for i in range(n):
            entry = dates[i % max(1, len(dates) - 2)]
            exit_d = dates[min(len(dates) - 1, (i % max(1, len(dates) - 2)) + 2)]
            pnl = round(0.01 * ((i % 5) - 2), 6)
            trades.append(
                {
                    "trade_id": f"syn_normal_{i + 1}",
                    "symbol": f"60000{i % 10}",
                    "entry_date": entry,
                    "exit_date": exit_d,
                    "hold_days": 2 + (i % 4),
                    "pnl_pct": pnl,
                    "fees": 5.0 + i * 0.1,
                    "evidence_eligible": True,
                    "evidence_ref": f"sha256:fixture_normal_{i + 1}",
                    "reason_codes": [],
                }
            )
        nav = [1.0]
        for t in trades:
            nav.append(round(nav[-1] * (1.0 + float(t["pnl_pct"])), 8))
        return {
            "window_start": dates[0],
            "window_end": dates[-1],
            "window_dates": dates,
            "as_of": dates[-1],
            "trades": trades,
            "nav_series": [{"date": dates[min(i, len(dates) - 1)], "nav": v} for i, v in enumerate(nav)],
            "effective_sell_sample": n,
            "evidence_eligible": True,
            "fixture_kind": "normal_sample",
            "reason_codes": [],
        }

    if kind == "insufficient_sample":
        n = trade_count if trade_count is not None else 8
        trades = []
        for i in range(n):
            trades.append(
                {
                    "trade_id": f"syn_thin_{i + 1}",
                    "symbol": "600000",
                    "entry_date": dates[0],
                    "exit_date": dates[min(3, len(dates) - 1)],
                    "hold_days": 3,
                    "pnl_pct": 0.02 if i % 2 == 0 else -0.01,
                    "fees": 3.0,
                    "evidence_eligible": True,
                    "evidence_ref": f"sha256:fixture_thin_{i + 1}",
                    "reason_codes": [],
                }
            )
        return {
            "window_start": dates[0],
            "window_end": dates[-1],
            "window_dates": dates,
            "as_of": dates[-1],
            "trades": trades,
            "nav_series": [1.0, 1.01, 1.005],
            "effective_sell_sample": n,
            "evidence_eligible": False,
            "fixture_kind": "insufficient_sample",
            "reason_codes": ["acceptance_fixture_not_strategy_evidence"],
        }

    if kind == "benchmark_missing":
        product = build_synthetic_replay_fixture(kind="normal_sample", trade_count=32, window_dates=dates)
        product["fixture_kind"] = "benchmark_missing"
        product["benchmark_expected_status"] = "benchmark_unavailable"
        return product

    raise ValueError(f"unknown fixture kind: {kind!r}")


__all__ = [
    "FINAL_ARTIFACT_SCHEMA_VERSION",
    "validate_stage4_final_evaluation_pair",
    "Stage4FinalEvaluation",
    "TradeEvidenceRef",
    "build_stage4_final_evaluation_artifact",
    "evaluate_stage4_final",
    "build_synthetic_replay_fixture",
    "EVALUATOR_SCHEMA_VERSION",
    "MIN_EFFECTIVE_SAMPLE",
    "validate_stage4_final_evaluation_artifact",
    "write_stage4_final_evaluation_artifact",
]
