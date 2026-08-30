from __future__ import annotations

from datetime import date, timedelta

from quantdesk import market_intelligence_utils as market_utils


def test_read_daily_tail_does_not_scalar_parse_every_history_row(
    tmp_path,
    monkeypatch,
) -> None:
    daily_dir = tmp_path / "data" / "daily"
    daily_dir.mkdir(parents=True)
    start = date(2022, 1, 1)
    rows = ["date,open,high,low,close,volume,amount,adjust"]
    for offset in range(1200):
        current = start + timedelta(days=offset)
        rows.append(
            f"{current.isoformat()},10,11,9,10.5,1000,10500,raw"
        )
    (daily_dir / "000001.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    original = market_utils.normalize_date_text
    scalar_parse_calls = 0

    def counting_normalize(value: object) -> str:
        nonlocal scalar_parse_calls
        scalar_parse_calls += 1
        return original(value)

    monkeypatch.setattr(market_utils, "normalize_date_text", counting_normalize)

    result = market_utils.read_daily_tail(
        "000001",
        project_root=tmp_path,
        limit=8,
        cutoff_date="2024-06-01",
    )

    assert len(result) == 8
    assert result[-1]["date"] == "2024-06-01"
    assert scalar_parse_calls <= 2
