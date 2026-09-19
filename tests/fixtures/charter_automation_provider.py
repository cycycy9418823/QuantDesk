"""Provider and clock boundaries for a disposable CLI automation test only."""
from __future__ import annotations

import datetime as _datetime
import json
import os
import runpy
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

ROOT = Path(os.environ["QUANTDESK_AUTOMATION_FIXTURE_ROOT"]).resolve()
FIXTURE = json.loads((ROOT / "provider_fixture.json").read_text(encoding="utf-8"))
TARGET = FIXTURE["target_date"]
_FROZEN_NOW = datetime.fromisoformat(TARGET + "T18:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))


class _FrozenDateTime(_datetime.datetime):
    """Freeze the wall clock so a nightly subprocess sees a completed session.

    The full pipeline runs ``run_daily_data_refresh.py`` as a subprocess, which
    the ``runpy`` hook below cannot reach. Fixing ``datetime.now`` at the class
    level covers both the in-process and subprocess stage runners.
    """

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW.astimezone(tz) if tz is not None else _FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return _FROZEN_NOW.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


_datetime.datetime = _FrozenDateTime


def record(provider: str) -> None:
    with (ROOT / "provider_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"provider": provider, "pid": os.getpid()}) + "\n")


def universe():
    record("universe")
    return pd.DataFrame(FIXTURE["universe"])


def index_daily(symbol: str):
    record("benchmark")
    previous = (datetime.fromisoformat(TARGET) - timedelta(days=1)).date().isoformat()
    return pd.DataFrame([
        {"date": previous, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
        {"date": TARGET, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
    ])


ak.stock_info_a_code_name = universe
ak.stock_zh_index_daily = index_daily
ak.stock_board_industry_name_em = lambda: pd.DataFrame()


def request(self, method, url, **kwargs):
    if "Market_Center.getHQNodeData" not in url:
        raise requests.ConnectionError("fixture has no response for " + url.split("?")[0])
    record("market_snapshot")
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(FIXTURE["quotes"]).encode()
    result.encoding = "utf-8"
    return result


requests.Session.request = request

from quantdesk import data_providers
from quantdesk.providers import news_provider
from quantdesk import market_clock


def dated_quotes(symbols, **kwargs):
    record("dated_quotes")
    return data_providers.SinaBatchResult(rows=[
        {"symbol": row["code"], "date": TARGET, "time": "15:00:00",
         "open": row["open"], "high": row["high"], "low": row["low"],
         "price": row["trade"], "volume": row["volume"]}
        for row in FIXTURE["quotes"] if row["code"] in symbols
    ])


data_providers.fetch_spot_sina_batch = dated_quotes
news_provider._fetch_announce_eastmoney = lambda *args, **kwargs: []
market_clock.is_cn_a_share_trading_day = lambda *args, **kwargs: True

# The CLI still validates the real snapshot and dates; only the wall clock is fixed.
original_run_path = runpy.run_path


def run_path(path, init_globals=None, run_name=None):
    if Path(path).name == "run_daily_data_refresh.py" and run_name == "__main__":
        namespace = original_run_path(path, init_globals=init_globals, run_name="fixture_refresh")
        original_clock_check = namespace["_complete_live_session"]
        namespace["main"].__globals__["_complete_live_session"] = lambda target: original_clock_check(
            target, now=datetime.fromisoformat(TARGET + "T18:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        raise SystemExit(namespace["main"]())
    return original_run_path(path, init_globals=init_globals, run_name=run_name)


runpy.run_path = run_path


def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("real network forbidden in automation fixture")
    paths = []
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            paths = [args[0]]
    elif event in {"os.remove", "os.mkdir", "os.rmdir", "os.rename"}:
        paths = list(args[:2] if event == "os.rename" else args[:1])
    for value in paths:
        if isinstance(value, (str, bytes, os.PathLike)) and not Path(os.fsdecode(value)).resolve().is_relative_to(ROOT):
            raise RuntimeError("fixture write outside disposable project refused")


sys.addaudithook(audit)
