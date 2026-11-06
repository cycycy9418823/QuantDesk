"""Read the saved brief and matching delivery evidence without running a sender."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
PREVIEW = "results/morning_brief/morning_brief_latest.json"


def _local_time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(SHANGHAI) if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


class _Reader:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.errors: list[dict[str, str]] = []
        self.sources: dict[str, str] = {}

    def text(self, path: Path, *, limit: int = 4_000_000) -> str | None:
        relative = path.relative_to(self.root).as_posix()
        try:
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("path_escape")
            before = path.stat()
            if before.st_size > limit:
                raise ValueError("too_large")
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("changed_during_read")
            self.sources[relative] = hashlib.sha256(raw).hexdigest()
            return raw.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeError) as exc:
            self.errors.append({"path": relative, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
            return None

    def mapping(self, path: Path) -> dict[str, Any] | None:
        text = self.text(path)
        if text is None:
            return None
        try:
            value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if not isinstance(value, dict):
                raise ValueError("not_mapping")
            return value
        except ValueError:
            self.errors.append({"path": path.relative_to(self.root).as_posix(), "reason": "invalid_json"})
            return None

    def files(self, directory: str, pattern: str) -> list[Path]:
        folder = self.root / directory
        if not folder.exists():
            return []
        if not folder.resolve().is_relative_to(self.root):
            self.errors.append({"path": directory, "reason": "path_escape"})
            return []
        try:
            paths = sorted(folder.glob(pattern))
            if len(paths) > 1000:
                raise ValueError("too_many_files")
            return paths
        except (OSError, ValueError) as exc:
            self.errors.append({"path": directory, "reason": type(exc).__name__})
            return []


def _content(payload: dict[str, Any]) -> tuple[str, str, str]:
    # Format only the saved snapshot, never rebuild it from today's account/data.
    from quantdesk.morning_brief import render_morning_brief_markdown

    saved = payload.get("saved_content")
    if saved is not None:
        if not isinstance(saved, dict) or any(not isinstance(saved.get(k), str) for k in ("title", "body", "technical", "sha256")):
            raise ValueError("invalid_saved_content")
        digest = hashlib.sha256((saved['title'].rstrip() + "\n" + saved['body'].rstrip() + "\n" + saved['technical'].rstrip()).encode()).hexdigest()
        if digest != saved["sha256"]:
            raise ValueError("saved_content_hash_mismatch")
        return saved["title"], saved["body"], digest
    technical = render_morning_brief_markdown(payload)
    owner = payload.get("owner_brief")
    if isinstance(owner, dict) and isinstance(owner.get("title"), str) and isinstance(owner.get("body"), str):
        title, body = owner["title"], owner["body"]
    else:
        title = f"QuantDesk 研究早报 {payload.get('as_of') or '数据待核'}"
        body = technical
    digest = hashlib.sha256((title.rstrip() + "\n" + body.rstrip() + "\n" + technical.rstrip()).encode()).hexdigest()
    return title, body, digest


def _delivery(receipts: list[dict[str, Any]], attempts: list[dict[str, Any]], *, damaged: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "unknown", "label": "发送结果未核实", "user_received": None,
                              "message": "没有足够的同日同内容记录；不能当作已送达或未发送。"}
    if damaged:
        return {**result, "message": "投递证据缺失、损坏或相互不符，发送结果未核实。"}
    if any(r.get("status") == "ambiguous" or r.get("reason_code") in {"ambiguous_delivery", "uncertain_prior_attempt", "delivery_state_write_failed"}
           for r in receipts + attempts):
        return {**result, "label": "结果不明，未自动重发", "message": "服务商接收情况无法确定；先核实手机记录，不能盲目重发。"}
    accepted = [r for r in receipts if r.get("status") == "sent" and r.get("http_status") == 200
                and type(r.get("provider_code")) is int and r["provider_code"] == 0]
    if accepted:
        return {**result, "status": "provider_accepted", "label": "服务商已接收", "message": "已有服务商接收回执；不代表手机已收到或你已读。"}
    if receipts:
        if all(r.get("status") == "failed" for r in receipts):
            return {**result, "status": "failed", "label": "发送失败", "message": "发送记录明确失败，本地日报仍可阅读。"}
        return {**result, "label": "发送未获确认", "message": "存在发送尝试，但没有可核实的成功回执；不自动推断手机收到了。"}
    if any(r.get("status") not in {"blocked", "preview_generated", "skipped_non_trading_day"} for r in attempts):
        return result
    reasons = {str(r.get("reason_code") or "") for r in attempts}
    if "outside_delivery_window" in reasons:
        return {**result, "status": "not_sent", "label": "未发送：超过发送时段", "message": "这份内容已生成，但任务检查时已超过08:30–08:49:59的发送时段。可在这里阅读。"}
    labels = {"dry_run": "仅生成预览", "network_not_authorized": "未授权联网发送", "push_not_authorized": "未授权推送",
              "non_trading_day": "休市未发送", "snapshot_blocked": "资料未通过发送核对",
              "brief_business_date_stale": "资料日期过旧", "brief_business_date_future": "资料日期超前",
              "send_clock_day_changed": "生成期间跨日，未发送", "send_clock_moved_backwards": "时钟回退，未发送"}
    for reason, label in labels.items():
        if reason in reasons:
            return {**result, "status": "not_sent", "label": label, "message": "程序记录未调用发送；已生成的内容与原资料日期保留在这里。"}
    return result


def build_morning_brief_view(project_root: str | Path, *, now: datetime | None = None,
                             requested_date: str = "") -> dict[str, Any]:
    """Return the latest saved content with independently checked delivery status."""
    reader = _Reader(Path(project_root))
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    archive_dates = sorted((p.stem for p in reader.files("results/morning_brief", "????-??-??.json")
                            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)), reverse=True)
    source = PREVIEW
    selected_date = requested_date
    if requested_date:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested_date):
                raise ValueError("invalid_date")
            date.fromisoformat(requested_date)
        except ValueError:
            return {"ok": False, "available": False, "read_only": True, "message": "日报日期格式无效"}
    elif current.date().isoformat() in archive_dates:
        selected_date = current.date().isoformat()
    if selected_date:
        source = f"results/morning_brief/{selected_date}.json"
    base: dict[str, Any] = {"ok": True, "available": False, "read_only": True, "label": "纸面 · 未验证",
                            "read_at": current.isoformat(timespec="seconds"), "source": PREVIEW,
                            "archive_dates": archive_dates, "selected_date": selected_date,
                            "source_errors": reader.errors, "source_sha256": reader.sources}
    base["source"] = source
    payload = reader.mapping(reader.root / source)
    if not payload:
        return {**base, "message": "未读到已生成的早报；查看页面不会触发生成或发送。"}
    generated = _local_time(payload.get("generated_at"))
    if selected_date:
        report = reader.text(reader.root / "reports/morning_brief" / f"{selected_date}.md")
        if (not generated or generated.date().isoformat() != selected_date or report is None
                or hashlib.sha256(report.encode("utf-8")).hexdigest() != payload.get("archive_report_sha256")):
            return {**base, "message": "当日存档正文缺失、日期或摘要不符，暂不能作为已保存日报。"}
    try:
        title, body, content_sha = _content(payload)
    except (ValueError, TypeError, AttributeError, KeyError):
        return {**base, "message": "已存早报格式异常，未重建或覆盖原记录。"}
    delivery_day = generated.date().isoformat() if generated else ""
    receipts: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    damaged = generated is None
    error_count = len(reader.errors)
    if generated:
        for path in reader.files("results/notifications", f"serverchan_morning_brief_{generated:%Y%m%d}*.json"):
            row = reader.mapping(path)
            if not row:
                continue
            stamp = _local_time(row.get("observed_at"))
            if not stamp or stamp.date() != generated.date() or row.get("delivery_day") != delivery_day:
                damaged = True
                continue
            if row.get("summary_sha256") == content_sha:
                receipts.append(row)
                evidence.append({"path": path.relative_to(reader.root).as_posix(), "kind": "receipt",
                                 "status": row.get("status"), "reason_code": row.get("reason_code"),
                                 "observed_at": row.get("observed_at"), "http_status": row.get("http_status"), "provider_code": row.get("provider_code")})
        for path in reader.files("logs/ops/scheduled_tasks", "quantdesk-morning-brief-*.log"):
            text = reader.text(path)
            if text is None:
                continue
            active_day = ""
            active_at = ""
            for line in text.splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    if active_day == delivery_day:
                        damaged = True
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("task") == "QuantDesk Morning Brief" and row.get("status") == "started":
                    active_day, active_at = str(row.get("date") or ""), str(row.get("at") or "")
                if active_day == delivery_day and row.get("task_type") == "morning_brief_delivery":
                    if row.get("content_sha256") == content_sha and row.get("as_of") == payload.get("as_of") and row.get("run_id") == payload.get("run_id"):
                        attempts.append(row)
                        evidence.append({"path": path.relative_to(reader.root).as_posix(), "kind": "task_result",
                                         "started_at": active_at, "status": row.get("status"), "reason_code": row.get("reason_code")})
    damaged = damaged or len(reader.errors) > error_count
    delivery = _delivery(receipts, attempts, damaged=damaged)
    for relative, digest in list(reader.sources.items()):
        text = reader.text(reader.root / relative)
        if text is None or reader.sources.get(relative) != digest:
            return {**base, "message": "早报或发送记录在读取期间发生变化，请重新读取；本次未认定发送结果。"}
    return {**base, "available": True, "title": title, "body": body, "content_sha256": content_sha,
            "generated_at": str(payload.get("generated_at") or ""), "delivery_day": delivery_day,
            "late_generated": bool(payload.get("late_generated")) if "late_generated" in payload else
                bool(generated and (generated.hour, generated.minute, generated.second) > (8, 35, 0)),
            "as_of": str(payload.get("as_of") or ""), "expected_as_of": str(payload.get("expected_as_of") or ""),
            "date_status_at_generation": str(payload.get("date_status") or "unverified"),
            "is_today": generated is not None and generated.date() == current.date(),
            "is_future": generated is not None and generated > current,
            "delivery": delivery, "evidence": evidence,
            "message": "以下保留生成时原文和日期，可能包含当时的旧状态；不会用今晚数据改写早报，也不会自动补发。"}
