"""The trader's own investment philosophy — written by it, for itself.

The operator never tells this trader what kind of investor to be. So its style has
to live somewhere, and it has to be allowed to change: a real person starts out
convinced day-trading is the way, gets burned, drifts to holding things longer,
and can articulate why. That drift is the whole thing worth watching, and it is
invisible unless it is recorded.

Two things are stored:

- ``stance``: what the trader currently believes and how it wants to invest, in its
  own words, plus a revision history so a change of mind is visible rather than
  silently overwritten.
- ``style_ledger``: realised outcomes grouped by the style each position was opened
  under. This is what turns "I think I'm a swing trader" into "swing trades have
  lost money for me 7 times out of 10", which is the evidence the trader gets to
  reason over next time.

Nothing here constrains what it may do. It is a diary, not a rulebook — a stance
is a statement of intent, and the trader is free to contradict its own stance and
then explain why.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_trader.paths import PHILOSOPHY_PATH
from quantdesk.utils import atomic_write_json

SCHEMA = "ai_trader_philosophy_v1"
MAX_REVISIONS = 60
MAX_NOTES = 40


def _empty() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "stance": {
            "summary": "",
            "preferred_styles": [],
            "updated_on": "",
            "reasoning": "",
        },
        "revisions": [],
        "style_ledger": {},
        "notes": [],
    }


def load_philosophy(path: Path | None = None) -> dict[str, Any]:
    """Read the trader's philosophy, degrading to an empty one when absent.

    A corrupt or truncated file is moved aside before degrading. Without that, the
    next ``save_philosophy`` would overwrite it with the empty object and every
    stance, revision, note and style record would be gone — on the one file whose
    entire purpose is to be a long-lived record of the trader changing its mind,
    on a machine that blue-screens.
    """

    target = Path(path) if path is not None else PHILOSOPHY_PATH
    if not target.is_file():
        return _empty()
    payload: Any = None
    failure = ""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    if not failure and not isinstance(payload, dict):
        failure = f"unexpected top-level type: {type(payload).__name__}"
    if failure:
        base = _empty()
        base["error"] = failure
        backup = _move_aside(target)
        if backup:
            base["backup_path"] = str(backup)
        return base
    base = _empty()
    for key in base:
        if key in payload:
            base[key] = payload[key]
    return base


def _move_aside(target: Path) -> Path | None:
    """Rename a damaged philosophy file so it is never silently overwritten."""

    for index in range(1, 100):
        candidate = target.with_name(f"{target.stem}.bad{index:02d}{target.suffix}")
        if candidate.exists():
            continue
        try:
            target.rename(candidate)
        except OSError:
            return None
        return candidate
    return None


def record_stance(
    philosophy: dict[str, Any],
    *,
    as_of: str,
    summary: str,
    preferred_styles: list[str],
    reasoning: str,
) -> dict[str, Any]:
    """Set the current stance, keeping the previous one in the revision history.

    A restatement of the same summary is not a revision; only an actual change of
    mind is appended, so the history stays a record of turning points.
    """

    out = dict(philosophy)
    stance = dict(out.get("stance") or {})
    summary = str(summary or "").strip()
    if not summary:
        return out
    if str(stance.get("summary") or "").strip() != summary:
        revisions = list(out.get("revisions") or [])
        if stance.get("summary"):
            revisions.append({
                "on": stance.get("updated_on") or "",
                "summary": stance.get("summary") or "",
                "preferred_styles": list(stance.get("preferred_styles") or []),
                "reasoning": stance.get("reasoning") or "",
            })
        out["revisions"] = revisions[-MAX_REVISIONS:]
    out["stance"] = {
        "summary": summary,
        "preferred_styles": [str(s) for s in (preferred_styles or []) if s],
        "updated_on": str(as_of or "")[:10],
        "reasoning": str(reasoning or "").strip(),
    }
    return out


def record_closed_trade(
    philosophy: dict[str, Any],
    *,
    style: str,
    net_return: float | None,
    hold_days: int | None,
) -> dict[str, Any]:
    """Attribute one finished position to the style it was opened under."""

    label = str(style or "").strip() or "未标注"
    out = dict(philosophy)
    ledger = dict(out.get("style_ledger") or {})
    row = dict(ledger.get(label) or {"trades": 0, "wins": 0, "sum_net": 0.0, "sum_hold_days": 0})
    row["trades"] = int(row.get("trades") or 0) + 1
    if net_return is not None:
        value = float(net_return)
        row["sum_net"] = round(float(row.get("sum_net") or 0.0) + value, 6)
        if value > 0:
            row["wins"] = int(row.get("wins") or 0) + 1
    if hold_days is not None:
        row["sum_hold_days"] = int(row.get("sum_hold_days") or 0) + int(hold_days)
    trades = max(int(row["trades"]), 1)
    row["mean_net"] = round(float(row.get("sum_net") or 0.0) / trades, 6)
    row["win_rate"] = round(int(row.get("wins") or 0) / trades, 4)
    row["mean_hold_days"] = round(int(row.get("sum_hold_days") or 0) / trades, 1)
    ledger[label] = row
    out["style_ledger"] = ledger
    return out


def add_note(philosophy: dict[str, Any], *, as_of: str, note: str) -> dict[str, Any]:
    """Append a dated lesson the trader wants to remember."""

    text = str(note or "").strip()
    if not text:
        return philosophy
    out = dict(philosophy)
    notes = list(out.get("notes") or [])
    notes.append({"on": str(as_of or "")[:10], "note": text})
    out["notes"] = notes[-MAX_NOTES:]
    return out


def style_scoreboard(philosophy: dict[str, Any]) -> list[str]:
    """Short human lines the trader can be shown about its own track record."""

    ledger = philosophy.get("style_ledger") or {}
    lines: list[str] = []
    for label, row in sorted(
        ledger.items(), key=lambda kv: int((kv[1] or {}).get("trades") or 0), reverse=True
    ):
        if not isinstance(row, dict) or not row.get("trades"):
            continue
        lines.append(
            f"{label}: {row['trades']}笔, 平均净{float(row.get('mean_net') or 0.0):+.2%}, "
            f"胜率{float(row.get('win_rate') or 0.0):.0%}, 平均持有{row.get('mean_hold_days')}天"
        )
    return lines


def save_philosophy(philosophy: dict[str, Any], path: Path | None = None) -> Path:
    target = Path(path) if path is not None else PHILOSOPHY_PATH
    payload = dict(philosophy)
    # Transient load-failure markers describe this run, not the trader's beliefs.
    payload.pop("error", None)
    payload.pop("backup_path", None)
    payload["schema_version"] = SCHEMA
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, payload)
    return target


__all__ = [
    "PHILOSOPHY_PATH",
    "SCHEMA",
    "add_note",
    "load_philosophy",
    "record_closed_trade",
    "record_stance",
    "save_philosophy",
    "style_scoreboard",
]
