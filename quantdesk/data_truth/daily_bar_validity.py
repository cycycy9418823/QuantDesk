"""Single source of truth for daily-bar validity.

Both data planes must answer the same question with the same rules:
"may this row be written into a normal canonical daily CSV?"

Rules (identical for the fast snapshot path and the mootdx/engine slow path):
    open > 0, high > 0, low > 0, close > 0, volume > 0,
    high >= max(open, close, low), low <= min(open, close, high).

A row failing any rule is NOT a normal trading bar. Zero-volume flat rows are
how several vendors encode "suspended today"; writing them as bars violates
this repository's own convention (600929 carries 2,043 historical bars and not
one of them has zero volume). The fetch side must instead classify suspension
and record evidence - never fabricate a bar.
"""

from __future__ import annotations

from typing import Any, Mapping

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def is_valid_daily_bar(open_: Any, high: Any, low: Any, close: Any, volume: Any) -> tuple[bool, str]:
    """Return ``(valid, reason_code)`` for one candidate daily bar row."""

    def _num(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if out != out:  # NaN
            return None
        return out

    values = {
        "open": _num(open_),
        "high": _num(high),
        "low": _num(low),
        "close": _num(close),
        "volume": _num(volume),
    }
    for name, val in values.items():
        if val is None:
            return False, f"invalid_daily_bar:{name}_not_numeric"
        if name == "volume":
            if val <= 0:
                return False, "invalid_daily_bar:volume_not_positive"
        elif val <= 0:
            return False, f"invalid_daily_bar:{name}_not_positive"
    if values["high"] < max(values["open"], values["close"], values["low"]):
        return False, "invalid_daily_bar:high_below_body"
    if values["low"] > min(values["open"], values["close"], values["high"]):
        return False, "invalid_daily_bar:low_above_body"
    return True, ""


def daily_bar_validity_from_row(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Validity of one row mapping that carries the standard column names."""

    return is_valid_daily_bar(
        row.get("open"),
        row.get("high"),
        row.get("low"),
        row.get("close"),
        row.get("volume"),
    )


def filter_invalid_daily_bars(
    frame: Any,
    *,
    symbol: str = "",
    max_reason_samples: int = 5,
) -> tuple[Any, list[str]]:
    """Drop invalid bar rows from a normalized daily frame.

    Returns ``(filtered_frame, reasons)``. The frame is returned unchanged
    (same object) when it is empty or lacks the required columns, so callers
    without those columns fail open to the previous behaviour instead of
    crashing. ``frame.attrs`` is preserved because the slow path stores
    baostock session-suspension evidence there.
    """

    try:
        columns = list(frame.columns)
    except AttributeError:
        return frame, []
    if frame is None or len(frame) == 0:
        return frame, []
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        return frame, []

    keep_flags = []
    reasons: list[str] = []
    for row in frame.itertuples(index=False):
        data = dict(zip(columns, row, strict=True))
        ok, reason = daily_bar_validity_from_row(data)
        keep_flags.append(ok)
        if not ok and len(reasons) < max_reason_samples:
            date = str(data.get("date") or "")
            reasons.append(f"{reason}:{symbol}:{date}" if symbol else f"{reason}:{date}")
    if all(keep_flags):
        return frame, []

    attrs = getattr(frame, "attrs", None)
    filtered = frame[[bool(f) for f in keep_flags]]
    if attrs is not None:
        try:
            filtered.attrs = dict(attrs)
        except Exception:  # noqa: BLE001 - attrs are advisory metadata
            pass
    dropped = len(frame) - len(filtered)
    if dropped and len(reasons) < max_reason_samples:
        reasons.append(f"invalid_daily_bars_dropped:{symbol or '-'}:count={dropped}")
    return filtered, reasons
