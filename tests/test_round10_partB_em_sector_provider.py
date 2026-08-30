"""Round 10 Part B — Eastmoney sector provider + multi-level collapse guards.

Two independent provider defects had frozen forward PIT sector membership
capture at 100% failure (90/90 boards) on 2026-07-22..24:

    1. akshare 1.18.64 removed ``stock_board_industry_cons_ths``, so the THS
       fetcher raised ``membership_provider_unavailable:ths`` for every board
       *without issuing a single network request*.  The freeze was therefore
       never upstream rate limiting.
    2. akshare's Eastmoney board helpers hardcode numbered ``push2`` hosts,
       which answer ``502`` from Eastmoney's own gateway while the
       ``push2delay`` variants serve the identical payload.  That made the board
       cache silently demote to the THS taxonomy, which in turn pinned
       ``aligned_membership_count`` at 0 (membership and board quotes must come
       from the same taxonomy).

Fixing (2) exposed a latent third problem: Eastmoney's industry universe is the
Shenwan *three-level* tree, so every symbol is reported under its level-1,
level-2 and level-3 industry at once.  The old capture loop treated the second
sighting as ``multi_sector_conflict``, so a working provider would have failed
the capture on all ~5.6k symbols.  Downstream ``join_sector_membership_as_of``
requires exactly one active classification per symbol per day, so the
cross-section is collapsed to the coarsest containing board.

These tests are fully offline: every fetcher is injected.  Nothing here touches
the network, ``data/universe``, or any canonical dataset.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quantdesk.data_truth.em_sector_provider import coarsest_board_per_symbol
from quantdesk.data_truth.sector_history_archive import (
    _default_member_fetcher,
    capture_current_sector_membership,
)
from quantdesk.shared import now_text


# --- a miniature Shenwan three-level tree ------------------------------------
#
# 电子 (L1) > 半导体 (L2) > 半导体设备 (L3)
# 食品饮料 (L1) > 白酒Ⅱ (L2) > 白酒Ⅲ (L3)
# plus 银行/银行Ⅱ: a real Eastmoney identical-set twin pair, which is why tie
# breaking must be deterministic rather than fetch-order dependent.

_L3 = {
    "半导体设备": ["301583", "603690"],
    "白酒Ⅲ": ["600519"],
}
_L2 = {
    "半导体": ["301583", "603690", "688361"],
    "白酒Ⅱ": ["600519", "000568"],
}
_L1 = {
    "电子": ["301583", "603690", "688361", "002049"],
    "食品饮料": ["600519", "000568", "603288"],
}
_TWINS = {
    "银行": ["601398", "600036"],
    "银行Ⅱ": ["601398", "600036"],
}

_TREE: dict[str, list[str]] = {**_L3, **_L2, **_L1, **_TWINS}

_NAMES = {
    "301583": "托伦斯",
    "603690": "至纯科技",
    "688361": "中科飞测",
    "002049": "紫光国微",
    "600519": "贵州茅台",
    "000568": "泸州老窖",
    "603288": "海天味业",
    "601398": "工商银行",
    "600036": "招商银行",
}


def _tree_fetcher(provider: str, board: str) -> pd.DataFrame:
    """Stand in for the live Eastmoney member endpoint."""

    symbols = _TREE.get(board)
    if symbols is None:
        raise RuntimeError(f"eastmoney_board_code_unknown:{board}")
    return pd.DataFrame(
        [{"代码": symbol, "名称": _NAMES[symbol]} for symbol in symbols]
    )


def _write_board_cache(path: Path, boards: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        [{"name": board, "pct_chg": 1.0 + index} for index, board in enumerate(boards)]
    )
    path.write_text(frame.to_csv(index=False), encoding="utf-8")
    return path


# --- collapse semantics ------------------------------------------------------


def test_collapse_picks_the_coarsest_board_per_symbol() -> None:
    """A symbol present at all three Shenwan levels resolves to its level-1
    industry, because that is the board with the most members."""

    resolved = coarsest_board_per_symbol({k: set(v) for k, v in _TREE.items()})

    # 301583 appears in 半导体设备 (L3, 2), 半导体 (L2, 3), 电子 (L1, 4).
    assert resolved["301583"] == "电子"
    assert resolved["603690"] == "电子"
    # only L2 + L1
    assert resolved["688361"] == "电子"
    # only L1
    assert resolved["002049"] == "电子"
    assert resolved["600519"] == "食品饮料"
    assert resolved["000568"] == "食品饮料"
    assert resolved["603288"] == "食品饮料"


def test_collapse_yields_exactly_one_board_per_symbol() -> None:
    """The downstream PIT join rejects ambiguous rows, so the collapse must be a
    function: one symbol, one board."""

    resolved = coarsest_board_per_symbol({k: set(v) for k, v in _TREE.items()})

    assert set(resolved) == set(_NAMES)
    assert all(isinstance(board, str) and board for board in resolved.values())


def test_collapse_is_order_independent_for_identical_set_twins() -> None:
    """Eastmoney publishes identical-set twin boards (``银行``/``银行Ⅱ``).  Size
    alone cannot break that tie, so the name is the tiebreaker and the result
    must not depend on fetch/iteration order."""

    forward = coarsest_board_per_symbol({k: set(v) for k, v in _TREE.items()})
    reversed_tree = {
        key: set(_TREE[key]) for key in reversed(list(_TREE))
    }
    backward = coarsest_board_per_symbol(reversed_tree)

    assert forward == backward
    assert forward["601398"] == backward["601398"]
    # deterministic pick among the twins
    assert forward["601398"] in {"银行", "银行Ⅱ"}


# --- capture integration -----------------------------------------------------


def test_capture_survives_multi_level_membership(tmp_path: Path) -> None:
    """Regression for the defect that would have broken capture the moment the
    provider started working: multi-level sightings must NOT be reported as
    ``multi_sector_conflict``."""

    cache = _write_board_cache(tmp_path / "cache" / "board.csv", list(_TREE))

    payload = capture_current_sector_membership(
        tmp_path,
        board_cache_path=cache,
        provider="eastmoney",
        network=True,
        captured_at=now_text(),
        member_fetcher=_tree_fetcher,
        output_dir=tmp_path / "out",
    )

    assert payload["ok"] is True, payload["errors"]
    assert payload["status"] == "completed"
    assert payload["errors"] == []
    assert not [error for error in payload["errors"] if "multi_sector_conflict" in error]
    assert payload["fetched_board_count"] == len(_TREE)
    # one row per symbol, not one row per (symbol, board) sighting
    assert payload["row_count"] == len(_NAMES)
    assert payload["canonical_write"] is False
    assert payload["data_universe_written"] is False


def test_capture_output_has_one_row_per_symbol(tmp_path: Path) -> None:
    """The written artifact itself must carry a unique symbol index."""

    cache = _write_board_cache(tmp_path / "cache" / "board.csv", list(_TREE))

    payload = capture_current_sector_membership(
        tmp_path,
        board_cache_path=cache,
        provider="eastmoney",
        network=True,
        captured_at=now_text(),
        member_fetcher=_tree_fetcher,
        output_dir=tmp_path / "out",
    )

    frame = pd.read_csv(Path(payload["path"]), dtype=str)
    assert frame["symbol"].is_unique
    assert len(frame) == len(_NAMES)
    assert set(frame.columns) == {
        "symbol",
        "name",
        "sector",
        "industry",
        "source",
        "captured_at",
    }
    # self-declared observation column (裁定十七) is preserved
    assert frame["captured_at"].nunique() == 1


def test_capture_reports_board_level_failure_without_writing(tmp_path: Path) -> None:
    """Partial provider responses stay degraded and stage nothing: an unknown
    board must fail the whole capture closed."""

    cache = _write_board_cache(
        tmp_path / "cache" / "board.csv", [*_TREE, "不存在的板块"]
    )

    payload = capture_current_sector_membership(
        tmp_path,
        board_cache_path=cache,
        provider="eastmoney",
        network=True,
        captured_at=now_text(),
        member_fetcher=_tree_fetcher,
        output_dir=tmp_path / "out",
    )

    assert payload["ok"] is False
    assert payload["status"] == "degraded"
    assert payload["path"] == ""
    assert any("不存在的板块" in error for error in payload["errors"])
    assert not list((tmp_path / "out").glob("*.csv"))


def test_capture_requires_explicit_network_authorization(tmp_path: Path) -> None:
    cache = _write_board_cache(tmp_path / "cache" / "board.csv", list(_TREE))

    payload = capture_current_sector_membership(
        tmp_path,
        board_cache_path=cache,
        provider="eastmoney",
        network=False,
        captured_at=now_text(),
        member_fetcher=_tree_fetcher,
        output_dir=tmp_path / "out",
    )

    assert payload["status"] == "skipped"
    assert payload["errors"] == ["network_not_authorized"]
    assert not (tmp_path / "out").exists()


# --- provider routing --------------------------------------------------------


def test_eastmoney_routes_to_the_delayed_host_module(monkeypatch) -> None:
    """``eastmoney`` must NOT be served by akshare, whose hardcoded ``push2``
    hosts are the reason the board cache silently demoted to THS."""

    calls: list[str] = []

    def _fake(board: str) -> pd.DataFrame:
        calls.append(board)
        return pd.DataFrame([{"代码": "600519", "名称": "贵州茅台"}])

    monkeypatch.setattr(
        "quantdesk.data_truth.em_sector_provider.fetch_industry_board_members",
        _fake,
    )

    frame = _default_member_fetcher("eastmoney", "食品饮料")

    assert calls == ["食品饮料"]
    assert list(frame["代码"]) == ["600519"]


def test_unroutable_provider_degrades_with_an_explicit_reason(monkeypatch) -> None:
    """An unroutable provider must raise a named reason code rather than
    silently resolving to a different taxonomy.  ``ths`` is exactly this case:
    akshare 1.18.64 no longer ships ``stock_board_industry_cons_ths``.

    The absence is simulated rather than assumed, so this stays hermetic (and
    keeps asserting the fail-closed contract) even if a future akshare restores
    the function.
    """

    akshare = pytest.importorskip("akshare")
    monkeypatch.delattr(akshare, "stock_board_industry_cons_ths", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        _default_member_fetcher("ths", "白酒")

    assert "membership_provider_unavailable:ths" in str(excinfo.value)


def test_installed_akshare_confirms_the_documented_ths_root_cause() -> None:
    """Forensic pin for the root cause: on the installed akshare the THS member
    function really is absent while the Eastmoney one still exists.  If a future
    upgrade restores it this test fails loudly, prompting a re-evaluation of the
    provider choice instead of leaving a stale comment behind."""

    akshare = pytest.importorskip("akshare")

    assert not hasattr(akshare, "stock_board_industry_cons_ths")
    assert hasattr(akshare, "stock_board_industry_cons_em")


def test_unknown_provider_never_falls_back_to_another_taxonomy() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _default_member_fetcher("some_new_vendor", "白酒")

    assert "membership_provider_unavailable:some_new_vendor" in str(excinfo.value)


# --- page-scoped retry -------------------------------------------------------
#
# A live 496-board sweep on 2026-07-25 lost exactly one board to a transient
# ``SSLEOFError`` on its second page.  Because the capture is all-or-nothing,
# that single dropped page degraded the whole ~13 minute cross-section (495/496
# boards fetched, 5,612 symbols collected, still ``status=degraded``).  Retry is
# therefore page-scoped: a whole-query retry would restart from page 1, and the
# original implementation additionally leaked the already-collected rows into
# the retry attempt, which would have silently duplicated members.


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _page_payload(total: int, codes: list[str]) -> dict[str, object]:
    return {
        "data": {
            "total": total,
            "diff": [{"f12": code, "f14": f"N{code}"} for code in codes],
        }
    }


def _install_fake_requests(monkeypatch: pytest.MonkeyPatch, handler) -> list[str]:
    """Route ``requests.get`` to ``handler`` and record the hosts attempted."""

    import requests

    hosts: list[str] = []

    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        hosts.append(str(url).split("//", 1)[-1].split("/", 1)[0])
        page = int(str((kwargs.get("params") or {}).get("pn") or 1))  # type: ignore[union-attr]
        return handler(page, len(hosts))

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        "quantdesk.data_truth.em_sector_provider._RETRY_BACKOFF_SECONDS", 0.0
    )
    return hosts


def test_transient_failure_on_a_later_page_does_not_lose_or_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.data_truth import em_sector_provider as provider

    first_page = [f"00{index:04d}" for index in range(100)]
    second_page = [f"60{index:04d}" for index in range(50)]
    failed: list[int] = []

    def handler(page: int, call_index: int) -> _FakeResponse:
        if page == 1:
            return _FakeResponse(_page_payload(150, first_page))
        if not failed:  # fail page 2 exactly once
            failed.append(page)
            raise OSError("SSLEOFError: EOF occurred in violation of protocol")
        return _FakeResponse(_page_payload(150, second_page))

    _install_fake_requests(monkeypatch, handler)

    rows = provider._fetch_clist("b:BK1206 f:!50", "f12,f14")
    codes = [str(row.get("f12")) for row in rows]

    assert failed == [2], "the transient page-2 failure must actually have fired"
    assert len(codes) == 150
    assert len(set(codes)) == 150, "retry must not duplicate already-collected rows"
    assert codes == first_page + second_page


def test_page_retry_rotates_hosts_and_degrades_visibly_when_all_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.data_truth import em_sector_provider as provider

    def handler(page: int, call_index: int) -> _FakeResponse:
        raise OSError("connection reset")

    hosts = _install_fake_requests(monkeypatch, handler)

    with pytest.raises(provider.EastmoneySectorUnavailable) as excinfo:
        provider._fetch_clist("m:90 t:2 f:!50", "f12,f14")

    assert "eastmoney_delayed_hosts_unavailable" in str(excinfo.value)
    assert len(hosts) == provider._PAGE_ATTEMPTS
    # Every configured delayed host must be tried before the query is abandoned.
    assert set(hosts) == set(provider._CLIST_HOSTS)
    # No attempt may target the broken non-delayed hosts.
    assert not any(host.endswith("push2.eastmoney.com") for host in hosts)


def test_provider_never_targets_the_failing_push2_hosts() -> None:
    from quantdesk.data_truth import em_sector_provider as provider

    for host in provider._CLIST_HOSTS:
        assert "push2delay" in host
        assert not host.endswith("push2.eastmoney.com")


# --- staging cache destination guard ---------------------------------------


def test_sector_cache_refresh_rejects_outside_staging_root_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staging validation root must never be able to target a production cache.

    Regression for the Round 10 incident: the old helper wrote the global cache
    first and only then called ``relative_to(project_root)`` for its response,
    so a staging script that changed the root but missed the cache global
    overwrote the production file before raising ``ValueError``.
    """

    from quantdesk.data_truth import em_sector_provider as provider
    from scripts.server.helpers import market as market_helpers

    staging_root = tmp_path / "staging"
    outside_cache = tmp_path / "production" / "data" / "cache" / "industry_board_latest.csv"
    outside_cache.parent.mkdir(parents=True)
    outside_cache.write_bytes(b"production-sentinel\n")
    before_bytes = outside_cache.read_bytes()
    before_mtime = outside_cache.stat().st_mtime_ns
    provider_calls: list[bool] = []

    def fake_board_list() -> pd.DataFrame:
        provider_calls.append(True)
        return pd.DataFrame([{"name": "电子", "pct_chg": 1.0}])

    monkeypatch.setattr(market_helpers, "project_root", staging_root)
    monkeypatch.setattr(market_helpers, "INDUSTRY_BOARD_CACHE", outside_cache)
    monkeypatch.setattr(provider, "fetch_industry_board_list", fake_board_list)

    with pytest.raises(ValueError, match="industry_board_cache_outside_project_root"):
        market_helpers.refresh_industry_board_cache()

    assert provider_calls == [], "destination validation must run before provider work"
    assert outside_cache.read_bytes() == before_bytes
    assert outside_cache.stat().st_mtime_ns == before_mtime


def test_sector_cache_refresh_rejects_outside_parent_before_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destination guard must run before creating an outside directory."""

    from quantdesk.data_truth import em_sector_provider as provider
    from scripts.server.helpers import market as market_helpers

    staging_root = tmp_path / "staging"
    outside_cache = (
        tmp_path / "outside-uncreated" / "data" / "cache" / "industry_board_latest.csv"
    )
    provider_calls: list[bool] = []

    def fake_board_list() -> pd.DataFrame:
        provider_calls.append(True)
        return pd.DataFrame([{"name": "example", "pct_chg": 1.0}])

    assert not outside_cache.parent.exists()
    monkeypatch.setattr(market_helpers, "project_root", staging_root)
    monkeypatch.setattr(market_helpers, "INDUSTRY_BOARD_CACHE", outside_cache)
    monkeypatch.setattr(provider, "fetch_industry_board_list", fake_board_list)

    with pytest.raises(ValueError, match="industry_board_cache_outside_project_root"):
        market_helpers.refresh_industry_board_cache()

    assert provider_calls == []
    assert not outside_cache.parent.exists()


def test_sector_cache_refresh_rejects_symlink_escape_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache path that resolves through a root-exiting link must fail closed."""

    from quantdesk.data_truth import em_sector_provider as provider
    from scripts.server.helpers import market as market_helpers

    staging_root = tmp_path / "staging"
    outside_dir = tmp_path / "production" / "data" / "cache"
    outside_dir.mkdir(parents=True)
    outside_cache = outside_dir / "industry_board_latest.csv"
    outside_cache.write_bytes(b"production-sentinel\n")
    before_bytes = outside_cache.read_bytes()
    before_mtime = outside_cache.stat().st_mtime_ns

    link_parent = staging_root / "data"
    link_parent.mkdir(parents=True)
    cache_link = link_parent / "cache"
    try:
        cache_link.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    provider_calls: list[bool] = []

    def fake_board_list() -> pd.DataFrame:
        provider_calls.append(True)
        return pd.DataFrame([{"name": "example", "pct_chg": 1.0}])

    monkeypatch.setattr(market_helpers, "project_root", staging_root)
    monkeypatch.setattr(
        market_helpers,
        "INDUSTRY_BOARD_CACHE",
        cache_link / "industry_board_latest.csv",
    )
    monkeypatch.setattr(provider, "fetch_industry_board_list", fake_board_list)

    with pytest.raises(ValueError, match="industry_board_cache_outside_project_root"):
        market_helpers.refresh_industry_board_cache()

    assert provider_calls == []
    assert outside_cache.read_bytes() == before_bytes
    assert outside_cache.stat().st_mtime_ns == before_mtime


def test_sector_cache_refresh_uses_only_the_scoped_target_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correctly scoped validation writes only below its staging root."""

    from quantdesk.data_truth import em_sector_provider as provider
    from scripts.server.helpers import market as market_helpers

    staging_root = tmp_path / "staging"
    scoped_cache = staging_root / "data" / "cache" / "industry_board_latest.csv"
    production_cache_dir = tmp_path / "production" / "data" / "cache"

    monkeypatch.setattr(market_helpers, "project_root", staging_root)
    monkeypatch.setattr(market_helpers, "INDUSTRY_BOARD_CACHE", scoped_cache)
    # This deliberately stays outside the staging root. The fixed helper must
    # not consult it; it must mkdir the validated target's parent instead.
    monkeypatch.setattr(market_helpers, "CACHE_DIR", production_cache_dir)
    monkeypatch.setattr(
        provider,
        "fetch_industry_board_list",
        lambda: pd.DataFrame(
            [
                {
                    "name": "电子",
                    "code": "BK0477",
                    "close": 1234.5,
                    "pct_chg": 1.25,
                    "amount": 8.0,
                    "leading_stock": "示例股",
                }
            ]
        ),
    )

    payload = market_helpers.refresh_industry_board_cache()

    assert payload["ok"] is True
    assert payload["cache_path"] == "data/cache/industry_board_latest.csv"
    assert scoped_cache.is_file()
    assert not production_cache_dir.exists()
