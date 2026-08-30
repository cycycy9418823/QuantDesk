from __future__ import annotations

__all__ = ["build_intraday_live_workbench"]


def __getattr__(name: str):
    if name == "build_intraday_live_workbench":
        from quantdesk.intraday.intraday_live_engine import build_intraday_live_workbench

        return build_intraday_live_workbench
    raise AttributeError(name)
