"""Read-only project records and bounded runtime observations for the home page.

This module never writes STATUS.md, never fetches market data, and never
runs health checks. ``readable`` means the file could be opened — not that
the desk is healthy.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

STATUS_FILENAME = "STATUS.md"
SOURCE_LABEL = "项目记录 · 非实时核验"
DISCLAIMER = "纸面 · 未验证"
MAX_STATUS_BYTES = 256 * 1024
MAX_RUNTIME_OBSERVATION_BYTES = 8 * 1024 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")

_HEADER_RE = re.compile(
    r"^#\s*STATUS\s*[·•]\s*更新于\s*(?P<when>.+?)\s*[·•]\s*更新者[：:]\s*(?P<who>.+?)\s*$"
)
_REMAINING_RE = re.compile(r"剩\s*(?P<days>.+?)\s*个交易日")
_CHECKPOINT_RE = re.compile(r"检查点\s*(?P<when>\d{4}-\d{2}-\d{2})")


def _now_iso() -> str:
    return datetime.now(SHANGHAI).replace(microsecond=0).isoformat()


def _file_mtime_iso(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=SHANGHAI)
    return stamp.replace(microsecond=0).isoformat()


def _display_or_missing(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text in {"—", "-", "–", "－"}:
        return "未提供"
    return text


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_sections(body: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if current_title or current_lines:
                sections.append(
                    {
                        "title": current_title,
                        "body": "\n".join(current_lines).strip("\n"),
                    }
                )
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_title or any(item.strip() for item in current_lines):
        sections.append(
            {
                "title": current_title,
                "body": "\n".join(current_lines).strip("\n"),
            }
        )
    return sections


def _section_body(sections: list[dict[str, str]], title: str) -> str:
    for item in sections:
        if item["title"] == title:
            return item["body"]
    return ""


def _first_content_line(text: str) -> str:
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("（") and not stripped.startswith("("):
            return stripped
    return ""


def _paren_or_following_explanation(text: str) -> str:
    lines = [line.rstrip() for line in text.split("\n")]
    collected: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if stripped.startswith("（") or stripped.startswith("("):
                started = True
                collected.append(stripped)
                if stripped.endswith("）") or stripped.endswith(")"):
                    break
            continue
        if not stripped:
            break
        collected.append(stripped)
        if stripped.endswith("）") or stripped.endswith(")"):
            break
    if collected:
        return "\n".join(collected)
    # Fall back: everything after the first content line.
    content = _first_content_line(text)
    if not content:
        return ""
    rest = text.split(content, 1)[-1].strip()
    return rest


def _labeled_block(text: str, label: str) -> str:
    marker = label.rstrip("：:") + "："
    alt = label.rstrip("：:") + ":"
    lines = text.split("\n")
    capture = False
    collected: list[str] = []
    stop_prefixes = ("最近三件事", "下一步", "卡住")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(marker) or stripped.startswith(alt):
            remainder = stripped.split("：", 1)[-1] if "：" in stripped else stripped.split(":", 1)[-1]
            remainder = remainder.strip()
            capture = True
            collected = [remainder] if remainder else []
            continue
        if capture:
            if any(stripped.startswith(prefix) for prefix in stop_prefixes):
                break
            if re.match(r"^#{1,6}\s", stripped):
                break
            collected.append(line.rstrip())
    return "\n".join(collected).strip()


def _parse_line_progress(body: str) -> dict[str, str]:
    return {
        "recent": _labeled_block(body, "最近三件事") or "未提供",
        "next": _labeled_block(body, "下一步") or "未提供",
        "stuck": _labeled_block(body, "卡住") or "未提供",
    }


def _parse_phase(text: str) -> dict[str, str]:
    line = _first_content_line(text)
    remaining_raw = ""
    match = _REMAINING_RE.search(line)
    if match:
        remaining_raw = match.group("days").strip()
    checkpoint = ""
    check = _CHECKPOINT_RE.search(line)
    if check:
        checkpoint = check.group("when")
    summary = re.split(r"\s*[·|]\s*(?=(?:检查点|剩)\s*)", line, maxsplit=1)[0].strip()
    remaining_display = _display_or_missing(remaining_raw)
    return {
        "raw": line or "未提供",
        "summary": summary or "未提供",
        "checkpoint": checkpoint or "未提供",
        "remaining_trading_days": remaining_display,
        "remaining_trading_days_note": (
            "记录写明剩余交易日未提供"
            if remaining_display == "未提供"
            else ""
        ),
    }


def _health_trade_date_label(health_line: str, explanation: str) -> str:
    blob = f"{health_line}\n{explanation}"
    if re.search(r"20\d{2}-\d{2}-\d{2}", blob):
        found = re.findall(r"20\d{2}-\d{2}-\d{2}", blob)
        return "记录中出现的日期：" + "、".join(dict.fromkeys(found))
    return "记录未明确标注"


def _page_scope_note(root: Path) -> str:
    text = str(root).replace("\\", "/").lower()
    if "/.worktrees/" in text or text.endswith("/.worktrees"):
        return "工作树预览 · 未合入"
    return ""


def health_freshness(root: Path, parsed: dict[str, Any], read_at: str) -> dict[str, Any]:
    """Check the recorded business date against the local 22:30 health cycle."""
    result: dict[str, Any] = {"status": "unknown", "expected_trade_date": None,
        "observed_trade_date": None, "generated_at": None, "warning": "健康记录日期无法核实"}
    try:
        current = datetime.fromisoformat(read_at).astimezone(SHANGHAI)
        generated = datetime.fromisoformat(str(parsed.get("document_updated_at") or ""))
        generated = generated.replace(tzinfo=SHANGHAI) if generated.tzinfo is None else generated.astimezone(SHANGHAI)
    except ValueError:
        return result
    result["generated_at"] = generated.isoformat()
    if generated > current:
        result["warning"] = "健康记录标注了未来生成时间，不能视为当前状态"
        return result
    blob = f"{parsed.get('health_line', '')}\n{parsed.get('health_explanation', '')}"
    explicit = re.search(r"健康业务日期\s*(\d{4}-\d{2}-\d{2})", blob)
    # Legacy health_line output used the current local date as its default.
    # A manual document edit cannot establish a health observation timestamp.
    observed = explicit.group(1) if explicit else generated.date().isoformat() if parsed.get("document_updater") == "health_line" else None
    result["observed_trade_date"] = observed
    if not parsed.get("health_line") or parsed.get("health_line") == "未提供":
        result.update(status="missing", warning="健康行缺失，无法确认最近运行结果")
        return result
    try:
        path = root / "data" / "calendar" / "a_share_trading_calendar.csv"
        sessions: dict[str, bool] = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = date.fromisoformat(row.get("date", "")).isoformat()
                flag = str(row.get("is_open", "")).strip().lower()
                if flag not in {"true", "false", "1", "0"}:
                    raise ValueError("calendar_flag_unknown")
                opened = flag in {"true", "1"}
                if day in sessions and sessions[day] != opened:
                    raise ValueError("calendar_date_conflict")
                sessions[day] = opened
        today = current.date().isoformat()
        if today not in sessions:
            raise ValueError("calendar_day_missing")
        due_today = sessions[today] and (current.hour, current.minute) >= (22, 30)
        available = sorted(day for day, opened in sessions.items() if opened and (day < today or (day == today and due_today)))
        if not available:
            raise ValueError("calendar_previous_session_missing")
        expected = available[-1]
        result["expected_trade_date"] = expected
    except (OSError, ValueError, KeyError):
        result["warning"] = "本地交易日历缺失或未覆盖当前日期，健康记录新鲜度未核实"
        return result
    if observed is None:
        result["warning"] = "记录未注明健康业务日期，文档修改时间不能代替运行时间"
    elif observed > today or observed > generated.date().isoformat():
        result["warning"] = "健康业务日期与生成时间不符，不能视为当前状态"
    elif observed < expected or generated < datetime.fromisoformat(expected + "T22:30:00+08:00"):
        result.update(status="stale", warning=f"健康记录已过期：应覆盖 {expected}，记录截至 {observed}")
    else:
        result.update(status="current" if due_today else "not_due", warning="")
    return result


def _empty_payload(*, read_at: str, error: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "readable": False,
        "source_kind": STATUS_FILENAME,
        "source_label": SOURCE_LABEL,
        "disclaimer": DISCLAIMER,
        "page_scope_note": "",
        "document_updated_at": None,
        "document_updated_at_display": "未提供",
        "document_updater": "未提供",
        "file_modified_at": None,
        "read_at": read_at,
        "sha256": "",
        "byte_size": 0,
        "encoding": "",
        "health_line": "",
        "health_explanation": "",
        "health_trade_date_label": "记录未明确标注",
        "health_freshness": {"status": "missing", "expected_trade_date": None,
            "observed_trade_date": None, "generated_at": None, "warning": "健康记录不可读，无法确认最近运行结果"},
        "phase": {},
        "sections": [],
        "line_progress": {},
        "raw_text": "",
        "parse_warnings": [],
        "error": error,
        "readable_means": "file_opened_not_system_health",
    }
    if extra:
        payload.update(extra)
    return payload


def _parse_status_text(text: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    normalized = _normalize_newlines(text)
    lines = normalized.split("\n")
    header = lines[0].strip() if lines else ""
    header_match = _HEADER_RE.match(header)
    document_updated_at = None
    document_updater = None
    body = normalized
    if header_match:
        document_updated_at = header_match.group("when").strip()
        document_updater = header_match.group("who").strip()
        body = "\n".join(lines[1:]).lstrip("\n")
    else:
        warnings.append("missing_or_unrecognized_status_header")

    sections = _split_sections(body)
    if not sections:
        warnings.append("no_markdown_sections")

    health_body = _section_body(sections, "今日健康行")
    if not health_body:
        warnings.append("missing_health_section")
    health_line = _first_content_line(health_body)
    health_explanation = _paren_or_following_explanation(health_body)
    phase_body = _section_body(sections, "当前阶段")
    if not phase_body:
        warnings.append("missing_phase_section")
    phase = _parse_phase(phase_body)

    line_progress = {
        "B": _parse_line_progress(_section_body(sections, "B 线")),
        "C": _parse_line_progress(_section_body(sections, "C 线")),
        "A0": _parse_line_progress(_section_body(sections, "A0 线")),
    }
    for key, title in (("B", "B 线"), ("C", "C 线"), ("A0", "A0 线")):
        if not _section_body(sections, title):
            warnings.append(f"missing_section:{title}")

    parsed = {
        "document_updated_at": document_updated_at,
        "document_updated_at_display": _display_or_missing(document_updated_at),
        "document_updater": _display_or_missing(document_updater),
        "health_line": health_line or "未提供",
        "health_explanation": health_explanation or "未提供",
        "health_trade_date_label": _health_trade_date_label(health_line, health_explanation),
        "phase": phase,
        "sections": sections,
        "line_progress": line_progress,
    }
    return parsed, warnings


def read_project_status(project_root: str | Path) -> dict[str, Any]:
    """Read STATUS.md under ``project_root``. Never creates the file."""

    read_at = _now_iso()
    try:
        root = Path(project_root).resolve()
    except OSError as exc:
        return _empty_payload(read_at=read_at, error=f"root_unreadable:{exc.__class__.__name__}")
    extra_scope = {"page_scope_note": _page_scope_note(root)}
    try:
        path = (root / STATUS_FILENAME).resolve()
    except OSError as exc:
        return _empty_payload(
            read_at=read_at,
            error=f"read_failed:{exc.__class__.__name__}",
            extra=extra_scope,
        )
    try:
        path.relative_to(root)
    except ValueError:
        return _empty_payload(read_at=read_at, error="path_escape")
    if path.name != STATUS_FILENAME:
        return _empty_payload(read_at=read_at, error="unexpected_filename")

    try:
        if not path.exists():
            return _empty_payload(read_at=read_at, error="missing_file", extra=extra_scope)
        if not path.is_file():
            return _empty_payload(read_at=read_at, error="not_a_file", extra=extra_scope)
    except FileNotFoundError:
        return _empty_payload(read_at=read_at, error="missing_file", extra=extra_scope)
    except OSError as exc:
        return _empty_payload(
            read_at=read_at,
            error=f"read_failed:{exc.__class__.__name__}",
            extra=extra_scope,
        )

    try:
        with path.open("rb") as handle:
            before_stat = os.fstat(handle.fileno())
            raw_bytes = handle.read(MAX_STATUS_BYTES + 1)
            stat_info = os.fstat(handle.fileno())
    except FileNotFoundError:
        return _empty_payload(read_at=read_at, error="missing_file", extra=extra_scope)
    except IsADirectoryError:
        return _empty_payload(read_at=read_at, error="not_a_file", extra=extra_scope)
    except OSError as exc:
        return _empty_payload(
            read_at=read_at,
            error=f"read_failed:{exc.__class__.__name__}",
            extra=extra_scope,
        )

    if (
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    ) != (
        stat_info.st_size,
        stat_info.st_mtime_ns,
        stat_info.st_ctime_ns,
    ):
        return _empty_payload(
            read_at=read_at,
            error="changed_during_read",
            extra=extra_scope,
        )

    file_modified_at = datetime.fromtimestamp(stat_info.st_mtime, tz=SHANGHAI).replace(microsecond=0).isoformat()
    byte_size = len(raw_bytes)
    if byte_size > MAX_STATUS_BYTES:
        return _empty_payload(
            read_at=read_at,
            error="too_large",
            extra={
                **extra_scope,
                "byte_size": byte_size,
                "max_bytes": MAX_STATUS_BYTES,
            },
        )
    if byte_size == 0:
        return _empty_payload(
            read_at=read_at,
            error="empty_file",
            extra={**extra_scope, "file_modified_at": file_modified_at},
        )

    try:
        text = raw_bytes.decode("utf-8-sig")
        encoding = "utf-8-sig" if raw_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        return _empty_payload(
            read_at=read_at,
            error="bad_encoding",
            extra={
                **extra_scope,
                "byte_size": byte_size,
                "file_modified_at": file_modified_at,
            },
        )

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    parsed, warnings = _parse_status_text(text)
    payload = {
        "ok": True,
        "readable": True,
        "source_kind": STATUS_FILENAME,
        "source_label": SOURCE_LABEL,
        "disclaimer": DISCLAIMER,
        "page_scope_note": extra_scope["page_scope_note"],
        "document_updated_at": parsed["document_updated_at"],
        "document_updated_at_display": parsed["document_updated_at_display"],
        "document_updater": parsed["document_updater"],
        "file_modified_at": file_modified_at,
        "read_at": read_at,
        "sha256": sha256,
        "byte_size": byte_size,
        "encoding": encoding,
        "health_line": parsed["health_line"],
        "health_explanation": parsed["health_explanation"],
        "health_trade_date_label": parsed["health_trade_date_label"],
        "health_freshness": health_freshness(root, parsed, read_at),
        "phase": parsed["phase"],
        "sections": parsed["sections"],
        "line_progress": parsed["line_progress"],
        "raw_text": _normalize_newlines(text),
        "parse_warnings": warnings,
        "error": None,
        "readable_means": "file_opened_not_system_health",
    }
    return payload


def read_runtime_observations(project_root: str | Path, *, report_date: str | None = None) -> dict[str, Any]:
    """Read small, existing result files; never run health checks or expose model bodies."""

    root = Path(project_root).resolve()

    def read_object(relative: str) -> dict[str, Any]:
        try:
            path = (root / relative).resolve()
            path.relative_to(root)
            with path.open("rb") as stream:
                raw = stream.read(MAX_RUNTIME_OBSERVATION_BYTES + 1)
            if len(raw) > MAX_RUNTIME_OBSERVATION_BYTES:
                return {}
            def reject_constant(value: str) -> None:
                raise ValueError("Non-finite JSON")
            value = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def day(value: Any) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None

    def count(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    profiles: dict[str, dict[str, Any]] = {}
    for profile, directory, label in (("default", "ai_trader", "默认档"), ("glm", "ai_trader_glm", "GLM 档")):
        source = f"results/{directory}/cycle_latest.json"
        cycle = read_object(source)
        plan = cycle.get("plan") if isinstance(cycle.get("plan"), dict) else {}
        llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
        used = llm.get("used") if type(llm.get("used")) is bool else None
        observed_date = day(cycle.get("as_of")) or day(plan.get("decision_date"))
        date_conflict = ("as_of" in cycle and day(cycle.get("as_of")) is None
                         or "decision_date" in plan and day(plan.get("decision_date")) is None
                         or bool(day(cycle.get("as_of")) and day(plan.get("decision_date"))
                                 and cycle["as_of"] != plan["decision_date"]))
        if date_conflict:
            used = None
        reason = llm.get("unavailable_reason")
        model = str(llm.get("model") or "").lower()
        model_label = next((label for key, label in (("kimi", "Kimi"), ("glm", "GLM"), ("deepseek", "DeepSeek"))
                            if key in model), None)
        explanations = {
            "transport:TimeoutError": "等待回答超时", "transport:timeout": "等待回答超时",
            "transport:URLError": "连接模型服务失败，具体原因尚未确定",
            "transport:HTTPError": "模型服务返回错误",
        }
        detail = explanations.get(reason, "未取得模型回答，具体原因待查") if isinstance(reason, str) else "未取得模型回答"
        if used is True:
            message = "模型已回应；判断未被接收" if plan.get("committed") is False else "模型已回应"
        elif used is False:
            if plan.get("decision_mode") == "hold_only":
                message = f"{detail}；维持已有持仓，本轮无新增交易意图"
            else:
                message = f"{detail}；固定规则接管" if reason else "本轮未使用模型，由固定规则判断"
        else:
            message = "尚无可确认的模型回应记录，不能当作成功或零成交"
        if date_conflict:
            message = "运行记录与原计划日期冲突或无效，模型回应状态未核验"
        profiles[profile] = {
            "label": label, "as_of": observed_date, "model_replied": used,
            "model_label": model_label,
            "decision_mode": plan.get("decision_mode") if isinstance(plan.get("decision_mode"), str) else None,
            "record_status": "date_conflict" if date_conflict else "readable" if cycle else "missing_or_unreadable",
            "decision_accepted": plan.get("committed") if type(plan.get("committed")) is bool else None,
            "message": message, "source": source,
        }

    observed_dates = [item["as_of"] for item in profiles.values() if item["as_of"]]
    target_date = max(observed_dates) if observed_dates else day(report_date)
    source = f"data/snapshots/{target_date}/manifest.json" if target_date else None
    manifest = read_object(source) if source else {}
    declared_date = manifest.get("trade_date")
    if declared_date != target_date:
        manifest = {}
    collected = count(manifest.get("collected_slot_count"))
    expected = count(manifest.get("expected_slot_count"))
    quoted = count(manifest.get("quoted_row_count"))
    valid = count(manifest.get("valid_quote_count"))
    ratio = valid / quoted if quoted and valid is not None and valid <= quoted else None
    invalid = quoted - valid if ratio is not None else None
    if collected is None or expected is None or expected == 0 or collected > expected:
        collected = expected = None
        message = "采集次数尚无可靠记录；数量未知"
    else:
        message = f"已采集 {collected}/{expected} 次"
    if ratio is not None:
        message += f"；{ratio:.2%} 的报价通过检查，缺失或无效 {invalid} 条"
    else:
        message += "；有效报价比例未知"
    message += "。报价缺失单独提示，不阻断整个系统；具体股票成交仍需真实有效价格。"
    return {
        "source_label": "已有运行记录 · 查看不会重新运行任务", "profiles": profiles,
        "snapshot": {
            "as_of": target_date, "document_report_date": day(report_date),
            "source": source, "collected_slots": collected, "expected_slots": expected,
            "quoted_rows": quoted, "valid_quotes": valid, "invalid_quotes": invalid,
            "valid_quote_ratio": ratio, "blocks_workflow": False, "message": message,
        },
    }


__all__ = [
    "MAX_STATUS_BYTES",
    "STATUS_FILENAME",
    "read_project_status",
    "read_runtime_observations",
]
