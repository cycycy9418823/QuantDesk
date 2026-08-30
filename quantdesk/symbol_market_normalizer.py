from __future__ import annotations

from typing import Any


def normalize_symbol_market(symbol: Any) -> dict[str, str]:
    original = "" if symbol is None else str(symbol).strip()
    digits = "".join(ch for ch in original if ch.isdigit())
    normalized = digits.zfill(6)[-6:] if digits else ""
    market = "unknown"
    provider_symbol = ""
    support_status = "unsupported_symbol"
    if not normalized:
        return {"original_symbol": original, "normalized_symbol": "", "market": market, "provider_symbol": "", "support_status": support_status}
    if normalized.startswith(("000", "001", "002", "003", "300", "301")):
        market = "sz"
        provider_symbol = f"sz{normalized}"
        support_status = "supported"
    elif normalized.startswith(("600", "601", "603", "605", "688")):
        market = "sh"
        provider_symbol = f"sh{normalized}"
        support_status = "supported"
    elif normalized.startswith("900"):
        market = "sh_b_share"
        provider_symbol = f"sh{normalized}"
        support_status = "unsupported_b_share"
    elif normalized.startswith("200"):
        market = "sz_b_share"
        provider_symbol = f"sz{normalized}"
        support_status = "unsupported_b_share"
    elif normalized.startswith(("8", "4", "9")):
        market = "bj"
        provider_symbol = f"bj{normalized}"
        support_status = "unsupported_beijing_exchange"
    return {"original_symbol": original, "normalized_symbol": normalized, "market": market, "provider_symbol": provider_symbol, "support_status": support_status}


def normalize_many(symbols: list[str]) -> list[dict[str, str]]:
    return [normalize_symbol_market(symbol) for symbol in symbols]
