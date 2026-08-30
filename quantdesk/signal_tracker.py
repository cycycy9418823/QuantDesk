"""Signal tracker — record strategy signals and track historical win rates.

Records each buy/sell signal a strategy generates, then after N days
computes the outcome. Produces per-signal and per-strategy statistics:
  - total signals triggered
  - win rate
  - average gain / average loss
  - best / worst outcome

Signals are stored in a local JSON file for persistence.

All results are for local research only. Not investment advice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

SIGNAL_DISCLAIMER = (
    "信号回顾仅用于本地历史统计，历史胜率不保证未来结果，"
    "不构成投资建议，不自动下单。"
)


@dataclass
class SignalRecord:
    """One recorded signal."""

    signal_id: str = ""
    symbol: str = ""
    name: str = ""
    strategy: str = ""
    signal_type: str = ""  # buy / sell / hold
    signal_date: str = ""
    signal_price: float = 0.0
    data_grade: str = ""

    # outcome (filled after evaluation)
    outcome_date: str = ""
    outcome_price: float = 0.0
    outcome_pnl_pct: float = 0.0
    outcome_days: int = 0
    outcome_status: str = ""  # win / loss / pending / expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "name": self.name,
            "strategy": self.strategy,
            "signal_type": self.signal_type,
            "signal_date": self.signal_date,
            "signal_price": round(self.signal_price, 3),
            "data_grade": self.data_grade,
            "outcome_date": self.outcome_date,
            "outcome_price": round(self.outcome_price, 3),
            "outcome_pnl_pct": round(self.outcome_pnl_pct, 6),
            "outcome_days": self.outcome_days,
            "outcome_status": self.outcome_status,
        }

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> SignalRecord:
        return SignalRecord(
            signal_id=str(d.get("signal_id", "")),
            symbol=str(d.get("symbol", "")),
            name=str(d.get("name", "")),
            strategy=str(d.get("strategy", "")),
            signal_type=str(d.get("signal_type", "")),
            signal_date=str(d.get("signal_date", "")),
            signal_price=float(d.get("signal_price", 0)),
            data_grade=str(d.get("data_grade", "")),
            outcome_date=str(d.get("outcome_date", "")),
            outcome_price=float(d.get("outcome_price", 0)),
            outcome_pnl_pct=float(d.get("outcome_pnl_pct", 0)),
            outcome_days=int(d.get("outcome_days", 0)),
            outcome_status=str(d.get("outcome_status", "")),
        )


@dataclass
class SignalStats:
    """Statistics for a group of signals."""

    strategy: str = ""
    signal_type: str = ""
    total_signals: int = 0
    evaluated_signals: int = 0
    pending_signals: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    best_outcome: float = 0.0
    worst_outcome: float = 0.0
    avg_hold_days: float = 0.0
    disclaimer: str = SIGNAL_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "signal_type": self.signal_type,
            "total_signals": self.total_signals,
            "evaluated_signals": self.evaluated_signals,
            "pending_signals": self.pending_signals,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_gain": round(self.avg_gain, 6),
            "avg_loss": round(self.avg_loss, 6),
            "best_outcome": round(self.best_outcome, 6),
            "worst_outcome": round(self.worst_outcome, 6),
            "avg_hold_days": round(self.avg_hold_days, 1),
            "disclaimer": self.disclaimer,
        }


def generate_signal_id(symbol: str, strategy: str, signal_date: str, signal_type: str) -> str:
    """Generate a deterministic signal ID."""
    return f"{symbol}_{strategy}_{signal_date}_{signal_type}"


def record_signal(
    symbol: str,
    name: str,
    strategy: str,
    signal_type: str,
    signal_date: str,
    signal_price: float,
    data_grade: str = "",
) -> SignalRecord:
    """Create a new signal record."""
    return SignalRecord(
        signal_id=generate_signal_id(symbol, strategy, signal_date, signal_type),
        symbol=symbol,
        name=name,
        strategy=strategy,
        signal_type=signal_type,
        signal_date=signal_date,
        signal_price=signal_price,
        data_grade=data_grade,
        outcome_status="pending",
    )


def evaluate_signal(
    signal: SignalRecord,
    outcome_price: float,
    outcome_date: str,
    outcome_days: int = 0,
) -> SignalRecord:
    """Evaluate a signal's outcome by comparing signal price to outcome price."""
    if signal.signal_price <= 0:
        signal.outcome_status = "expired"
        return signal

    if signal.signal_type == "buy":
        pnl_pct = (outcome_price - signal.signal_price) / signal.signal_price
    elif signal.signal_type == "sell":
        pnl_pct = (signal.signal_price - outcome_price) / signal.signal_price
    else:
        pnl_pct = 0.0

    signal.outcome_price = outcome_price
    signal.outcome_date = outcome_date
    signal.outcome_pnl_pct = pnl_pct
    signal.outcome_days = outcome_days
    signal.outcome_status = "win" if pnl_pct > 0 else "loss"
    return signal


def compute_signal_stats(
    signals: Sequence[SignalRecord],
    strategy: str = "",
    signal_type: str = "",
) -> SignalStats:
    """Compute statistics for a group of signals."""
    evaluated = [s for s in signals if s.outcome_status in ("win", "loss")]
    pending = [s for s in signals if s.outcome_status == "pending"]
    wins = [s for s in evaluated if s.outcome_status == "win"]
    losses = [s for s in evaluated if s.outcome_status == "loss"]

    win_rate = len(wins) / len(evaluated) if evaluated else 0.0
    avg_gain = sum(s.outcome_pnl_pct for s in wins) / len(wins) if wins else 0.0
    avg_loss = sum(s.outcome_pnl_pct for s in losses) / len(losses) if losses else 0.0
    best = max((s.outcome_pnl_pct for s in evaluated), default=0.0)
    worst = min((s.outcome_pnl_pct for s in evaluated), default=0.0)
    avg_days = sum(s.outcome_days for s in evaluated) / len(evaluated) if evaluated else 0.0

    return SignalStats(
        strategy=strategy,
        signal_type=signal_type,
        total_signals=len(signals),
        evaluated_signals=len(evaluated),
        pending_signals=len(pending),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_gain=avg_gain,
        avg_loss=avg_loss,
        best_outcome=best,
        worst_outcome=worst,
        avg_hold_days=avg_days,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_signals(path: Path) -> list[SignalRecord]:
    """Load signals from a JSON file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [SignalRecord.from_dict(d) for d in data]
    except Exception as exc:  # noqa: BLE001
        marker = path.with_suffix(path.suffix + ".corrupted")
        try:
            marker.write_text(
                json.dumps(
                    {
                        "status": "warning",
                        "failed_reason": f"signal history JSON corrupted: {exc}",
                        "source": str(path),
                        "disclaimer": SIGNAL_DISCLAIMER,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as marker_exc:  # noqa: BLE001
            logger.warning("failed to write signal corruption marker %s: %s", marker, marker_exc)
        logger.warning("signal history JSON corrupted at %s: %s", path, exc)
    return []


def save_signals(signals: Sequence[SignalRecord], path: Path) -> Path:
    """Save signals to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [s.to_dict() for s in signals]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def append_signal(signal: SignalRecord, path: Path) -> Path:
    """Append a signal to the store, deduplicating by signal_id."""
    existing = load_signals(path)
    ids = {s.signal_id for s in existing}
    if signal.signal_id not in ids:
        existing.append(signal)
    return save_signals(existing, path)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def signal_stats_to_markdown(stats_list: Sequence[SignalStats]) -> str:
    """Render signal statistics as Markdown."""
    lines: list[str] = []
    lines.append("# 信号回顾统计")
    lines.append("")
    lines.append(f"> {SIGNAL_DISCLAIMER}")
    lines.append("")

    if not stats_list:
        lines.append("没有可统计的信号。")
        return "\n".join(lines)

    lines.append("| 策略 | 类型 | 总信号 | 已评估 | 胜率 | 平均盈利 | 平均亏损 | 最佳 | 最差 | 平均天数 |")
    lines.append("|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in stats_list:
        lines.append(
            f"| {s.strategy} | {s.signal_type} | {s.total_signals} "
            f"| {s.evaluated_signals} | {s.win_rate:.1%} "
            f"| {s.avg_gain:+.2%} | {s.avg_loss:+.2%} "
            f"| {s.best_outcome:+.2%} | {s.worst_outcome:+.2%} "
            f"| {s.avg_hold_days:.0f} |"
        )

    lines.append("")
    lines.append(f"*{SIGNAL_DISCLAIMER}*")
    return "\n".join(lines)
