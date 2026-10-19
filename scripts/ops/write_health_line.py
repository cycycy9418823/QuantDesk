"""Write the CHARTER §7 health line at the top of STATUS.md."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CANON = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
HEADER_MARK = "# STATUS · 更新于"
SECTION = "## 今日健康行"
AI_LEDGERS = (
    ("默认", "state/ai_trader/ledger.json"),
    ("glm", "state/ai_trader_glm/ledger.json"),
)
AI_DECISIONS = (
    ("默认", "state/ai_trader/decision_latest.json"),
    ("glm", "state/ai_trader_glm/decision_latest.json"),
)


def log_path(root: Path) -> Path:
    return root / "logs" / "ops" / "health_line.log"


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def last_csv_date(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    first = lines[-1].split(",", 1)[0].strip()
    return first[:10] if len(first) >= 10 else None


def count_universe(root: Path) -> int:
    path = root / "data" / "universe" / "a_share_symbols.csv"
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    return max(0, len(rows) - 1) if rows else 0


def _daily_roster(root: Path) -> set[str]:
    path = root / "data/universe/a_share_symbols.csv"
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            codes = {str(row.get("symbol") or row.get("code") or "").strip()
                     for row in csv.DictReader(handle)}
    except (OSError, UnicodeError, csv.Error):
        return set()
    return {code for code in codes if len(code) == 6 and code.isascii() and code.isdigit()}


def _same_run_provider_list(root: Path, target: str, symbols: set[str]) -> set[str]:
    """Read the dated run's own evidence; a provider listing is not full-day proof."""
    try:
        latest = load_json(root / "results/data_chain/daily_data_chain_latest.json") or {}
        run_id = latest.get("run_id", "")
        if (not isinstance(run_id, str) or not run_id.startswith("daily-data-chain-")
                or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in run_id)):
            return set()
        run_dir = root / "results/data_chain/runs" / run_id
        manifest_path = run_dir / "manifest.json"
        if (root / str(latest.get("manifest_path", ""))).resolve() != manifest_path.resolve():
            return set()
        manifest = load_json(manifest_path) or {}
        for doc in (latest, manifest):
            if (doc.get("task_type") != "daily_data_chain" or doc.get("authoritative") is not True
                    or doc.get("target_date") != target or doc.get("run_id") != run_id
                    or doc.get("status") not in {"completed", "completed_with_warnings", "completed_with_skips"}):
                return set()
        if not latest.get("pipeline_run_id") or latest["pipeline_run_id"] != manifest.get("pipeline_run_id"):
            return set()
        roster = root / "data/universe/a_share_symbols.csv"
        scope = manifest.get("scope") or {}
        if (scope.get("research_universe_source_path") != "data/universe/a_share_symbols.csv"
                or scope.get("research_universe_source_sha256") != hashlib.sha256(roster.read_bytes()).hexdigest()):
            return set()
        session = (manifest.get("components") or {}).get("session_expectation") or {}
        sidecar = load_json(run_dir / "session_expectation.json") or {}
        if (session.get("schema_version") != "session_expectation.v1"
                or session.get("target_date") != target
                or session.get("provider_date") != target.replace("-", "")
                or session.get("network_enabled") is not True
                or Path(str(session.get("project_root", ""))).resolve() != root.resolve()
                or Path(str(session.get("output_path", ""))).resolve() != (run_dir / "session_expectation.json").resolve()
                or set(session.get("symbols") or []) != symbols):
            return set()
        # The run manifest adds status fields after saving the sidecar. Compare
        # the actual inputs and classifications, not those later status labels.
        for key in ("schema_version", "target_date", "provider_date", "symbols", "per_symbol", "source_evidence"):
            if sidecar.get(key) != session.get(key):
                return set()
        provider = (session.get("source_evidence") or {}).get("provider") or {}
        digest = provider.get("sha256", "")
        if (provider.get("source") != "akshare.stock_tfp_em" or provider.get("as_of") != target
                or provider.get("status") != "success" or provider.get("read_ok") is not True
                or provider.get("network_enabled") is not True or provider.get("error")
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest.lower())
                or type(provider.get("count")) is not int or provider["count"] <= 0):
            return set()
        listed = set()
        for symbol, row in (session.get("per_symbol") or {}).items():
            if symbol not in symbols or not isinstance(row, dict) or row.get("symbol") != symbol:
                continue
            if any(isinstance(ref, dict) and ref.get("source") == "akshare.stock_tfp_em"
                   and ref.get("symbol") == symbol and ref.get("target_date") == target
                   and ref.get("status") == "returned_code" for ref in row.get("evidence", [])):
                listed.add(symbol)
        return listed
    except (OSError, UnicodeError, TypeError, ValueError, AttributeError):
        return set()


def _raw_target_trade(path: Path, symbol: str, target: str) -> bool:
    """Only explicit raw positive-volume target rows can contradict a halt list."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            declarations = [row[key] for key in ("adjust", "price_basis") if key in row]
            if not declarations or any(str(value).strip().lower() != "raw" for value in declarations):
                return False
        matches = [row for row in rows if row.get("date") == target and row.get("symbol") == symbol]
        if len(matches) != 1:
            return False
        row = matches[0]
        prices = [float(row[key]) for key in ("open", "high", "low", "close")]
        volume = float(row["volume"])
        return (all(math.isfinite(value) and value > 0 for value in [*prices, volume])
                and prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1])
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError, ValueError):
        return False


def daily_cell(root: Path, target: str) -> tuple[str, str]:
    daily = root / "data" / "daily"
    symbols = _daily_roster(root)
    current = {code for code in symbols if last_csv_date(daily / f"{code}.csv") == target}
    have, universe = len(current), len(symbols)
    missing_symbols = symbols - current
    missing = len(missing_symbols)
    mark = "✓" if universe and missing / universe <= 0.01 else "✗"
    # The 1% threshold stays a rendering rule only: a non-zero gap is always
    # printed, so a green mark can never swallow the number.
    gap = f"(缺{missing})" if missing else ""
    extras = [] if mark == "✓" else [f"日线缺口 {missing}/{universe}"]
    listed = _same_run_provider_list(root, target, symbols) if symbols else set()
    listed_missing = listed & missing_symbols
    label = f"；供应商停牌名单命中{len(listed_missing)}（全天状态待核）" if listed_missing else ""
    if missing_symbols - listed_missing:
        extras.append(f"日线缺口原因未核验{len(missing_symbols - listed_missing)}只")
    if not symbols:
        extras.append("日线名册缺失或不可读，数量未核验")
    conflicts = sorted(code for code in listed & current if _raw_target_trade(daily / f"{code}.csv", code, target))
    if conflicts:
        extras.append(f"停牌名单与当日raw成交证据冲突{len(conflicts)}只：{','.join(conflicts[:12])}；保留当日行情，不沿用停牌")
    return f"日线 {mark}{have}/{universe}{gap}{label}", "\n".join(extras)


def stamp_date(stamp: str) -> str:
    raw = str(stamp or "").replace("snapshot_", "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return ""


def last_backup_success(root: Path) -> str:
    log = log_path(root).parent / "backup_push.log"
    if not log.is_file():
        return ""
    last = ""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("all_hashes_match"):
            when = stamp_date(str(row.get("stamp") or ""))
            if when:
                last = when
    return last


def snapshot_quality(root: Path, target: str) -> dict:
    """Keep archive coverage separate from the stricter, field-verified count."""
    day = root / "data" / "snapshots" / target
    data = load_json(day / "manifest.json") or {}

    def count(key: str) -> int | None:
        value = data.get(key)
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
            return number if number >= 0 and float(value) == number else None
        except (ValueError, TypeError, OverflowError):
            return None

    collected, completed = count("collected_slot_count"), count("completed_slot_count")
    verified, expected = count("field_verified_complete_slot_count"), count("expected_slot_count")
    paired = False
    slots = data.get("field_verified_complete_slots")
    if (expected and collected == completed == verified == expected
            and isinstance(slots, list) and len(slots) == expected
            and all(isinstance(slot, str) and re.fullmatch(r"\d{4}", slot) for slot in slots)
            and len(set(slots)) == expected and data.get("trade_date") == target):
        paired = True
        for slot in slots:
            csv_path = day / f"{slot}.csv"
            metadata = load_json(day / f"{slot}.json") or {}
            try:
                if (metadata.get("trade_date") != target or metadata.get("slot_time") != slot
                        or not csv_path.is_file() or csv_path.stat().st_size > 32 * 1024 * 1024
                        or metadata.get("csv_sha256") != hashlib.sha256(csv_path.read_bytes()).hexdigest()):
                    paired = False
                    break
            except OSError:
                paired = False
                break
    return {"collected": collected, "completed": completed, "verified": verified,
            "partial": count("partial_slot_count"), "expected": expected,
            "archive_integrity_verified": paired}


def last_snapshot_complete(root: Path, before: str | None = None) -> str:
    snap = root / "data" / "snapshots"
    if not snap.is_dir():
        return ""
    dates = sorted((p.name for p in snap.iterdir() if p.is_dir()), reverse=True)
    for day in dates:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or (before and day > before):
            continue
        if snapshot_quality(root, day)["archive_integrity_verified"]:
            return day
    return ""


def snapshot_slot_files(root: Path, target: str) -> int:
    """How many slot CSVs exist for the day, independent of the manifest claim."""

    day = root / "data" / "snapshots" / target
    return len(list(day.glob("*.csv"))) if day.is_dir() else 0


def pull_log_rows(root: Path) -> list[dict]:
    """Every parsable line of the snapshot pull log (the log is JSON-lines)."""

    log = log_path(root).parent / "snapshot_pull.log"
    if not log.is_file():
        return []
    rows: list[dict] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def short_moment(stamp: str) -> str:
    """`2026-09-24T10:12:33+08:00` -> `09-24 10:12`（只影响显示，不参与判定）。"""

    text = str(stamp or "")
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[5:10]} {text[11:16]}"
    return text


# 只有字段名本身表示“已发布”的键才能当上游发布时间。collected_at / generated_at 说的是
# 采集和生成，不冒充发布：没有明确发布证据就保持未知，并让它们保留自己的名字。
PUBLISHED_KEYS = ("upstream_published_at", "published_at")
COLLECTED_KEYS = ("collected_at", "generated_at")
# 旧调用名保留，内容已收窄为“发布”语义的键。
UPSTREAM_KEYS = PUBLISHED_KEYS


def first_stamp(source: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def manifest_version(root: Path, target: str) -> dict:
    """当前数据的版本指纹；清单读不到就是空指纹（宁可未知，不借别版）。"""

    path = root / "data" / "snapshots" / target / "manifest.json"
    try:
        raw = path.read_bytes()
    except OSError:
        return {"manifest_sha256": "", "manifest_revision": None}
    revision = None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        revision = data.get("revision")
    return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "manifest_revision": revision}


def snapshot_arrival(root: Path, target: str) -> dict:
    """业务日期 vs 本地到达时间：pulled_at 只认与当前数据同一版的那一行。

    行情日期取业务日期（目录名/清单），本地到达时间取拉取日志里发布完成的那一刻。
    到达记录还要和当前清单指纹对得上：新数据已换版而成功记录没写成，就只能说本地到达
    时间未知——不能借上一版的时间，也不能拿重试那一刻补签。
    上游发布时间只认明确的发布字段；采集/生成时间保留自己的名字。
    """

    rows = [row for row in pull_log_rows(root) if str(row.get("date") or "") == target]
    published = [row for row in rows
                 if row.get("published") is True and row.get("pulled_at")]
    current = manifest_version(root, target)
    matched = [row for row in published
               if current["manifest_sha256"]
               and str(row.get("manifest_sha256") or "") == current["manifest_sha256"]]
    last = matched[-1] if matched else {}
    if matched:
        evidence = "matched"
    elif not published:
        evidence = "no_publish_record"
    elif str(published[-1].get("manifest_sha256") or ""):
        evidence = "other_version"
    else:
        evidence = "unbound_receipt"
    manifest = load_json(root / "data" / "snapshots" / target / "manifest.json") or {}
    upstream = first_stamp(manifest, PUBLISHED_KEYS)
    return {
        "business_date": target,
        "attempts": len(rows),
        "published": bool(published),
        "local_received_at": str(last.get("pulled_at") or ""),
        "arrival_evidence": evidence if rows else "",
        "current_manifest_sha256": current["manifest_sha256"],
        "manifest_revision": current["manifest_revision"],
        "upstream_published_at": upstream,
        "upstream_known": bool(upstream),
        "upstream_collected_at": first_stamp(manifest, COLLECTED_KEYS),
    }


def arrival_unknown_note(target: str, arrival: dict) -> str:
    """到达时间给不出可信值时的说法：按实际证据分类，都不冒认、不借别版。"""

    evidence = str(arrival.get("arrival_evidence") or "")
    if evidence == "no_publish_record":
        return f"快照 {target} 有拉取记录但没有一次发布完成，本地到达时间未知"
    if evidence == "other_version":
        return (f"快照 {target} 的当前数据版本没有对应的到达记录"
                "（已有记录属于另一版），本地到达时间未知")
    if evidence == "unbound_receipt":
        return f"快照 {target} 的到达记录没有数据版本凭证，本地到达时间未知"
    return f"快照 {target} 本地到达时间未知"


def snapshot_cell(root: Path, target: str) -> tuple[str, str]:
    quality = snapshot_quality(root, target)
    files = snapshot_slot_files(root, target)
    mark = "✓" if quality["archive_integrity_verified"] and files >= quality["expected"] else "✗"
    completed = quality["completed"] if quality["completed"] is not None else "未知"
    verified = quality["verified"] if quality["verified"] is not None else "未知"
    cell = f"快照 {mark}{files}张/覆盖完成 {completed}/字段核实 {verified}"
    notes: list[str] = []
    arrival = snapshot_arrival(root, target)
    received = arrival["local_received_at"]
    if received:
        cell += f"（本地 {short_moment(received)} 收到）"
        if received[:10] > target:
            notes.append(f"行情日期仍是 {target}：本地 {received[:10]} 才拿到，"
                         "复制时间不回改行情日期")
    elif arrival["attempts"]:
        notes.append(arrival_unknown_note(target, arrival))
    if arrival["attempts"]:
        notes.append("上游发布 " + (arrival["upstream_published_at"] or "未知"))
        if arrival["upstream_collected_at"]:
            # 采集/生成时间保留自己的名字，不写成发布时间。
            notes.append(f"上游采集/生成 {arrival['upstream_collected_at']}（不是发布时间）")
    if mark == "✓":
        return cell, " · ".join(notes)
    prev = last_snapshot_complete(root, before=target)
    prev_txt = prev or "无可核实记录"
    if files:
        notes.append(f"快照 {target} 已归档 {files} 张，完整字段或档案哈希仍未核实；上次完整 {prev_txt}")
    else:
        notes.append(f"快照未拉回 {target}，上次完整 {prev_txt}")
    if quality["partial"] is not None:
        notes.append(f"部分覆盖 {quality['partial']} 档；覆盖完成不等于报价日期、价格口径和价格字段完整")
    return cell, " · ".join(notes)


BENCHMARK_FILES = ("hs300.csv", "zz500.csv", "sh000001.csv")


def previous_trading_day(root: Path, target: str) -> tuple[str, str]:
    """Previous open session before ``target`` (calendar first, weekday fallback)."""

    cal = root / "data" / "calendar" / "a_share_trading_calendar.csv"
    if cal.is_file():
        try:
            with cal.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
                days = [
                    str(row.get("date") or "").strip()
                    for row in csv.DictReader(handle)
                    if str(row.get("is_open") or "").strip().lower() == "true"
                ]
        except OSError:
            days = []
        earlier = sorted(day for day in days if day and day < target)
        if earlier:
            return earlier[-1], ""
        if days:
            return "", f"日历无 {target} 之前的上一个交易日"
    try:
        day = datetime.strptime(target, "%Y-%m-%d").date()
    except ValueError:
        return "", f"目标日 {target} 无法解析，基准只按日期文本比较"
    day = day - timedelta(days=1)
    while day.weekday() >= 5:
        day = day - timedelta(days=1)
    return day.isoformat(), "日历无记录，基准按前一工作日判定（降级，非交易日历）"


def _plain_day(value: str | None) -> str:
    """把 CSV 里的日期文本校验成真实日历日；不是真日期就返回空串。

    ``date.fromisoformat`` 会拒掉 ``not-a-date`` / ``2026-99-99`` 这类文本——它们按
    字符串大小比较会排到真日期后面，把坏证据读成"更新"（R10-0928-BENCHMARK-DATE）。
    """

    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def benchmark_cell(root: Path, target: str) -> tuple[str, str]:
    """The three index files must at least carry the previous trading day.

    Only a file that is missing / unreadable, or one that stopped *before* that
    day, is an alarm: a file that already carries the target day is merely
    fresher, not stale. The green mark shows the oldest of the three dates, so
    the laggard stays visible instead of hiding behind the expected day.
    """

    expected, note = previous_trading_day(root, target)
    if not expected:
        # 日历里找不到目标日之前的交易日：没有可比的基准日，证据不足，不能判绿
        # （R10-0928-BENCHMARK-DATE）。
        reason = note or "日历缺目标日之前的交易日"
        return "基准 ✗", f"无法判定：{reason}；补日历或人工确认后重跑"
    stale: list[str] = []
    days: list[str] = []
    for name in BENCHMARK_FILES:
        label = name.replace(".csv", "")
        path = root / "data" / "benchmark" / name
        if not path.is_file():
            stale.append(f"{label}=缺失")
            continue
        raw_day = last_csv_date(path)
        if not raw_day:
            # 文件在，但读不出日期（空文件/表头以外没数据）——同样要点名。
            stale.append(f"{label}=读不出")
            continue
        day = _plain_day(raw_day)
        if not day:
            # 日期栏写的是真日期之外的文本（not-a-date / 2026-99-99）：按字符串比较会
            # 排在真日期后面，把坏证据读成"更新"，所以先校验、点名为坏日期、不判绿。
            stale.append(f"{label}=坏日期")
            continue
        days.append(day)
        if day < expected:
            stale.append(f"{label}={day}")
    if not stale and days:
        return f"基准 ✓{min(days)}", note
    detail = "基准停在哪天：" + ("无上一交易日" if not expected else f"应有 {expected}，实际 " + " ".join(stale))
    return "基准 ✗", " · ".join(item for item in (detail, note) if item)


def minute_cell(root: Path, target: str) -> tuple[str, str]:
    """Minute round gaps, including storage-blocked writes, stay countable."""

    data = load_json(root / "results" / "intraday" / "intraday_refresh_latest.json")
    if data is None:
        return "分钟 ✗", "分钟回执缺失 results/intraday/intraday_refresh_latest.json"
    try:
        requested = int(data.get("requested_count") or 0)
        fresh = int(data.get("fresh_count") or 0)
        missing = int(data.get("missing_count") or 0)
    except (TypeError, ValueError):
        requested = fresh = missing = 0
    blocked = sum(
        1
        for row in (data.get("provider_status") or [])
        if isinstance(row, dict) and row.get("status") == "storage_blocked"
    )
    as_of = str(data.get("target_date") or "")[:10]
    mark = "✓" if requested and missing == 0 and blocked == 0 and as_of and as_of >= target else "✗"
    counts = f"{fresh}/{requested}"
    gap = []
    if missing:
        gap.append(f"缺{missing}")
    if blocked:
        gap.append(f"拒写{blocked}")
    cell = f"分钟 {mark}{counts}" + (f"({'+'.join(gap)})" if gap else "")
    if mark == "✓":
        return cell, ""
    detail = f"分钟数据日 {as_of or '缺失'}"
    if gap:
        detail += "，" + "，".join(gap)
    return cell, detail


def strategy_cell(root: Path, target: str) -> tuple[str, str]:
    path = root / "results" / "auto_paper_portfolio_latest.json"
    data = load_json(path) or {}
    generated = str(data.get("generated_at") or "")
    intake = data.get("candidate_intake") if isinstance(data.get("candidate_intake"), dict) else {}
    as_of = str(intake.get("as_of") or "")[:10]
    mark = "✓" if as_of and as_of >= target else "✗"
    extra = "" if mark == "✓" else f"策略账数据日 {as_of or '缺失'}，生成 {generated or '缺失'}"
    return f"策略账 {mark}", extra


def ai_cell(root: Path, target: str) -> tuple[str, str]:
    fills = 0
    parts: list[str] = []
    missing: list[str] = []
    stale = False
    model_unverified = False
    for label, rel in AI_LEDGERS:
        path = root / rel
        data = load_json(path)
        if data is None:
            missing.append(rel)
            parts.append(f"{label} 缺失")
            continue
        as_of = str(data.get("as_of") or "")[:10]
        trades = data.get("trades") if isinstance(data.get("trades"), list) else []
        n = 0
        for item in trades:
            if isinstance(item, dict) and str(item.get("date") or "")[:10] == target:
                n += 1
        fills += n
        if not as_of or as_of < target:
            stale = True
        parts.append(f"{label} {as_of or '缺失'} {n}笔")
        journal = load_json(path.with_name("journal_latest.json")) or {}
        llm = journal.get("llm") if isinstance(journal.get("llm"), dict) else {}
        if str(journal.get("as_of") or "")[:10] != target or type(llm.get("used")) is not bool:
            model_unverified = True
            parts.append(f"{label} 当日模型参与未核验（journal缺失、陈旧或llm.used不明）")
        elif llm["used"] is False:
            model_unverified = True
            parts.append(f"{label} 规则回退（模型未参与）")
    if missing:
        return f"AI账 ✗{fills}笔", f"AI账本缺失 {missing[0]}"
    mark = "✗" if stale else ("△" if model_unverified else "✓")
    return f"AI账 {mark}{fills}笔", " · ".join(parts)


def review_cell(root: Path, target: str) -> tuple[str, str]:
    counts: list[int] = []
    as_ofs: list[str] = []
    labels: list[str] = []
    for label, rel in AI_DECISIONS:
        data = load_json(root / rel)
        if data is None:
            return "核对 ✗", f"核对记录缺失 {rel}"
        as_of = str(data.get("as_of") or "")[:10]
        recs = data.get("records") if isinstance(data.get("records"), list) else []
        n = 0
        for item in recs:
            if not isinstance(item, dict):
                continue
            if str(item.get("decision_date") or "")[:10] == target and item.get("result") == "陈述与数据不符":
                n += 1
        counts.append(n)
        as_ofs.append(as_of)
        labels.append(label)
    if any(not item or item < target for item in as_ofs):
        lag = min((item for item in as_ofs if item), default="缺失")
        return "核对 ✗", f"核对记录截至 {lag}"
    total = sum(counts)
    explain = " · ".join(f"{lab} {n}" for lab, n in zip(labels, counts))
    if total == 0:
        return "核对 ✓", explain
    return f"核对 ✗{total}条陈述不符", explain


def backup_cell(root: Path, target: str) -> tuple[str, str]:
    log = log_path(root).parent / "backup_push.log"
    if not log.is_file():
        return "备份 ✗", f"备份未在 {target} 成功，上次成功 从未成功"
    last = ""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            last = line
    try:
        row = json.loads(last) if last else {}
    except json.JSONDecodeError:
        return "备份 ✗", f"备份未在 {target} 成功，上次成功 {last_backup_success(root) or '从未成功'}"
    formatted = stamp_date(str(row.get("stamp") or ""))
    ok = formatted == target and bool(row.get("all_hashes_match"))
    if ok:
        return "备份 ✓", ""
    prev = last_backup_success(root) or "从未成功"
    return "备份 ✗", f"备份未在 {target} 成功，上次成功 {prev}"


def is_trading_day(root: Path, target: str) -> tuple[bool, str]:
    cal = root / "data" / "calendar" / "a_share_trading_calendar.csv"
    if not cal.is_file():
        return True, f"日历无 {target}"
    with cal.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            day = str(row.get("date") or "").strip()
            if day == target:
                flag = str(row.get("is_open") or "").strip().lower()
                return flag == "true", ""
    return True, f"日历无 {target}"


def rewrite_status(root: Path, header: str, section_line: str, explain: str) -> None:
    path = root / "STATUS.md"
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    rest = original
    if rest.startswith(HEADER_MARK) or rest.startswith("# STATUS"):
        idx = 0
        lines = rest.splitlines()
        if lines and lines[0].startswith("# STATUS"):
            idx = 1
            while idx < len(lines) and lines[idx].strip() == "":
                idx += 1
            if idx < len(lines) and lines[idx].strip() == SECTION:
                idx += 1
                while idx < len(lines) and lines[idx].strip() == "":
                    idx += 1
                if idx < len(lines) and lines[idx].startswith("日线"):
                    idx += 1
                if idx < len(lines) and lines[idx] and not lines[idx].startswith("#") and not lines[idx].startswith("##"):
                    if "日线" not in lines[idx][:2]:
                        idx += 1
                while idx < len(lines) and lines[idx].strip() == "":
                    idx += 1
                rest = "\n".join(lines[idx:])
            else:
                rest = original
    block = [header, "", SECTION, "", section_line]
    if explain:
        block.extend([explain])
    block.extend(["", rest.lstrip("\n") if rest else ""])
    path.write_text("\n".join(block).rstrip() + "\n", encoding="utf-8")


def build_line(root: Path, target: str) -> tuple[str, list[str], bool]:
    trading, cal_note = is_trading_day(root, target)
    explains: list[str] = [f"健康业务日期 {target}"]
    if cal_note:
        explains.append(cal_note)
    if not trading:
        backup, extra = backup_cell(root, target)
        line = f"非交易日 {target} · {backup}"
        if extra:
            explains.append(extra)
        return line, explains, False
    cells = []
    for fn in (daily_cell, benchmark_cell, snapshot_cell, minute_cell,
               strategy_cell, ai_cell, review_cell, backup_cell):
        cell, extra = fn(root, target)
        cells.append(cell)
        if extra:
            explains.extend(extra.splitlines())
    return " · ".join(cells), explains, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", default="")
    parser.add_argument("--json", default="false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else CANON
    now = datetime.now(SHANGHAI).replace(microsecond=0)
    target = args.target_date or now.strftime("%Y-%m-%d")
    line, explains, trading = build_line(root, target)
    header = f"{HEADER_MARK} {now.strftime('%Y-%m-%d %H:%M')} · 更新者：health_line"
    payload = {
        "target_date": target,
        "line": line,
        "explains": explains,
        "header": header,
        "trading_day": trading,
        "dry_run": bool(args.dry_run),
    }
    if not args.dry_run:
        rewrite_status(root, header, line, " · ".join(explains))
        log = log_path(root)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
