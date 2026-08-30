"""Round 10 Part E — incremental benchmark index updates.

``data/benchmark/`` held three index series frozen at 2026-07-10 while the daily
chain kept reporting green.  The forensic reason: no code path in the repository
ever *writes* ``data/benchmark``.  ``data_update_orchestrator.latest_benchmark_date``
only reads it to compute ``benchmark_staleness_days``, so the staleness was
measured faithfully every day and never acted on.

The three files carry three genuinely different schemas, each with live
consumers:

    hs300.csv     date,open,high,low,close,volume,pct_chg,amount
    zz500.csv     date,benchmark_symbol,close,pct_chg,source,provider,fetched_at
    sh000001.csv  date,open,high,low,close,volume

so there is no single canonical shape to converge on, and the pre-existing
``metadata_evidence_status.repair_benchmark`` cannot be reused: it truncates at a
hardcoded ``<= 2026-05-31``, rewrites whole files in one narrow schema, and does
not know about ``sh000001.csv`` at all.

These tests are fully offline (the index fetcher is injected) and operate on a
temporary project root, so no canonical dataset is touched.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantdesk.data_truth.benchmark_incremental_update import (
    BENCHMARK_SPECS,
    BenchmarkSpec,
    update_benchmark_files,
)


# --- fixtures: the three real schemas, in miniature ---------------------------

_HS300_HEADER = "date,open,high,low,close,volume,pct_chg,amount"
_HS300_ROWS = [
    "2026-07-09,4774.402,4882.848,4734.867,4876.313,28162770500,2.539756839084739,",
    "2026-07-10,4887.536,4906.49,4780.747,4780.787,30413028600,-1.95898007367451,",
]
_ZZ500_HEADER = "date,benchmark_symbol,close,pct_chg,source,provider,fetched_at"
_ZZ500_ROWS = [
    "2026-07-09,sh000905,8652.787,3.13,akshare.stock_zh_index_daily,akshare,2026-07-12T16:17:41+08:00",
    "2026-07-10,sh000905,8503.971,-1.72,akshare.stock_zh_index_daily,akshare,2026-07-12T16:17:41+08:00",
]
_SH_HEADER = "date,open,high,low,close,volume"
_SH_ROWS = [
    "2026-07-09,3977.547,4040.539,3938.878,4036.588,55306398000",
    "2026-07-10,4031.537,4074.828,3995.807,3996.162,62745006500",
]


def _write_local(path, header: str, rows: list[str]) -> None:
    """Write a benchmark CSV the way the real ones are stored: BOM + CRLF."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = header + "\r\n" + "".join(row + "\r\n" for row in rows)
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    bench = root / "data" / "benchmark"
    _write_local(bench / "hs300.csv", _HS300_HEADER, _HS300_ROWS)
    _write_local(bench / "zz500.csv", _ZZ500_HEADER, _ZZ500_ROWS)
    _write_local(bench / "sh000001.csv", _SH_HEADER, _SH_ROWS)
    return root


def _remote(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a Sina-shaped index frame: date,open,high,low,close,volume."""

    return pd.DataFrame(
        [
            {
                "date": date,
                "open": close - 1.0,
                "high": close + 2.0,
                "low": close - 3.0,
                "close": close,
                "volume": 10_000_000_000,
            }
            for date, close in rows
        ]
    )


def _overlap_plus(new_rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Remote frame that agrees on the two local sessions, then extends."""

    return _remote([("2026-07-09", 0.0), ("2026-07-10", 0.0)] + new_rows)


def _fetcher_for(frames: dict[str, pd.DataFrame]):
    def fetch(symbol: str) -> pd.DataFrame:
        return frames[symbol]

    return fetch


def _agreeing_fetcher(new_rows: list[tuple[str, float]]):
    """A fetcher whose overlap matches every fixture's local closes."""

    hs = _remote([("2026-07-09", 4876.313), ("2026-07-10", 4780.787)] + new_rows)
    zz = _remote([("2026-07-09", 8652.787), ("2026-07-10", 8503.971)] + new_rows)
    sh = _remote([("2026-07-09", 4036.588), ("2026-07-10", 3996.162)] + new_rows)
    return _fetcher_for({"sh000300": hs, "sh000905": zz, "sh000001": sh})


def _read_text(path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _lines(path) -> list[str]:
    return [line for line in _read_text(path).split("\r\n") if line]


# --- the core claim: rows actually land on disk -------------------------------


def test_append_persists_to_disk_and_extends_every_file(project):
    payload = update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="2026-07-25T23:59:00+08:00",
    )

    assert payload["status"] == "updated"
    for item in payload["results"]:
        assert item["status"] == "appended", item
        assert item["appended_count"] == 1, item

    # Re-read from disk rather than trusting the returned payload.
    for name in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        path = project / "data" / "benchmark" / name
        lines = _lines(path)
        assert len(lines) == 4, name  # header + 2 existing + 1 appended
        assert lines[-1].startswith("2026-07-13"), name


def test_history_bytes_are_never_rewritten(project):
    path = project / "data" / "benchmark" / "hs300.csv"
    before = path.read_bytes()

    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="2026-07-25T23:59:00+08:00",
    )

    after = path.read_bytes()
    assert after.startswith(before), "existing bytes must be a strict prefix"
    assert len(after) > len(before)


def test_bom_and_crlf_are_preserved(project):
    path = project / "data" / "benchmark" / "sh000001.csv"

    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 3913.794)]),
        fetched_at="2026-07-25T23:59:00+08:00",
    )

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM must survive the append"
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b""), "no bare LF may be introduced"


def test_dry_run_changes_nothing_on_disk(project):
    paths = {
        name: (project / "data" / "benchmark" / name).read_bytes()
        for name in ("hs300.csv", "zz500.csv", "sh000001.csv")
    }

    payload = update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        dry_run=True,
        fetched_at="2026-07-25T23:59:00+08:00",
    )

    assert payload["dry_run"] is True
    assert {item["status"] for item in payload["results"]} == {"dry_run"}
    for name, before in paths.items():
        assert (project / "data" / "benchmark" / name).read_bytes() == before


# --- fail-closed behaviour ----------------------------------------------------


def test_overlap_close_mismatch_blocks_the_write(project):
    path = project / "data" / "benchmark" / "hs300.csv"
    before = path.read_bytes()
    # Same dates, a different index's levels: stitching these together would
    # silently splice two series into one history.
    wrong = _remote([("2026-07-09", 1234.5), ("2026-07-10", 1240.0), ("2026-07-13", 1250.0)])
    fetcher = _fetcher_for(
        {
            "sh000300": wrong,
            "sh000905": _remote([("2026-07-09", 8652.787), ("2026-07-10", 8503.971)]),
            "sh000001": _remote([("2026-07-09", 4036.588), ("2026-07-10", 3996.162)]),
        }
    )

    payload = update_benchmark_files(project, fetcher=fetcher, fetched_at="x")

    hs = next(item for item in payload["results"] if item["label"] == "hs300")
    assert hs["status"] == "blocked"
    assert "overlap_close_mismatch" in hs["reason_codes"]
    assert path.read_bytes() == before, "a blocked file must not be touched"
    assert payload["status"] == "degraded"


def test_no_verifiable_overlap_blocks_the_write(project):
    path = project / "data" / "benchmark" / "hs300.csv"
    before = path.read_bytes()
    # A remote frame that shares no date with the local history proves nothing.
    disjoint = _remote([("2026-08-03", 4700.0), ("2026-08-04", 4710.0)])
    fetcher = _fetcher_for(
        {
            "sh000300": disjoint,
            "sh000905": _remote([("2026-07-09", 8652.787), ("2026-07-10", 8503.971)]),
            "sh000001": _remote([("2026-07-09", 4036.588), ("2026-07-10", 3996.162)]),
        }
    )

    payload = update_benchmark_files(project, fetcher=fetcher, fetched_at="x")

    hs = next(item for item in payload["results"] if item["label"] == "hs300")
    assert hs["status"] == "blocked"
    assert "no_verifiable_overlap" in hs["reason_codes"]
    assert path.read_bytes() == before


def test_fetch_failure_is_isolated_to_its_own_file(project):
    hs_path = project / "data" / "benchmark" / "hs300.csv"
    hs_before = hs_path.read_bytes()

    def fetch(symbol: str) -> pd.DataFrame:
        if symbol == "sh000300":
            raise RuntimeError("index_fetch_failed:sh000300:SSLError:boom")
        close = 8503.971 if symbol == "sh000905" else 3996.162
        prev = 8652.787 if symbol == "sh000905" else 4036.588
        return _remote([("2026-07-09", prev), ("2026-07-10", close), ("2026-07-13", close + 1)])

    payload = update_benchmark_files(project, fetcher=fetch, fetched_at="x")

    hs = next(item for item in payload["results"] if item["label"] == "hs300")
    assert hs["status"] == "failed"
    assert hs_path.read_bytes() == hs_before
    # The other two still advance: one sick endpoint must not freeze everything.
    others = [item for item in payload["results"] if item["label"] != "hs300"]
    assert all(item["status"] == "appended" for item in others), others
    assert payload["status"] == "degraded"


def test_missing_local_file_is_skipped_not_created(project):
    path = project / "data" / "benchmark" / "hs300.csv"
    path.unlink()

    payload = update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="x",
    )

    hs = next(item for item in payload["results"] if item["label"] == "hs300")
    assert hs["status"] == "skipped"
    assert "local_file_missing" in hs["reason_codes"]
    assert not path.exists(), "a missing benchmark file must not be invented"


# --- idempotence and value correctness ---------------------------------------


def test_second_run_is_up_to_date_and_idempotent(project):
    fetcher = _agreeing_fetcher([("2026-07-13", 4695.383)])
    update_benchmark_files(project, fetcher=fetcher, fetched_at="x")
    snapshot = {
        name: (project / "data" / "benchmark" / name).read_bytes()
        for name in ("hs300.csv", "zz500.csv", "sh000001.csv")
    }

    payload = update_benchmark_files(project, fetcher=fetcher, fetched_at="y")

    assert payload["status"] == "up_to_date"
    assert {item["status"] for item in payload["results"]} == {"up_to_date"}
    for name, before in snapshot.items():
        assert (project / "data" / "benchmark" / name).read_bytes() == before


def test_pct_chg_is_derived_from_the_preceding_close(project):
    # 2026-07-10 close is 8503.971 locally; the appended row closes at 8138.144.
    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 8138.144)]),
        fetched_at="x",
    )

    lines = _lines(project / "data" / "benchmark" / "zz500.csv")
    cells = lines[-1].split(",")
    expected = (8138.144 / 8503.971 - 1.0) * 100.0
    assert cells[0] == "2026-07-13"
    assert float(cells[3]) == pytest.approx(expected)


def test_pct_chg_chains_across_multiple_appended_sessions(project):
    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 8000.0), ("2026-07-14", 8200.0)]),
        fetched_at="x",
    )

    lines = _lines(project / "data" / "benchmark" / "zz500.csv")
    # The second appended row must chain off the first appended close, not off
    # the last *local* close.
    second = lines[-1].split(",")
    assert second[0] == "2026-07-14"
    assert float(second[3]) == pytest.approx((8200.0 / 8000.0 - 1.0) * 100.0)


def test_empty_local_column_stays_empty_rather_than_fabricated(project):
    # hs300.csv's ``amount`` is empty in all 5946 real rows and the Sina frame
    # carries no amount at all. Inventing one would be a data-truth violation.
    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="x",
    )

    lines = _lines(project / "data" / "benchmark" / "hs300.csv")
    header = lines[0].split(",")
    cells = lines[-1].split(",")
    assert len(cells) == len(header)
    assert cells[header.index("amount")] == ""


def test_constant_columns_are_filled_for_the_wide_provenance_schema(project):
    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 8138.144)]),
        fetched_at="2026-07-25T23:59:00+08:00",
    )

    lines = _lines(project / "data" / "benchmark" / "zz500.csv")
    header = lines[0].split(",")
    cells = lines[-1].split(",")
    row = dict(zip(header, cells))
    assert row["benchmark_symbol"] == "sh000905"
    assert row["source"] == "akshare.stock_zh_index_daily"
    assert row["provider"] == "akshare"
    assert row["fetched_at"] == "2026-07-25T23:59:00+08:00"


def test_only_sessions_after_the_local_last_date_are_appended(project):
    # A remote frame containing an *older* gap must not backfill history: this
    # module is append-only and makes no claim about repairing the past.
    hs = _remote(
        [
            ("2026-07-08", 4755.534),
            ("2026-07-09", 4876.313),
            ("2026-07-10", 4780.787),
            ("2026-07-13", 4695.383),
        ]
    )
    fetcher = _fetcher_for(
        {
            "sh000300": hs,
            "sh000905": _remote([("2026-07-09", 8652.787), ("2026-07-10", 8503.971)]),
            "sh000001": _remote([("2026-07-09", 4036.588), ("2026-07-10", 3996.162)]),
        }
    )

    payload = update_benchmark_files(project, fetcher=fetcher, fetched_at="x")

    hs_result = next(item for item in payload["results"] if item["label"] == "hs300")
    assert hs_result["appended_dates"] == ["2026-07-13"]
    lines = _lines(project / "data" / "benchmark" / "hs300.csv")
    assert len(lines) == 4
    assert "2026-07-08" not in _read_text(project / "data" / "benchmark" / "hs300.csv").split("2026-07-10")[1]


def test_specs_cover_the_three_real_benchmark_files():
    assert {spec.filename for spec in BENCHMARK_SPECS} == {
        "hs300.csv",
        "zz500.csv",
        "sh000001.csv",
    }
    assert {spec.remote_symbol for spec in BENCHMARK_SPECS} == {
        "sh000300",
        "sh000905",
        "sh000001",
    }


def test_appended_frame_stays_loadable_by_the_backtest_loader(project):
    from quantdesk.backtest_loaders import load_benchmark_series

    update_benchmark_files(
        project,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="x",
    )

    for name in ("hs300.csv", "zz500.csv", "sh000001.csv"):
        series = load_benchmark_series(project / "data" / "benchmark" / name)
        assert len(series) == 3, name
        assert str(series.index[-1].date()) == "2026-07-13", name
        assert series.index.is_monotonic_increasing, name


def test_custom_spec_subset_leaves_other_files_untouched(project):
    zz_before = (project / "data" / "benchmark" / "zz500.csv").read_bytes()
    only_hs = (BenchmarkSpec(label="hs300", filename="hs300.csv", remote_symbol="sh000300"),)

    payload = update_benchmark_files(
        project,
        specs=only_hs,
        fetcher=_agreeing_fetcher([("2026-07-13", 4695.383)]),
        fetched_at="x",
    )

    assert len(payload["results"]) == 1
    assert (project / "data" / "benchmark" / "zz500.csv").read_bytes() == zz_before


# --- chain wiring -------------------------------------------------------------
#
# Appending the series is only half the fix: without a caller the gap reopens on
# the next session.  These tests pin the daily chain's benchmark node, including
# the two conditions under which it must NOT reach the network.


def _stub_chain(monkeypatch, chain):
    """Neutralize the chain's heavy nodes so only the benchmark node is exercised.

    A formal (non-dry-run) network run is rejected outright unless the requested
    symbols equal the whole current analysis pool -- see
    ``formal_scope_not_full_current_analysis_pool``.  That guard is correct and
    must not be weakened, so the canonical pool is narrowed to the same single
    symbol the tests request instead.  ``resolve_symbols`` reads the real
    ``data/universe`` pool regardless of ``project_root``, which is why it has to
    be stubbed for a tmp-root run at all.
    """

    monkeypatch.setattr(
        chain,
        "resolve_symbols",
        lambda **k: [{"symbol": "600000", "name": "600000", "included": "True"}],
    )
    monkeypatch.setattr(
        chain,
        "build_runtime_preflight",
        lambda *a, **k: {
            "status": "passed",
            "ready": True,
            "selected_executable": "project/.venv/Scripts/python.exe",
            "runtime_source": "project_venv",
            "modules": {"pandas": True, "mootdx": True, "akshare": True},
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "update_daily_prices",
        lambda **k: {
            "status": "dry_run",
            "target_date": k["target_date"],
            "symbols_checked": len(k["symbols"]),
            "after": {"fresh_count": 1, "stale_count": 0, "missing_count": 0},
        },
    )
    monkeypatch.setattr(
        chain,
        "collect_intraday_required_symbols",
        lambda **k: {"items": [{"symbol": "600000"}]},
    )
    monkeypatch.setattr(
        chain,
        "update_intraday_1m_for_symbols",
        lambda *a, **k: {
            "status": "dry_run",
            "updated_symbols": [],
            "unchanged_symbols": [],
            "failed_symbols": [],
        },
    )
    monkeypatch.setattr(
        chain,
        "audit_intraday_batch",
        lambda symbols, target_date, **k: {
            "summary": {
                "complete_session": len(symbols),
                "formal_execution_eligible": len(symbols),
                "missing": 0,
                "partial_session": 0,
                "invalid": 0,
            },
            "items": [],
        },
    )


def test_chain_exposes_a_benchmark_update_node(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain

    _stub_chain(monkeypatch, chain)
    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-24",
        symbols=["600000"],
        network=True,
        dry_run=True,
        run_id="bench-node",
        rebuild_features=False,
    )

    assert "benchmark_update" in payload["components"]


def test_chain_benchmark_node_skips_on_dry_run_and_names_the_reason(tmp_path, monkeypatch):
    from quantdesk import daily_data_chain as chain

    _stub_chain(monkeypatch, chain)
    calls: list[object] = []
    monkeypatch.setattr(
        chain,
        "update_benchmark_files",
        lambda *a, **k: calls.append(a) or {"status": "updated"},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-24",
        symbols=["600000"],
        network=True,
        dry_run=True,
        run_id="bench-dry",
        rebuild_features=False,
    )

    node = payload["components"]["benchmark_update"]
    assert node["status"] == "skipped"
    assert calls == []


def test_chain_benchmark_node_refuses_a_non_canonical_root(tmp_path, monkeypatch):
    """A staging clone must never fetch and append into its own benchmark dir."""

    from quantdesk import daily_data_chain as chain

    _stub_chain(monkeypatch, chain)
    calls: list[object] = []
    monkeypatch.setattr(
        chain,
        "update_benchmark_files",
        lambda *a, **k: calls.append(a) or {"status": "updated"},
    )

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-24",
        symbols=["600000"],
        network=True,
        dry_run=False,
        run_id="bench-staging",
        rebuild_features=False,
    )

    node = payload["components"]["benchmark_update"]
    assert node["status"] == "skipped"
    assert "not_canonical_project_root" in node["reason_codes"]
    assert calls == []


def test_chain_benchmark_node_reports_a_fetch_failure_without_killing_the_run(
    tmp_path, monkeypatch
):
    from quantdesk import daily_data_chain as chain

    _stub_chain(monkeypatch, chain)
    monkeypatch.setattr(chain, "PROJECT_ROOT", tmp_path.resolve())

    def boom(*a, **k):
        raise RuntimeError("index_fetch_failed:sh000300:SSLError:boom")

    monkeypatch.setattr(chain, "update_benchmark_files", boom)

    payload = chain.run_daily_data_chain(
        project_root=tmp_path,
        target_date="2026-07-24",
        symbols=["600000"],
        network=True,
        dry_run=False,
        run_id="bench-fail",
        rebuild_features=False,
    )

    node = payload["components"]["benchmark_update"]
    assert node["status"] == "failed"
    assert any("index_fetch_failed" in code for code in node["reason_codes"])
    # The chain itself must still terminate normally: a stale reference series is
    # a degraded input, not a reason to abandon the daily run.
    assert payload["status"] != "failed"
