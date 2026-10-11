"""Date-bounded observation only; never feed the paper execution queues."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ai_trader import daytime_decide, daytime_fill, daytime_runner, daytime_screen, llm
from ai_trader.paths import CyclePaths, PROJECT_ROOT, active_profile
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.shared import write_text

SCHEMA = "ai_trader_observation_only_v1"
SLOTS = ("1000", "1100", "1330", "1430")
OUTPUT_RELATIVE = Path("state/ai_trader_observation")
MAX_TOKENS = 2000
TIMEOUT_SECONDS = 120
MAX_QUOTE_AGE_SECONDS = 300
INPUT_WAIT_SECONDS = 165
INPUT_POLL_SECONDS = 5
LOCAL_QUOTES_DATE = "2026-10-08"
LOCAL_QUOTES_SLOT = "1430"
LOCAL_QUOTES_MAX_SYMBOLS = 6000
LOCAL_QUOTES_BATCH_SIZE = 500
LOCAL_QUOTES_BUDGET_SECONDS = 60
LOCAL_QUOTES_WAIT_SECONDS = 30


@dataclass
class ObservationModelCall(daytime_decide.ModelCall):
    request_attempted: bool = False
    response_evidence: dict[str, Any] = field(default_factory=dict)


def atomic_write_json(path: Path, value: dict[str, Any]) -> Path:
    """Use the existing flush/fsync/atomic-replace text writer for receipts."""
    return write_text(path, json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))


def _paths(root: Path) -> CyclePaths:
    original = CyclePaths()
    return CyclePaths(**{f.name: root / getattr(original, f.name).relative_to(PROJECT_ROOT)
                         for f in fields(CyclePaths)})


def call_default_model(prompt: str) -> daytime_decide.ModelCall:
    """One request to the existing default model; explicit transport limits."""
    if active_profile() not in ("", "default"):
        return ObservationModelCall(error="default_profile_required")
    diagnostics: dict[str, Any] = {}
    config = llm.load_llm_config(explicit_optin=True, diagnostics=diagnostics)
    if not config:
        return ObservationModelCall(error=str(diagnostics.get("reason") or "model_unavailable"))
    config = {**config, "max_tokens": str(MAX_TOKENS), "timeout": str(TIMEOUT_SECONDS)}
    started = time.monotonic()
    text = llm._call(daytime_decide.SYSTEM_PROMPT, prompt, config, diagnostics)
    return ObservationModelCall(text=text or "", model=config.get("model", ""), request_attempted=True,
        elapsed_ms=int((time.monotonic()-started)*1000), prompt_chars=len(prompt), ok=bool(text),
        response_evidence=llm.persistable_response_evidence(diagnostics),
        error="" if text else str(diagnostics.get("reason") or diagnostics.get("error_type") or "empty_response"))


def _fresh_evidence(rows: list[dict[str, Any]], current: datetime) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    fresh: list[dict[str, Any]] = []
    times: list[str] = []
    reasons: dict[str, int] = {}
    for row in rows:
        q = next(iter(daytime_fill.quote_index([row]).values()), {})
        quoted = daytime_fill._stamp(q.get("time"))
        if q.get("time_error") or quoted is None:
            reason = "missing_or_conflicting_quote_timestamp"
        elif not q.get("trading") or (q.get("price") or 0) <= 0:
            reason = "invalid_or_nontrading_price"
        elif quoted.date() != current.date():
            reason = "different_quote_date"
        elif (current - quoted).total_seconds() < 0:
            reason = "future_quote"
        elif (current - quoted).total_seconds() > MAX_QUOTE_AGE_SECONDS:
            reason = "stale_quote"
        else:
            fresh.append(row)
            times.append(quoted.isoformat())
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
    return fresh, times, reasons


def _snapshot_evidence(project: Path, current: datetime, slot: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Read original quote fields, never the legacy cache's synthesized clock."""
    day = current.date().isoformat()
    slot_start = current.replace(hour=int(slot[:2]), minute=int(slot[2:]), second=0, microsecond=0)
    previous = (slot_start - timedelta(minutes=5)).strftime("%H%M")
    empty = {"rows": [], "slot": slot, "source": "", "path": ""}
    first = empty
    checks: list[dict[str, Any]] = []
    for candidate in (slot, previous):
        path = project / daytime_runner.SLOTS_RELATIVE / day / f"{candidate}.csv"
        rows = daytime_runner._read_csv(path)
        quotes = {"rows": rows, "slot": candidate, "source": daytime_runner.SOURCE_CLOUD_SLOT,
                  "path": str(path)}
        if rows and not first["rows"]:
            first = quotes
        fresh, times, reasons = _fresh_evidence(rows, current)
        checks.append({"path": str(path), "slot": candidate, "input_rows": len(rows),
                       "fresh_rows": len(fresh), "rejected_reasons": reasons})
        if fresh:
            return quotes, fresh, times, checks
    return first, [], [], checks


def _hq_read_once(keys: list[str], *, timeout: float) -> str:
    """One bounded reply from the existing HQ endpoint, without redirects/retries."""
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request("https://hq.sinajs.cn/list=" + ",".join(keys),
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("quote_response_too_large")
    return raw.decode("gb18030", errors="replace")


def _local_hq_evidence(project: Path, current: datetime, slot: str, receipt: Path,
                       base: dict[str, Any], read_clock: Callable[[], datetime],
                       tick: Callable[[], float], fetcher: Callable[..., str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep quote-request claims and original responses inside this observation."""
    import csv
    import io
    from quantdesk.data_providers import parse_sina_quote_text
    from quantdesk.snapshot_history import UNIVERSE_RELATIVE_PATH, _load_symbols, _sina_hq_symbol_key

    evidence_path = receipt.with_suffix(".quotes.json")
    quotes: dict[str, Any] = {"rows": [], "slot": slot, "source": "sina.hq_observation", "path": str(evidence_path)}
    info: dict[str, Any] = {"quote_requests_reserved": 0, "requested_rows": 0, "reason": "",
                            "universe_path": str(project/UNIVERSE_RELATIVE_PATH), "evidence_path": str(evidence_path)}
    if current.date().isoformat() != LOCAL_QUOTES_DATE or slot != LOCAL_QUOTES_SLOT or daytime_runner.floor_slot(current) != slot:
        return quotes, {**info, "reason": "outside_direct_quote_window"}
    try:
        raw = (project/UNIVERSE_RELATIVE_PATH).read_bytes()
        listing = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except (OSError, UnicodeError, csv.Error):
        return quotes, {**info, "reason": "quote_universe_unavailable"}
    symbols = _load_symbols(project, [row.get("symbol") or row.get("代码") or row.get("code") for row in listing])
    keys = list(dict.fromkeys(key for symbol in symbols if (key := _sina_hq_symbol_key(symbol))))
    info.update(requested_rows=len(keys), universe_sha256=hashlib.sha256(raw).hexdigest(),
                universe_updated_at=sorted({str(row.get("updated_at") or "") for row in listing}),
                unrecognized_symbols=len(symbols)-len(keys))
    if not keys or len(keys) > LOCAL_QUOTES_MAX_SYMBOLS:
        return quotes, {**info, "reason": "quote_symbol_cap_exceeded" if keys else "quote_universe_empty"}
    quotes["requested_rows"] = len(keys)
    evidence: dict[str, Any] = {"schema_version": "observation_quote_input_v1", "as_of": current.date().isoformat(),
        "slot": slot, "source": quotes["source"], "universe": dict(info), "requested_symbols": keys,
        "response_format": "original HQ text decoded as GB18030; not original HTTP bytes", "batches": [], "rows": []}
    deadline = tick() + LOCAL_QUOTES_BUDGET_SECONDS
    window_end = current.replace(hour=14, minute=35, second=0, microsecond=0)
    claim = {**base, "status": "input_reserved", "model_attempt_reserved": False,
             "source_path": str(evidence_path), "quote_requests_reserved": 0,
             "local_quote_input": dict(info), "reason": "quote_request_reserved_no_automatic_repeat"}
    for start in range(0, len(keys), LOCAL_QUOTES_BATCH_SIZE):
        request_time = to_shanghai_datetime(read_clock())
        remaining = min(deadline-tick(), (window_end-request_time).total_seconds()-TIMEOUT_SECONDS-15)
        if request_time.date().isoformat() != LOCAL_QUOTES_DATE or daytime_runner.floor_slot(request_time) != slot or remaining <= 0:
            info["reason"] = "quote_input_budget_exhausted"
            break
        chunk = keys[start:start+LOCAL_QUOTES_BATCH_SIZE]
        claim["quote_requests_reserved"] += 1
        claim["quote_request_started_at"] = request_time.isoformat()
        claim["quote_request"] = {"batch_index": start//LOCAL_QUOTES_BATCH_SIZE,
            "requested_symbols": chunk, "timeout_seconds": min(5.0, remaining)}
        claim["max_quote_requests"] = LOCAL_QUOTES_MAX_SYMBOLS//LOCAL_QUOTES_BATCH_SIZE
        # A persisted slot claim precedes every outbound request. A crash consumes
        # the entire slot, including any still-unattempted quote/model budget.
        atomic_write_json(receipt, claim)
        info["quote_requests_reserved"] = claim["quote_requests_reserved"]
        batch: dict[str, Any] = {"requested_symbols": chunk, "request_started_at": request_time.isoformat(),
                                 "timeout_seconds": min(5.0, remaining), "rejected_reasons": {}}
        try:
            text = fetcher(chunk, timeout=batch["timeout_seconds"])
            batch.update(received_at=to_shanghai_datetime(read_clock()).isoformat(), response_text=text)
            parsed = parse_sina_quote_text(text)
        except Exception as exc:
            batch.update(received_at=to_shanghai_datetime(read_clock()).isoformat(), error=type(exc).__name__)
            parsed = []
        counts = Counter(str(item.symbol).strip().lower() for item in parsed)
        for item in parsed:
            key = str(item.symbol).strip().lower()
            reason = "unrequested_identity" if key not in chunk else "duplicate_identity" if counts[key] != 1 else ""
            if not reason and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.date)
                               or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", item.time)):
                reason = "missing_or_conflicting_quote_timestamp"
            if reason:
                batch["rejected_reasons"][reason] = batch["rejected_reasons"].get(reason, 0) + 1
                continue
            row = {k: None if isinstance(v, float) and not math.isfinite(v) else v for k, v in item.to_dict().items()}
            row.update(symbol=key[2:], raw_symbol=key, quote_date=item.date, quote_time=item.time,
                       received_at=batch["received_at"], source=quotes["source"])
            accepted, _, rejected = _fresh_evidence([row], to_shanghai_datetime(datetime.fromisoformat(batch["received_at"])))
            if not accepted:
                batch["rejected_reasons"] = dict(Counter(batch["rejected_reasons"]) + Counter(rejected))
                continue
            previous, price = row.get("prev_close"), row.get("price")
            if previous and previous > 0 and price is not None and price > 0:
                change = (price/previous-1)*100
                row["pct_chg"] = change if math.isfinite(change) else None
            evidence["rows"].append(row)
        evidence["batches"].append(batch)
        atomic_write_json(evidence_path, evidence)
    quotes["rows"] = evidence["rows"]
    rejected = Counter()
    for batch in evidence["batches"]:
        rejected.update(batch["rejected_reasons"])
    info.update(returned_rows=len(quotes["rows"]), missing_rows=len(keys)-len(quotes["rows"]), rejected_reasons=dict(rejected))
    return quotes, info


def run_observation(root: Path | None = None, *, enable: bool = False,
                    authorized_date: str = "", now: datetime | None = None,
                    clock: Callable[[], datetime] | None = None,
                    model: Callable[[str], daytime_decide.ModelCall] | None = None,
                    wait_seconds: float = 0,
                    sleep: Callable[[float], None] | None = None,
                    monotonic: Callable[[], float] | None = None,
                    enable_local_quotes: bool = False,
                    quote_fetcher: Callable[..., str] | None = None) -> dict[str, Any]:
    """At most four attempts on the explicitly named date, one per fixed slot.

    A durable reservation consumes its slot even on crash/timeout. There is no
    automatic repeat of a model request whose result is unknown. No minute fetch,
    fill, execution-intent append, or trading-day upgrade is invoked here.
    """
    base: dict[str, Any] = {"schema_version": SCHEMA, "observation_only": True,
        "ledger_written": False, "execution_queue_written": False,
        "evidence_status": "unverified", "validation_label": "纸面观察 · 未核验",
        "calls_this_run": 0, "written": False}
    if not enable:
        return {**base, "status": "disabled", "reason": "explicit_observation_optin_required"}
    project = Path(root or PROJECT_ROOT).resolve()
    current = to_shanghai_datetime(now)
    try:
        day = date.fromisoformat(authorized_date).isoformat()
    except ValueError:
        return {**base, "status": "skipped", "reason": "authorized_date_required"}
    if day != current.date().isoformat():
        return {**base, "status": "skipped", "reason": "outside_authorized_date"}
    if active_profile() not in ("", "default"):
        return {**base, "status": "skipped", "reason": "default_profile_required"}
    slot = daytime_runner.floor_slot(current)
    if slot not in SLOTS or not daytime_runner._verified_open_day(project, day):
        return {**base, "status": "skipped", "reason": "outside_observation_window"}
    directory = project / OUTPUT_RELATIVE / day
    receipt = directory / f"{slot}.json"
    read_clock = clock or (lambda: to_shanghai_datetime())
    base.update(as_of=day, slot=slot, profile="default", observation_started_at=current.isoformat())
    try:
        with interprocess_file_lock(directory/"observer.lock", timeout_seconds=0):
            if receipt.exists():
                try:
                    previous = json.loads(receipt.read_bytes())
                    if (not isinstance(previous, dict) or previous.get("schema_version") != SCHEMA
                            or previous.get("as_of") != day or previous.get("slot") != slot):
                        raise ValueError("invalid_receipt")
                except (ValueError, OSError):
                    return {**base, "status": "blocked", "reason": "existing_receipt_unreadable"}
                return {**base, "status": "already_attempted", "previous_status": previous.get("status"),
                        "original_record": previous, "reason": "no_automatic_repeat", "receipt": str(receipt)}
            runtime = _paths(project)
            try:
                ledger = json.loads(runtime.ledger.read_bytes())
                account_available = (isinstance(ledger, dict) and isinstance(ledger.get("positions"), list)
                                     and isinstance(ledger.get("cash"), (int, float)))
            except (ValueError, OSError):
                account_available = False
            tick = monotonic or time.monotonic
            pause = sleep or time.sleep
            started_wait = tick()
            wait_limit = max(0.0, min(float(wait_seconds), INPUT_WAIT_SECONDS))
            direct_allowed = bool(enable_local_quotes and day == LOCAL_QUOTES_DATE and slot == LOCAL_QUOTES_SLOT)
            if direct_allowed:
                wait_limit = min(wait_limit, LOCAL_QUOTES_WAIT_SECONDS)
            reads = 0
            scheduled_wait = 0.0
            while True:
                quotes, fresh, quote_times, input_checks = _snapshot_evidence(project, current, slot)
                reads += 1
                if not account_available or fresh:
                    break
                remaining = wait_limit - max(tick() - started_wait, scheduled_wait)
                window_end = current.replace(hour=int(slot[:2]), minute=int(slot[2:]), second=0, microsecond=0) + timedelta(minutes=5)
                remaining = min(remaining, (window_end - current).total_seconds())
                if remaining <= 0:
                    break
                delay = min(INPUT_POLL_SECONDS, remaining)
                pause(delay)
                scheduled_wait += delay
                current = to_shanghai_datetime(read_clock())
                if current.date().isoformat() != day or daytime_runner.floor_slot(current) != slot:
                    fresh = []
                    quote_times = []
                    break
            base.update(input_checked_at=current.isoformat(), input_checks=input_checks,
                        input_read_count=reads, input_wait_seconds=round(max(tick()-started_wait, scheduled_wait), 3),
                        legacy_cache_used=False)
            if account_available and not fresh and direct_allowed:
                quotes, local_info = _local_hq_evidence(project, current, slot, receipt, base,
                    read_clock, tick, quote_fetcher or _hq_read_once)
                current = to_shanghai_datetime(read_clock())
                fresh, quote_times, rejected = _fresh_evidence(quotes["rows"], current)
                local_info["rejected_reasons"] = dict(Counter(local_info.get("rejected_reasons", {})) + Counter(rejected))
                base.update(local_quote_input=local_info, input_checked_at=current.isoformat(),
                            quote_requests_reserved=local_info["quote_requests_reserved"],
                            source=quotes["source"], source_path=quotes["path"], input_slot=quotes["slot"])
            input_rows = quotes.get("requested_rows", len(quotes["rows"]))
            if not account_available or not fresh:
                reason = "account_unavailable" if not account_available else "no_fresh_quote_evidence"
                skipped = {**base, "status": "skipped", "reason": reason,
                           "input_rows": input_rows, "fresh_rows": len(fresh), "written": True}
                atomic_write_json(receipt, skipped)
                return {**skipped, "receipt": str(receipt)}
            direct_input = quotes["source"] == "sina.hq_observation"
            screen = daytime_screen.build_screen(project, now=current, paths=runtime,
                slot_rows=fresh, slot=quotes["slot"], previous_rows=[] if direct_input else None)
            if direct_input:
                screen["warnings"].append("本轮直接报价没有已核验的相邻5分钟对照，相关比较缺失")
                screen["warnings"].append("请求采用现有全A名单，名单更新于：" +
                    ";".join(base["local_quote_input"].get("universe_updated_at") or ["未知"]))
            screen.update(source=quotes["source"], source_path=quotes["path"], snapshot_time=max(quote_times))
            screen["coverage"] = {"rows": input_rows, "valid_quotes": len(fresh),
                                  "ratio": len(fresh)/input_rows}
            prompt = daytime_screen.render_text(screen)
            input_digest = hashlib.sha256(json.dumps(fresh, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            request_time = to_shanghai_datetime(read_clock())
            boundary_reason = ""
            if request_time.date().isoformat() != day or daytime_runner.floor_slot(request_time) != slot:
                boundary_reason = "observation_window_elapsed"
            elif any(not 0 <= (request_time - daytime_fill._stamp(stamp)).total_seconds() <= MAX_QUOTE_AGE_SECONDS
                     for stamp in quote_times):
                boundary_reason = "quote_evidence_expired_before_call"
            if boundary_reason:
                skipped = {**base, "status": "skipped", "reason": boundary_reason,
                           "input_rows": input_rows, "fresh_rows": 0, "written": True,
                           "request_checked_at": request_time.isoformat()}
                atomic_write_json(receipt, skipped)
                return {**skipped, "receipt": str(receipt)}
            reserved = {**base, "status": "reserved", "model_attempt_reserved": True,
                        "max_calls_this_slot": 1, "max_tokens": MAX_TOKENS, "timeout_seconds": TIMEOUT_SECONDS,
                        "input_rows": input_rows, "fresh_rows": len(fresh),
                        "input_slot": quotes["slot"],
                        "request_started_at": request_time.isoformat(),
                        "source": quotes["source"], "source_path": quotes["path"],
                        "quote_time_min": min(quote_times), "quote_time_max": max(quote_times),
                        "input_sha256": input_digest, "prompt": prompt,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
            # A request is never sent unless this reservation is already durable.
            atomic_write_json(receipt, reserved)
            completed: dict[str, Any]
            try:
                with llm.llm_transport.capture_requests(receipt.parent / "llm_requests"):
                    call = (model or call_default_model)(prompt)
                payload = llm._extract_json(call.text) if call.ok else None
                attempted = bool(getattr(call, "request_attempted", True))
                completed = {**reserved, "calls_this_run": int(attempted),
                    "status": "recorded_unverified" if payload is not None else "model_failed",
                    "model": call.model, "raw_response": call.text,
                    "model_completed_at": to_shanghai_datetime(read_clock()).isoformat() if attempted else "",
                    "elapsed_ms": call.elapsed_ms,
                    "error": call.error or ("invalid_json_object" if payload is None else ""),
                    "proposals_unverified": payload}
                evidence = getattr(call, "response_evidence", None)
                if isinstance(evidence, dict) and evidence:
                    completed["response_evidence"] = {
                        **evidence,
                        "parse_stage": "accepted" if payload is not None else "observation_json_rejected",
                    }
            except Exception as exc:
                completed = {**reserved, "calls_this_run": 1, "status": "model_failed",
                    "error": type(exc).__name__, "model_completed_at": to_shanghai_datetime(read_clock()).isoformat()}
            completed.update(recorded_at=to_shanghai_datetime(read_clock()).isoformat(), written=True)
            try:
                atomic_write_json(receipt, completed)
            except (OSError, RuntimeError, ValueError) as exc:
                return {**base, "calls_this_run": completed["calls_this_run"], "status": "save_failed", "error": type(exc).__name__,
                        "reason": "reservation_preserved_no_automatic_repeat", "receipt": str(receipt)}
            return {**completed, "receipt": str(receipt)}
    except TimeoutError:
        return {**base, "status": "busy", "reason": "observation_lock_held"}
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "save_failed", "error": type(exc).__name__}


def write_daily_summary(root: Path, authorized_date: str) -> Path:
    """One human-readable observation page, built only from saved receipts."""
    day = date.fromisoformat(authorized_date).isoformat()
    directory = Path(root).resolve()/OUTPUT_RELATIVE/day
    labels = {"reserved": "调用已预留，结果未知，不自动重试", "input_reserved": "取行情已预留，结果未知，不自动重试", "skipped": "跳过，未调用模型",
              "recorded_unverified": "已保存模型原话，未核验", "model_failed": "模型未返回有效判断"}
    lines = [f"# {day} 盘中观察", "", "仅观察 · 全部判断未核验 · 不写两本账 · 不进入成交队列", ""]
    for slot in SLOTS:
        path = directory/f"{slot}.json"
        lines += [f"## {slot[:2]}:{slot[2:]}", ""]
        if not path.is_file():
            lines += ["尚无本次记录，不能认定已运行。", ""]; continue
        try:
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
                raise ValueError("invalid_receipt")
        except (OSError, ValueError):
            lines += ["记录不可读，结果未知。", ""]; continue
        requests = "未知（已预留1次，不自动重试）" if value.get("status") == "reserved" else value.get("calls_this_run", 0)
        lines += [labels.get(value.get("status"), "状态未知"), "",
                  f"- 模型：{value.get('model') or '无有效模型记录'}",
                  f"- 报价时间：{value.get('quote_time_min') or '缺失'} 至 {value.get('quote_time_max') or '缺失'}",
                  f"- 模型完成：{value.get('model_completed_at') or '未执行或结果未知'}",
                  f"- 保存时间：{value.get('recorded_at') or '没有完整判断的保存时间'}",
                  f"- 等待资料：{value.get('input_wait_seconds', 0)}秒；读取{value.get('input_read_count', 1)}次",
                  f"- 本地取报价：预留{value.get('quote_requests_reserved', 0)}批；只存观察记录",
                  f"- 请求尝试：{requests}；原因：{value.get('error') or value.get('reason') or '—'}", ""]
        local = value.get("local_quote_input")
        if isinstance(local, dict):
            lines += [f"- 现有全A名单：{local.get('requested_rows', 0)}只；本轮有效报价{value.get('fresh_rows', 0)}只",
                      "- 名单更新时间：" + ";".join(local.get("universe_updated_at") or ["未知"]), ""]
        raw = value.get("raw_response")
        if raw:
            fence = '`' * max(3, max((len(s) for s in re.findall(r'`+', str(raw))), default=0)+1)
            lines += ["模型原文（尚未核验，不能据此认定能成交）：", "", fence+"text", str(raw), fence, ""]
    return write_text(directory/'OBSERVATIONS.md', '\n'.join(lines))


__all__ = ["run_observation", "call_default_model", "write_daily_summary", "OUTPUT_RELATIVE", "SLOTS", "INPUT_WAIT_SECONDS"]
