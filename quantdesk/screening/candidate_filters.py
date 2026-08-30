"""Single candidate-eligibility gate for stock screening (Phase 2).

Centralizes five A-share exclusion rules that were previously missing on the
fallback universe path or only enforced ad-hoc:

1. ST / *ST / 退 (delisting-risk) names
2. explicit delisted symbols (from a provided delisted set / overlay)
3. new listings (insufficient history / listed_days below a floor)
4. suspended bars (zero volume or flat OHLC with no volume)
5. one-word limit-up bars (涨停一字板) — not buyable next session, must not
   silently enter the candidate ledger as if fillable

Design: pure functions, no IO beyond what callers pass in. The screener main
path and its fallback universe both call :func:`evaluate_candidate_eligibility`
so they apply *identical* exclusions. Reasons are returned (not silently
dropped) so callers can surface why a symbol was excluded.

Research-only; not investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from quantdesk.shared import normalize_symbol, safe_float

# ST / delisting-risk name markers (matched case-insensitively on the name).
ST_NAME_MARKERS: tuple[str, ...] = ("ST", "*ST", "退")

#: Default minimum number of daily bars / listed days before a symbol is
#: considered out of its "new listing" window. New listings have outsized
#: volatility and frequently no price limit on day one, so they distort
#: screening; excluded by default.
DEFAULT_MIN_HISTORY_DAYS = 60


@dataclass
class EligibilityResult:
    """Outcome of evaluating one candidate against the eligibility gate."""

    eligible: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


def is_excluded_name(name: Any) -> bool:
    """True if *name* carries an ST / *ST / 退市 marker."""

    text = str(name or "").strip().upper()
    if not text:
        return False
    return any(marker in text for marker in ST_NAME_MARKERS)


def _is_suspended_row(row: Mapping[str, Any]) -> bool:
    """Conservative suspension/non-tradable detection from a metrics/quote row.

    Treats zero/ën volume as suspended. If OHLC are all equal *and* volume is
    zero/missing, also suspended. Missing volume alone is not enough (some
    sources omit it), so we only flag when we positively see zero volume.
    """

    vol = safe_float(row.get("volume") if "volume" in row else row.get("avg_volume_20d"), None)
    if vol is not None and vol <= 0:
        return True
    return False


def is_one_word_limit_candidate(row: Mapping[str, Any]) -> bool:
    """True if the row looks like a one-word limit-up bar (涨停一字板).

    Uses the ``limit_up_proxy`` flag emitted by ``symbol_metrics`` when present,
    else falls back to an OHLC check (open≈high≈low≈close near the top with a
    strongly positive day). One-word limit-up bars cannot be bought the next
    session, so they must not enter the candidate ledger as fillable.
    """

    proxy = row.get("limit_up_proxy")
    if isinstance(proxy, bool):
        return proxy
    # Fallback: open == high == low == close (a sealed one-word bar) with a
    # positive move. Only applies when we actually have OHLC.
    o = safe_float(row.get("open"), None)
    h = safe_float(row.get("high"), None)
    low = safe_float(row.get("low"), None)
    c = safe_float(row.get("close"), None)
    if None in (o, h, low, c):
        return False
    flat = abs(h - low) <= 1e-9 and abs(o - c) <= 1e-9 and abs(o - h) <= 1e-9
    if not flat:
        return False
    if isinstance(row.get("is_limit_up"), bool):
        return bool(row.get("is_limit_up"))
    limit_up_price = safe_float(row.get("limit_up_price"), None)
    if limit_up_price is not None and limit_up_price > 0:
        return c >= limit_up_price * 0.999
    pct_chg = safe_float(row.get("pct_chg"), None)
    if pct_chg is not None:
        # QuantDesk pct_chg columns are percent points (9.9 == +9.9%).
        return pct_chg >= 9.8
    change_pct = safe_float(row.get("change_pct"), None)
    if change_pct is None:
        change_pct = safe_float(row.get("pct_change"), None)
    if change_pct is not None:
        # Generic change_pct may be fraction (0.099) or percent (9.9).
        return change_pct >= 0.098 if abs(change_pct) <= 1.0 else change_pct >= 9.8
    pre_close = safe_float(row.get("pre_close") if "pre_close" in row else row.get("prev_close"), None)
    if pre_close is not None and pre_close > 0:
        return (c / pre_close - 1.0) >= 0.098
    return False


def evaluate_candidate_eligibility(
    row: Mapping[str, Any],
    *,
    name: Any = None,
    delisted_symbols: Iterable[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    exclude_one_word_limit: bool = True,
    exclude_new_listings: bool = True,
    exclude_suspended: bool = True,
) -> EligibilityResult:
    """Evaluate one candidate row against the five exclusion rules.

    Parameters
    ----------
    row:
        A metrics / quote dict for one symbol (as produced by ``symbol_metrics``
        or a spot snapshot row).
    name:
        Display name; if omitted, ``row['name']`` is used. Used for ST detection.
    delisted_symbols:
        Optional set of normalized symbols known to be delisted (PIT overlay).
    min_history_days:
        Minimum bars/listed-days; below this the symbol is a "new listing".
    exclude_one_word_limit / exclude_new_listings / exclude_suspended:
        Toggles, all on by default.
    """

    reasons: list[str] = []
    symbol = normalize_symbol(row.get("symbol") or row.get("code") or "")

    resolved_name = name if name is not None else row.get("name")
    if is_excluded_name(resolved_name):
        reasons.append("st_or_delisting_name")

    if delisted_symbols:
        delisted = {normalize_symbol(s) for s in delisted_symbols}
        if symbol and symbol in delisted:
            reasons.append("delisted_symbol")

    if exclude_new_listings:
        history_days = row.get("history_days")
        if history_days is None:
            history_days = row.get("listed_days")
        if history_days is None:
            history_days = row.get("row_count")
        hd = safe_float(history_days, None)
        if hd is not None and hd < float(min_history_days):
            reasons.append(f"new_listing_history_days_lt_{min_history_days}")

    if exclude_suspended and _is_suspended_row(row):
        reasons.append("suspended_zero_volume")

    if exclude_one_word_limit and is_one_word_limit_candidate(row):
        reasons.append("one_word_limit_up_not_buyable")

    return EligibilityResult(eligible=not reasons, reasons=reasons)


def filter_candidate_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    name_map: Mapping[str, str] | None = None,
    delisted_symbols: Iterable[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    exclude_one_word_limit: bool = True,
    exclude_new_listings: bool = True,
    exclude_suspended: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into (kept, excluded). Excluded rows carry ``exclude_reasons``.

    ``name_map`` (symbol -> name) lets callers supply names when the rows
    themselves lack them (the screener fallback path historically had no name,
    defeating ST detection entirely).
    """

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    names = {normalize_symbol(k): v for k, v in (name_map or {}).items()}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or "")
        name = row.get("name") or names.get(symbol)
        result = evaluate_candidate_eligibility(
            row,
            name=name,
            delisted_symbols=delisted_symbols,
            min_history_days=min_history_days,
            exclude_one_word_limit=exclude_one_word_limit,
            exclude_new_listings=exclude_new_listings,
            exclude_suspended=exclude_suspended,
        )
        enriched = dict(row)
        if result.eligible:
            kept.append(enriched)
        else:
            enriched["exclude_reasons"] = result.reasons
            excluded.append(enriched)
    return kept, excluded
