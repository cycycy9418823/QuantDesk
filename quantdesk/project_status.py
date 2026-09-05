"""Read-only STATUS.md for the 8000 research home page.

This module never writes STATUS.md, never fetches market data, and never
runs health checks. ``readable`` means the file could be opened — not that
the desk is healthy.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

STATUS_FILENAME = "STATUS.md"
SOURCE_LABEL = "项目记录 · 非实时核验"
DISCLAIMER = "纸面 · 未验证"
MAX_STATUS_BYTES = 256 * 1024
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
            if stripped.startswith("## "):
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
    remaining_display = _display_or_missing(remaining_raw)
    return {
        "raw": line or "未提供",
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
    path = (root / STATUS_FILENAME).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return _empty_payload(read_at=read_at, error="path_escape")
    if path.name != STATUS_FILENAME:
        return _empty_payload(read_at=read_at, error="unexpected_filename")

    extra_scope = {"page_scope_note": _page_scope_note(root)}
    if not path.exists():
        return _empty_payload(read_at=read_at, error="missing_file", extra=extra_scope)
    if not path.is_file():
        return _empty_payload(read_at=read_at, error="not_a_file", extra=extra_scope)

    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_STATUS_BYTES + 1)
            stat_info = os.fstat(handle.fileno())
    except FileNotFoundError:
        return _empty_payload(read_at=read_at, error="missing_file", extra=extra_scope)
    except OSError as exc:
        return _empty_payload(
            read_at=read_at,
            error=f"read_failed:{exc.__class__.__name__}",
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
        "phase": parsed["phase"],
        "sections": parsed["sections"],
        "line_progress": parsed["line_progress"],
        "raw_text": _normalize_newlines(text),
        "parse_warnings": warnings,
        "error": None,
        "readable_means": "file_opened_not_system_health",
    }
    return payload


__all__ = [
    "MAX_STATUS_BYTES",
    "STATUS_FILENAME",
    "read_project_status",
]
