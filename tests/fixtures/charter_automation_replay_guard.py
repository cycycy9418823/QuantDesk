"""Read-only source evidence replay: deny real network and writes outside the copy."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ["QUANTDESK_AUTOMATION_FIXTURE_ROOT"]).resolve()


def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("real network unavailable in recorded-evidence replay")
    paths = []
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            paths = [args[0]]
    elif event in {"os.remove", "os.mkdir", "os.rmdir", "os.rename"}:
        paths = list(args[:2] if event == "os.rename" else args[:1])
    for value in paths:
        if isinstance(value, (str, bytes, os.PathLike)) and not Path(os.fsdecode(value)).resolve().is_relative_to(ROOT):
            raise RuntimeError("replay write outside disposable project refused")


sys.addaudithook(audit)
