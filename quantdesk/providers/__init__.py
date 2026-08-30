"""Optional A-share provider adapters for QuantDesk.

Adapters in this package are capability-aware and defensive. Importing this
package must not open network access; individual providers remain lazy-loaded
until callers explicitly request them.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Final

_PROVIDER_MODULES: Final[tuple[str, ...]] = (
    "mootdx_provider",
    "tencent_provider",
    "iwencai_provider",
    "ths_hotspot_provider",
    "news_provider",
    "research_report_provider",
    "announcement_provider",
)

__all__ = list(_PROVIDER_MODULES)


def __getattr__(name: str) -> ModuleType:
    if name in _PROVIDER_MODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_PROVIDER_MODULES))
