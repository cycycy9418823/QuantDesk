"""T06: daily-price normalization and publication must retain source evidence.

Only the provider fetch boundary is replaced in engine tests. Classification,
normalization, local reads, merging and atomic CSV writes use production code.
All writable paths are beneath pytest's tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quantdesk import daily_price_providers as providers
from quantdesk import daily_price_update_engine as engine
from quantdesk.price_basis import PRICE_BASIS_RAW, classify_frame_price_basis


SYMBOL = "600405"
PREVIOUS = "2026-09-28"
TARGET = "2026-09-29"
CALENDAR = {"is_open_by_date": {PREVIOUS: True, TARGET: True}}


def _bar(day: str = TARGET, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": day,
        "symbol": SYMBOL,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 1000.0,
        "amount": 10200.0,
        "adjust": "raw",
        "source": "fixture_exchange_response",
        "updated_at": f"{day}T15:01:02+08:00",
    }
    row.update(changes)
    return row


def _seed_daily(tmp_path: Path, frame: pd.DataFrame) -> tuple[Path, bytes]:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    path = daily_dir / f"{SYMBOL}.csv"
    # Seed the original evidence directly, before calling the reader under test.
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path, path.read_bytes()


def _run_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: pd.DataFrame,
) -> dict[str, Any]:
    def fake_fetch(
        provider_name: str,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        network: bool = False,
    ) -> tuple[pd.DataFrame, list[str]]:
        assert provider_name == "sina_daily"
        assert symbol == SYMBOL
        assert start_date <= end_date == TARGET
        assert network is True
        return fresh.copy(deep=True), []

    monkeypatch.setattr(engine, "fetch_daily", fake_fetch)
    return engine._fetch_merge_write_symbol_unlocked(
        "sina_daily",
        SYMBOL,
        target=TARGET,
        network=True,
        calendar=CALENDAR,
        daily_dir=tmp_path / "daily",
        metadata_dir=tmp_path / "metadata",
    )


def _assert_rejected_without_write(
    outcome: dict[str, Any], path: Path, before: bytes, *, basis: bool = False,
) -> None:
    assert outcome["updated"] is False, outcome
    assert outcome.get("resolved") is not True, outcome
    assert path.read_bytes() == before
    reason = str(outcome.get("reason") or "")
    assert reason, outcome
    if basis:
        diagnostics = " ".join([reason, *map(str, outcome.get("warnings") or [])]).lower()
        assert "basis" in diagnostics or "adjust" in diagnostics, outcome


@pytest.mark.parametrize("missing", ["absent_columns", "null_values"])
def test_normalizer_preserves_missing_ohl_instead_of_copying_close(missing: str) -> None:
    frame = pd.DataFrame([_bar()])
    if missing == "absent_columns":
        frame = frame.drop(columns=["open", "high", "low"])
    else:
        frame.loc[0, ["open", "high", "low"]] = float("nan")
    original = frame.copy(deep=True)

    normalized, warnings = providers.normalize_daily_df(frame, SYMBOL, source="wrapper")

    pd.testing.assert_frame_equal(frame, original)
    assert warnings, "Missing price evidence must remain visible."
    if not normalized.empty:
        for column in ("open", "high", "low"):
            assert column not in normalized or normalized[column].isna().all()
        assert normalized["close"].tolist() == [10.2]


@pytest.mark.parametrize(
    "source", ["local_existing", "akshare_daily", "tushare_daily", "tencent_daily"],
)
def test_normalizer_does_not_infer_missing_adjust_from_source(source: str) -> None:
    frame = pd.DataFrame([_bar()]).drop(columns=["adjust"])

    normalized, warnings = providers.normalize_daily_df(frame, SYMBOL, source=source)

    basis, reason = classify_frame_price_basis(normalized)
    assert basis != PRICE_BASIS_RAW
    assert reason, "A source label does not prove the original price basis."
    if not normalized.empty and "adjust" in normalized:
        assert normalized["adjust"].isna().all() or all(
            str(value).strip().lower() in {"", "unknown", "mixed_or_unknown"}
            for value in normalized["adjust"].tolist()
        )
    assert warnings


def test_existing_source_and_capture_time_survive_normalize_merge_csv_and_read(
    tmp_path: Path,
) -> None:
    old = _bar(PREVIOUS, source="original_provider_a")
    new = _bar(TARGET, source="original_provider_b")
    existing, _ = providers.normalize_daily_df(
        pd.DataFrame([old]), SYMBOL, source="local_existing",
    )
    fresh, _ = providers.normalize_daily_df(
        pd.DataFrame([new]), SYMBOL, source="fetch_wrapper",
    )
    merged = providers.merge_daily_frames(existing, fresh)
    providers.write_daily(SYMBOL, merged, daily_dir=tmp_path / "daily")

    disk = pd.read_csv(tmp_path / "daily" / f"{SYMBOL}.csv", dtype=str)
    reread = providers.read_local_daily(SYMBOL, daily_dir=tmp_path / "daily")
    expected = {row["date"]: (row["source"], row["updated_at"]) for row in (old, new)}
    for frame in (disk, reread):
        observed = {
            str(row["date"]): (row["source"], row["updated_at"])
            for row in frame.to_dict("records")
        }
        assert observed == expected
        assert classify_frame_price_basis(frame) == (PRICE_BASIS_RAW, "")


@pytest.mark.parametrize("hidden_by", ["invalid_date", "duplicate_date"])
@pytest.mark.parametrize("bad_adjust", ["qfq", None], ids=["mixed", "unknown"])
def test_local_reader_cannot_erase_basis_conflict_during_cleanup(
    tmp_path: Path, hidden_by: str, bad_adjust: str | None,
) -> None:
    bad_day = "invalid-date" if hidden_by == "invalid_date" else PREVIOUS
    original = pd.DataFrame([
        _bar(bad_day, adjust=bad_adjust),
        _bar(PREVIOUS),
    ])
    path, before = _seed_daily(tmp_path, original)

    read, warnings = providers.read_local_daily_with_warnings(
        SYMBOL, daily_dir=path.parent,
    )

    basis, reason = classify_frame_price_basis(read)
    assert basis != PRICE_BASIS_RAW
    assert reason
    assert warnings, "The source-frame rejection must remain visible to callers."
    assert path.read_bytes() == before


@pytest.mark.parametrize("hidden_by", ["invalid_date", "duplicate_date"])
@pytest.mark.parametrize("bad_adjust", ["qfq", None], ids=["mixed", "unknown"])
def test_engine_rejects_original_local_basis_conflict_without_rewriting_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    hidden_by: str, bad_adjust: str | None,
) -> None:
    bad_day = "invalid-date" if hidden_by == "invalid_date" else PREVIOUS
    path, before = _seed_daily(tmp_path, pd.DataFrame([
        _bar(bad_day, adjust=bad_adjust),
        _bar(PREVIOUS),
    ]))

    outcome = _run_engine(tmp_path, monkeypatch, pd.DataFrame([_bar()]))

    _assert_rejected_without_write(outcome, path, before, basis=True)


@pytest.mark.parametrize("invalid_field", ["volume", "open"])
@pytest.mark.parametrize("bad_adjust", ["qfq", None], ids=["mixed", "unknown"])
def test_engine_classifies_fresh_basis_before_invalid_bar_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    invalid_field: str, bad_adjust: str | None,
) -> None:
    path, before = _seed_daily(tmp_path, pd.DataFrame([_bar(PREVIOUS)]))
    bad = _bar(PREVIOUS, adjust=bad_adjust)
    bad[invalid_field] = 0.0 if invalid_field == "volume" else None
    fresh = pd.DataFrame([bad, _bar()])

    outcome = _run_engine(tmp_path, monkeypatch, fresh)

    _assert_rejected_without_write(outcome, path, before, basis=True)


@pytest.mark.parametrize("missing", ["absent_columns", "null_values"])
def test_engine_cannot_publish_missing_ohl_as_real_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    path, before = _seed_daily(tmp_path, pd.DataFrame([_bar(PREVIOUS)]))
    fresh = pd.DataFrame([_bar()])
    if missing == "absent_columns":
        fresh = fresh.drop(columns=["open", "high", "low"])
    else:
        fresh.loc[0, ["open", "high", "low"]] = float("nan")

    outcome = _run_engine(tmp_path, monkeypatch, fresh)

    _assert_rejected_without_write(outcome, path, before)


def test_engine_accepts_complete_raw_and_preserves_dates_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _bar(PREVIOUS, source="original_provider_a")
    new = _bar(TARGET, source="original_provider_b")
    path, _ = _seed_daily(tmp_path, pd.DataFrame([old]))

    outcome = _run_engine(tmp_path, monkeypatch, pd.DataFrame([new]))

    assert outcome.get("resolved") is True, outcome
    assert outcome["updated"] is True, outcome
    assert not outcome["reason"], outcome
    written = pd.read_csv(path, dtype={"symbol": str})
    assert written["date"].tolist() == [PREVIOUS, TARGET]
    assert classify_frame_price_basis(written) == (PRICE_BASIS_RAW, "")
    for actual, expected in zip(written.to_dict("records"), (old, new), strict=True):
        for field in ("date", "symbol", "open", "high", "low", "close", "source", "updated_at"):
            assert actual[field] == expected[field]


# Additional boundaries are selectable with ``-k t06_boundary`` so the original
# 22 regression cases can retain their separately recorded baseline result.


@pytest.mark.parametrize("bad_adjust", [None, "qfq"], ids=["unknown", "mixed"])
def test_t06_boundary_merge_cannot_borrow_raw_basis_from_same_date(
    bad_adjust: str | None,
) -> None:
    old = pd.DataFrame([_bar()])
    fresh = pd.DataFrame([_bar(adjust=bad_adjust, close=10.3)])

    merged = providers.merge_daily_frames(old, fresh)

    basis, reason = classify_frame_price_basis(merged)
    assert basis != PRICE_BASIS_RAW
    assert reason, "A raw old row must not certify conflicting fresh evidence."


@pytest.mark.parametrize("field", ["source", "updated_at"])
@pytest.mark.parametrize("missing", ["absent_column", "null_value"])
def test_t06_boundary_merge_cannot_borrow_old_row_provenance(
    field: str, missing: str,
) -> None:
    old = pd.DataFrame([_bar(source="old_response")])
    new_row = _bar(source="new_response", close=10.3)
    if missing == "absent_column":
        new_row.pop(field)
    else:
        new_row[field] = None

    merged = providers.merge_daily_frames(old, pd.DataFrame([new_row]))

    assert len(merged) == 1
    assert merged.iloc[0]["close"] == 10.3
    value = merged.iloc[0].get(field)
    assert pd.isna(value) or value == "", (
        f"Fresh prices must not borrow the old row's {field}: {value!r}"
    )


@pytest.mark.parametrize("bad_adjust", ["qfq", None], ids=["mixed", "unknown"])
@pytest.mark.parametrize("hidden_by", ["none", "invalid_date", "duplicate_date"])
def test_t06_boundary_writer_rejects_uncertain_original_basis_before_replace(
    tmp_path: Path, bad_adjust: str | None, hidden_by: str,
) -> None:
    path, before = _seed_daily(tmp_path, pd.DataFrame([_bar(PREVIOUS)]))
    bad_day = "invalid-date" if hidden_by == "invalid_date" else PREVIOUS
    good_day = PREVIOUS if hidden_by == "duplicate_date" else TARGET
    incoming = pd.DataFrame([_bar(bad_day, adjust=bad_adjust), _bar(good_day)])

    try:
        providers.write_daily(SYMBOL, incoming, daily_dir=path.parent)
    except (ValueError, RuntimeError) as exc:
        assert any(token in str(exc).lower() for token in ("basis", "adjust", "raw"))
    else:
        pytest.fail("A direct writer must explicitly reject uncertain original basis.")

    assert path.read_bytes() == before


@pytest.mark.parametrize("adjust", ["qfq", "hfq"])
def test_t06_boundary_generic_writer_preserves_pure_adjusted_research_data(
    tmp_path: Path, adjust: str,
) -> None:
    incoming = pd.DataFrame([_bar(adjust=adjust)])
    expected_basis = classify_frame_price_basis(incoming)

    providers.write_daily(SYMBOL, incoming, daily_dir=tmp_path / "research")

    written = pd.read_csv(tmp_path / "research" / f"{SYMBOL}.csv", dtype={"symbol": str})
    assert classify_frame_price_basis(written) == expected_basis
    for field in ("date", "symbol", "open", "high", "low", "close", "adjust", "source", "updated_at"):
        assert written.iloc[0][field] == incoming.iloc[0][field]


@pytest.mark.parametrize("adjust", ["qfq", "hfq"])
def test_t06_boundary_canonical_engine_rejects_pure_adjusted_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adjust: str,
) -> None:
    path, before = _seed_daily(tmp_path, pd.DataFrame([_bar(PREVIOUS)]))

    outcome = _run_engine(tmp_path, monkeypatch, pd.DataFrame([_bar(adjust=adjust)]))

    _assert_rejected_without_write(outcome, path, before, basis=True)


def _adapter_response(
    monkeypatch: pytest.MonkeyPatch, provider: str, response: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], list[dict[str, Any]]]:
    """Replace only the external SDK/HTTP-return boundary, not its adapter."""
    calls: list[dict[str, Any]] = []

    def fetch(**kwargs: Any) -> pd.DataFrame:
        calls.append(dict(kwargs))
        return response.copy(deep=True)

    if provider == "akshare_daily":
        import sys
        from importlib.machinery import ModuleSpec
        from types import ModuleType

        sdk = ModuleType("akshare")
        sdk.__spec__ = ModuleSpec("akshare", loader=None)
        sdk.stock_zh_a_hist = fetch
        monkeypatch.setitem(sys.modules, "akshare", sdk)
    else:
        assert provider == "sina_daily"
        monkeypatch.setattr("quantdesk.data_providers.fetch_daily_sina", fetch)

    normalized, warnings = providers.fetch_daily(
        provider, SYMBOL, TARGET, TARGET, network=True,
    )
    assert len(calls) == 1
    assert calls[0]["adjust"] == ""
    assert calls[0]["start_date"] == calls[0]["end_date"] == TARGET.replace("-", "")
    return normalized, warnings, calls


@pytest.mark.parametrize("provider", ["akshare_daily", "sina_daily"])
@pytest.mark.parametrize("adjust", ["qfq", "hfq", "unknown"])
def test_t06_boundary_adapter_does_not_overwrite_returned_adjust(
    monkeypatch: pytest.MonkeyPatch, provider: str, adjust: str,
) -> None:
    response = pd.DataFrame([_bar(adjust=adjust, source="original_response")])
    expected_basis = classify_frame_price_basis(response)
    original = response.copy(deep=True)

    try:
        frame, _warnings, _calls = _adapter_response(monkeypatch, provider, response)
    except (ValueError, RuntimeError) as exc:
        assert any(token in str(exc).lower() for token in ("basis", "adjust", "raw"))
    else:
        assert classify_frame_price_basis(frame) == expected_basis
        if not frame.empty:
            assert frame.iloc[0]["source"] == "original_response"
            assert frame.iloc[0]["updated_at"] == response.iloc[0]["updated_at"]
    pd.testing.assert_frame_equal(response, original)


@pytest.mark.parametrize("provider", ["akshare_daily", "sina_daily"])
def test_t06_boundary_explicit_raw_adapter_request_labels_unmarked_response(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    response = pd.DataFrame([_bar(source="original_response")]).drop(columns=["adjust"])

    frame, _warnings, _calls = _adapter_response(monkeypatch, provider, response)

    assert len(frame) == 1
    assert classify_frame_price_basis(frame) == (PRICE_BASIS_RAW, "")
    for field in ("date", "open", "high", "low", "close", "source", "updated_at"):
        assert frame.iloc[0][field] == response.iloc[0][field]


@pytest.mark.parametrize("missing", ["absent_column", "null_value"])
def test_t06_boundary_normalizer_does_not_invent_missing_capture_time(missing: str) -> None:
    row = _bar()
    if missing == "absent_column":
        row.pop("updated_at")
    else:
        row["updated_at"] = None

    normalized, _warnings = providers.normalize_daily_df(
        pd.DataFrame([row]), SYMBOL, source="local_existing",
    )

    assert len(normalized) == 1
    value = normalized.iloc[0].get("updated_at")
    assert pd.isna(value) or value == ""


@pytest.mark.parametrize("provider", ["akshare_daily", "sina_daily"])
def test_t06_boundary_capture_time_can_be_added_at_actual_fetch_boundary(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    response = pd.DataFrame([_bar()]).drop(columns=["adjust", "source", "updated_at"])
    started = pd.Timestamp.now().floor("s")

    normalized, _warnings, calls = _adapter_response(monkeypatch, provider, response)

    finished = pd.Timestamp.now().ceil("s")
    assert len(calls) == len(normalized) == 1
    stamp = pd.Timestamp(normalized.iloc[0]["updated_at"])
    assert not pd.isna(stamp)
    # Existing adapters use local timestamps; accept an aware timestamp too.
    if stamp.tzinfo is not None:
        from datetime import datetime

        stamp = stamp.tz_convert(datetime.now().astimezone().tzinfo).tz_localize(None)
    assert started <= stamp <= finished
    assert str(normalized.iloc[0]["source"]).strip() == provider
    assert classify_frame_price_basis(normalized) == (PRICE_BASIS_RAW, "")


# Focused review cases; select separately with ``-k t06_review``.


@pytest.mark.parametrize("missing_column", ["date", "close"])
@pytest.mark.parametrize("bad_adjust", ["qfq", None], ids=["mixed", "unknown"])
@pytest.mark.parametrize("negative_side", ["existing", "fresh"])
def test_t06_review_empty_rejected_input_cannot_become_writable_after_merge(
    tmp_path: Path, missing_column: str, bad_adjust: str | None, negative_side: str,
) -> None:
    path, before = _seed_daily(tmp_path, pd.DataFrame([_bar(PREVIOUS)]))
    invalid = pd.DataFrame([
        _bar(PREVIOUS, adjust=bad_adjust),
        _bar(TARGET),
    ]).drop(columns=[missing_column])
    good = pd.DataFrame([_bar(TARGET)])

    try:
        negative, _warnings = providers.normalize_daily_df(invalid, SYMBOL)
        inputs = (negative, good) if negative_side == "existing" else (good, negative)
        merged = providers.merge_daily_frames(*inputs)
        providers.write_daily(SYMBOL, merged, daily_dir=path.parent)
    except ValueError as exc:
        assert str(exc).strip(), "Rejection must explain the missing or conflicting evidence."
    else:
        pytest.fail("An empty rejected input must not be discarded to certify a writable raw merge.")

    assert path.read_bytes() == before


@pytest.mark.parametrize("bad_side", ["existing", "fresh"])
def test_t06_review_merge_basis_conflict_survives_plain_csv_round_trip(
    tmp_path: Path, bad_side: str,
) -> None:
    bad = pd.DataFrame([_bar("invalid-date", adjust="qfq")])
    good = pd.DataFrame([_bar()])
    inputs = (bad, good) if bad_side == "existing" else (good, bad)

    try:
        merged = providers.merge_daily_frames(*inputs)
    except ValueError as exc:
        assert str(exc).strip()
        return

    # This intentionally bypasses write_daily: source evidence must survive
    # ordinary DataFrame serialization, which never persists DataFrame.attrs.
    path = tmp_path / "plain_merge.csv"
    merged.to_csv(path, index=False)
    reread = pd.read_csv(path)
    basis, reason = classify_frame_price_basis(reread)
    assert basis != PRICE_BASIS_RAW
    assert reason, "An attrs-only rejection disappears at the CSV boundary."


@pytest.mark.parametrize("column_names", [("adjust", "复权"), ("adjust", "adjust")])
@pytest.mark.parametrize("reverse_columns", [False, True], ids=["raw_first", "qfq_first"])
def test_t06_review_conflicting_basis_columns_are_not_selected_by_order(
    column_names: tuple[str, str], reverse_columns: bool,
) -> None:
    prices = pd.DataFrame([_bar()]).drop(columns=["adjust"])
    evidence = pd.DataFrame([["raw", "qfq"]], columns=list(column_names))
    if reverse_columns:
        evidence = evidence.iloc[:, ::-1]
    source = pd.concat([prices, evidence], axis=1)
    original = source.copy(deep=True)

    try:
        normalized, _warnings = providers.normalize_daily_df(source, SYMBOL)
    except ValueError as exc:
        assert str(exc).strip(), "Ambiguous columns need an explicit rejection."
    else:
        basis, reason = classify_frame_price_basis(normalized)
        assert basis != PRICE_BASIS_RAW
        assert reason, "Selecting the first conflicting column hides original evidence."
    pd.testing.assert_frame_equal(source, original)


@pytest.mark.parametrize("column_names", [("adjust", "复权"), ("adjust", "adjust")])
@pytest.mark.parametrize("reverse_columns", [False, True])
def test_t06_review_equal_raw_columns_are_explicitly_rejected_or_preserved(
    column_names: tuple[str, str], reverse_columns: bool,
) -> None:
    prices = pd.DataFrame([_bar()]).drop(columns=["adjust"])
    evidence = pd.DataFrame([["raw", "raw"]], columns=list(column_names))
    if reverse_columns:
        evidence = evidence.iloc[:, ::-1]
    source = pd.concat([prices, evidence], axis=1)

    try:
        normalized, _warnings = providers.normalize_daily_df(source, SYMBOL)
    except ValueError as exc:
        assert str(exc).strip(), "A strict duplicate-column policy must be explicit."
    else:
        assert len(normalized) == 1
        assert classify_frame_price_basis(normalized) == (PRICE_BASIS_RAW, "")
        for field in ("date", "open", "high", "low", "close", "source", "updated_at"):
            assert normalized.iloc[0][field] == prices.iloc[0][field]
