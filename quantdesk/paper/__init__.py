from __future__ import annotations

__all__ = ["build_multi_strategy_account_book"]


def __getattr__(name: str):
    if name == "build_multi_strategy_account_book":
        from quantdesk.paper.multi_strategy_account_book import build_multi_strategy_account_book

        return build_multi_strategy_account_book
    raise AttributeError(name)
