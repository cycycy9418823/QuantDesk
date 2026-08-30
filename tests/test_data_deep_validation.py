from __future__ import annotations

from pathlib import Path

from quantdesk.data_deep_validation import deep_validate_daily_csv


def test_deep_validate_daily_csv_skips_bad_rows_with_visible_warning(tmp_path: Path) -> None:
    path = tmp_path / "000001.csv"
    path.write_text(
        "\n".join(
            [
                "date,open,high,low,close,volume",
                "2024-01-02,10.0,10.5,9.8,10.1,100000",
                "2024-01-03,10.0,10.5,9.8,10.2,100000,unexpected_extra_field",
                "2024-01-04,10.0,10.5,9.8,10.3,100000",
            ]
        ),
        encoding="utf-8-sig",
    )

    result = deep_validate_daily_csv(path)

    assert result.quality_label != "READ_ERROR"
    assert result.total_rows == 2
    assert any("csv_bad_lines_skipped" in warning for warning in result.warnings)
