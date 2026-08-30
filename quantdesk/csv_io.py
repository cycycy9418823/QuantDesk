from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pandas as pd
from pandas.errors import ParserError


def read_csv_frame(path: str | Path, **kwargs: Any) -> pd.DataFrame:
    """Read a CSV with UTF-8-SIG by default and visible warning provenance.

    The helper keeps legacy call sites simple by returning a DataFrame, but it
    stores read warnings in ``frame.attrs["warnings"]`` instead of silently
    swallowing missing files or parse errors.
    """
    target = Path(path)
    warnings: list[str] = []
    if not target.exists():
        frame = pd.DataFrame()
        frame.attrs["warnings"] = [f"csv_missing:{target}"]
        return frame
    options = dict(kwargs)
    options.setdefault("encoding", "utf-8-sig")
    try:
        frame = pd.read_csv(target, **options)
    except ParserError as exc:
        warnings.append(f"csv_parse_failed:{target}:{exc}")
        if "on_bad_lines" not in options:
            try:
                retry_options = dict(options)
                retry_options["on_bad_lines"] = "skip"
                frame = pd.read_csv(target, **retry_options)
                warnings.append(f"csv_bad_lines_skipped:{target}")
            except Exception as retry_exc:  # noqa: BLE001 - caller needs visible degraded state
                frame = pd.DataFrame()
                warnings.append(f"csv_read_failed:{target}:{type(retry_exc).__name__}:{retry_exc}")
        else:
            frame = pd.DataFrame()
            warnings.append(f"csv_read_failed:{target}:{type(exc).__name__}:{exc}")
    except Exception as exc:  # noqa: BLE001 - caller needs visible degraded state
        frame = pd.DataFrame()
        warnings.append(f"csv_read_failed:{target}:{type(exc).__name__}:{exc}")
    if frame.empty:
        warnings.append(f"csv_empty:{target}")
    frame.attrs["warnings"] = warnings
    return frame


def read_csv_chunks(path: str | Path, **kwargs: Any) -> Iterator[pd.DataFrame]:
    """Yield CSV chunks with the same default encoding contract as read_csv_frame.

    This intentionally stays thin: callers that need large-file streaming can
    keep their existing fallback behavior, while the direct pandas entry point
    and BOM default live in one project helper.
    """
    options = dict(kwargs)
    options.setdefault("encoding", "utf-8-sig")
    for chunk in pd.read_csv(Path(path), **options):
        chunk.attrs["warnings"] = []
        yield chunk
