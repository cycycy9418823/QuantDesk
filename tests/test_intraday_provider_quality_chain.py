"""Offline quality-chain tests for the intraday minute data sources.

This suite is fully offline: every provider frame is a fixed in-memory
DataFrame or a mocked callable. No socket is ever opened. It covers:

* the default provider order (local -> sina -> akshare -> tencent -> mootdx);
* Sina full-minute OHLCV quality (session filtering, dedupe, ordering,
  14:57->15:00 auction gap not misreported);
* AKShare em -> sina-interface fallback with sh/sz prefix conversion and
  BSE unsupported handling;
* Tencent price-only degrade labels;
* the unified quality gate contract fields;
* the pure price-conflict diagnostic helper.

All behavior is verified through pure functions so the suite stays
deterministic and never touches the network, data/daily, or paper state.
"""

from __future__ import annotations

import inspect
import socket
import types

import pandas as pd
import pytest
from tests.minute_evidence_fixture import final_minute_fixture, seal_minute_csv_fixture

import quantdesk.intraday.intraday_providers as providers
import quantdesk.intraday.intraday_update_engine as engine
from quantdesk.intraday.intraday_quality_audit import assess_complete_session
from quantdesk.intraday.intraday_providers import (
    DEFAULT_INTRADAY_PROVIDER_ORDER,
    akshare_sina_symbol,
)
from quantdesk.intraday.intraday_schema import (
    build_intraday_quality_gate,
    diagnose_intraday_price_conflict,
    filter_intraday_trading_session,
    source_time_order_state,
    read_intraday_csv,
)

TARGET = "2026-05-15"


def _ohlcv_row(dt, o, h, l, c, v, symbol="600000"):
    return {"symbol": symbol, "datetime": dt, "open": o, "high": h, "low": l, "close": c, "volume": v}


# --- 1. default provider order ------------------------------------------------


class TestDefaultProviderOrder:
    def test_order_is_local_mootdx_tencent_akshare_sina(self):
        assert DEFAULT_INTRADAY_PROVIDER_ORDER == [
            "local_existing",
            "mootdx_intraday",
            "tencent_intraday",
            "akshare_intraday",
            "sina_intraday",
        ]

    def test_tushare_not_in_intraday_default_chain(self):
        assert not any("tushare" in p for p in DEFAULT_INTRADAY_PROVIDER_ORDER)


# --- 2. Sina session filtering ------------------------------------------------


class TestSinaSessionFilter:
    def test_out_of_session_rows_are_dropped(self):
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:15:00", 10, 10, 10, 10, 100),  # pre-open auction, drop
            _ohlcv_row(f"{TARGET} 09:30:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 11:30:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 12:15:00", 10, 10, 10, 10, 100),  # lunch break, drop
            _ohlcv_row(f"{TARGET} 13:00:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 15:00:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 15:30:00", 10, 10, 10, 10, 100),  # after close, drop
        ])
        kept, filtered_count = filter_intraday_trading_session(frame, target_date=TARGET)
        times = list(kept["datetime"].str[11:16])
        assert "09:15" not in times
        assert "12:15" not in times
        assert "15:30" not in times
        assert "09:30" in times and "11:30" in times and "13:00" in times and "15:00" in times
        assert filtered_count == 3

    def test_1457_to_1500_auction_gap_is_not_misreported(self):
        # A common real pattern: bars stop at 14:57, then the 15:00 close
        # auction appears. This must NOT be flagged as an out-of-session row.
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 14:56:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 14:57:00", 10, 10.1, 9.9, 10.0, 200),
            _ohlcv_row(f"{TARGET} 15:00:00", 10, 10.1, 9.9, 10.0, 500),
        ])
        kept, filtered_count = filter_intraday_trading_session(frame, target_date=TARGET)
        assert filtered_count == 0
        assert list(kept["datetime"].str[11:16]) == ["14:56", "14:57", "15:00"]


# --- 3. unified quality gate --------------------------------------------------


class TestQualityGate:
    def _sina_frame(self):
        return final_minute_fixture(pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
            _ohlcv_row(f"{TARGET} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
        ]))

    def test_full_sina_ohlcv_gate_allows_full_minute_signal(self):
        gate = build_intraday_quality_gate(
            self._sina_frame(), provider="sina_intraday", target_date=TARGET,
        )
        assert gate["provider"] == "sina_intraday"
        assert gate["source"] == "sina_intraday"
        assert gate["status"] == "ok"
        assert gate["ohlcv_complete"] is True
        assert gate["price_only_intraday"] is False
        assert gate["volume_semantics"] == "per_minute"
        assert gate["signal_capability"] == "full_minute_ohlcv"
        assert gate["signal_eligible"] is True
        assert gate["ohlcv_signal_eligible"] is True
        assert gate["supports_target_date"] is True
        assert gate["rows"] == 3
        # every required field must be present
        for key in (
            "provider", "status", "target_date", "rows", "latest_datetime",
            "supports_target_date", "data_quality", "ohlcv_complete",
            "price_only_intraday", "volume_semantics", "signal_capability",
            "signal_eligible", "session_filtered_count", "failed_reason",
            "skipped_reason", "warnings",
        ):
            assert key in gate, key

    def test_price_only_tencent_gate_is_trend_only(self):
        # price-only frame: open==high==low==close, cumulative/unknown volume
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.0, 10.0, 10.0, 1000),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.1, 10.1, 10.1, 2000),
        ])
        gate = build_intraday_quality_gate(
            frame, provider="tencent_intraday", target_date=TARGET,
            price_only=True,
        )
        assert gate["ohlcv_complete"] is False
        assert gate["price_only_intraday"] is True
        assert gate["volume_semantics"] == "cumulative_or_unknown"
        assert gate["signal_capability"] == "price_trend_only"
        assert gate["signal_eligible"] is False
        assert gate["ohlcv_signal_eligible"] is False

    def test_explicit_one_price_ohlcv_is_not_inferred_as_price_only(self):
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:30:00", 10.29, 10.29, 10.29, 10.29, 1000, "600722"),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.29, 10.29, 10.29, 10.29, 2000, "600722"),
            _ohlcv_row(f"{TARGET} 09:32:00", 10.29, 10.29, 10.29, 10.29, 3000, "600722"),
        ])

        result = providers._normalize_provider_frame(
            "mootdx_intraday",
            "600722",
            TARGET,
            frame,
            source="mootdx_intraday",
            price_basis="raw",
        )

        assert result["price_only_intraday"] is False
        assert result["ohlcv_complete"] is True
        assert result["signal_eligible"] is False  # Legacy shape does not prove finality.

    @pytest.mark.parametrize("source_column", ["price", "close"])
    def test_synthesized_ohlc_remains_price_only_and_blocked(self, source_column):
        frame = pd.DataFrame([
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:30:00",
                source_column: 10.0,
                "volume": 1000,
            },
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:31:00",
                source_column: 10.1,
                "volume": 2000,
            },
        ])

        result = providers._normalize_provider_frame(
            "synthetic_price_provider",
            "600000",
            TARGET,
            frame,
            source="synthetic_price_provider",
            price_basis="raw",
        )

        assert result["price_only_intraday"] is True
        assert result["ohlcv_complete"] is False
        assert result["signal_eligible"] is False

    def test_pre_normalized_price_only_provenance_cannot_be_lost(self):
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.0, 10.0, 10.0, 1000),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.1, 10.1, 10.1, 2000),
        ])

        result = providers._normalize_provider_frame(
            "tencent_intraday",
            "600000",
            TARGET,
            frame,
            source="tencent_intraday",
            price_basis="raw",
            price_only_intraday=True,
        )

        assert result["price_only_intraday"] is True
        assert result["ohlcv_complete"] is False
        assert result["signal_eligible"] is False

    def test_explicit_false_cannot_downgrade_synthesized_price_only_provenance(self):
        frame = pd.DataFrame([
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:30:00",
                "close": 10.0,
                "volume": 1000,
            },
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:31:00",
                "close": 10.1,
                "volume": 2000,
            },
        ])

        result = providers._normalize_provider_frame(
            "synthetic_price_provider",
            "600000",
            TARGET,
            frame,
            source="synthetic_price_provider",
            price_basis="raw",
            price_only_intraday=False,
        )

        assert result["price_only_intraday"] is True
        assert result["ohlcv_complete"] is False
        assert result["signal_eligible"] is False

    def test_normalizer_preserves_persisted_price_only_provenance_column(self):
        frame = pd.DataFrame([
            {
                **_ohlcv_row(
                    f"{TARGET} 09:30:00", 10.0, 10.0, 10.0, 10.0, 1000
                ),
                "price_only_intraday": True,
            },
            {
                **_ohlcv_row(
                    f"{TARGET} 09:31:00", 10.1, 10.1, 10.1, 10.1, 2000
                ),
                "price_only_intraday": True,
            },
        ])

        result = providers._normalize_provider_frame(
            "persisted_staging",
            "600000",
            TARGET,
            frame,
            source="persisted_staging",
            price_basis="raw",
        )

        assert result["price_only_intraday"] is True
        assert result["signal_eligible"] is False

    def test_reverse_time_order_makes_signal_ineligible(self):
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
        ])
        gate = build_intraday_quality_gate(
            frame, provider="sina_intraday", target_date=TARGET, assume_sorted=True,
        )
        assert gate["signal_eligible"] is False
        assert any("reverse" in w or "倒序" in w for w in gate["warnings"])

    def test_target_date_mismatch_is_not_eligible(self):
        frame = pd.DataFrame([
            _ohlcv_row("2026-05-14 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
        ])
        gate = build_intraday_quality_gate(
            frame, provider="sina_intraday", target_date=TARGET,
        )
        assert gate["supports_target_date"] is False
        assert gate["signal_eligible"] is False

    def test_empty_frame_fails_without_fabrication(self):
        gate = build_intraday_quality_gate(
            pd.DataFrame(), provider="sina_intraday", target_date=TARGET,
        )
        assert gate["status"] in {"failed", "missing"}
        assert gate["rows"] == 0
        assert gate["signal_eligible"] is False
        assert gate["ohlcv_complete"] is False

    def test_missing_amount_only_warns(self):
        frame = self._sina_frame()  # no amount column
        gate = build_intraday_quality_gate(
            frame, provider="sina_intraday", target_date=TARGET,
        )
        # still eligible; amount is optional and only warns
        assert gate["signal_eligible"] is True
        assert any("amount" in w for w in gate["warnings"])


# --- 4. AKShare sina-interface symbol conversion ------------------------------


class TestAkshareSinaSymbol:
    @pytest.mark.parametrize("code,expected", [
        ("600000", "sh600000"),
        ("688001", "sh688001"),
        ("000001", "sz000001"),
        ("300001", "sz300001"),
        ("601398", "sh601398"),
        ("002415", "sz002415"),
    ])
    def test_prefix_conversion(self, code, expected):
        out = akshare_sina_symbol(code)
        assert out["symbol"] == expected
        assert out["supported"] is True

    def test_bse_is_unsupported_not_guessed(self):
        out = akshare_sina_symbol("830001")
        assert out["supported"] is False
        assert out["symbol"] == ""
        assert "unsupported" in out["reason"] or "北交所" in out["reason"]


# --- 5. price conflict diagnostic --------------------------------------------


class TestPriceConflictDiagnostic:
    def test_within_tolerance_is_ok(self):
        out = diagnose_intraday_price_conflict(sina_close=10.00, tencent_price=10.02)
        assert out["status"] == "ok"
        assert out["conflict"] is False

    def test_beyond_tolerance_is_conflict(self):
        out = diagnose_intraday_price_conflict(sina_close=10.00, tencent_price=10.50)
        assert out["status"] == "source_price_conflict"
        assert out["conflict"] is True
        # must not create orders or mutate anything: pure dict only
        assert "order" not in out
        assert out["wait_or_degrade_reason"]

    def test_absolute_tolerance_floor_002(self):
        # 0.3% of 5.00 = 0.015, but floor is 0.02 -> a 0.02 gap is within
        out = diagnose_intraday_price_conflict(sina_close=5.00, tencent_price=5.02)
        assert out["conflict"] is False

    def test_percent_tolerance_for_high_price(self):
        # 0.3% of 100 = 0.30; a 0.25 gap is within tolerance
        out = diagnose_intraday_price_conflict(sina_close=100.00, tencent_price=100.25)
        assert out["conflict"] is False
        # a 0.50 gap exceeds it
        out2 = diagnose_intraday_price_conflict(sina_close=100.00, tencent_price=100.50)
        assert out2["conflict"] is True

    def test_missing_price_is_not_a_false_conflict(self):
        out = diagnose_intraday_price_conflict(sina_close=None, tencent_price=10.0)
        assert out["conflict"] is False
        assert out["status"] in {"insufficient_data", "ok"}


# --- 6. offline guarantee -----------------------------------------------------


class TestOfflineGuarantee:
    def test_quality_gate_opens_no_socket(self, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("network access attempted in offline test")

        monkeypatch.setattr(socket.socket, "connect", _boom)
        gate = build_intraday_quality_gate(
            pd.DataFrame([_ohlcv_row(f"{TARGET} 09:30:00", 10, 10.1, 9.9, 10, 200)]),
            provider="sina_intraday", target_date=TARGET,
        )
        assert gate["provider"] == "sina_intraday"


# --- 7. AKShare real fallback call chain (mocked akshare module) --------------


def _fake_akshare_module(**handlers):
    """Build a stand-in akshare module exposing only the given callables."""

    mod = types.SimpleNamespace()
    for name, fn in handlers.items():
        setattr(mod, name, fn)
    return mod


class TestAkshareRealFallbackChain:
    def _install(self, monkeypatch, module):
        monkeypatch.setattr(providers, "_dependency_available", lambda name: name == "akshare")
        monkeypatch.setattr(providers.importlib, "import_module", lambda name: module)

    def test_em_exception_then_sina_interface_is_called(self, monkeypatch):
        calls = {}

        def em(**kwargs):
            calls["em"] = kwargs
            raise RuntimeError("em down")

        def sina(**kwargs):
            calls["sina"] = kwargs
            return _full_ohlcv_frame(TARGET, symbol=kwargs["symbol"][-6:])

        module = _fake_akshare_module(stock_zh_a_hist_min_em=em, stock_zh_a_minute=sina)
        self._install(monkeypatch, module)
        result = providers._fetch_akshare_intraday("600000", TARGET)
        assert "sina" in calls, "sina interface must be called after em failure"
        assert calls["sina"]["symbol"] == "sh600000"
        assert result["status"] == "ok"
        assert result["rows"] == 2

    def test_em_empty_then_sina_interface_is_called(self, monkeypatch):
        calls = {}

        def em(**kwargs):
            calls["em"] = kwargs
            return pd.DataFrame()

        def sina(**kwargs):
            calls["sina"] = kwargs
            return _full_ohlcv_frame(TARGET, symbol=kwargs["symbol"][-6:])

        module = _fake_akshare_module(stock_zh_a_hist_min_em=em, stock_zh_a_minute=sina)
        self._install(monkeypatch, module)
        result = providers._fetch_akshare_intraday("000001", TARGET)
        assert "sina" in calls
        assert calls["sina"]["symbol"] == "sz000001"
        assert result["status"] == "ok"

    def test_em_wrong_date_then_sina_interface_is_called(self, monkeypatch):
        calls = {}

        def em(**kwargs):
            calls["em"] = kwargs
            # only a different day -> does not cover target date
            return pd.DataFrame([
                _ohlcv_row("2026-05-14 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
                _ohlcv_row("2026-05-14 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
            ])

        def sina(**kwargs):
            calls["sina"] = kwargs
            return _full_ohlcv_frame(TARGET, symbol=kwargs["symbol"][-6:])

        module = _fake_akshare_module(stock_zh_a_hist_min_em=em, stock_zh_a_minute=sina)
        self._install(monkeypatch, module)
        result = providers._fetch_akshare_intraday("600000", TARGET)
        assert "sina" in calls, "wrong-date em must fall through to sina"
        assert result["status"] == "ok"
        # never passed off 05-14 data as 05-15
        assert result["target_date"] == TARGET
        assert all(str(dt)[:10] == TARGET for dt in result["data"]["datetime"])

    def test_sh_sz_prefix_is_correct(self, monkeypatch):
        seen = []

        def em(**kwargs):
            raise RuntimeError("force sina")

        def sina(**kwargs):
            seen.append(kwargs["symbol"])
            return _full_ohlcv_frame(TARGET, symbol=kwargs["symbol"][-6:])

        module = _fake_akshare_module(stock_zh_a_hist_min_em=em, stock_zh_a_minute=sina)
        self._install(monkeypatch, module)
        providers._fetch_akshare_intraday("600000", TARGET)
        providers._fetch_akshare_intraday("000001", TARGET)
        assert seen == ["sh600000", "sz000001"]

    def test_bse_does_not_call_sina_interface(self, monkeypatch):
        calls = {"sina": 0}

        def em(**kwargs):
            raise RuntimeError("em down")

        def sina(**kwargs):  # pragma: no cover - must never run for BSE
            calls["sina"] += 1
            return pd.DataFrame()

        module = _fake_akshare_module(stock_zh_a_hist_min_em=em, stock_zh_a_minute=sina)
        self._install(monkeypatch, module)
        result = providers._fetch_akshare_intraday("830001", TARGET)
        assert calls["sina"] == 0, "BSE must not call the sina minute interface"
        assert result["status"] in {"skipped", "failed"}
        assert result["rows"] == 0


# --- 8. Sina session filter wired into the provider path ---------------------


class TestSinaProviderPathFiltersSession:
    def test_1530_row_is_filtered_by_normalize_provider_frame(self):
        frame = pd.DataFrame([
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
            _ohlcv_row(f"{TARGET} 15:30:00", 10.1, 10.3, 10.0, 10.2, 300),
        ])
        result = providers._normalize_provider_frame(
            "sina_intraday", "600000", TARGET, frame, source="sina_intraday",
        )
        assert result["status"] == "ok"
        assert result["session_filtered_count"] == 1
        times = list(result["data"]["datetime"].str[11:16])
        assert "15:30" not in times
        assert result["signal_eligible"] is False  # Filtering does not supply finality.


# --- 9. price-only not accepted as a formal minute update --------------------


class TestPriceOnlyNotFormalUpdate:
    def test_price_only_provider_result_is_not_written_or_counted(self, monkeypatch, tmp_path):
        # Fake a provider that returns a pre-normalized price-only frame.  The
        # provenance flag—not a flat O=H=L=C value shape—is authoritative.
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "tencent_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.0, 10.0, 10.0, 1000),
                    _ohlcv_row(f"{target_date} 09:31:00", 10.1, 10.1, 10.1, 10.1, 2000),
                ])
                return providers._normalize_provider_frame(
                    "tencent_intraday", symbol, target_date, frame,
                    source="tencent_intraday",
                    price_only_intraday=True,
                )
            # every other provider misses the target date
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"],
            target_date=TARGET,
            provider_order=["tencent_intraday"],
            network=True,
            intraday_dir=tmp_path,
            output_path=tmp_path / "update.json",
        )
        # price-only must not be a formal update and must not write a csv
        assert payload["updated_symbols"] == 0
        assert "600000" in payload["failed_symbol_list"]
        assert not (tmp_path / "600000.csv").exists()
        tencent_row = next(r for r in payload["provider_results"] if r["provider"] == "tencent_intraday")
        assert tencent_row.get("price_only_intraday") is True
        assert tencent_row.get("signal_eligible") is False
        assert tencent_row.get("degraded", 0) >= 1

    def test_price_only_does_not_block_later_provider(self, monkeypatch, tmp_path):
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "tencent_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.0, 10.0, 10.0, 1000),
                    _ohlcv_row(f"{target_date} 09:31:00", 10.1, 10.1, 10.1, 10.1, 2000),
                ])
                return providers._normalize_provider_frame(
                    "tencent_intraday", symbol, target_date, frame,
                    source="tencent_intraday",
                    price_only_intraday=True,
                )
            if provider == "sina_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
                    _ohlcv_row(f"{target_date} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
                ])
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, final_minute_fixture(frame),
                    source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"],
            target_date=TARGET,
            # tencent (price-only) first, sina (full ohlcv) after
            provider_order=["tencent_intraday", "sina_intraday"],
            network=True,
            intraday_dir=tmp_path,
            output_path=tmp_path / "update.json",
        )
        # sina after tencent must still succeed -> price-only did not block the chain
        assert payload["updated_symbols"] == 1
        assert "600000" in payload["updated_symbol_list"]
        assert (tmp_path / "600000.csv").exists()


# --- 10. datetime+close only must not be judged as full OHLCV ----------------


class TestDatetimeCloseOnlyNotComplete:
    def test_close_only_frame_is_not_ohlcv_complete(self):
        # frame with datetime + close but no open/high/low/volume
        frame = pd.DataFrame([
            {"symbol": "600000", "datetime": f"{TARGET} 09:30:00", "close": 10.1},
            {"symbol": "600000", "datetime": f"{TARGET} 09:31:00", "close": 10.2},
        ])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False
        assert gate["ohlcv_signal_eligible"] is False

    def test_all_nan_prices_not_ohlcv_complete(self):
        frame = pd.DataFrame([
            {"symbol": "600000", "datetime": f"{TARGET} 09:30:00", "open": None, "high": None, "low": None, "close": None, "volume": 100},
            {"symbol": "600000", "datetime": f"{TARGET} 09:31:00", "open": None, "high": None, "low": None, "close": None, "volume": 200},
        ])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False

    def test_single_valid_row_is_insufficient(self):
        frame = pd.DataFrame([_ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200)])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        # one row is too few to be a signal-eligible replay (mirrors validate_intraday_payload)
        assert gate["signal_eligible"] is False


# --- 11. helper is defined exactly once --------------------------------------


class TestHelperNotDuplicated:
    def test_akshare_sina_symbol_defined_once(self):
        src = inspect.getsource(providers)
        assert src.count("def akshare_sina_symbol(") == 1


# --- 12. OHLCV value integrity: volume + price + high/low --------------------


class TestOhlcvValueIntegrity:
    def _two_rows(self, **override):
        rows = [
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
        ]
        df = pd.DataFrame(rows)
        for key, values in override.items():
            df[key] = values
        return df

    def test_all_none_volume_is_not_complete(self):
        frame = self._two_rows(volume=[None, None])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False
        assert any("volume" in w for w in gate["warnings"])

    def test_non_numeric_volume_is_not_complete(self):
        frame = self._two_rows(volume=["abc", "xyz"])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False

    def test_negative_volume_is_not_complete(self):
        frame = self._two_rows(volume=[-10, -20])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False

    def test_zero_volume_no_trade_minute_is_allowed(self):
        # a genuine no-trade minute (volume 0) is still valid OHLCV
        frame = final_minute_fixture(self._two_rows(volume=[0, 0]))
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is True
        assert gate["signal_eligible"] is False
        assert "zero_volume_no_trade_evidence" in gate["bar_evidence"]["reason_codes"]

    def test_non_positive_price_is_not_complete(self):
        frame = self._two_rows(close=[0.0, -1.0])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False

    def test_illegal_high_low_envelope_is_not_complete(self):
        # high < low on both rows -> illegal envelope
        frame = self._two_rows(high=[9.0, 9.1], low=[10.5, 10.6])
        gate = build_intraday_quality_gate(frame, provider="sina_intraday", target_date=TARGET)
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False


# --- 13. update engine fail-closes on any degrade signal ---------------------


def _full_ohlcv_frame(target_date, symbol="600000"):
    return final_minute_fixture(pd.DataFrame([
        _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200, symbol),
        _ohlcv_row(f"{target_date} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300, symbol),
    ]))


class TestUpdateEngineFailClose:
    def test_engine_cannot_downgrade_synthesized_price_only_provenance(
        self, monkeypatch, tmp_path
    ):
        close_only = _formal_session_frame()[
            ["symbol", "datetime", "close", "volume"]
        ].copy()

        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            return {
                "provider": provider,
                "status": "ok",
                "symbol": symbol,
                "target_date": target_date,
                "data": close_only,
                "ohlcv_complete": True,
                "signal_eligible": True,
                "price_only_intraday": False,
                "price_basis": "raw",
                "signal_capability": "full_minute_ohlcv",
                "volume_semantics": "per_minute",
                "suggested_next_action": "tampered legacy provider result",
                "warnings": [],
            }

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"],
            target_date=TARGET,
            provider_order=["legacy_intraday"],
            network=True,
            intraday_dir=tmp_path,
            output_path=tmp_path / "update.json",
        )

        assert payload["updated_symbols"] == 0
        assert not (tmp_path / "600000.csv").exists()
        row = payload["provider_results"][0]
        assert row["price_only_intraday"] is True
        assert row["reason_code"] == "price_only_intraday"

    def test_single_row_full_ohlcv_is_not_written(self, monkeypatch, tmp_path):
        # one valid OHLCV row -> insufficient -> not signal-eligible -> no csv, no update
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "sina_intraday":
                frame = pd.DataFrame([_ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200)])
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, frame, source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["updated_symbols"] == 0
        assert not (tmp_path / "600000.csv").exists()
        row = next(r for r in payload["provider_results"] if r["provider"] == "sina_intraday")
        assert row.get("degraded", 0) >= 1

    def test_reverse_order_provider_data_is_not_accepted(self, monkeypatch, tmp_path):
        # provider streams rows in reverse source order -> not signal-eligible
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "sina_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
                    _ohlcv_row(f"{target_date} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
                ])
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, frame, source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["updated_symbols"] == 0
        assert not (tmp_path / "600000.csv").exists()
        row = next(r for r in payload["provider_results"] if r["provider"] == "sina_intraday")
        assert row.get("signal_eligible") is False
        assert row.get("source_time_order_valid") is False

    def test_fail_close_even_if_ohlcv_complete_wrongly_true(self, monkeypatch, tmp_path):
        # A provider result where ohlcv_complete is (incorrectly) True but
        # signal_eligible is False must still fail-close: no write, no update.
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "sina_intraday":
                res = providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, _full_ohlcv_frame(target_date),
                    source="sina_intraday",
                )
                # tamper: pretend it's complete while keeping signal_eligible False
                res["ohlcv_complete"] = True
                res["signal_eligible"] = False
                if isinstance(res.get("quality_gate"), dict):
                    res["quality_gate"]["signal_eligible"] = False
                return res
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["updated_symbols"] == 0
        assert not (tmp_path / "600000.csv").exists()

    def test_degraded_then_later_qualified_provider_succeeds(self, monkeypatch, tmp_path):
        # first provider degraded (reverse order), second provider clean -> written
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "tencent_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
                ])
                return providers._normalize_provider_frame(
                    "tencent_intraday", symbol, target_date, frame, source="tencent_intraday",
                )
            if provider == "sina_intraday":
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, _full_ohlcv_frame(target_date),
                    source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET,
            provider_order=["tencent_intraday", "sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["updated_symbols"] == 1
        assert "600000" in payload["updated_symbol_list"]
        assert (tmp_path / "600000.csv").exists()
        tencent_row = next(r for r in payload["provider_results"] if r["provider"] == "tencent_intraday")
        assert tencent_row.get("degraded", 0) >= 1

    def test_degrade_reason_not_always_price_only(self, monkeypatch, tmp_path):
        # a reverse-order (not price-only) degrade must not be labelled price-only
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "sina_intraday":
                frame = pd.DataFrame([
                    _ohlcv_row(f"{target_date} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
                    _ohlcv_row(f"{target_date} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
                ])
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, frame, source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        row = next(r for r in payload["provider_results"] if r["provider"] == "sina_intraday")
        reason = str(row.get("failed_reason") or "")
        assert "price-only" not in reason
        assert row.get("price_only_intraday") is False


# --- 14. local_existing is NOT a quality bypass ------------------------------


def _write_local_csv(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


class TestLocalExistingGoesThroughGate:
    def test_local_single_row_does_not_short_circuit_later_sina(self, monkeypatch, tmp_path):
        # local CSV has only one target-date row -> not signal-eligible -> must NOT
        # be accepted as unchanged; the chain must continue to a qualified sina.
        _write_local_csv(tmp_path / "600000.csv", [
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
        ])

        real_fetch = providers.fetch_intraday_1m

        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "local_existing":
                return real_fetch("local_existing", symbol, target_date, intraday_dir=intraday_dir, network=network)
            if provider == "sina_intraday":
                return providers._normalize_provider_frame(
                    "sina_intraday", symbol, target_date, _full_ohlcv_frame(target_date),
                    source="sina_intraday",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET,
            provider_order=["local_existing", "sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["unchanged_symbols"] == 0, "single-row local must not count as unchanged"
        assert payload["updated_symbols"] == 1
        assert "600000" in payload["updated_symbol_list"]
        local_row = next(r for r in payload["provider_results"] if r["provider"] == "local_existing")
        assert local_row.get("degraded", 0) >= 1

    def test_local_price_only_is_not_unchanged(self, monkeypatch, tmp_path):
        # A true price-only local CSV (original `price` field, no OHLC) must not
        # be recorded as unchanged. Explicit flat OHLC can be a real limit day.
        _write_local_csv(tmp_path / "600000.csv", [
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:30:00",
                "price": 10.0,
                "volume": 1000,
            },
            {
                "symbol": "600000",
                "datetime": f"{TARGET} 09:31:00",
                "price": 10.1,
                "volume": 2000,
            },
        ])

        real_fetch = providers.fetch_intraday_1m

        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "local_existing":
                return real_fetch("local_existing", symbol, target_date, intraday_dir=intraday_dir, network=network)
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["local_existing"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["unchanged_symbols"] == 0
        local_row = next(r for r in payload["provider_results"] if r["provider"] == "local_existing")
        assert local_row.get("price_only_intraday") is True
        assert local_row.get("signal_eligible") is False
        assert local_row.get("degraded", 0) >= 1

    def test_qualified_local_full_ohlcv_is_unchanged(self, monkeypatch, tmp_path):
        # a full, multi-row, forward-order local CSV passes the gate -> unchanged
        _write_local_csv(tmp_path / "600000.csv", [
            _ohlcv_row(f"{TARGET} 09:30:00", 10.0, 10.2, 9.9, 10.1, 200),
            _ohlcv_row(f"{TARGET} 09:31:00", 10.1, 10.3, 10.0, 10.2, 300),
            _ohlcv_row(f"{TARGET} 09:32:00", 10.2, 10.4, 10.1, 10.3, 400),
        ])
        seal_minute_csv_fixture(tmp_path / "600000.csv", convention="minute_start")

        real_fetch = providers.fetch_intraday_1m

        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "local_existing":
                return real_fetch("local_existing", symbol, target_date, intraday_dir=intraday_dir, network=network)
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["local_existing"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["unchanged_symbols"] == 1
        assert "600000" in payload["unchanged_symbol_list"]
        local_row = next(r for r in payload["provider_results"] if r["provider"] == "local_existing")
        assert local_row.get("ohlcv_complete") is True
        assert local_row.get("signal_eligible") is True
        assert local_row.get("degraded", 0) == 0


# --- 15. provider missing the quality gate cannot bypass ---------------------


class TestMissingQualityGateFailsClose:
    def test_provider_without_quality_fields_is_not_accepted(self, monkeypatch, tmp_path):
        # a rogue provider returns ok data but no quality-gate fields at all
        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "sina_intraday":
                return {
                    "provider": "sina_intraday",
                    "status": "ok",
                    "symbol": symbol,
                    "target_date": target_date,
                    "rows": 2,
                    "data": _full_ohlcv_frame(target_date),
                    "warnings": [],
                    # NOTE: deliberately no ohlcv_complete / signal_eligible
                }
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET, provider_order=["sina_intraday"],
            network=True, intraday_dir=tmp_path, output_path=tmp_path / "update.json",
        )
        assert payload["updated_symbols"] == 0
        assert payload["unchanged_symbols"] == 0
        assert not (tmp_path / "600000.csv").exists()
        row = next(r for r in payload["provider_results"] if r["provider"] == "sina_intraday")
        assert row.get("reason_code") == "missing_quality_gate"
        assert row.get("degraded", 0) >= 1


# --- 16. source order uses schema aliases (day / Chinese time) ---------------


class TestSourceOrderUsesAliases:
    def test_day_column_reverse_is_detected(self):
        # 'day' is the Sina minute alias for datetime; reverse must be caught
        frame = pd.DataFrame([
            {"symbol": "600000", "day": f"{TARGET} 09:32:00", "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3, "volume": 400},
            {"symbol": "600000", "day": f"{TARGET} 09:31:00", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 300},
            {"symbol": "600000", "day": f"{TARGET} 09:30:00", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 200},
        ])
        assert source_time_order_state(frame) is False

    def test_chinese_time_column_reverse_is_detected(self):
        frame = pd.DataFrame([
            {"代码": "600000", "时间": f"{TARGET} 09:32:00", "开盘": 10.2, "最高": 10.4, "最低": 10.1, "收盘": 10.3, "成交量": 400},
            {"代码": "600000", "时间": f"{TARGET} 09:31:00", "开盘": 10.1, "最高": 10.3, "最低": 10.0, "收盘": 10.2, "成交量": 300},
            {"代码": "600000", "时间": f"{TARGET} 09:30:00", "开盘": 10.0, "最高": 10.2, "最低": 9.9, "收盘": 10.1, "成交量": 200},
        ])
        assert source_time_order_state(frame) is False

    def test_day_column_forward_is_ok(self):
        frame = pd.DataFrame([
            {"symbol": "600000", "day": f"{TARGET} 09:30:00", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 200},
            {"symbol": "600000", "day": f"{TARGET} 09:31:00", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 300},
        ])
        assert source_time_order_state(frame) is True


# --- 17. unknown source order fails closed -----------------------------------


class TestUnknownOrderFailsClose:
    def test_no_time_column_is_unknown_order(self):
        # no datetime/date/time column at all -> order cannot be determined
        frame = pd.DataFrame([
            {"symbol": "600000", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 200},
            {"symbol": "600000", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 300},
        ])
        assert source_time_order_state(frame) is None

    def test_gate_fails_close_when_order_explicitly_unknown(self):
        frame = _full_ohlcv_frame(TARGET)
        gate = build_intraday_quality_gate(
            frame, provider="sina_intraday", target_date=TARGET,
            source_time_order_valid=None,  # explicit unknown
        )
        assert gate["source_time_order_valid"] is None
        assert gate["ohlcv_complete"] is False
        assert gate["signal_eligible"] is False
        assert any("无法确定" in w or "原始时间顺序" in w for w in gate["warnings"])


# --- 18. formal session price basis fails closed -----------------------------


def _formal_session_frame(symbol="600000", target_date=TARGET):
    times = list(pd.date_range(f"{target_date} 09:31", periods=120, freq="min"))
    times += list(pd.date_range(f"{target_date} 13:01", periods=120, freq="min"))
    frame = pd.DataFrame([
        _ohlcv_row(timestamp, 10.0, 10.2, 9.9, 10.1, 100, symbol)
        for timestamp in times
    ])
    frame["price_basis"] = "raw"
    return frame


class TestFormalSessionPriceBasis:
    def test_raw_and_unknown_rows_fail_closed(self):
        frame = _formal_session_frame()
        frame.loc[0, "price_basis"] = "unknown"

        result = assess_complete_session(
            frame,
            symbol="600000",
            target_date=TARGET,
        )

        assert result["status"] == "invalid_session"
        assert result["price_basis"] == "mixed_or_unknown"
        assert result["price_basis_values"] == ["raw", "unknown"]
        assert result["formal_execution_eligible"] is False
        assert "price_basis_not_raw" in result["reason_codes"]

    def test_explicit_raw_does_not_override_qfq_row(self):
        frame = _formal_session_frame()
        frame.loc[0, "price_basis"] = "qfq"

        result = assess_complete_session(
            frame,
            symbol="600000",
            target_date=TARGET,
            price_basis="raw",
        )

        assert result["status"] == "invalid_session"
        assert result["price_basis"] == "mixed_or_unknown"
        assert result["price_basis_values"] == ["qfq", "raw"]
        assert result["formal_execution_eligible"] is False
        assert "price_basis_not_raw" in result["reason_codes"]

    def test_all_explicit_raw_rows_are_formally_eligible(self):
        result = assess_complete_session(
            final_minute_fixture(_formal_session_frame(), convention="minute_end"),
            symbol="600000",
            target_date=TARGET,
            price_basis="raw",
        )

        assert result["status"] == "complete_session"
        assert result["price_basis"] == "raw"
        assert result["price_basis_values"] == ["raw"]
        assert result["formal_execution_eligible"] is True
        assert "price_basis_not_raw" not in result["reason_codes"]

    def test_full_explicit_one_price_session_is_formally_eligible(self):
        frame = _formal_session_frame(symbol="600722").assign(
            open=10.29,
            high=10.29,
            low=10.29,
            close=10.29,
            volume=1000,
        )

        result = assess_complete_session(
            final_minute_fixture(frame, convention="minute_end"),
            symbol="600722",
            target_date=TARGET,
            price_basis="raw",
        )

        assert result["status"] == "complete_session"
        assert result["formal_execution_eligible"] is True
        assert "price_only_intraday" not in result["reason_codes"]

    def test_price_only_provenance_blocks_even_a_full_flat_session(self):
        frame = _formal_session_frame(symbol="600722").assign(
            open=10.29,
            high=10.29,
            low=10.29,
            close=10.29,
            volume=1000,
        )

        result = assess_complete_session(
            frame,
            symbol="600722",
            target_date=TARGET,
            price_basis="raw",
            price_only_intraday=True,
        )

        assert result["status"] == "invalid_session"
        assert result["formal_execution_eligible"] is False
        assert "price_only_intraday" in result["reason_codes"]


class TestLocalMixedPriceBasisFallsThrough:
    def test_local_raw_plus_unknown_does_not_short_circuit_complete_raw_provider(self, monkeypatch, tmp_path):
        local = _formal_session_frame(target_date=TARGET)
        local["price_basis"] = "raw"
        obsolete = local.iloc[[0]].copy()
        obsolete["datetime"] = f"{TARGET} 09:30:00"
        obsolete["price_basis"] = "unknown"
        local = pd.concat([obsolete, local], ignore_index=True)
        _write_local_csv(tmp_path / "600000.csv", local.to_dict(orient="records"))
        real_fetch = providers.fetch_intraday_1m

        def fake_fetch(provider, symbol, target_date, *, intraday_dir, network):
            if provider == "local_existing":
                return real_fetch(
                    "local_existing", symbol, target_date,
                    intraday_dir=intraday_dir, network=network,
                )
            if provider == "mootdx_intraday":
                return providers._normalize_provider_frame(
                    "mootdx_intraday", symbol, target_date,
                    final_minute_fixture(_formal_session_frame(
                        symbol=symbol,
                        target_date=target_date,
                    ), convention="minute_end"),
                    source="mootdx_intraday", price_basis="raw",
                )
            return providers._empty_result(provider, symbol, target_date, "failed", "no data")

        monkeypatch.setattr(engine, "fetch_intraday_1m", fake_fetch)
        payload = engine.update_intraday_1m_for_symbols(
            symbols=["600000"], target_date=TARGET,
            provider_order=["local_existing", "mootdx_intraday"],
            network=True, intraday_dir=tmp_path,
            output_path=tmp_path / "update.json",
            require_raw_price_basis=True,
        )

        assert payload["unchanged_symbols"] == 0
        assert payload["updated_symbol_list"] == ["600000"]
        local_result = next(
            row for row in payload["provider_results"]
            if row["provider"] == "local_existing"
        )
        assert local_result["reason_code"] == "price_basis_not_raw"
        assert "can execute" not in str(
            local_result["suggested_next_action"]
        ).lower()
        assert "not formal" in str(
            local_result["suggested_next_action"]
        ).lower()
        written = read_intraday_csv(tmp_path / "600000.csv", default_symbol="600000")["frame"]
        assert len(written) == 240
        assert set(written["price_basis"].astype(str)) == {"raw"}
        audit = assess_complete_session(
            written,
            symbol="600000",
            target_date=TARGET,
        )
        assert audit["status"] == "complete_session"
        assert audit["unique_session_minutes"] == 240
        assert audit["session_coverage_ratio"] == 1.0
        assert audit["formal_execution_eligible"] is True
