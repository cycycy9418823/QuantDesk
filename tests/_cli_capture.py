from __future__ import annotations

import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Callable


def run_main_json(main: Callable[[list[str] | None], int], argv: list[str], *, check: bool = True) -> dict:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(argv)
    if check and code != 0:
        raise AssertionError(f"CLI main returned {code}: {stderr.getvalue() or stdout.getvalue()}")
    text = stdout.getvalue().strip()
    if "\n" in text:
        text = text.splitlines()[-1]
    return json.loads(text)


def run_qd_json(argv: list[str], *, check: bool = True) -> dict:
    """Run scripts.qd through its unified entrypoint without spawning its child script."""
    from scripts.qd import main as qd_main

    previous = os.environ.get("QUANTDESK_QD_IN_PROCESS")
    os.environ["QUANTDESK_QD_IN_PROCESS"] = "1"
    try:
        return run_main_json(qd_main, argv, check=check)
    finally:
        if previous is None:
            os.environ.pop("QUANTDESK_QD_IN_PROCESS", None)
        else:
            os.environ["QUANTDESK_QD_IN_PROCESS"] = previous
