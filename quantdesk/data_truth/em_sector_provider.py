"""Eastmoney sector provider that does not depend on akshare's hardcoded host.

Round 10 Part B.  Two independent provider defects had frozen forward PIT
sector capture at 100% failure:

1. ``akshare`` 1.18.64 removed ``stock_board_industry_cons_ths`` entirely, so
   the THS membership fetcher raised ``membership_provider_unavailable:ths``
   for every board without ever issuing a network request.  The failure was
   therefore never rate limiting.
2. ``akshare``'s Eastmoney board helpers hardcode numbered ``push2`` hosts
   (``17.push2``/``29.push2``).  Those hosts currently answer ``502`` from
   Eastmoney's own nginx layer (or drop the connection), while the
   ``push2delay`` variants serve the identical payload normally.

This module talks to the delayed-quote hosts directly so neither defect can
silently degrade the chain again.  The data is delayed-quote grade, which is
exactly the right grade for an end-of-session forward observation.

Everything here is a read-only market observation.  Nothing in this module
writes to ``data/universe`` or any canonical dataset.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

import pandas as pd

# Delayed-quote hosts.  ``push2.eastmoney.com`` and its numbered siblings are
# intentionally NOT used: see the module docstring.
_CLIST_HOSTS: tuple[str, ...] = (
    "push2delay.eastmoney.com",
    "17.push2delay.eastmoney.com",
    "29.push2delay.eastmoney.com",
)
_CLIST_PATH = "/api/qt/clist/get"

# ``m:90 t:2`` is Eastmoney's industry-board universe.  It currently resolves to
# the full Shenwan three-level tree (level 1 + 2 + 3), which is why the returned
# board count is far larger than the legacy THS board list.
_INDUSTRY_BOARD_FILTER = "m:90 t:2 f:!50"
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_PAGE_SIZE = 100
_TIMEOUT = 20.0
# Per-page attempt budget.  A full-universe capture issues ~500 board requests
# in one pass, so a single transient TLS reset must not fail its board: the
# whole-cross-section capture is all-or-nothing downstream, and one lost board
# discards the other ~495.  Attempts rotate across the delayed hosts.
_PAGE_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = 1.5
_BOARD_CODE_PATTERN = re.compile(r"^BK\d+$")

_BOARD_FIELDS = "f2,f3,f4,f6,f12,f14,f104,f105,f128,f136"
_MEMBER_FIELDS = "f2,f3,f4,f6,f12,f14"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


class EastmoneySectorUnavailable(RuntimeError):
    """Raised when every delayed-quote host refuses to serve a request."""


def _fetch_page(page: int, filter_expression: str, fields: str) -> dict[str, Any] | None:
    """Fetch one page, retrying across delayed hosts before giving up.

    Retry is deliberately page-scoped rather than query-scoped.  A whole-query
    retry would have to discard the pages already collected and restart from
    page 1, and the previous implementation additionally leaked those rows into
    the retry attempt.  Eastmoney drops the occasional connection mid-sweep
    (observed once in a 496-board run as ``SSLEOFError`` on page 2), and because
    the membership capture is all-or-nothing across every board, one dropped
    page is enough to degrade the entire cross-section.  Retrying just that page
    keeps a transient blip from costing a ~13 minute sweep.
    """

    import requests  # imported lazily so offline test paths need no network stack

    params = {
        "pn": str(page),
        "pz": str(_PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": _UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": filter_expression,
        "fields": fields,
    }
    last_error: Exception | None = None
    for attempt in range(_PAGE_ATTEMPTS):
        # Rotate hosts across attempts so a single sick host cannot own a page.
        host = _CLIST_HOSTS[attempt % len(_CLIST_HOSTS)]
        try:
            response = requests.get(
                f"https://{host}{_CLIST_PATH}",
                params=params,
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - retry, then degrade visibly
            last_error = exc
            if attempt < _PAGE_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        data = body.get("data")
        # ``data: null`` is Eastmoney's empty-result encoding, not a failure.
        return data if isinstance(data, dict) else None
    raise EastmoneySectorUnavailable(
        f"eastmoney_delayed_hosts_unavailable:{type(last_error).__name__}:{last_error}"
        if last_error is not None
        else "eastmoney_delayed_hosts_unavailable"
    )


def _fetch_clist(filter_expression: str, fields: str) -> list[dict[str, Any]]:
    """Page through one ``clist`` query."""

    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _fetch_page(page, filter_expression, fields)
        if data is None:
            break
        chunk = data.get("diff") or []
        if isinstance(chunk, dict):
            chunk = list(chunk.values())
        rows.extend(item for item in chunk if isinstance(item, dict))
        total = int(data.get("total") or 0)
        if not chunk or len(rows) >= total:
            break
        page += 1
        if page > 200:  # hard stop; never spin on a misbehaving page cursor
            break
    return rows


def _numeric(value: Any) -> Any:
    return pd.to_numeric(value, errors="coerce")


def fetch_industry_board_list() -> pd.DataFrame:
    """Return the Eastmoney industry-board cross-section.

    Columns are the canonical cache columns used by
    ``refresh_industry_board_cache`` so the caller needs no renaming step.
    """

    rows = _fetch_clist(_INDUSTRY_BOARD_FILTER, _BOARD_FIELDS)
    records = [
        {
            "name": str(row.get("f14") or "").strip(),
            "code": str(row.get("f12") or "").strip(),
            "close": _numeric(row.get("f2")),
            "change": _numeric(row.get("f4")),
            "pct_chg": _numeric(row.get("f3")),
            "amount": _numeric(row.get("f6")),
            "leading_stock": str(row.get("f128") or "").strip(),
            "leading_pct_chg": _numeric(row.get("f136")),
        }
        for row in rows
    ]
    frame = pd.DataFrame(
        records,
        columns=[
            "name",
            "code",
            "close",
            "change",
            "pct_chg",
            "amount",
            "leading_stock",
            "leading_pct_chg",
        ],
    )
    if frame.empty:
        return frame
    frame = frame[frame["name"].astype(str).str.strip() != ""]
    return frame.reset_index(drop=True)


_BOARD_CODE_CACHE: dict[str, str] | None = None


def _board_code_map(refresh: bool = False) -> dict[str, str]:
    global _BOARD_CODE_CACHE
    if _BOARD_CODE_CACHE is None or refresh:
        frame = fetch_industry_board_list()
        _BOARD_CODE_CACHE = {
            str(row.get("name") or "").strip(): str(row.get("code") or "").strip()
            for row in frame.to_dict(orient="records")
            if str(row.get("name") or "").strip() and str(row.get("code") or "").strip()
        }
    return _BOARD_CODE_CACHE


def reset_board_code_cache() -> None:
    """Drop the memoized board-name to board-code map (test/refresh hook)."""

    global _BOARD_CODE_CACHE
    _BOARD_CODE_CACHE = None


def resolve_board_code(board: str) -> str:
    """Map a board name (or pass through a ``BK`` code) to its Eastmoney code."""

    text = str(board or "").strip()
    if not text:
        raise ValueError("eastmoney_board_identifier_empty")
    if _BOARD_CODE_PATTERN.match(text):
        return text
    mapping = _board_code_map()
    code = mapping.get(text)
    if not code:
        # One forced refresh covers a board that appeared after the last call.
        code = _board_code_map(refresh=True).get(text)
    if not code:
        raise KeyError(f"eastmoney_board_code_unknown:{text}")
    return code


def fetch_industry_board_members(board: str) -> pd.DataFrame:
    """Return the current constituent cross-section of one industry board.

    Column names deliberately match Eastmoney's Chinese headers so downstream
    normalizers (``_member_symbol``/``_member_name``) keep working unchanged.
    """

    code = resolve_board_code(board)
    rows = _fetch_clist(f"b:{code} f:!50", _MEMBER_FIELDS)
    records = [
        {
            "代码": str(row.get("f12") or "").strip(),
            "名称": str(row.get("f14") or "").strip(),
            "最新价": _numeric(row.get("f2")),
            "涨跌幅": _numeric(row.get("f3")),
        }
        for row in rows
    ]
    frame = pd.DataFrame(records, columns=["代码", "名称", "最新价", "涨跌幅"])
    if frame.empty:
        return frame
    frame = frame[frame["代码"].astype(str).str.strip() != ""]
    return frame.reset_index(drop=True)


def coarsest_board_per_symbol(
    board_members: dict[str, Iterable[str]],
) -> dict[str, str]:
    """Collapse a multi-level board tree to one board per symbol.

    Eastmoney's industry universe is the Shenwan three-level tree, so a stock is
    reported under its level-1, level-2 and level-3 industry simultaneously.
    Downstream PIT joins require exactly one active classification per symbol
    per day (``join_sector_membership_as_of`` rejects ambiguous rows), so the
    cross-section is collapsed to the coarsest containing board.

    Because level-1 sets contain level-2 sets contain level-3 sets, "coarsest"
    is simply the board with the most members.  Ties (Eastmoney publishes a few
    identical-set twins such as ``银行``/``银行Ⅱ``) break on board name so the
    result is deterministic regardless of fetch order.
    """

    sizes = {board: len(set(symbols)) for board, symbols in board_members.items()}
    candidates: dict[str, list[str]] = {}
    for board, symbols in board_members.items():
        for symbol in set(symbols):
            if symbol:
                candidates.setdefault(symbol, []).append(board)
    return {
        symbol: sorted(boards, key=lambda name: (-sizes[name], name))[0]
        for symbol, boards in candidates.items()
    }


__all__ = [
    "EastmoneySectorUnavailable",
    "coarsest_board_per_symbol",
    "fetch_industry_board_list",
    "fetch_industry_board_members",
    "reset_board_code_cache",
    "resolve_board_code",
]
