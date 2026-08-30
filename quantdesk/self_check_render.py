from __future__ import annotations

import json
from typing import Iterable, Protocol


class CheckRow(Protocol):
    status: str
    name: str
    detail: str


def render_check_rows(rows: Iterable[CheckRow]) -> str:
    return "\n".join(f"{row.status} {row.name}: {row.detail}" for row in rows)


def summarize_check_rows(rows: Iterable[CheckRow]) -> dict[str, int]:
    items = list(rows)
    return {
        "pass": sum(row.status == "PASS" for row in items),
        "warn": sum(row.status == "WARN" for row in items),
        "fail": sum(row.status == "FAIL" for row in items),
    }


def render_summary_json(summary: dict[str, int]) -> str:
    return json.dumps(summary, ensure_ascii=False)


def render_self_check_output(rows: Iterable[CheckRow]) -> tuple[str, dict[str, int]]:
    items = list(rows)
    summary = summarize_check_rows(items)
    body = render_check_rows(items)
    if body:
        return body + "\n" + render_summary_json(summary), summary
    return render_summary_json(summary), summary
