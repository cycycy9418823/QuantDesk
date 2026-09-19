from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from quantdesk import daily_price_providers as providers
from quantdesk.daily_price_update_engine import DEFAULT_PROVIDERS
from quantdesk.daily_session_evidence import validate_baostock_session_evidence

@pytest.fixture(autouse=True)
def isolated_baostock_session_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_BAOSTOCK_SESSION_LOCK_PATH", tmp_path / "baostock_session.lock")


class _Reply:
    error_code = "0"
    error_msg = ""


class _Query:
    fields = [
        "date", "code", "open", "high", "low", "close",
        "volume", "amount", "tradestatus", "isST",
    ]
    error_code = "0"
    error_msg = ""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class _FakeBaostock:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.logged_out = False

    def login(self) -> _Reply:
        return _Reply()

    def logout(self) -> _Reply:
        self.logged_out = True
        return _Reply()

    def query_history_k_data_plus(self, code: str, fields: str, **kwargs) -> _Query:
        self.calls.append((code, dict(kwargs)))
        return _Query(
            [
                ["2026-07-03", code, "4.62", "4.71", "4.54", "4.61", "17199508", "79693321.99", "1", "0"],
                ["2026-07-06", code, "4.61", "4.61", "4.61", "4.61", "", "", "0", "0"],
                ["2026-07-16", code, "4.20", "4.35", "4.18", "4.19", "15456306", "65812939.09", "1", "0"],
            ]
        )


def _daily_frame(symbol: str, dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": [symbol] * len(dates),
            "open": [4.0] * len(dates),
            "high": [4.2] * len(dates),
            "low": [3.9] * len(dates),
            "close": [4.1] * len(dates),
            "volume": [1000.0] * len(dates),
            "amount": [4100.0] * len(dates),
            "adjust": ["raw"] * len(dates),
            "source": ["test"] * len(dates),
            "updated_at": ["2026-07-17"] * len(dates),
        }
    )


def _session_evidence(
    symbol: str,
    start_date: str,
    end_date: str,
    statuses: dict[str, str],
) -> dict[str, object]:
    records = [
        {"trade_date": trade_date, "tradestatus": status}
        for trade_date, status in sorted(statuses.items())
    ]
    records_sha256 = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "baostock_session_evidence.v1",
        "symbol": symbol,
        "provider_code": f"sh.{symbol}",
        "source": "baostock.query_history_k_data_plus",
        "adjustflag": "3",
        "query_start_date": start_date,
        "query_end_date": end_date,
        "provider_error_code": "0",
        "captured_at": "2026-07-17T12:00:00+08:00",
        "records": records,
        "records_sha256": records_sha256,
    }


def test_baostock_daily_returns_trading_bars_and_explicit_suspension_evidence(monkeypatch) -> None:
    fake = _FakeBaostock()
    assert providers.provider_availability("baostock_daily")["implemented"] is True
    assert DEFAULT_PROVIDERS[2] == "baostock_daily"
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(providers, "dependency_available", lambda name: name == "baostock")

    frame, warnings = providers.fetch_daily(
        "baostock_daily",
        "600405",
        "2026-07-01",
        "2026-07-16",
        network=True,
    )

    assert frame["date"].tolist() == ["2026-07-03", "2026-07-16"]
    assert set(frame["adjust"]) == {"raw"}
    assert set(frame["source"]) == {"baostock_daily"}
    evidence = validate_baostock_session_evidence(
        frame.attrs["suspension_evidence"],
        expected_symbol="600405",
        expected_start_date="2026-07-01",
        expected_end_date="2026-07-16",
    )
    assert evidence["valid"] is True
    assert evidence["suspended_dates"] == ["2026-07-06"]
    assert evidence["traded_dates"] == ["2026-07-03", "2026-07-16"]
    assert any("explicit_suspension_dates" in item for item in warnings)
    assert fake.calls[0][0] == "sh.600405"
    assert fake.calls[0][1]["adjustflag"] == "3"
    assert fake.logged_out is True


def test_baostock_daily_rejects_rows_for_a_different_symbol(monkeypatch) -> None:
    class _WrongCodeBaostock(_FakeBaostock):
        def query_history_k_data_plus(
            self,
            code: str,
            fields: str,
            **kwargs,
        ) -> _Query:
            self.calls.append((code, dict(kwargs)))
            return _Query(
                [
                    [
                        "2026-07-16",
                        "sh.600406",
                        "4.20",
                        "4.35",
                        "4.18",
                        "4.19",
                        "15456306",
                        "65812939.09",
                        "1",
                        "0",
                    ]
                ]
            )

    fake = _WrongCodeBaostock()
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(
        providers,
        "dependency_available",
        lambda name: name == "baostock",
    )

    with pytest.raises(RuntimeError, match="baostock_symbol_mismatch"):
        providers.fetch_daily(
            "baostock_daily",
            "600405",
            "2026-07-01",
            "2026-07-16",
            network=True,
        )


def test_baostock_daily_serializes_the_global_sdk_session(monkeypatch) -> None:
    class _ConcurrentBaostock(_FakeBaostock):
        def __init__(self) -> None:
            super().__init__()
            self.guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def login(self) -> _Reply:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            return _Reply()

        def logout(self) -> _Reply:
            with self.guard:
                self.active -= 1
            return _Reply()

    fake = _ConcurrentBaostock()
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(
        providers,
        "dependency_available",
        lambda name: name == "baostock",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda symbol: providers.fetch_daily(
                    "baostock_daily",
                    symbol,
                    "2026-07-01",
                    "2026-07-16",
                    network=True,
                ),
                ["600405", "600405"],
            )
        )

    assert len(results) == 2
    assert fake.max_active == 1


def test_daily_gap_gate_exempts_only_provider_proven_suspensions(tmp_path: Path, monkeypatch) -> None:
    import quantdesk.daily_price_update_engine as engine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    _daily_frame("600405", ["2026-07-01", "2026-07-02", "2026-07-03"]).to_csv(
        daily_dir / "600405.csv",
        index=False,
        encoding="utf-8-sig",
    )
    fresh = _daily_frame(
        "600405",
        ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"],
    )
    fresh.attrs["suspension_evidence"] = _session_evidence(
        "600405",
        "2026-06-26",
        "2026-07-16",
        {
            "2026-07-03": "1",
            "2026-07-06": "0",
            "2026-07-07": "0",
            "2026-07-08": "0",
            "2026-07-09": "0",
            "2026-07-10": "0",
            "2026-07-13": "1",
            "2026-07-14": "1",
            "2026-07-15": "1",
            "2026-07-16": "1",
        },
    )
    calendar = {
        "is_open_by_date": {
            f"2026-07-{day:02d}": True
            for day in (1, 2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16)
        }
    }

    monkeypatch.setattr(engine, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(engine, "provider_availability", lambda name: {"status": "available", "failed_reason": ""})
    monkeypatch.setattr(engine, "fetch_daily", lambda *args, **kwargs: (fresh.copy(), ["baostock_explicit_suspension_dates:5"]))
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: calendar)
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})

    payload = engine.update_daily_prices(
        target_date="2026-07-16",
        providers=["baostock_daily"],
        network=True,
        symbols="600405",
        output_path=tmp_path / "daily_update.json",
    )

    written = pd.read_csv(daily_dir / "600405.csv", encoding="utf-8-sig")
    assert payload["status"] == "completed"
    assert payload["failed_symbols"] == 0
    assert payload["stale_count_after"] == 0
    assert payload["data_freshness_status"] == "fresh"
    assert payload["missing_recent_trade_dates_by_symbol"] == {}
    sidecar = (
        tmp_path
        / "metadata"
        / "provider_session_evidence"
        / "baostock"
        / "600405.json"
    )
    assert sidecar.exists()
    offline_status = engine.local_daily_status_for_symbols(
        [{"symbol": "600405", "name": "600405"}],
        target_date="2026-07-16",
    )
    assert offline_status["stale_count"] == 0
    assert offline_status["missing_recent_trade_dates_by_symbol"] == {}
    assert written["date"].astype(str).tolist() == [
        "2026-07-01", "2026-07-02", "2026-07-03",
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16",
    ]
    provider_result = payload["provider_results"][0]
    assert (
        "interval_gap_exempted_by_explicit_suspension:600405:2026-07-09,2026-07-10"
        in provider_result["warnings"]
    )


def test_effective_coverage_stops_before_first_unexplained_gap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import quantdesk.daily_price_update_engine as engine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    dates = [
        "2026-07-08",
        "2026-07-10",
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
        "2026-07-16",
    ]
    _daily_frame("600405", dates).to_csv(
        daily_dir / "600405.csv",
        index=False,
        encoding="utf-8-sig",
    )
    calendar = {
        "is_open_by_date": {
            trade_date: True
            for trade_date in [
                "2026-07-08",
                "2026-07-09",
                "2026-07-10",
                "2026-07-13",
                "2026-07-14",
                "2026-07-15",
                "2026-07-16",
            ]
        }
    }
    monkeypatch.setattr(engine, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: calendar)

    status = engine.local_daily_status_for_symbols(
        [{"symbol": "600405", "name": "600405"}],
        target_date="2026-07-16",
    )

    assert status["stale_count"] == 1
    assert status["missing_recent_trade_dates_by_symbol"] == {
        "600405": ["2026-07-09"]
    }
    assert status["max_local_date"] == "2026-07-16"
    assert status["coverage_date"] == "2026-07-08"
    assert status["effective_latest_by_symbol"]["600405"] == "2026-07-08"


def test_target_day_suspension_is_fresh_without_fabricating_a_daily_bar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import quantdesk.daily_price_update_engine as engine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    dates = [
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
    ]
    existing = _daily_frame("600405", dates)
    existing.to_csv(
        daily_dir / "600405.csv",
        index=False,
        encoding="utf-8-sig",
    )
    fresh = existing.copy()
    fresh.attrs["suspension_evidence"] = _session_evidence(
        "600405",
        "2026-07-08",
        "2026-07-16",
        {
            **{trade_date: "1" for trade_date in dates},
            "2026-07-16": "0",
        },
    )
    calendar = {
        "is_open_by_date": {
            trade_date: True
            for trade_date in dates + ["2026-07-16"]
        }
    }

    monkeypatch.setattr(engine, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(
        engine,
        "fetch_daily",
        lambda *args, **kwargs: (
            fresh.copy(),
            ["baostock_explicit_suspension_dates:2026-07-16"],
        ),
    )
    monkeypatch.setattr(engine, "load_cn_trading_calendar", lambda: calendar)
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})

    payload = engine.update_daily_prices(
        target_date="2026-07-16",
        providers=["baostock_daily"],
        network=True,
        symbols="600405",
        output_path=tmp_path / "daily_update.json",
    )

    written = pd.read_csv(daily_dir / "600405.csv", encoding="utf-8-sig")
    assert payload["status"] == "completed"
    assert payload["failed_symbols"] == 0
    assert payload["updated_symbols"] == 0
    assert payload["verified_no_session_symbols"] == 1
    assert payload["verified_no_session_symbol_list"] == ["600405"]
    assert payload["stale_count_after"] == 0
    assert payload["data_freshness_status"] == "fresh"
    assert payload["coverage_date_after"] == "2026-07-16"
    assert "2026-07-16" not in set(written["date"].astype(str))


def test_nondefault_project_root_network_write_is_blocked_before_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import quantdesk.daily_price_update_engine as engine

    daily_dir = tmp_path / "data" / "daily"
    metadata_dir = tmp_path / "data" / "metadata"
    daily_dir.mkdir(parents=True)
    metadata_dir.mkdir(parents=True)
    _daily_frame("600405", ["2026-07-15"]).to_csv(
        daily_dir / "600405.csv",
        index=False,
        encoding="utf-8-sig",
    )
    called = False

    def forbidden_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not run for nondefault root")

    monkeypatch.setattr(engine, "fetch_daily", forbidden_fetch)
    monkeypatch.setattr(
        engine,
        "provider_availability",
        lambda name: {"status": "available", "failed_reason": ""},
    )
    monkeypatch.setattr(
        engine,
        "load_cn_trading_calendar",
        lambda: {
            "is_open_by_date": {
                "2026-07-15": True,
                "2026-07-16": True,
            }
        },
    )
    monkeypatch.setattr(engine, "theme_map_status", lambda: {})
    monkeypatch.setattr(engine, "daily_feature_status", lambda: {})

    payload = engine.update_daily_prices(
        project_root=tmp_path,
        daily_dir=daily_dir,
        metadata_dir=metadata_dir,
        target_date="2026-07-16",
        providers=["baostock_daily"],
        network=True,
        symbols="600405",
        output_path=tmp_path / "blocked.json",
    )

    assert called is False
    assert payload["status"] == "blocked"
    assert payload["stale_count_after"] == 1
    assert any(
        "nondefault_project_root_network_write_blocked" in warning
        for warning in payload["warnings"]
    )
    written = pd.read_csv(
        daily_dir / "600405.csv",
        encoding="utf-8-sig",
    )
    assert written["date"].astype(str).tolist() == ["2026-07-15"]


def test_baostock_daily_network_false_does_not_import_provider(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "baostock", raising=False)
    frame, warnings = providers.fetch_daily(
        "baostock_daily", "600405", "2026-07-01", "2026-07-16", network=False
    )
    assert frame.empty
    assert warnings and "network=false" in warnings[0]
