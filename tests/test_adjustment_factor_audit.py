from pathlib import Path

from quantdesk.data_truth.adjustment_factor_audit import audit_adjustment_factor_file_set, audit_adjustment_factors


def test_adjustment_factor_audit_marks_low_coverage_partial(tmp_path: Path) -> None:
    path = tmp_path / "adjustment_factors.csv"
    path.write_text(
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n",
        encoding="utf-8",
    )

    result = audit_adjustment_factors(path, {"000001", "000002"}, {"000001", "000002"}, {"000002"})

    assert result["exists"] is True
    assert result["adjustment_factors_status"] in {"partial_low_coverage", "partial_usable_for_audit"}
    assert result["formal_or_partial_classification"] == "partial_formal_metadata"


def test_adjustment_factor_audit_actually_counts_duplicate_keys(tmp_path: Path) -> None:
    # 同一 (symbol, trade_date) 出现两次 → 复权口径歧义，必须被真实计数，而非硬编码 None。
    path = tmp_path / "adjustment_factors.csv"
    path.write_text(
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n"
        "2026-01-01,000001,1.2,unit,test\n"
        "2026-01-02,000001,1.3,unit,test\n",
        encoding="utf-8",
    )

    result = audit_adjustment_factors(path, {"000001"}, {"000001"}, {"000001"})

    assert isinstance(result["duplicates"], int)
    assert result["duplicates"] == 1
    # 存在重复键时，状态不得判为可正式使用，且 issues 要留痕。
    assert any("duplicate" in str(item).lower() for item in result["issues"])


def test_adjustment_factor_audit_clean_file_has_zero_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "adjustment_factors.csv"
    path.write_text(
        "trade_date,symbol,adj_factor,source,provider\n"
        "2026-01-01,000001,1.0,unit,test\n"
        "2026-01-02,000001,1.1,unit,test\n",
        encoding="utf-8",
    )

    result = audit_adjustment_factors(path, {"000001"}, {"000001"}, {"000001"})

    assert result["duplicates"] == 0
    assert result["abnormal_factor_jump_count"] == 0


def test_adjustment_factor_file_set_accepts_identical_alias_with_warning(tmp_path: Path) -> None:
    content = (
        "trade_date,symbol,adj_factor,source\n"
        "2026-01-01,000001,1.0,unit\n"
    )
    (tmp_path / "adjustment_factors.csv").write_text(content, encoding="utf-8")
    (tmp_path / "adjustment_factor.csv").write_text(content, encoding="utf-8")

    result = audit_adjustment_factor_file_set(tmp_path)

    assert result["authoritative_file"] == "adjustment_factors.csv"
    assert result["file_set_status"] == "canonical_with_duplicate_alias"
    assert result["alternate_files"][0]["name"] == "adjustment_factor.csv"
    assert result["alternate_files"][0]["matches_authoritative_sample"] is True
    assert any("duplicate_alias_adjustment_factor_file" in item for item in result["issues"])


def test_adjustment_factor_file_set_flags_divergent_alias(tmp_path: Path) -> None:
    (tmp_path / "adjustment_factors.csv").write_text(
        "trade_date,symbol,adj_factor,source\n"
        "2026-01-01,000001,1.0,unit\n",
        encoding="utf-8",
    )
    (tmp_path / "adjustment_factor.csv").write_text(
        "trade_date,symbol,adj_factor,source\n"
        "2026-01-01,000001,2.0,unit\n",
        encoding="utf-8",
    )

    result = audit_adjustment_factor_file_set(tmp_path)

    assert result["authoritative_file"] == "adjustment_factors.csv"
    assert result["file_set_status"] == "canonical_with_divergent_alias"
    assert result["alternate_files"][0]["matches_authoritative_sample"] is False
    assert any("divergent_adjustment_factor_file" in item for item in result["issues"])
