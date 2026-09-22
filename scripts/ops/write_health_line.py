"""Write the CHARTER §7 health line at the top of STATUS.md."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
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


def daily_cell(root: Path, target: str) -> tuple[str, str]:
    daily = root / "data" / "daily"
    have = 0
    files = list(daily.glob("*.csv")) if daily.is_dir() else []
    for path in files:
        if last_csv_date(path) == target:
            have += 1
    universe = count_universe(root) or 5555
    missing = max(0, universe - have)
    mark = "✓" if universe and missing / universe <= 0.01 else "✗"
    # The 1% threshold stays a rendering rule only: a non-zero gap is always
    # printed, so a green mark can never swallow the number.
    gap = f"(缺{missing})" if missing else ""
    extra = "" if mark == "✓" else f"日线缺口 {missing}/{universe}"
    return f"日线 {mark}{have}/{universe}{gap}", extra


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


def last_snapshot_complete(root: Path) -> str:
    snap = root / "data" / "snapshots"
    if not snap.is_dir():
        return ""
    dates = sorted((p.name for p in snap.iterdir() if p.is_dir()), reverse=True)
    for day in dates:
        man = snap / day / "manifest.json"
        data = load_json(man)
        if not data:
            continue
        try:
            collected = int(data.get("collected_slot_count") or 0)
        except (TypeError, ValueError):
            collected = 0
        if collected >= 46:
            return day
    return ""


def snapshot_slot_files(root: Path, target: str) -> int:
    """How many slot CSVs exist for the day, independent of the manifest claim."""

    day = root / "data" / "snapshots" / target
    return len(list(day.glob("*.csv"))) if day.is_dir() else 0


def snapshot_cell(root: Path, target: str) -> tuple[str, str]:
    man = root / "data" / "snapshots" / target / "manifest.json"
    collected = 0
    if man.is_file():
        data = load_json(man)
        if data is not None:
            try:
                collected = int(data.get("collected_slot_count") or 0)
            except (TypeError, ValueError):
                collected = 0
    files = snapshot_slot_files(root, target)
    mark = "✓" if collected >= 46 else "✗"
    # Both numbers are always shown: how many sheets were pulled and how many
    # slots the manifest calls complete ("48张/完成 0" must stay readable).
    cell = f"快照 {mark}{files}张/完成 {collected}"
    if mark == "✓":
        return cell, ""
    prev = last_snapshot_complete(root)
    prev_txt = prev or "从未成功"
    return cell, f"快照未拉回 {target}，上次完整 {prev_txt}"


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


def benchmark_cell(root: Path, target: str) -> tuple[str, str]:
    """The three index files must already carry the previous trading day."""

    expected, note = previous_trading_day(root, target)
    stale: list[str] = []
    for name in BENCHMARK_FILES:
        path = root / "data" / "benchmark" / name
        day = (last_csv_date(path) if path.is_file() else None) or "缺失"
        if not expected or day != expected:
            stale.append(f"{name.replace('.csv', '')}={day}")
    if not stale:
        return f"基准 ✓{expected}", note
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
    if missing:
        return f"AI账 ✗{fills}笔", f"AI账本缺失 {missing[0]}"
    mark = "✓" if not stale else "✗"
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
    explains: list[str] = []
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
            explains.append(extra)
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
        rewrite_status(root, header, line, explains[0] if explains else "")
        log = log_path(root)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
