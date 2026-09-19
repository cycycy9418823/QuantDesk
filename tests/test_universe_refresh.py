from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from quantdesk.universe_refresh import refresh_universe_snapshot, restore_formal_learning_universe


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_baseline(root: Path, symbols: list[str]) -> Path:
    path = root / "baseline.csv"
    pd.DataFrame({"symbol": symbols}).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _outputs(root: Path) -> dict[str, Path]:
    return {
        "spot_snapshot": root / "cache" / "spot.csv",
        "spot_meta": root / "cache" / "spot.json",
        "low_price_universe": root / "universe" / "low.csv",
    }


def _complete_fetcher(symbols: list[str], quote_date: str = "2026-07-17"):
    def fetch(*, target_date: str):
        frame = pd.DataFrame(
            {
                "symbol": symbols,
                "name": [f"Name{i}" for i in range(len(symbols))],
                "price": [10.0 + i for i in range(len(symbols))],
                "amount": [1000000.0] * len(symbols),
                "quote_date": [quote_date] * len(symbols),
            }
        )
        frame.attrs.update(
            {
                "quote_date": quote_date,
                "provider_status": "complete",
                "expected_pages": 1,
                "observed_pages": 1,
            }
        )
        return frame

    return fetch


def test_complete_snapshot_writes_all_outputs_atomically(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001", "600000", "920001"])
    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(["000001", "600000", "920001"])},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert payload["status"] == "completed"
    assert payload["stale"] is False
    assert payload["preserved_previous"] is False
    assert payload["provider"] == "eastmoney"
    assert payload["coverage_ratio"] == 1.0
    assert payload["observed_count"] == 3
    assert all(path.exists() for path in outputs.values())
    assert payload["new_sha256"][str(outputs["spot_snapshot"])] == _sha(outputs["spot_snapshot"])
    assert json.loads(outputs["spot_meta"].read_text(encoding="utf-8"))["quote_date"] == "2026-07-17"
    low = pd.read_csv(outputs["low_price_universe"], encoding="utf-8-sig", dtype={"symbol": str})
    assert set(low["symbol"]) == {"000001", "600000", "920001"}


def test_partial_or_low_coverage_preserves_previous_bytes(tmp_path: Path):
    outputs = _outputs(tmp_path)
    for path in outputs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((f"old:{path.name}\n").encode())
    before = {str(path): _sha(path) for path in outputs.values()}
    baseline = _write_baseline(tmp_path, ["000001", "600000", "920001", "000002"])

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(["000001", "600000"])},
        output_paths=outputs,
        baseline_path=baseline,
        min_coverage=0.9,
    )

    assert payload["status"] == "blocked"
    assert payload["stale"] is True
    assert payload["preserved_previous"] is True
    assert "coverage_below_minimum" in payload["reason_codes"]
    assert {str(path): _sha(path) for path in outputs.values()} == before
    assert payload["old_sha256"] == payload["new_sha256"]


def test_network_disabled_never_calls_provider_or_writes(tmp_path: Path):
    outputs = _outputs(tmp_path)
    for path in outputs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"protected")
    before = {str(path): _sha(path) for path in outputs.values()}
    called = False

    def fetch(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network fetch must not run")

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=False,
        fetchers={"eastmoney": fetch},
        output_paths=outputs,
    )

    assert payload["status"] == "blocked"
    assert "network_disabled" in payload["reason_codes"]
    assert called is False
    assert {str(path): _sha(path) for path in outputs.values()} == before


def test_malformed_date_duplicate_and_provider_partial_fail_closed(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001", "600000"])

    def malformed(*, target_date: str):
        frame = pd.DataFrame(
            {
                "symbol": ["sh000001", "000001"],
                "name": ["A", "A2"],
                "price": [1.0, 1.1],
                "quote_date": ["2026-07-16", "not-a-date"],
            }
        )
        frame.attrs.update({"provider_status": "partial", "quote_date": "2026-07-16"})
        return frame

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": malformed},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert payload["status"] == "blocked"
    assert payload["stale"] is True
    assert payload["preserved_previous"] is True
    assert any(code in payload["reason_codes"] for code in {"provider_partial", "quote_date_mismatch", "duplicate_symbol"})


def test_partial_eastmoney_falls_back_to_complete_sina(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001", "600000"])
    calls: list[str] = []

    def partial(*, target_date: str):
        calls.append("eastmoney")
        frame = _complete_fetcher(["000001", "600000"])(target_date=target_date)
        frame.attrs["provider_status"] = "partial"
        frame.attrs["provider_warning"] = "page 2 failed"
        return frame

    def complete(*, target_date: str):
        calls.append("sina")
        return _complete_fetcher(["000001", "600000"])(target_date=target_date)

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"sina": complete, "eastmoney": partial},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert payload["status"] == "completed"
    assert payload["provider"] == "sina"
    assert calls == ["eastmoney", "sina"]
    assert payload["provider_attempts"][0]["reason_codes"] == ["provider_partial"]


def test_no_default_provider_does_not_fabricate_outputs(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001"])
    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers=None,
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert payload["status"] == "blocked"
    assert payload["provider_attempts"][0]["provider"] == "eastmoney"
    assert payload["provider_attempts"][1]["provider"] == "sina"
    assert payload["reason_codes"] == ["provider_unavailable"]
    assert not any(path.exists() for path in outputs.values())


def test_atomic_publish_failure_rolls_back_all_previous_bytes(tmp_path: Path, monkeypatch):
    import quantdesk.universe_refresh as universe_refresh

    outputs = _outputs(tmp_path)
    for path in outputs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((f"old:{path.name}").encode())
    before = {str(path): _sha(path) for path in outputs.values()}
    baseline = _write_baseline(tmp_path, ["000001", "600000"])
    original_replace = universe_refresh.os.replace

    def fail_meta_publish(source, destination):
        if Path(destination) == outputs["spot_meta"]:
            raise OSError("simulated metadata publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(universe_refresh.os, "replace", fail_meta_publish)
    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(["000001", "600000"])},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["atomic_publish_failed"]
    assert payload["preserved_previous"] is True
    assert {str(path): _sha(path) for path in outputs.values()} == before


def test_default_baseline_is_canonical_a_share_symbols(tmp_path: Path):
    outputs = _outputs(tmp_path)
    canonical = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": ["000001", "600000"]}).to_csv(canonical, index=False)
    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(["000001", "600000"])},
        output_paths=outputs,
    )
    assert payload["status"] == "completed"
    assert payload["baseline_path"].endswith("data\\universe\\a_share_symbols.csv")
    assert payload["baseline_count"] == 2


def test_missing_provider_status_fails_closed(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001"])

    def missing_status(*, target_date: str):
        return pd.DataFrame(
            {
                "symbol": ["000001"],
                "price": [10.0],
                "quote_date": [target_date],
            }
        )

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": missing_status},
        output_paths=outputs,
        baseline_path=baseline,
    )
    assert payload["status"] == "blocked"
    assert "provider_status_not_complete" in payload["reason_codes"]
    assert not any(path.exists() for path in outputs.values())


def test_default_eastmoney_fetcher_emits_complete_page_and_date_evidence():
    from datetime import datetime

    from quantdesk.universe_refresh import build_default_fetchers

    stamp = datetime(2026, 7, 17, 10, 0, 0).timestamp()
    pages = {
        1: {
            "data": {
                "total": 2,
                "diff": [{"f12": "000001", "f14": "A", "f2": 10, "f124": stamp}],
            }
        },
        2: {
            "data": {
                "total": 2,
                "diff": [{"f12": "600000", "f14": "B", "f2": 11, "f124": stamp}],
            }
        },
    }

    def request_get(url, params=None, **kwargs):
        return pages[int(params["pn"])]

    fetch = build_default_fetchers(
        request_get=request_get,
        page_size=1,
        max_pages=5,
        retries=0,
        sleep=lambda _: None,
    )["eastmoney"]
    frame = fetch(target_date="2026-07-17")
    assert len(frame) == 2
    assert frame.attrs["provider_status"] == "complete"
    assert frame.attrs["quote_date"] == "2026-07-17"
    assert frame.attrs["expected_pages"] == 2
    assert frame.attrs["observed_pages"] == 2
    assert frame.attrs["expected_rows"] == 2


def test_default_sina_fetcher_emits_complete_evidence_with_injected_clock():
    from quantdesk.universe_refresh import build_default_fetchers

    pages = {
        1: [{"code": "000001", "name": "A", "trade": "10", "ticktime": "15:00:00"}],
        2: [],
    }

    def request_get(url, params=None, **kwargs):
        return pages[int(params["page"])]

    fetch = build_default_fetchers(
        request_get=request_get,
        clock=lambda: "2026-07-17",
        page_size=1,
        max_pages=5,
        retries=0,
        sleep=lambda _: None,
    )["sina"]
    frame = fetch(target_date="2026-07-17")
    assert len(frame) == 1
    assert frame.attrs["provider_status"] == "complete"
    assert frame.attrs["quote_date"] == "2026-07-17"
    assert frame.attrs["expected_pages"] == 1
    assert frame.attrs["observed_pages"] == 1


def test_default_eastmoney_fetcher_marks_midstream_failure_partial():
    from datetime import datetime

    from quantdesk.universe_refresh import build_default_fetchers

    stamp = datetime(2026, 7, 17, 10, 0, 0).timestamp()

    def request_get(url, params=None, **kwargs):
        if int(params["pn"]) == 1:
            return {
                "data": {
                    "total": 2,
                    "diff": [{"f12": "000001", "f14": "A", "f2": 10, "f124": stamp}],
                }
            }
        raise OSError("network down")

    fetch = build_default_fetchers(
        request_get=request_get,
        page_size=1,
        max_pages=5,
        retries=0,
        sleep=lambda _: None,
    )["eastmoney"]
    frame = fetch(target_date="2026-07-17")
    assert len(frame) == 1
    assert frame.attrs["provider_status"] == "partial"
    assert frame.attrs["expected_pages"] == 2
    assert frame.attrs["observed_pages"] == 1


def test_default_sina_fetcher_marks_max_page_cap_partial():
    from quantdesk.universe_refresh import build_default_fetchers

    def request_get(url, params=None, **kwargs):
        return [{"code": "000001", "trade": "10", "ticktime": "15:00:00"}]

    fetch = build_default_fetchers(
        request_get=request_get,
        clock=lambda: "2026-07-17",
        page_size=1,
        max_pages=1,
        retries=0,
        sleep=lambda _: None,
    )["sina"]
    frame = fetch(target_date="2026-07-17")
    assert frame.attrs["provider_status"] == "partial"
    assert frame.attrs["observed_pages"] == 1
    assert frame.attrs["expected_pages"] == 2


def test_refresh_accepts_injected_default_fetcher_adapter_end_to_end(tmp_path: Path):
    from datetime import datetime

    from quantdesk.universe_refresh import build_default_fetchers

    outputs = _outputs(tmp_path)
    baseline_file = tmp_path / "data" / "universe" / "a_share_symbols.csv"
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": ["000001"]}).to_csv(baseline_file, index=False)
    stamp = datetime(2026, 7, 17, 10, 0, 0).timestamp()

    def request_get(url, params=None, **kwargs):
        return {
            "data": {
                "total": 1,
                "diff": [{"f12": "000001", "f14": "A", "f2": 10, "f124": stamp}],
            }
        }

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers=build_default_fetchers(
            request_get=request_get,
            page_size=100,
            retries=0,
            sleep=lambda _: None,
        ),
        output_paths=outputs,
    )
    assert payload["status"] == "completed"
    assert payload["provider"] == "eastmoney"
    assert payload["coverage_ratio"] == 1.0


def test_default_eastmoney_fetcher_keeps_bse_920xxx_segment():
    from datetime import datetime

    from quantdesk.universe_refresh import build_default_fetchers

    stamp = datetime(2026, 7, 17, 10, 0, 0).timestamp()
    seen: list[dict] = []

    def request_get(url, params=None, **kwargs):
        seen.append(dict(params or {}))
        return {
            "data": {
                "total": 1,
                "diff": [{"f12": "920266", "f14": "BSE", "f2": 10, "f124": stamp}],
            }
        }

    frame = build_default_fetchers(
        request_get=request_get,
        retries=0,
        sleep=lambda _: None,
    )["eastmoney"](target_date="2026-07-17")
    assert frame["symbol"].tolist() == ["920266"]
    assert "m:0+t:81" in seen[0]["fs"]


def test_interprocess_refresh_busy_preserves_previous_bytes(
    tmp_path: Path, monkeypatch
):
    from contextlib import contextmanager

    import quantdesk.universe_refresh as universe_refresh

    outputs = _outputs(tmp_path)
    for path in outputs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((f"old:{path.name}").encode())
    before = {str(path): _sha(path) for path in outputs.values()}
    baseline = _write_baseline(tmp_path, ["000001"])

    @contextmanager
    def busy_lock(*args, **kwargs):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(universe_refresh, "interprocess_file_lock", busy_lock)
    called = False

    def fetch(**kwargs):
        nonlocal called
        called = True
        return _complete_fetcher(["000001"])(target_date="2026-07-17")

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": fetch},
        output_paths=outputs,
        baseline_path=baseline,
    )
    assert payload["status"] == "blocked"
    assert payload["reason_codes"] == ["universe_refresh_busy"]
    assert payload["preserved_previous"] is True
    assert called is False
    assert {str(path): _sha(path) for path in outputs.values()} == before


def test_missing_bse_segment_blocks_even_when_global_coverage_passes(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline_symbols = [
        "600000", "600001", "600002", "600003", "600004",
        "000001", "000002", "000003", "300001", "300002",
        "920266",
    ]
    baseline = _write_baseline(tmp_path, baseline_symbols)
    observed = baseline_symbols[:-1]
    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(observed)},
        output_paths=outputs,
        baseline_path=baseline,
        min_coverage=0.90,
    )
    assert payload["coverage_ratio"] >= 0.90
    assert payload["status"] == "blocked"
    assert payload["market_coverage"]["bse"]["baseline_count"] == 1
    assert payload["market_coverage"]["bse"]["observed_count"] == 0
    assert "market_segment_missing:bse" in payload["reason_codes"]


def test_low_valid_price_ratio_blocks_snapshot_and_preserves_outputs(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline_symbols = [f"6000{index:02d}" for index in range(10)]
    baseline = _write_baseline(tmp_path, baseline_symbols)

    def low_price_quality(*, target_date: str):
        frame = _complete_fetcher(baseline_symbols)(target_date=target_date)
        frame.loc[0:1, "price"] = None
        return frame

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": low_price_quality},
        output_paths=outputs,
        baseline_path=baseline,
        min_valid_price_ratio=0.90,
    )
    assert payload["status"] == "blocked"
    assert payload["valid_price_count"] == 8
    assert payload["valid_price_ratio"] == 0.8
    assert "valid_price_ratio_below_minimum" in payload["reason_codes"]
    assert not any(path.exists() for path in outputs.values())


def test_zero_low_price_pool_is_rejected_before_publish(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["600000", "600001"])

    def all_above_buffer(*, target_date: str):
        frame = _complete_fetcher(["600000", "600001"])(target_date=target_date)
        frame["price"] = [30.0, 31.0]
        return frame

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": all_above_buffer},
        output_paths=outputs,
        baseline_path=baseline,
    )
    assert payload["status"] == "blocked"
    assert "low_price_pool_empty" in payload["reason_codes"]
    assert payload["low_price_pool"]["candidate_active_count"] == 0
    assert not any(path.exists() for path in outputs.values())


def test_low_price_pool_sudden_shrink_preserves_previous_universe(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline_symbols = [f"6000{index:02d}" for index in range(10)]
    baseline = _write_baseline(tmp_path, baseline_symbols)
    outputs["low_price_universe"].parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "symbol": baseline_symbols,
            "included": [True] * 10,
            "in_buffer": [False] * 10,
        }
    ).to_csv(outputs["low_price_universe"], index=False)
    previous = outputs["low_price_universe"].read_bytes()

    def shrunk(*, target_date: str):
        frame = _complete_fetcher(baseline_symbols)(target_date=target_date)
        frame["price"] = [10.0] + [30.0] * 9
        return frame

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": shrunk},
        output_paths=outputs,
        baseline_path=baseline,
    )
    assert payload["status"] == "blocked"
    assert "low_price_pool_shrink_below_minimum" in payload["reason_codes"]
    assert payload["low_price_pool"]["previous_active_count"] == 10
    assert payload["low_price_pool"]["candidate_active_count"] == 1
    assert outputs["low_price_universe"].read_bytes() == previous

def test_default_refresh_writes_discovery_without_mutating_formal_pool(tmp_path: Path):
    universe_dir = tmp_path / "data" / "universe"
    universe_dir.mkdir(parents=True)
    baseline = universe_dir / "a_share_symbols.csv"
    pd.DataFrame({"symbol": ["000001", "600000", "920001"]}).to_csv(
        baseline, index=False, encoding="utf-8-sig"
    )
    formal = universe_dir / "low_price_learning_symbols.csv"
    formal_bytes = b"symbol,included,in_buffer\n000001,True,False\n"
    formal.write_bytes(formal_bytes)

    payload = refresh_universe_snapshot(
        tmp_path,
        "2026-07-17",
        network=True,
        fetchers={"eastmoney": _complete_fetcher(["000001", "600000", "920001"])},
    )

    discovery = universe_dir / "low_price_discovery_symbols.csv"
    assert payload["status"] == "completed"
    assert discovery.exists()
    assert formal.read_bytes() == formal_bytes
    assert payload["output_paths"]["low_price_universe"] == str(discovery)


def test_restore_formal_pool_from_explicit_membership_keeps_discovery(tmp_path: Path):
    universe_dir = tmp_path / "data" / "universe"
    cache_dir = tmp_path / "data" / "cache"
    universe_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    spot = cache_dir / "spot_snapshot_latest.csv"
    pd.DataFrame(
        {
            "symbol": ["000001", "600000", "920001"],
            "name": ["A", "B", "C"],
            "price": [10.0, 20.0, 8.0],
            "amount": [1.0, 2.0, 3.0],
            "source": ["test"] * 3,
        }
    ).to_csv(spot, index=False, encoding="utf-8-sig")
    discovery = universe_dir / "low_price_discovery_symbols.csv"
    discovery.write_bytes(b"discovery-evidence")

    payload = restore_formal_learning_universe(
        tmp_path,
        ["000001", "920001"],
        source="test_explicit_contract",
    )

    formal = universe_dir / "low_price_learning_symbols.csv"
    restored = pd.read_csv(formal, encoding="utf-8-sig", dtype={"symbol": str})
    assert payload["status"] == "completed"
    assert payload["symbol_count"] == 2
    assert set(restored["symbol"]) == {"000001", "920001"}
    assert restored["included"].all()
    assert not restored["in_buffer"].any()
    assert discovery.read_bytes() == b"discovery-evidence"

def test_default_sina_fetcher_uses_expected_settlement_day_before_open():
    from datetime import datetime, timezone

    from quantdesk.universe_refresh import build_default_fetchers

    pages = {
        1: [{"code": "600405", "name": "A", "trade": "5", "ticktime": "15:00:00"}],
        2: [],
    }

    def request_get(url, params=None, **kwargs):
        return pages[int(params["page"])]

    fetch = build_default_fetchers(
        request_get=request_get,
        clock=lambda: datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
        page_size=1,
        max_pages=5,
        retries=0,
        sleep=lambda _: None,
    )["sina"]
    frame = fetch(target_date="2026-07-16")
    assert frame.attrs["provider_status"] == "complete"
    assert frame.attrs["quote_date"] == "2026-07-16"


def test_live_refresh_failure_reuses_verified_same_target_cache(tmp_path: Path):
    outputs = _outputs(tmp_path)
    baseline = _write_baseline(tmp_path, ["000001", "600000", "920001"])
    first = refresh_universe_snapshot(
        tmp_path,
        "2026-07-16",
        network=True,
        fetchers={"sina": _complete_fetcher(["000001", "600000", "920001"], "2026-07-16")},
        output_paths=outputs,
        baseline_path=baseline,
    )
    before = {str(path): _sha(path) for path in outputs.values()}
    assert first["status"] == "completed"

    second = refresh_universe_snapshot(
        tmp_path,
        "2026-07-16",
        network=True,
        fetchers={"sina": _complete_fetcher(["000001", "600000", "920001"], "2026-07-17")},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert second["status"] == "completed"
    assert second["formal_eligible"] is True
    assert second["provider"] == "local_verified_cache"
    assert second["quote_date"] == "2026-07-16"
    assert second["preserved_previous"] is True
    assert {str(path): _sha(path) for path in outputs.values()} == before
    assert "live_provider_refresh_failed_used_verified_cache" in second["warnings"]


def test_verified_cache_rejects_invalid_baseline(tmp_path: Path):
    outputs = _outputs(tmp_path)
    valid_symbols = ["000001", "600000", "920001"]
    baseline = _write_baseline(tmp_path, valid_symbols)
    first = refresh_universe_snapshot(
        tmp_path,
        "2026-07-16",
        network=True,
        fetchers={"sina": _complete_fetcher(valid_symbols, "2026-07-16")},
        output_paths=outputs,
        baseline_path=baseline,
    )
    assert first["status"] == "completed"
    before = {str(path): _sha(path) for path in outputs.values()}

    pd.DataFrame({"symbol": [*valid_symbols, "INVALID"]}).to_csv(
        baseline,
        index=False,
        encoding="utf-8-sig",
    )
    second = refresh_universe_snapshot(
        tmp_path,
        "2026-07-16",
        network=True,
        fetchers={"sina": _complete_fetcher(valid_symbols, "2026-07-17")},
        output_paths=outputs,
        baseline_path=baseline,
    )

    assert second["status"] == "blocked"
    assert second["formal_eligible"] is False
    assert "baseline_invalid_symbol" in second["reason_codes"]
    assert {str(path): _sha(path) for path in outputs.values()} == before


def test_default_sina_fetcher_marks_intraday_snapshot_partial():
    from datetime import datetime, timezone

    from quantdesk.universe_refresh import build_default_fetchers

    pages = {
        1: [{"code": "600405", "name": "A", "trade": "5", "ticktime": "11:00:00"}],
        2: [],
    }

    def request_get(url, params=None, **kwargs):
        return pages[int(params["page"])]

    fetch = build_default_fetchers(
        request_get=request_get,
        clock=lambda: datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc),
        page_size=1,
        max_pages=5,
        retries=0,
        sleep=lambda _: None,
    )["sina"]
    frame = fetch(target_date="2026-07-16")

    assert frame.attrs["provider_status"] == "partial"
    assert frame.attrs["formal_eligible"] is False
    assert "intraday" in frame.attrs["provider_warning"]
