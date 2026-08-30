from pathlib import Path

from quantdesk.csv_io import read_csv_chunks, read_csv_frame


def test_read_csv_frame_handles_utf8_sig(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_text("\ufeffsymbol,name\n000001,平安银行\n", encoding="utf-8")

    frame = read_csv_frame(path, dtype=str)

    assert list(frame.columns) == ["symbol", "name"]
    assert frame.iloc[0]["symbol"] == "000001"
    assert frame.attrs["warnings"] == []


def test_read_csv_frame_skips_bad_lines_with_visible_warning(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("symbol,name\n000001,ok\n000002,bad,extra\n000003,ok3\n", encoding="utf-8")

    frame = read_csv_frame(path, dtype=str)

    assert frame["symbol"].tolist() == ["000001", "000003"]
    warnings = frame.attrs["warnings"]
    assert any(item.startswith("csv_parse_failed:") for item in warnings)
    assert any(item.startswith("csv_bad_lines_skipped:") for item in warnings)


def test_read_csv_chunks_handles_utf8_sig(tmp_path: Path) -> None:
    path = tmp_path / "chunked.csv"
    path.write_text("\ufeffsymbol,value\n000001,1\n000002,2\n000003,3\n", encoding="utf-8")

    chunks = list(read_csv_chunks(path, dtype=str, chunksize=2))

    assert [list(chunk["symbol"]) for chunk in chunks] == [["000001", "000002"], ["000003"]]
    assert all(chunk.attrs["warnings"] == [] for chunk in chunks)
