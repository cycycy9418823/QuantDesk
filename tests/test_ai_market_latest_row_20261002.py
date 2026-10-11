"""D3/T17：长短 CSV 都按**有效日期**取真实最新行；行数与代码顺序不得冒充异动排名。

全部合成夹具：不联网、不写正式 data/。
"""

from __future__ import annotations

from pathlib import Path

from ai_trader import market

HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"
DAY = "2026-09-28"


def _row(day: str, symbol: str, close: float, pct: str | float) -> str:
    return f"{day},{symbol},{close},{close},{close},{close},1000,10000,{pct},raw"


def _write(root: Path, symbol: str, rows: list[tuple[str, float, str | float]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{symbol}.csv"
    path.write_text("\n".join([HEADER] + [_row(d, symbol, c, p) for d, c, p in rows]) + "\n", encoding="utf-8")
    return path


def _long_history(symbol: str, *, days: int = 500, last_pct: float = 7.5) -> list[tuple[str, float, str | float]]:
    """造一份远超 4000 字节的历史，最后一根是真实最新行（日期全部合法）。"""

    from datetime import date, timedelta

    start = date(2024, 1, 2)
    rows: list[tuple[str, float, str | float]] = []
    for index in range(days - 1):
        rows.append(((start + timedelta(days=index)).isoformat(), 10.0 + index * 0.01, f"{index % 7 - 3}.0"))
    rows.append((DAY, 12.34, last_pct))
    return rows


def test_long_csv_still_reports_the_real_latest_row(tmp_path: Path) -> None:
    """长 CSV：尾块里没有表头，也必须取到真实最新行的涨跌（不能静默丢成 None）。"""

    daily = tmp_path / "daily"
    path = _write(daily, "600000", _long_history("600000"))

    assert path.stat().st_size > 4000, "夹具必须超过 4000 字节才有意义"
    assert market.last_bar_date(path) == DAY
    assert market.last_bar_pct_chg(path) == 7.5


def test_trailing_junk_line_does_not_hide_the_real_latest_row(tmp_path: Path) -> None:
    """末尾有半截/残缺行时，取最后一条**有效日期**的行，而不是直接返回 None。"""

    daily = tmp_path / "daily"
    path = _write(daily, "600000", [("2026-09-25", 10.0, 1.0), (DAY, 10.5, 5.0)])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-09-29,600000,10.6,10.6,10.6,10.6\n")  # 残缺行：没有有效收盘

    assert market.last_bar_date(path) == DAY
    assert market.last_bar_pct_chg(path) == 5.0


def test_unknown_changes_cannot_make_a_mover_ranking(tmp_path: Path) -> None:
    """全部票都读不到涨跌时，不得凭代码顺序产出一份"异动排名"。"""

    daily = tmp_path / "daily"
    for symbol in ("600300", "600200", "600100"):
        _write(daily, symbol, [("2026-09-25", 10.0, 1.0), (DAY, 10.0, "")])

    names = market.focus_universe(daily, DAY, limit=10)

    assert names == [], f"读不到涨跌就不能给排名，实际 {names}"


def test_unknown_change_is_not_ranked_as_a_mover(tmp_path: Path) -> None:
    """涨跌读不到的票不得冒充"异动"混进排名。"""

    daily = tmp_path / "daily"
    _write(daily, "600000", [("2026-09-25", 10.0, 1.0), (DAY, 10.5, "+5.0")])
    _write(daily, "600001", [("2026-09-25", 10.0, 1.0), (DAY, 10.0, "")])  # 涨跌为空

    names = market.focus_universe(daily, DAY, limit=10)

    assert names[0] == "600000", names
    assert "600001" not in names, "读不到涨跌的票不能冒充当日异动"


def test_ranking_does_not_follow_file_or_code_order(tmp_path: Path) -> None:
    """排名必须按真实涨跌，不得由文件顺序或代码顺序决定。"""

    daily = tmp_path / "daily"
    # 故意按代码倒序创建文件：若排名跟着 glob/代码走，就会与真实涨跌相反。
    _write(daily, "600300", [("2026-09-25", 10.0, 1.0), (DAY, 9.9, "-1.0")])
    _write(daily, "600200", [("2026-09-25", 10.0, 1.0), (DAY, 10.3, "+3.0")])
    _write(daily, "600100", [("2026-09-25", 10.0, 1.0), (DAY, 11.0, "+10.0")])

    names = market.focus_universe(daily, DAY, limit=10)

    assert names[:3] == ["600100", "600200", "600300"], names


def test_short_csv_and_pinned_names_stay_unchanged(tmp_path: Path) -> None:
    """正常对照：短 CSV 照旧；持仓/自选/研究台名单仍固定在前面。"""

    daily = tmp_path / "daily"
    _write(daily, "600000", [("2026-09-25", 10.0, 1.0), (DAY, 10.5, 6.0)])
    _write(daily, "600001", [("2026-09-25", 10.0, 1.0), (DAY, 11.0, 9.0)])

    names = market.focus_universe(daily, DAY, held=["600000"], watch=["600002"], limit=10)

    assert names[0] == "600000" and names[1] == "600002", names
    assert names[2] == "600001", names  # 真实异动最高的跟在固定名单之后