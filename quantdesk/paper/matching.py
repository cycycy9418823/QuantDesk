"""Shared paper matching *policy declarations*.

This is not a finished unified matching engine. Each ledger header declares
which policy it uses. Ledgers must not impersonate each other. Existing fill
paths still live in the AI trader and the strategy book. Declaring a policy
does not verify a fill.

Do not treat a stamped header as proof that a fill had authentic evidence.

Current live policies:
- ``next_open``: fill at the next session open; a limit-up open cannot buy;
  a limit-down open cannot sell; missing open is no fill.
- ``daily_approx``: the strategy book's existing fill path (minutes when
  present, otherwise a labelled daily approximation when allowed).

``snapshot`` and ``limit_vs_1m`` are registered for later stages and are not live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from quantdesk.board_rules import classify_board

POLICY_NEXT_OPEN = "next_open"
POLICY_DAILY_APPROX = "daily_approx"
POLICY_SNAPSHOT = "snapshot"
POLICY_LIMIT_VS_1M = "limit_vs_1m"

FILL_MODEL_NEXT_OPEN_ONLY = "next_open_only"
FILL_MODEL_DAILY_APPROX = "daily_approx"

AI_TRADER_LEDGER_ID = "ai_trader_100k"
MATCHING_SOURCE = "quantdesk.paper.matching"


@dataclass(frozen=True)
class FillPolicy:
    id: str
    fill_model: str
    label_zh: str
    live: bool
    notes_zh: str

    def header(self) -> dict[str, Any]:
        return {
            "fill_policy": self.id,
            "fill_policy_fill_model": self.fill_model,
            "fill_policy_label_zh": self.label_zh,
            "fill_policy_live": self.live,
            "fill_policy_source": MATCHING_SOURCE,
        }


POLICIES: dict[str, FillPolicy] = {
    POLICY_NEXT_OPEN: FillPolicy(
        id=POLICY_NEXT_OPEN,
        fill_model=FILL_MODEL_NEXT_OPEN_ONLY,
        label_zh="次日开盘价，一字涨停买不进、一字跌停卖不出",
        live=True,
        notes_zh="AI 账当前规则。成交证据是次日开盘价，不用当天高低价成交。",
    ),
    POLICY_DAILY_APPROX: FillPolicy(
        id=POLICY_DAILY_APPROX,
        fill_model=FILL_MODEL_DAILY_APPROX,
        label_zh="有分钟用分钟，没有分钟时用日线近似（允许时）",
        live=True,
        notes_zh="策略账当前规则。公式仍在策略账自己的成交路径里，这里只声明。",
    ),
    POLICY_SNAPSHOT: FillPolicy(
        id=POLICY_SNAPSHOT,
        fill_model="snapshot",
        label_zh="对 5 分钟快照成交（未启用）",
        live=False,
        notes_zh="登记用。A1 之前不得当作已启用。",
    ),
    POLICY_LIMIT_VS_1M: FillPolicy(
        id=POLICY_LIMIT_VS_1M,
        fill_model="limit_vs_1m",
        label_zh="限价对 1 分钟 bar 成交（未启用）",
        live=False,
        notes_zh="登记用。A1 之前不得当作已启用。",
    ),
}


def get_policy(policy_id: str) -> FillPolicy:
    key = str(policy_id or "").strip()
    policy = POLICIES.get(key)
    if policy is None:
        raise ValueError(f"unknown_fill_policy:{key}")
    return policy


def policy_header(policy_id: str) -> dict[str, Any]:
    return get_policy(policy_id).header()


def declared_fill_policy(ledger: Mapping[str, Any] | None) -> str:
    if not isinstance(ledger, Mapping):
        return ""
    return str(ledger.get("fill_policy") or "").strip()


def stamp_fill_policy(ledger: dict[str, Any], policy_id: str) -> dict[str, Any]:
    """Write the policy onto a ledger header. Refuse impersonation and unlive policies."""

    policy = get_policy(policy_id)
    if not policy.live:
        raise ValueError(f"fill_policy_not_live:{policy.id}")
    existing = declared_fill_policy(ledger)
    if existing and existing != policy.id:
        raise ValueError(f"fill_policy_impersonation:{existing}->{policy.id}")
    ledger.update(policy.header())
    return ledger


def default_policy_for_ledger(ledger: Mapping[str, Any] | None) -> str:
    ledger_id = ""
    if isinstance(ledger, Mapping):
        ledger_id = str(ledger.get("ledger_id") or "").strip()
    if ledger_id == AI_TRADER_LEDGER_ID:
        return POLICY_NEXT_OPEN
    return POLICY_DAILY_APPROX


def ensure_declared_fill_policy(ledger: dict[str, Any], *, policy_id: str | None = None) -> dict[str, Any]:
    chosen = policy_id or declared_fill_policy(ledger) or default_policy_for_ledger(ledger)
    return stamp_fill_policy(ledger, chosen)


def _finite(value: float | None) -> bool:
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def next_open_limit_blocked(
    symbol: str, pct_chg: float | None, side: str, *, is_st: bool = False
) -> str | None:
    """Limit-up cannot buy, limit-down cannot sell, using the open gap.

    Same numeric tolerance the AI trader already used: board limit minus 0.3
    percentage points. Missing gap is not treated as a limit block.
    """

    if not _finite(pct_chg):
        return None
    change = float(pct_chg)
    try:
        rules = classify_board(symbol, is_st=is_st)
        up = float(rules.limit_up_pct) - 0.3
        down = float(rules.limit_down_pct) + 0.3
    except Exception:
        up, down = 9.7, -9.7
    if side == "buy" and change >= up:
        return f"limit_up:{symbol}:{change:.2f}%"
    if side == "sell" and change <= down:
        return f"limit_down:{symbol}:{change:.2f}%"
    return None


def evaluate_next_open_fill(
    bar: Mapping[str, Any] | None,
    *,
    symbol: str,
    side: str,
    prev_close: float | None = None,
    trade_date: str = "",
) -> dict[str, Any]:
    """Accept a next-open fill only with raw, dated, sourced price evidence."""

    row = dict(bar or {})
    basis_reason = str(row.get("price_basis_reason") or "")
    basis = str(row.get("price_basis") or "")
    if basis_reason == "mixed_price_basis":
        return {"ok": False, "reason": "mixed_price_basis"}
    if basis_reason == "unknown_price_basis" or basis in {"", "mixed_or_unknown"}:
        return {"ok": False, "reason": "unknown_price_basis"}
    if basis != "raw":
        return {"ok": False, "reason": "consumer_price_basis_not_allowed"}

    suspended = str(row.get("is_suspended") or "").strip().lower()
    if suspended in {"1", "1.0", "true", "yes"}:
        return {"ok": False, "reason": "suspended"}

    open_status = str(row.get("open_status") or "ok")
    open_px = row.get("open")
    if open_status == "missing" or open_px is None:
        return {"ok": False, "reason": "missing_open_price"}
    if open_status == "nonfinite" or not _finite(open_px):
        return {"ok": False, "reason": "nonfinite_open_price"}
    if float(open_px) <= 0:
        return {"ok": False, "reason": "invalid_open_price"}

    volume_status = str(row.get("volume_status") or "ok")
    volume = row.get("volume")
    if volume_status == "missing" or volume is None:
        return {"ok": False, "reason": "missing_volume"}
    if volume_status == "nonfinite" or not _finite(volume):
        return {"ok": False, "reason": "nonfinite_volume"}
    if float(volume) == 0:
        return {"ok": False, "reason": "zero_volume"}
    if float(volume) < 0:
        return {"ok": False, "reason": "invalid_volume"}

    if not _finite(prev_close) or float(prev_close) <= 0:
        return {"ok": False, "reason": "missing_or_invalid_previous_close"}
    gap_pct = (float(open_px) / float(prev_close) - 1.0) * 100.0
    is_st = str(row.get("is_st") or "").strip().lower() in {"1", "1.0", "true", "yes"}
    blocked = next_open_limit_blocked(symbol, gap_pct, side, is_st=is_st)
    if blocked:
        return {"ok": False, "reason": blocked}

    source = str(row.get("source") or "")
    missing_tokens = {"", "nan", "none", "null", "<na>", "nat", "n/a", "na"}
    if source.strip().lower() in missing_tokens:
        return {"ok": False, "reason": "missing_evidence_source"}
    timestamp_field = "fetched_at"
    timestamp = str(row.get(timestamp_field) or "")
    if timestamp.strip().lower() in missing_tokens:
        timestamp_field = "updated_at"
        timestamp = str(row.get(timestamp_field) or "")
    if timestamp.strip().lower() in missing_tokens:
        return {"ok": False, "reason": "missing_evidence_timestamp"}
    try:
        date.fromisoformat(trade_date)
        if str(row.get("date") or "") != trade_date:
            raise ValueError("bar date differs from the requested session")
    except (TypeError, ValueError):
        return {"ok": False, "reason": "price_evidence_date_mismatch"}
    try:
        if len(timestamp.strip()) <= 10:
            raise ValueError("a date without a time is not a fetch timestamp")
        datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "invalid_evidence_timestamp"}
    evidence = {
        "symbol": symbol,
        "trade_date": trade_date,
        "side": side,
        "price_field": "open",
        "price": float(open_px),
        "price_basis": "raw",
        "source": source,
        "timestamp": timestamp,
        "timestamp_field": timestamp_field,
        timestamp_field: timestamp,
        "volume": float(volume),
    }
    return {
        "ok": True,
        "reason": "",
        "price": float(open_px),
        "price_basis": "raw",
        "evidence_source": source,
        "evidence_timestamp": timestamp,
        "price_evidence": evidence,
    }


__all__ = [
    "AI_TRADER_LEDGER_ID",
    "FILL_MODEL_DAILY_APPROX",
    "FILL_MODEL_NEXT_OPEN_ONLY",
    "FillPolicy",
    "MATCHING_SOURCE",
    "POLICIES",
    "POLICY_DAILY_APPROX",
    "POLICY_LIMIT_VS_1M",
    "POLICY_NEXT_OPEN",
    "POLICY_SNAPSHOT",
    "declared_fill_policy",
    "default_policy_for_ledger",
    "ensure_declared_fill_policy",
    "evaluate_next_open_fill",
    "get_policy",
    "next_open_limit_blocked",
    "policy_header",
    "stamp_fill_policy",
]
