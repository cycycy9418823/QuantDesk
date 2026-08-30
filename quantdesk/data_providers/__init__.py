from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_LEGACY_PATH = Path(__file__).resolve().parents[1] / "data_providers.py"
if _LEGACY_PATH.exists():
    _spec = importlib.util.spec_from_file_location("_quantdesk_legacy_data_providers", _LEGACY_PATH)
    if _spec and _spec.loader:
        _module = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = _module
        _spec.loader.exec_module(_module)
        for _name, _value in vars(_module).items():
            if not _name.startswith("_") or _name in {"_sina_http_get"}:
                globals()[_name] = _value

        def fetch_spot_sina_single(symbol: str, *, timeout: float = 5.0):  # type: ignore[no-redef]
            _module._sina_http_get = globals()["_sina_http_get"]
            return _module.fetch_spot_sina_single(symbol, timeout=timeout)

        def fetch_spot_sina_batch(symbols, *, batch_size: int = 50, timeout: float = 5.0):  # type: ignore[no-redef]
            _module._sina_http_get = globals()["_sina_http_get"]
            return _module.fetch_spot_sina_batch(symbols, batch_size=batch_size, timeout=timeout)
