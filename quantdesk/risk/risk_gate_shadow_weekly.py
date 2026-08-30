"""Round 9S Part C — risk-gate shadow weekly aggregator (read-only, honest modes).

Consumes the daily shadow artifacts produced by
``risk_gate_shadow.summarize_shadow_records`` / ``write_shadow_summary``
(``results/risk_gate_shadow_latest.json`` plus any dated archives) and rolls
them up into a weekly review that answers a single governance question:

    "以当前证据，stop point 3b（风险门真拦截审批）还缺什么？"

The historical no-observation baseline is retained only as provenance. Current
production observations must come from validated dated archives; zero-traffic
days remain valid observations, while sparse, stale, weekend, or conflicting
copies cannot satisfy the five-trading-session threshold.

This module is pure + read-only over its inputs. It never evaluates a risk
decision, never enforces anything (``enforcement_applied`` is always False in
the source records — stop point 3b is record-only), and writes only to
``reports/`` (never a shadow sink or any canonical/state path), so it cannot
collide with the daily-chain tripwire.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar
from quantdesk.risk.risk_gate_shadow import shadow_archive_seal_rejection_reason
from quantdesk.version import API_SCHEMA_VERSION

WEEKLY_SCHEMA_VERSION = "round9s_risk_gate_shadow_weekly_v1"

# Below this many observed days the weekly draws no trend conclusion.
MIN_OBSERVED_DAYS = 5

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice. Shadow observation only; "
    "risk_gate_enforced=false and enforcement is never applied (stop point 3b is record-only)."
)

SHADOW_ENFORCEMENT_FIELDS = ("risk_gate_enforced", "enforcement_applied")
SHADOW_COUNT_FIELDS = (
    "observed_count",
    "evaluation_count",
    "would_pass_count",
    "would_block_count",
    "insufficient_evidence_count",
    "legacy_blocked_count",
    "shadow_error_count",
)
SHADOW_REQUIRED_FIELDS = SHADOW_ENFORCEMENT_FIELDS + SHADOW_COUNT_FIELDS

# The concrete evidence that stop point 3b (a real enforcing gate that can
# block an approval) still lacks. Each entry is stated as a gap, not a promise.
STOP_POINT_3B_REQUIREMENTS = (
    {
        "requirement": "sustained_shadow_observation",
        "detail": (
            "至少 MIN_OBSERVED_DAYS 个自然交易日的连续、通过校验的 shadow 按日归档。"
        ),
    },
    {
        "requirement": "nonzero_evaluation_volume",
        "detail": "评估笔数 > 0 且覆盖买/卖两侧，否则 would_block 恒为 0 无从校准。",
    },
    {
        "requirement": "complete_evidence_components",
        "detail": (
            "missing_components 归零：缺失证据组件时 would_block 可能是缺料"
            "而非真实风险，拦截阈值不可信。"
        ),
    },
    {
        "requirement": "false_block_review",
        "detail": "对 would_block=true 的样本人工复核，确认非误拦（无对照即无法定阈值）。",
    },
    {
        "requirement": "acceptor_ruling_to_enforce",
        "detail": (
            "验收人单独裁定把 enforcement_applied 由 False 翻转为 True 才可真拦截；"
            "当前铁律 risk_gate_enforced=false 未解除。"
        ),
    },
)


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_daily_summary(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("task_type") or "") == "risk_gate_shadow_daily_summary"


def _daily_summary_rejection_reason(payload: Mapping[str, Any]) -> str | None:
    if not _is_daily_summary(payload):
        return "payload_task_type_invalid"
    if any(name not in payload for name in SHADOW_ENFORCEMENT_FIELDS):
        return "shadow_required_field_missing"
    if (
        payload.get("risk_gate_enforced") is not False
        or payload.get("enforcement_applied") is not False
    ):
        return "shadow_enforcement_boundary_breach"
    counts: dict[str, int] = {}
    for name in SHADOW_COUNT_FIELDS:
        if name not in payload:
            return "shadow_required_field_missing"
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "shadow_count_malformed"
        counts[name] = value
    if counts["observed_count"] != (
        counts["evaluation_count"]
        + counts["legacy_blocked_count"]
        + counts["shadow_error_count"]
    ):
        return "shadow_count_inconsistent"
    if counts["evaluation_count"] != (
        counts["would_pass_count"]
        + counts["would_block_count"]
        + counts["insufficient_evidence_count"]
    ):
        return "shadow_count_inconsistent"
    return None


def _trailing_trading_days(as_of: str, count: int) -> list[str]:
    cutoff = _valid_iso_day(as_of)
    if cutoff is None:
        raise ValueError("as_of must be YYYY-MM-DD")
    current = date.fromisoformat(cutoff)
    calendar = load_cn_trading_calendar()
    days: list[str] = []
    while len(days) < count:
        if is_cn_a_share_trading_day(current, calendar):
            days.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(days))


@dataclass
class ShadowWeeklyReview:
    as_of: str
    mode: str  # "no_data" | "insufficient" | "observed"
    observed_days: int
    window_start: str | None
    window_end: str | None
    totals: dict[str, int]
    reason_counts: dict[str, int]
    missing_component_counts: dict[str, int]
    per_day: list[dict[str, Any]]
    stop_point_3b_gaps: list[dict[str, Any]]
    reason_codes: list[str] = field(default_factory=list)
    input_rejections: list[dict[str, Any]] = field(default_factory=list)
    enforcement_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": "risk_gate_shadow_weekly_review",
            "schema_version": WEEKLY_SCHEMA_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "generated_at": _now_text(),
            "as_of": self.as_of,
            "mode": self.mode,
            "observed_days": self.observed_days,
            "min_observed_days": MIN_OBSERVED_DAYS,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "totals": dict(self.totals),
            "reason_counts": dict(self.reason_counts),
            "missing_component_counts": dict(self.missing_component_counts),
            "per_day": list(self.per_day),
            "stop_point_3b_gaps": list(self.stop_point_3b_gaps),
            "reason_codes": list(self.reason_codes),
            "input_rejections": list(self.input_rejections),
            "risk_gate_enforced": False,
            "enforcement_applied": False,
            "shadow_only": True,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": DISCLAIMER,
        }


def _evaluate_3b_gaps(
    *,
    observed_days: int,
    totals: Mapping[str, int],
    missing_component_counts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Mark each 3b requirement met/unmet from the observed evidence only.

    ``acceptor_ruling_to_enforce`` and ``false_block_review`` are never marked
    met programmatically — they require a human ruling / review that no artifact
    can self-certify.
    """

    evaluation_count = _int(totals.get("evaluation_count"))
    would_block = _int(totals.get("would_block_count"))
    gaps: list[dict[str, Any]] = []
    for item in STOP_POINT_3B_REQUIREMENTS:
        requirement = item["requirement"]
        if requirement == "sustained_shadow_observation":
            met = observed_days >= MIN_OBSERVED_DAYS
            evidence = f"observed_days={observed_days} (need >= {MIN_OBSERVED_DAYS})"
        elif requirement == "nonzero_evaluation_volume":
            met = evaluation_count > 0
            evidence = f"evaluation_count={evaluation_count}"
        elif requirement == "complete_evidence_components":
            missing_total = sum(_int(v) for v in missing_component_counts.values())
            met = evaluation_count > 0 and missing_total == 0
            evidence = f"missing_component_total={missing_total}"
        elif requirement == "false_block_review":
            # Never auto-satisfied; needs human review of would_block samples.
            met = False
            evidence = f"would_block_count={would_block} (human review required)"
        else:  # acceptor_ruling_to_enforce
            met = False
            evidence = "enforcement_applied=false; needs a separate acceptor ruling"
        gaps.append(
            {
                "requirement": requirement,
                "detail": item["detail"],
                "met": bool(met),
                "evidence": evidence,
            }
        )
    return gaps


def summarize_shadow_weekly(
    daily_summaries: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> ShadowWeeklyReview:
    """Roll up a week of daily shadow summaries honestly.

    ``daily_summaries`` is a sequence of ``risk_gate_shadow_daily_summary``
    payloads (the schema emitted by ``summarize_shadow_records``). Anything not
    matching that ``task_type`` is ignored rather than silently counted, so a
    stray artifact cannot inflate the observed-day count.
    """

    expected_days = set(_trailing_trading_days(as_of, MIN_OBSERVED_DAYS))
    selected_inputs: dict[str, tuple[datetime, dict[str, Any], str]] = {}
    input_rejections: list[dict[str, str]] = []
    for index, summary in enumerate(daily_summaries):
        display = f"<input:{index}>"
        if not isinstance(summary, Mapping):
            input_rejections.append(
                {"path": display, "reason": "payload_task_type_invalid"}
            )
            continue
        reason = _daily_summary_rejection_reason(summary)
        if reason is not None:
            input_rejections.append({"path": display, "reason": reason})
            continue
        day = _valid_iso_day(summary.get("as_of"))
        if day is None:
            input_rejections.append(
                {"path": display, "reason": "payload_as_of_invalid"}
            )
            continue
        if not is_cn_a_share_trading_day(date.fromisoformat(day), load_cn_trading_calendar()):
            input_rejections.append(
                {"path": display, "reason": "as_of_not_trading_day", "as_of": day}
            )
            continue
        if day not in expected_days:
            input_rejections.append(
                {
                    "path": display,
                    "reason": "outside_trailing_trading_window",
                    "as_of": day,
                }
            )
            continue
        generated_at = _generated_at_sort_key(summary) or datetime.min.replace(
            tzinfo=timezone.utc
        )
        previous = selected_inputs.get(day)
        if previous is None or generated_at > previous[0]:
            if previous is not None:
                input_rejections.append(
                    {
                        "path": previous[2],
                        "reason": "duplicate_as_of_older_copy",
                        "as_of": day,
                    }
                )
            selected_inputs[day] = (generated_at, dict(summary), display)
        else:
            input_rejections.append(
                {
                    "path": display,
                    "reason": "duplicate_as_of_older_copy",
                    "as_of": day,
                }
            )
    valid = [row[1] for _, row in sorted(selected_inputs.items())]
    valid.sort(key=lambda s: str(s.get("as_of") or ""))
    observed_days = len(valid)

    totals = {
        "observed_count": sum(_int(s.get("observed_count")) for s in valid),
        "evaluation_count": sum(_int(s.get("evaluation_count")) for s in valid),
        "would_pass_count": sum(_int(s.get("would_pass_count")) for s in valid),
        "would_block_count": sum(_int(s.get("would_block_count")) for s in valid),
        "insufficient_evidence_count": sum(
            _int(s.get("insufficient_evidence_count")) for s in valid
        ),
        "legacy_blocked_count": sum(_int(s.get("legacy_blocked_count")) for s in valid),
        "shadow_error_count": sum(_int(s.get("shadow_error_count")) for s in valid),
    }

    reason_counter: Counter[str] = Counter()
    missing_counter: Counter[str] = Counter()
    per_day: list[dict[str, Any]] = []
    for summary in valid:
        for reason, count in (summary.get("reason_counts") or {}).items():
            if str(reason):
                reason_counter[str(reason)] += _int(count)
        for component, count in (summary.get("missing_component_counts") or {}).items():
            if str(component):
                missing_counter[str(component)] += _int(count)
        per_day.append(
            {
                "as_of": summary.get("as_of"),
                "evaluation_count": _int(summary.get("evaluation_count")),
                "would_pass_count": _int(summary.get("would_pass_count")),
                "would_block_count": _int(summary.get("would_block_count")),
                "insufficient_evidence_count": _int(summary.get("insufficient_evidence_count")),
                "legacy_blocked_count": _int(summary.get("legacy_blocked_count")),
                "shadow_error_count": _int(summary.get("shadow_error_count")),
            }
        )

    window_start = valid[0].get("as_of") if valid else None
    window_end = valid[-1].get("as_of") if valid else None

    reason_codes: list[str] = []
    if observed_days == 0:
        mode = "no_data"
        reason_codes.append("no_shadow_observation_yet")
        reason_codes.append("no_accepted_daily_archive_in_window")
    elif observed_days < MIN_OBSERVED_DAYS:
        mode = "insufficient"
        reason_codes.append("insufficient_observed_days")
    else:
        mode = "observed"
    if totals["evaluation_count"] == 0 and observed_days > 0:
        reason_codes.append("zero_evaluations_in_window")

    gaps = _evaluate_3b_gaps(
        observed_days=observed_days,
        totals=totals,
        missing_component_counts=dict(missing_counter),
    )

    return ShadowWeeklyReview(
        as_of=as_of,
        mode=mode,
        observed_days=observed_days,
        window_start=window_start if isinstance(window_start, str) else None,
        window_end=window_end if isinstance(window_end, str) else None,
        totals=totals,
        reason_counts=dict(reason_counter.most_common()),
        missing_component_counts=dict(missing_counter.most_common()),
        per_day=per_day,
        stop_point_3b_gaps=gaps,
        reason_codes=reason_codes,
        input_rejections=input_rejections,
    )


def load_daily_summaries(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Read daily shadow summary artifacts from disk (unreadable files skipped)."""

    out: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            out.append(dict(payload))
    return out


@dataclass
class ShadowDailyWindow:
    summaries: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    sources_by_as_of: dict[str, str]


def _generated_at_sort_key(payload: Mapping[str, Any]) -> datetime | None:
    text = str(payload.get("generated_at") or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_iso_day(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed.isoformat() if text == parsed.isoformat() else None


def load_shadow_daily_window(
    *,
    archive_dir: Path,
    latest_path: Path,
    as_of: str,
    limit: int | None = MIN_OBSERVED_DAYS,
) -> ShadowDailyWindow:
    """Load, validate, de-duplicate, and bound daily shadow observations.

    Dated files must be named exactly ``<payload.as_of>.json``.  ``latest`` is
    only an alternate copy and cannot inflate the observed-day count.  For one
    ``as_of`` the payload with the newest ``generated_at`` wins; ties prefer the
    dated archive as the durable source.
    """

    cutoff = _valid_iso_day(as_of)
    if cutoff is None:
        raise ValueError("as_of must be YYYY-MM-DD")
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be positive or None")

    archive_root = Path(archive_dir)
    latest = Path(latest_path)
    paths = sorted(archive_root.glob("*.json")) if archive_root.exists() else []
    if latest.is_file():
        paths.append(latest)
    expected_days = (
        set(_trailing_trading_days(cutoff, int(limit)))
        if limit is not None
        else None
    )
    calendar = load_cn_trading_calendar()

    selected: dict[str, tuple[datetime, bool, dict[str, Any], str]] = {}
    rejected: list[dict[str, str]] = []
    for path in paths:
        is_latest = path == latest
        if not is_latest and _valid_iso_day(path.stem) is None:
            rejected.append({"path": str(path), "reason": "archive_filename_invalid"})
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            rejected.append({"path": str(path), "reason": "payload_unreadable"})
            continue
        if not isinstance(payload, Mapping) or not _is_daily_summary(payload):
            rejected.append({"path": str(path), "reason": "payload_task_type_invalid"})
            continue
        day = _valid_iso_day(payload.get("as_of"))
        if day is None:
            rejected.append({"path": str(path), "reason": "payload_as_of_invalid"})
            continue
        if not is_latest and path.stem != day:
            rejected.append({"path": str(path), "reason": "archive_filename_mismatch"})
            continue
        if day > cutoff:
            rejected.append({"path": str(path), "reason": "payload_after_as_of", "as_of": day})
            continue
        if not is_cn_a_share_trading_day(date.fromisoformat(day), calendar):
            rejected.append(
                {"path": str(path), "reason": "as_of_not_trading_day", "as_of": day}
            )
            continue
        if expected_days is not None and day not in expected_days:
            rejected.append(
                {
                    "path": str(path),
                    "reason": "outside_trailing_trading_window",
                    "as_of": day,
                }
            )
            continue
        payload_reason = _daily_summary_rejection_reason(payload)
        if payload_reason is not None:
            rejection: dict[str, Any] = {
                "path": str(path),
                "reason": payload_reason,
                "as_of": day,
            }
            if payload_reason == "shadow_required_field_missing":
                rejection["missing_fields"] = [
                    name for name in SHADOW_REQUIRED_FIELDS if name not in payload
                ]
            rejected.append(rejection)
            continue
        if not is_latest:
            seal_reason = shadow_archive_seal_rejection_reason(path, payload)
            if seal_reason is not None:
                rejected.append(
                    {"path": str(path), "reason": seal_reason, "as_of": day}
                )
                continue
        generated_at = _generated_at_sort_key(payload)
        if generated_at is None:
            rejected.append({"path": str(path), "reason": "generated_at_invalid"})
            continue
        previous = selected.get(day)
        if is_latest and previous is not None and previous[1]:
            dated_payload = dict(previous[2])
            dated_payload.pop("archive_source", None)
            latest_payload = dict(payload)
            latest_payload.pop("archive_source", None)
            reason = (
                "duplicate_as_of_equivalent_copy"
                if dated_payload == latest_payload
                else "latest_conflicts_with_dated_archive"
            )
            rejected.append({"path": str(path), "reason": reason, "as_of": day})
            continue
        if is_latest and previous is None:
            rejected.append(
                {
                    "path": str(path),
                    "reason": "latest_without_valid_dated_archive",
                    "as_of": day,
                }
            )
            continue
        candidate = (generated_at, not is_latest, dict(payload), str(path))
        if previous is None or candidate[:2] > previous[:2]:
            if previous is not None:
                rejected.append(
                    {
                        "path": previous[3],
                        "reason": "duplicate_as_of_older_copy",
                        "as_of": day,
                    }
                )
            selected[day] = candidate
        else:
            rejected.append(
                {"path": str(path), "reason": "duplicate_as_of_older_copy", "as_of": day}
            )

    days = sorted(selected)
    if limit is not None:
        days = days[-int(limit) :]
    return ShadowDailyWindow(
        summaries=[selected[day][2] for day in days],
        rejected=rejected,
        sources_by_as_of={day: selected[day][3] for day in days},
    )


def _mode_banner(mode: str) -> str:
    return {
        "no_data": "无数据（NO DATA）——shadow 尚无任何生产观察产物",
        "insufficient": "数据不足（INSUFFICIENT）——观察天数低于最小样本",
        "observed": "已观察（OBSERVED）",
    }.get(mode, mode)


def render_weekly_markdown(review: ShadowWeeklyReview) -> str:
    """Render the weekly review as Markdown (honest, no beautification)."""

    payload = review.to_dict()
    lines: list[str] = []
    lines.append("# Risk-Gate Shadow 周报（stop point 3b 观察）")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"As of: {payload['as_of']}")
    lines.append("")
    lines.append(
        "Research Only · Paper Account Only · No Broker Route · Live Execution Locked · "
        "Not Investment Advice · Not Position Advice"
    )
    lines.append("")
    lines.append(
        "> 本报告仅汇总风险门在 shadow 模式下"
        "「若生效会怎样」的记录；当前不拦截、不修改、不跳过任何 legacy 纸面动作。"
    )
    lines.append("")
    lines.append(f"## 模式：{_mode_banner(review.mode)}")
    lines.append("")
    lines.append(f"- 观察天数：{review.observed_days}（最小样本 {MIN_OBSERVED_DAYS}）")
    lines.append(f"- 观察窗口：{review.window_start or '—'} … {review.window_end or '—'}")
    if review.reason_codes:
        lines.append(f"- 原因码：{', '.join(review.reason_codes)}")
    lines.append("")

    if review.mode == "no_data":
        lines.append(
            "当前窗口没有通过校验的 shadow 按日归档。"
            "本周报以诚实无数据模式交付；观察只能随自然纸面例行逐日积累。"
        )
        lines.append("")
    else:
        t = review.totals
        lines.append("## 汇总（仅记录，无行为效应）")
        lines.append("")
        lines.append(f"- 评估笔数：{t['evaluation_count']}")
        lines.append(f"- would_pass：{t['would_pass_count']}")
        lines.append(f"- would_block（仅 vetoed）：{t['would_block_count']}")
        lines.append(f"- insufficient_evidence：{t['insufficient_evidence_count']}")
        lines.append(f"- legacy_blocked（未过门）：{t['legacy_blocked_count']}")
        lines.append(f"- shadow_error：{t['shadow_error_count']}")
        lines.append("")
        if review.reason_counts:
            top = ", ".join(f"{k}={v}" for k, v in list(review.reason_counts.items())[:10])
            lines.append(f"- top reason_codes：{top}")
        if review.missing_component_counts:
            miss = ", ".join(
                f"{k}={v}" for k, v in list(review.missing_component_counts.items())[:10]
            )
            lines.append(f"- missing_components：{miss}")
        lines.append("")
        lines.append("### 逐日")
        lines.append("")
        lines.append("| 日期 | 评估 | would_pass | would_block | insufficient | legacy_blocked | error |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for day in review.per_day:
            lines.append(
                f"| {day['as_of']} | {day['evaluation_count']} | {day['would_pass_count']} "
                f"| {day['would_block_count']} | {day['insufficient_evidence_count']} "
                f"| {day['legacy_blocked_count']} | {day['shadow_error_count']} |"
            )
        lines.append("")
        if review.mode == "insufficient":
            lines.append(
                "> 观察天数不足最小样本，本周不下任何趋势结论；数字仅供逐日核对。"
            )
            lines.append("")

    lines.append("## 以当前证据，stop point 3b（真拦截审批）还缺什么")
    lines.append("")
    lines.append("| 需求 | 满足 | 证据 | 说明 |")
    lines.append("| --- | :---: | --- | --- |")
    for gap in review.stop_point_3b_gaps:
        met = "✅" if gap["met"] else "❌"
        lines.append(
            f"| {gap['requirement']} | {met} | {gap['evidence']} | {gap['detail']} |"
        )
    lines.append("")
    lines.append(
        "> enforcement_applied 恒为 false：把 shadow 记录翻转为真实拦截，"
        "需验收人单独裁定解除 risk_gate_enforced=false，本工具不做、也无权做此翻转。"
    )
    lines.append("")
    return "\n".join(lines)


def write_weekly_report(
    review: ShadowWeeklyReview,
    *,
    reports_dir: Path,
    reports_root: Path,
    as_of: str,
) -> dict[str, Path]:
    """Write the weekly report to ``reports/`` as paired .md + .json.

    Writes only under ``reports_dir`` — never a shadow sink, canonical, or
    state path — so it is safe to run alongside the daily chain.
    """

    reports_dir = Path(reports_dir).resolve()
    allowed_root = Path(reports_root).resolve()
    try:
        reports_dir.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("weekly report output must stay under reports_root") from exc
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"risk_gate_shadow_weekly_{as_of}.md"
    json_path = reports_dir / f"risk_gate_shadow_weekly_{as_of}.json"
    md_path.write_text(render_weekly_markdown(review), encoding="utf-8")
    json_path.write_text(
        json.dumps(review.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"markdown": md_path, "json": json_path}


__all__ = [
    "ShadowDailyWindow",
    "ShadowWeeklyReview",
    "WEEKLY_SCHEMA_VERSION",
    "MIN_OBSERVED_DAYS",
    "STOP_POINT_3B_REQUIREMENTS",
    "summarize_shadow_weekly",
    "load_daily_summaries",
    "load_shadow_daily_window",
    "render_weekly_markdown",
    "write_weekly_report",
]
