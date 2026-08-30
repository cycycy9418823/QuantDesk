from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

from quantdesk.platform_common import PROJECT_ROOT, base_payload, decision_id, finalize_outputs, now_iso

DEFAULT_PATH = PROJECT_ROOT / "state" / "decision_journal.jsonl"
DISCLAIMER = "Decision journal is research-only. No broker. No real orders."


def _resolve(path: str | Path = DEFAULT_PATH) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _default_outcome_review() -> dict[str, Any]:
    return {"status": "pending", "forward_returns": {}, "hit": None, "reviewed_at": None, "notes": ""}


def add_decision_entry(entry: dict, path: str | Path = DEFAULT_PATH) -> dict:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    confidence = _float(entry.get("confidence"))
    row = {
        "decision_id": entry.get("decision_id") or decision_id(),
        "created_at": entry.get("created_at") or now_iso(),
        "symbol": str(entry.get("symbol") or ""),
        "name": entry.get("name") or "",
        "strategy": entry.get("strategy") or "observe",
        "thesis": entry.get("thesis") or "",
        "invalidation_condition": entry.get("invalidation_condition") or "",
        "expected_horizon": entry.get("expected_horizon") or "5d",
        "confidence": confidence if confidence is not None else 0,
        "emotion_tag": entry.get("emotion_tag") or "uncertain",
        "source_task": entry.get("source_task") or "manual",
        "reference_price": _float(entry.get("reference_price")),
        "risk_flags": list(entry.get("risk_flags") or []),
        "outcome_review": entry.get("outcome_review") or _default_outcome_review(),
        "research_only": True,
        "disclaimer": DISCLAIMER,
    }
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    row["status"] = "passed"
    return row


def list_decision_entries(path: str | Path = DEFAULT_PATH, filters: dict | None = None) -> dict:
    target = _resolve(path)
    filters = dict(filters or {})
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if target.exists():
        for line_no, line in enumerate(target.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"decision_journal_invalid_jsonl_line:{line_no}")
                continue
            if filters.get("symbol") and row.get("symbol") != filters["symbol"]:
                continue
            if filters.get("strategy") and row.get("strategy") != filters["strategy"]:
                continue
            rows.append(row)
    return {"status": "warning" if warnings else "passed", "items": rows, "entry_count": len(rows), "path": str(target), "warnings": warnings}


def compute_decision_outcome(
    entry: dict,
    market_rows: list[dict] | None = None,
    *,
    horizons: Sequence[int] = (1, 5, 20),
) -> dict:
    review = dict(entry.get("outcome_review") or _default_outcome_review())
    rows = list(market_rows or [])
    reference_price = _float(entry.get("reference_price"))
    if not rows:
        review.update({"status": "data_missing", "reviewed_at": now_iso(), "notes": review.get("notes") or "market_data_missing"})
        return review
    rows.sort(key=lambda item: str(item.get("date") or item.get("trade_date") or ""))
    closes = [_float(row.get("close")) for row in rows]
    if reference_price is None:
        reference_price = next((value for value in closes if value is not None), None)
    if reference_price in (None, 0):
        review.update({"status": "data_missing", "reviewed_at": now_iso(), "notes": "reference_price_missing"})
        return review
    fwd: dict[str, float | None] = {}
    for horizon in horizons:
        value = closes[horizon] if horizon < len(closes) else None
        fwd[f"{horizon}d"] = round(value / reference_price - 1, 6) if value is not None else None
    direction = str(entry.get("thesis_direction") or entry.get("strategy") or "").lower()
    primary = fwd.get("5d") if fwd.get("5d") is not None else next((value for value in fwd.values() if value is not None), None)
    hit = None
    if primary is not None:
        hit = primary <= 0 if direction in {"avoid", "short", "risk"} else primary > 0
    review.update({"status": "reviewed" if primary is not None else "data_missing", "forward_returns": fwd, "hit": hit, "reviewed_at": now_iso()})
    return review


def review_decision_outcomes(path: str | Path = DEFAULT_PATH, market_data: dict | None = None) -> dict:
    payload = list_decision_entries(path)
    market_data = dict(market_data or {})
    for row in payload["items"]:
        rows = market_data.get(row.get("symbol")) or market_data.get("rows") or []
        row["outcome_review"] = compute_decision_outcome(row, rows)
    payload["review_count"] = len(payload["items"])
    payload["statistics"] = summarize_decision_statistics(payload["items"])
    return payload


def summarize_by_strategy(entries: list[dict]) -> list[dict]:
    return _summarize_group(entries, "strategy")


def summarize_by_emotion(entries: list[dict]) -> list[dict]:
    return _summarize_group(entries, "emotion_tag")


def summarize_by_confidence_bucket(entries: list[dict]) -> list[dict]:
    bucketed = []
    for row in entries:
        confidence = _float(row.get("confidence")) or 0
        if confidence >= 80:
            bucket = "high"
        elif confidence >= 50:
            bucket = "medium"
        else:
            bucket = "low"
        copy = dict(row)
        copy["confidence_bucket"] = bucket
        bucketed.append(copy)
    return _summarize_group(bucketed, "confidence_bucket")


def _summarize_group(entries: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in entries:
        groups[str(row.get(key) or "unknown")].append(row)
    out: list[dict[str, Any]] = []
    for name, rows in sorted(groups.items()):
        reviewed = [row for row in rows if (row.get("outcome_review") or {}).get("status") == "reviewed"]
        hits = [row for row in reviewed if (row.get("outcome_review") or {}).get("hit") is True]
        fwd5 = [
            (row.get("outcome_review") or {}).get("forward_returns", {}).get("5d")
            for row in reviewed
            if isinstance((row.get("outcome_review") or {}).get("forward_returns", {}).get("5d"), (int, float))
        ]
        out.append({
            key: name,
            "count": len(rows),
            "reviewed_count": len(reviewed),
            "hit_rate": round(len(hits) / len(reviewed), 6) if reviewed else None,
            "avg_forward_5d": round(sum(fwd5) / len(fwd5), 6) if fwd5 else None,
        })
    return out


def summarize_decision_statistics(entries: list[dict]) -> dict:
    reviewed = [row for row in entries if (row.get("outcome_review") or {}).get("status") == "reviewed"]
    pending = [row for row in entries if (row.get("outcome_review") or {}).get("status") in {"pending", "data_missing", None}]
    hits = [row for row in reviewed if (row.get("outcome_review") or {}).get("hit") is True]
    fwd5 = [
        (row.get("outcome_review") or {}).get("forward_returns", {}).get("5d")
        for row in reviewed
        if isinstance((row.get("outcome_review") or {}).get("forward_returns", {}).get("5d"), (int, float))
    ]
    risk_flags = Counter(flag for row in entries for flag in row.get("risk_flags", []))
    return {
        "total_decisions": len(entries),
        "reviewed_count": len(reviewed),
        "pending_count": len(pending),
        "hit_rate": round(len(hits) / len(reviewed), 6) if reviewed else None,
        "avg_forward_5d": round(sum(fwd5) / len(fwd5), 6) if fwd5 else None,
        "by_strategy": summarize_by_strategy(entries),
        "by_emotion": summarize_by_emotion(entries),
        "by_confidence_bucket": summarize_by_confidence_bucket(entries),
        "common_risk_flags": [{"risk_flag": key, "count": value} for key, value in risk_flags.most_common(10)],
    }


def _synthetic_entries() -> list[dict]:
    rows = [
        {"symbol": "600000", "strategy": "observe", "thesis": "calm value review", "emotion_tag": "calm", "confidence": 72, "reference_price": 10.0},
        {"symbol": "300750", "strategy": "long_swing", "thesis": "growth momentum review", "emotion_tag": "fomo", "confidence": 86, "reference_price": 20.0},
    ]
    market = {
        "600000": [{"date": f"2024-01-{idx + 1:02d}", "close": 10.0 + idx * 0.03} for idx in range(25)],
        "300750": [{"date": f"2024-01-{idx + 1:02d}", "close": 20.0 - idx * 0.02 + idx * idx * 0.005} for idx in range(25)],
    }
    out = []
    for row in rows:
        item = {
            "decision_id": decision_id(),
            "created_at": now_iso(),
            "name": "",
            "invalidation_condition": "",
            "expected_horizon": "5d",
            "source_task": "synthetic_test_data",
            "risk_flags": [],
            "research_only": True,
            **row,
        }
        item["outcome_review"] = compute_decision_outcome(item, market[item["symbol"]])
        out.append(item)
    return out


def build_decision_journal_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    path = cfg.get("path") or "state/decision_journal.jsonl"
    warnings: list[str] = []
    if cfg.get("synthetic_test_data"):
        entries = _synthetic_entries()
        data = {"status": "passed", "items": entries, "entry_count": len(entries), "path": "synthetic", "warnings": ["synthetic_decision_journal_used"]}
    else:
        if cfg.get("add"):
            add_decision_entry({
                "symbol": cfg.get("symbol") or "",
                "strategy": cfg.get("strategy") or "observe",
                "thesis": cfg.get("thesis") or "manual research note",
                "emotion_tag": cfg.get("emotion_tag") or "uncertain",
                "confidence": cfg.get("confidence") or 0,
            }, path)
        data = review_decision_outcomes(path) if cfg.get("review") else list_decision_entries(path, {"symbol": cfg.get("symbol"), "strategy": cfg.get("strategy")})
    warnings.extend(data.get("warnings") or [])
    entries = list(data.get("items") or [])
    statistics = data.get("statistics") or summarize_decision_statistics(entries)
    status = "warning" if warnings else "passed"
    payload = base_payload("decision_journal", status=status, warnings=list(dict.fromkeys(warnings)))
    payload.update({
        "disclaimer": DISCLAIMER,
        "synthetic_test_data": bool(cfg.get("synthetic_test_data")),
        **data,
        "statistics": statistics,
        "total_decisions": statistics.get("total_decisions", len(entries)),
        "reviewed_count": statistics.get("reviewed_count", 0),
        "pending_count": statistics.get("pending_count", 0),
        "hit_rate": statistics.get("hit_rate"),
        "by_strategy": statistics.get("by_strategy", []),
        "by_emotion": statistics.get("by_emotion", []),
        "by_confidence_bucket": statistics.get("by_confidence_bucket", []),
    })
    return finalize_outputs(payload, "decision_journal", render_decision_journal_markdown(payload))


def render_decision_journal_markdown(payload: dict) -> str:
    stats = payload.get("statistics") or {}
    lines = [
        "# Decision Journal",
        "",
        f"- status: `{payload.get('status')}`",
        f"- total_decisions: `{stats.get('total_decisions')}`",
        f"- reviewed_count: `{stats.get('reviewed_count')}`",
        f"- hit_rate: `{stats.get('hit_rate')}`",
        "",
        "| decision_id | symbol | strategy | emotion | review |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in payload.get("items", [])[:20]:
        review = item.get("outcome_review") or {}
        lines.append(f"| {item.get('decision_id')} | {item.get('symbol')} | {item.get('strategy')} | {item.get('emotion_tag')} | {review.get('status')} |")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])[:30]]])
    lines.extend(["", payload.get("disclaimer") or DISCLAIMER])
    return "\n".join(lines)
