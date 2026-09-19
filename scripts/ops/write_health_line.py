"""Write the CHARTER §7 health line at the top of STATUS.md."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CANON = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOG = CANON / "logs" / "ops" / "health_line.log"
HEADER_MARK = "# STATUS · 更新于"
SECTION = "## 今日健康行"


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


def count_universe() -> int:
    path = CANON / "data" / "universe" / "a_share_symbols.csv"
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    return max(0, len(rows) - 1) if rows else 0


def daily_cell(target: str) -> tuple[str, str]:
    daily = CANON / "data" / "daily"
    have = 0
    files = list(daily.glob("*.csv")) if daily.is_dir() else []
    for path in files:
        if last_csv_date(path) == target:
            have += 1
    universe = count_universe() or 5555
    mark = "✓" if universe and (universe - have) / universe <= 0.01 else "✗"
    extra = "" if mark == "✓" else f"日线缺口 {universe - have}/{universe}"
    return f"日线 {mark}{have}/{universe}", extra


def snapshot_cell(target: str) -> tuple[str, str]:
    man = CANON / "data" / "snapshots" / target / "manifest.json"
    if not man.is_file():
        return "快照 ✗0/48", "云端镜像未拉回"
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "快照 ✗0/48", "云端镜像未拉回"
    collected = int(data.get("collected_slot_count") or 0)
    mark = "✓" if collected >= 46 else "✗"
    extra = "" if mark == "✓" else (f"本机 {target} 的 {collected}/48" if collected else "云端镜像未拉回")
    return f"快照 {mark}{collected}/48", extra


def json_date(path: Path, keys: tuple[str, ...]) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)[:10]
    return None


def strategy_cell(target: str) -> tuple[str, str]:
    as_of = json_date(CANON / "results" / "auto_paper_portfolio_latest.json", ("as_of", "as_of_date", "date"))
    mark = "✓" if as_of == target else "✗"
    extra = "" if mark == "✓" else "策略账日期不是目标日"
    return f"策略账 {mark}", extra


def ai_cell(target: str) -> tuple[str, str]:
    dates = []
    fills = 0
    for rel in ("state/ai_trader/decision_latest.json", "state/ai_trader_glm/decision_latest.json"):
        path = CANON / rel
        dates.append(json_date(path, ("decision_date", "as_of", "date", "trade_date")))
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            fills += int(data.get("fill_count") or data.get("fills") or data.get("trade_count") or 0)
            if isinstance(data.get("fills"), list):
                fills += len(data["fills"])
    ok = any(item == target for item in dates)
    mark = "✓" if ok else "✗"
    extra = "" if ok else "AI账决策日期不是目标日"
    return f"AI账 {mark}{fills}笔", extra


def review_cell(target: str) -> tuple[str, str]:
    path = CANON / "results" / "strategy_decision_review_latest.json"
    if not path.is_file():
        return "核对 ✗", "核对文件缺失"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "核对 ✗", "核对文件不可读"
    count = int(
        data.get("statement_mismatch_count")
        or data.get("mismatch_count")
        or data.get("unverified_count")
        or 0
    )
    if count == 0:
        return "核对 ✓", ""
    return f"核对 ✗{count}条陈述不符", f"{count}条陈述不符"


def backup_cell(target: str) -> tuple[str, str]:
    log = CANON / "logs" / "ops" / "backup_push.log"
    if not log.is_file():
        return "备份 ✗", "尚无备份日志"
    last = ""
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    try:
        row = json.loads(last)
    except json.JSONDecodeError:
        return "备份 ✗", "备份日志不可读"
    stamp = str(row.get("stamp") or "")
    when = stamp.replace("snapshot_", "")[:8]
    formatted = f"{when[:4]}-{when[4:6]}-{when[6:8]}" if len(when) >= 8 else ""
    ok = formatted == target and bool(row.get("all_hashes_match"))
    return ("备份 ✓", "") if ok else ("备份 ✗", "备份未在目标日成功")


def is_trading_day(target: str) -> bool:
    cal = CANON / "data" / "calendar" / "a_share_trading_calendar.csv"
    if not cal.is_file():
        return True
    text = cal.read_text(encoding="utf-8", errors="replace")
    return target in text


def rewrite_status(header: str, section_line: str, explain: str) -> None:
    path = CANON / "STATUS.md"
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    rest = original
    if rest.startswith(HEADER_MARK) or rest.startswith("# STATUS"):
        parts = rest.split("\n", 8)
        # drop previous injected header/section if present
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
                    # explanation line
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", default="")
    parser.add_argument("--json", default="false")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI).replace(microsecond=0)
    target = args.target_date or now.strftime("%Y-%m-%d")
    explains: list[str] = []
    if not is_trading_day(target):
        backup, extra = backup_cell(target)
        line = f"非交易日 {target} · {backup}"
        if extra:
            explains.append(extra)
    else:
        cells = []
        for fn in (daily_cell, snapshot_cell, strategy_cell, ai_cell, review_cell, backup_cell):
            cell, extra = fn(target)
            cells.append(cell)
            if extra:
                explains.append(extra)
        line = " · ".join(cells)
    header = f"{HEADER_MARK} {now.strftime('%Y-%m-%d %H:%M')} · 更新者：health_line"
    rewrite_status(header, line, explains[0] if explains else "")
    payload = {"target_date": target, "line": line, "explains": explains, "header": header}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
