"""Flask app instance, numpy/pandas-safe JSON provider, and CORS for the 8000 server.

Split out of quant_api_server.py; ``app`` is re-exported from that namespace so
``app.test_client()`` tests and every ``@app.route`` decorator keep using one
single instance. The CSRF origin guard stays in quant_api_server.py and binds to
this same ``app`` via the re-exported reference.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Flask
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS

LOG = logging.getLogger("quant_api")
project_root = Path(__file__).resolve().parents[2]


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json_value(v) for v in value]
    if isinstance(value, np.ndarray):
        return sanitize_json_value(value.tolist())
    if isinstance(value, pd.DataFrame):
        return sanitize_json_value(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return sanitize_json_value(value.tolist())
    if value is pd.NaT:
        return "数据缺失"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else "数据缺失"
    try:
        if not isinstance(value, (str, bytes)) and pd.isna(value):
            return "数据缺失"
    except (TypeError, ValueError) as exc:
        LOG.warning(
            "json_sanitize_isna_check_failed: value_type=%s error=%s",
            type(value).__name__,
            exc,
        )
    return value


class NumpyJSONProvider(DefaultJSONProvider):
    """Serialize numpy/pandas values without leaking NaN/Infinity."""

    def dumps(self, obj: Any, **kwargs: Any) -> str:
        kwargs.setdefault("allow_nan", False)
        return super().dumps(sanitize_json_value(obj), **kwargs)

    def default(self, obj: Any) -> Any:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            value = float(obj)
            return value if math.isfinite(value) else "数据缺失"
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Timestamp):
            return obj.strftime("%Y-%m-%d")
        return super().default(obj)


app = Flask(
    __name__,
    template_folder=str(project_root / "app" / "templates"),
    static_folder=str(project_root / "app" / "static"),
)
app.json = NumpyJSONProvider(app)
# Always reload Jinja templates from disk on each render. The server runs with
# debug=False/use_reloader=False (see app.run at the bottom), so without this
# Flask caches daily_research.html in memory for the life of the process and
# edits to the template's CSS/structure never reach the browser on a refresh —
# only the static JS (served no-store) updates. That mismatch was the root cause
# of "I changed it but the page never changes". Cost is a cheap mtime stat/render.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
LOCAL_CORS_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": LOCAL_CORS_ORIGINS,
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type"],
            "supports_credentials": False,
        }
    },
)
