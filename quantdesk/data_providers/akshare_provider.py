from __future__ import annotations


def _load_akshare(network: bool = False):
    if not network:
        return None, ["network_false_akshare_skipped"]
    try:
        import akshare as ak  # type: ignore
        return ak, []
    except Exception as exc:
        return None, [f"akshare_unavailable:{exc}"]


def is_akshare_available() -> bool:
    try:
        import akshare  # noqa: F401
        return True
    except Exception:
        return False


def _call_akshare(func_name: str, network: bool = False, **kwargs) -> dict:
    ak, warnings = _load_akshare(network)
    if ak is None:
        status = "skipped" if not network else "failed"
        return {"ok": False, "status": status, "rows": [], "warnings": warnings, "source": "akshare", "network": network}
    if not hasattr(ak, func_name):
        return {"ok": False, "status": "failed", "rows": [], "warnings": [f"akshare_function_missing:{func_name}"], "source": "akshare", "network": network}
    try:
        data = getattr(ak, func_name)(**kwargs)
        rows = data.to_dict("records") if hasattr(data, "to_dict") else []
        return {"ok": True, "status": "passed", "rows": rows, "warnings": [], "source": "akshare", "network": network}
    except Exception as exc:
        return {"ok": False, "status": "failed", "rows": [], "warnings": [f"akshare_call_failed:{func_name}:{exc}"], "source": "akshare", "network": network}


def fetch_daily_bars_akshare(symbol: str, network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_zh_a_hist", network=network, symbol=symbol, **kwargs)


def fetch_fundamental_abstract_ths(symbol: str, network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_financial_abstract_ths", network=network, symbol=symbol, **kwargs)


def fetch_fundamental_abstract_em(symbol: str, network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_financial_abstract", network=network, symbol=symbol, **kwargs)


def fetch_financial_statements_em(symbol: str, network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_financial_report_sina", network=network, stock=symbol, **kwargs)


def fetch_industry_boards_em(network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_board_industry_name_em", network=network, **kwargs)


def fetch_concept_boards_em(network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_board_concept_name_em", network=network, **kwargs)


def fetch_intraday_min_em(symbol: str, network: bool = False, **kwargs) -> dict:
    return _call_akshare("stock_zh_a_hist_min_em", network=network, symbol=symbol, **kwargs)
