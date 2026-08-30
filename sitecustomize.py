"""Windows console encoding compatibility for project CLI subprocesses.

Many legacy tests launch CLI scripts with ``subprocess.run(..., text=True)``.
On Windows, the parent decodes captured output with the system preferred
encoding, so child CLIs should emit text with that same encoding.  File outputs
remain explicitly UTF-8 throughout the project; this only affects stdout/stderr.
"""

from __future__ import annotations

import locale
import os
import sys


def _reconfigure_streams_for_windows_subprocesses() -> None:
    if os.name != "nt":
        return
    if os.environ.get("QUANTDESK_DISABLE_STDIO_RECONFIGURE") == "1":
        return
    encoding = locale.getpreferredencoding(False) or "utf-8"
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding=encoding, errors="backslashreplace")


_reconfigure_streams_for_windows_subprocesses()
