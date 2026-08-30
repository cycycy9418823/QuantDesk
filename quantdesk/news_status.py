from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class NewsFetchStatus:
    symbol: str
    name: str
    source: str
    attempted: bool
    skipped: bool
    skip_reason: str
    fetch_success: bool
    item_count: int
    latest_title: str
    latest_time: str
    error_message: str
    warning_level: str
    warning_message: str
    fetched_at: str


NEWS_HEALTH_COLUMNS = [field.name for field in NewsFetchStatus.__dataclass_fields__.values()]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text


def _item_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("headline") or item.get("summary") or "")


def _item_time(item: dict[str, Any]) -> str:
    return str(item.get("published") or item.get("time") or item.get("datetime") or item.get("date") or "")


def news_status_sample_mode(symbol: str, name: str = "") -> NewsFetchStatus:
    return NewsFetchStatus(
        symbol=_symbol(symbol),
        name=str(name or ""),
        source="sample",
        attempted=False,
        skipped=True,
        skip_reason="sample_mode_no_network",
        fetch_success=False,
        item_count=0,
        latest_title="",
        latest_time="",
        error_message="",
        warning_level="INFO",
        warning_message="样例模式不联网抓取消息",
        fetched_at=now_text(),
    )


def provider_not_configured_status(symbol: str, name: str = "") -> NewsFetchStatus:
    return NewsFetchStatus(
        symbol=_symbol(symbol),
        name=str(name or ""),
        source="none",
        attempted=False,
        skipped=True,
        skip_reason="news_provider_not_configured",
        fetch_success=False,
        item_count=0,
        latest_title="",
        latest_time="",
        error_message="",
        warning_level="INFO",
        warning_message="未配置新闻源",
        fetched_at=now_text(),
    )


def news_status_no_news(symbol: str, name: str = "", source: str = "news") -> NewsFetchStatus:
    return NewsFetchStatus(
        symbol=_symbol(symbol),
        name=str(name or ""),
        source=str(source or "news"),
        attempted=True,
        skipped=False,
        skip_reason="",
        fetch_success=True,
        item_count=0,
        latest_title="",
        latest_time="",
        error_message="",
        warning_level="INFO",
        warning_message="暂无相关新闻",
        fetched_at=now_text(),
    )


def news_status_success(
    symbol: str,
    name: str,
    source: str,
    items: list[dict[str, Any]],
) -> NewsFetchStatus:
    if not items:
        return news_status_no_news(symbol, name, source)
    first = items[0]
    return NewsFetchStatus(
        symbol=_symbol(symbol),
        name=str(name or ""),
        source=str(source or "news"),
        attempted=True,
        skipped=False,
        skip_reason="",
        fetch_success=True,
        item_count=len(items),
        latest_title=_item_title(first),
        latest_time=_item_time(first),
        error_message="",
        warning_level="OK",
        warning_message="已抓取相关新闻",
        fetched_at=now_text(),
    )


def news_status_failure(symbol: str, name: str, source: str, error: Any) -> NewsFetchStatus:
    return NewsFetchStatus(
        symbol=_symbol(symbol),
        name=str(name or ""),
        source=str(source or "news"),
        attempted=True,
        skipped=False,
        skip_reason="",
        fetch_success=False,
        item_count=0,
        latest_title="",
        latest_time="",
        error_message=str(error)[:300],
        warning_level="WARNING",
        warning_message="消息抓取失败",
        fetched_at=now_text(),
    )


def status_from_items(
    symbol: str,
    name: str,
    source: str,
    items: list[dict[str, Any]],
    *,
    attempted: bool = True,
) -> NewsFetchStatus:
    status = news_status_success(symbol, name, source, items)
    if not attempted:
        status.attempted = False
    return status


def frame_from_statuses(statuses: list[NewsFetchStatus]) -> pd.DataFrame:
    if not statuses:
        return pd.DataFrame(columns=NEWS_HEALTH_COLUMNS)
    return pd.DataFrame([asdict(status) for status in statuses], columns=NEWS_HEALTH_COLUMNS)


def _truthy(row: dict[str, Any], key: str) -> bool:
    return row.get(key) is True or str(row.get(key, "")).lower() == "true"


def summarize_news_health_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    levels = [str(row.get("warning_level", "")).upper() for row in rows]
    return {
        "total": len(rows),
        "attempted": sum(_truthy(row, "attempted") for row in rows),
        "skipped": sum(_truthy(row, "skipped") for row in rows),
        "ok": sum(level == "OK" for level in levels),
        "info": sum(level == "INFO" for level in levels),
        "warning": sum(level == "WARNING" for level in levels),
        "error": sum(level == "ERROR" for level in levels),
        "no_news": sum(str(row.get("warning_message", "")) == "暂无相关新闻" for row in rows),
        "failed": sum(_truthy(row, "attempted") and not _truthy(row, "fetch_success") for row in rows),
        "sample_mode_skipped": sum(str(row.get("skip_reason", "")) == "sample_mode_no_network" for row in rows),
    }


def summarize_news_health(rows: list[dict[str, Any]]) -> dict[str, int]:
    return summarize_news_health_rows(rows)


def write_news_health_csv(statuses: list[NewsFetchStatus | dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for status in statuses:
        records.append(asdict(status) if isinstance(status, NewsFetchStatus) else dict(status))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=NEWS_HEALTH_COLUMNS)
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in NEWS_HEALTH_COLUMNS})
    return path


def read_news_health_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def news_status_label(row: dict[str, Any]) -> str:
    if not row:
        return "消息字段缺失"
    if str(row.get("latest_title") or "").strip():
        return str(row.get("latest_title")).strip()
    skip_reason = str(row.get("skip_reason", ""))
    message = str(row.get("warning_message", ""))
    if skip_reason == "sample_mode_no_network":
        return "样例模式未联网"
    if skip_reason == "news_provider_not_configured":
        return "未配置新闻源"
    if message == "暂无相关新闻":
        return "暂无相关新闻"
    if message == "消息抓取失败" or str(row.get("warning_level", "")).upper() in {"WARNING", "ERROR"}:
        return "消息抓取失败"
    if str(row.get("attempted", "")).lower() == "false" and str(row.get("skipped", "")).lower() == "false":
        return "消息字段缺失"
    return message or "消息字段缺失"


# Backward-compatible names used by existing report code.
sample_mode_status = news_status_sample_mode
failure_status = news_status_failure
