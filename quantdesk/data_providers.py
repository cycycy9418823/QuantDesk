from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# AKShare gateway
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "AKShare is an optional data dependency. Install requirements-data.txt "
            "or use `pip install .[data]` before running AKShare-backed providers."
        ) from exc
    return ak


def fetch_spot_eastmoney():
    return load_akshare().stock_zh_a_spot_em()


def fetch_spot_sina():
    return load_akshare().stock_zh_a_spot()


def fetch_daily_eastmoney(symbol: str, start_date: str, end_date: str, adjust: str):
    return load_akshare().stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )


def fetch_daily_sina(symbol: str, start_date: str, end_date: str, adjust: str):
    return load_akshare().stock_zh_a_daily(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )


def fetch_news_eastmoney(symbol: str):
    return load_akshare().stock_news_em(symbol=symbol)


# ---------------------------------------------------------------------------
# Sina single-quote fallback helpers (v2.3.1)
# ---------------------------------------------------------------------------


_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn",
}
_SINA_URL = "https://hq.sinajs.cn/list={codes}"
_SINA_QUOTE_PATTERN = re.compile(
    r'var\s+hq_str_([a-zA-Z0-9]+)\s*=\s*"([^"]*)"\s*;?',
    re.MULTILINE,
)


def _as_str(value: object) -> str:
    return str(value or "").strip()


def normalize_cn_symbol_for_sina(symbol: object) -> str | None:
    """Normalize a CN A-share symbol to Sina's ``sh``/``sz``/``bj`` form.

    Returns ``None`` when the symbol cannot be recognised.  Acceptable inputs
    include plain 6-digit codes, already-prefixed codes, and noisy variants
    like ``'SH600000'`` or ``'600000.SH'``.
    """

    text = _as_str(symbol).lower()
    if not text:
        return None
    text = text.replace(".sh", "").replace(".sz", "").replace(".ss", "").replace(".bj", "")
    if text.startswith(("sh", "sz", "bj")):
        digits = "".join(ch for ch in text[2:] if ch.isdigit())
        if len(digits) != 6:
            return None
        prefix = text[:2]
        return f"{prefix}{digits}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return None
    head = digits[0]
    prefix3 = digits[:3]
    # The 92 prefix is Beijing, not the Shanghai 900xxx B-share namespace.
    if digits.startswith("92"):
        return f"bj{digits}"
    if head == "6":
        return f"sh{digits}"
    if prefix3.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return f"sz{digits}"
    if prefix3.startswith(("688", "689")):
        return f"sh{digits}"
    if head == "9":
        return f"sh{digits}"
    if head == "2":
        return f"sz{digits}"
    return None


@dataclass
class SinaQuote:
    """Parsed single Sina quote."""

    symbol: str
    name: str = ""
    open: float | None = None
    prev_close: float | None = None
    price: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    date: str = ""
    time: str = ""
    price_basis: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "open": self.open,
            "prev_close": self.prev_close,
            "price": self.price,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "amount": self.amount,
            "date": self.date,
            "time": self.time,
            "price_basis": self.price_basis,
        }


def _to_float(text: str) -> float | None:
    if text is None:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except (TypeError, ValueError):
        return None


def parse_sina_quote_text(text: str) -> list[SinaQuote]:
    """Parse the raw ``hq.sinajs.cn`` response into :class:`SinaQuote` rows.

    The remote format is one assignment per line, e.g.::

        var hq_str_sh600000="浦发银行,9.080,9.090,9.070,9.130,9.050,...,2026-05-08,15:00:01,00,";

    The helper is permissive: unknown prefixes, missing fields and empty
    quotes return as much information as possible without raising.
    """

    quotes: list[SinaQuote] = []
    if not text:
        return quotes
    for match in _SINA_QUOTE_PATTERN.finditer(text):
        symbol = match.group(1)
        body = match.group(2)
        parts = body.split(",") if body else []
        if not parts:
            continue
        name = parts[0] if len(parts) > 0 else ""
        open_price = _to_float(parts[1]) if len(parts) > 1 else None
        prev_close = _to_float(parts[2]) if len(parts) > 2 else None
        price = _to_float(parts[3]) if len(parts) > 3 else None
        high = _to_float(parts[4]) if len(parts) > 4 else None
        low = _to_float(parts[5]) if len(parts) > 5 else None
        volume = _to_float(parts[8]) if len(parts) > 8 else None
        amount = _to_float(parts[9]) if len(parts) > 9 else None
        date = parts[30].strip() if len(parts) > 30 else ""
        time_str = parts[31].strip() if len(parts) > 31 else ""
        quotes.append(
            SinaQuote(
                symbol=symbol,
                name=name,
                open=open_price,
                prev_close=prev_close,
                price=price,
                high=high,
                low=low,
                volume=volume,
                amount=amount,
                date=date,
                time=time_str,
                # This parser handles the exchange quote endpoint, which sends
                # unadjusted prices. Generic CSV readers never infer this field.
                price_basis="raw",
            )
        )
    return quotes


def _sina_http_get(url: str, *, timeout: float = 5.0) -> str:
    """Thin urllib wrapper so tests can monkeypatch one entry point."""

    request = urllib.request.Request(url, headers=_SINA_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit user-triggered provider
        content = response.read()
    # Sina responds as GB18030 but declares nothing.  Force decode so Chinese
    # names come through correctly even when the transport strips BOM.
    text = content.decode("gb18030", errors="replace")
    return text


def fetch_spot_sina_single(symbol: str, *, timeout: float = 5.0) -> SinaQuote | None:
    """Fetch a single Sina quote.  Returns ``None`` when the symbol cannot be
    normalised; raises on transport errors so the caller can fall back.
    """

    normalized = normalize_cn_symbol_for_sina(symbol)
    if not normalized:
        return None
    url = _SINA_URL.format(codes=normalized)
    text = _sina_http_get(url, timeout=timeout)
    quotes = parse_sina_quote_text(text)
    return quotes[0] if quotes else None


@dataclass
class SinaBatchResult:
    """Result of a batch Sina quote request."""

    rows: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_errors: list[str] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def row_count(self) -> int:
        return int(len(self.rows))


def fetch_spot_sina_batch(
    symbols: Iterable[str],
    *,
    batch_size: int = 50,
    timeout: float = 5.0,
) -> SinaBatchResult:
    """Fetch multiple Sina quotes in batches.

    A failing batch does not abort the whole call; it only contributes a
    structured ``provider_errors`` entry.  The returned ``frame`` exposes the
    successful quotes in a stable column layout.
    """

    normalized: list[str] = []
    warnings: list[str] = []
    for raw in symbols or []:
        code = normalize_cn_symbol_for_sina(raw)
        if not code:
            warnings.append(f"无法识别 Sina 格式：{raw!r}")
            continue
        normalized.append(code)

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    if not normalized:
        return SinaBatchResult(rows=rows, warnings=warnings, provider_errors=errors, frame=pd.DataFrame())

    batch = max(int(batch_size or 50), 1)
    for start in range(0, len(normalized), batch):
        chunk = normalized[start : start + batch]
        url = _SINA_URL.format(codes=",".join(chunk))
        try:
            text = _sina_http_get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - bubble up as structured error
            errors.append(f"sina_batch[{start}:{start + len(chunk)}]: {exc}")
            continue
        for quote in parse_sina_quote_text(text):
            rows.append(quote.to_dict())

    if rows:
        frame = pd.DataFrame(rows)
    else:
        frame = pd.DataFrame(columns=["symbol", "name", "open", "prev_close", "price", "high", "low", "volume", "amount", "date", "time"])
    return SinaBatchResult(
        rows=rows,
        warnings=warnings,
        provider_errors=errors,
        frame=frame,
    )
