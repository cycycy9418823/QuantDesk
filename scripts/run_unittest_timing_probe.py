from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TIMING_START_PREFIX = "QD_TIMING_START "
TIMING_DONE_PREFIX = "QD_TIMING_DONE "
TIMING_SUMMARY_PREFIX = "QD_TIMING_SUMMARY "


def _emit(prefix: str, payload: dict[str, Any]) -> None:
    print(prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._started_at: dict[str, float] = {}

    def startTest(self, test: unittest.case.TestCase) -> None:
        test_id = test.id()
        self._started_at[test_id] = time.perf_counter()
        _emit(TIMING_START_PREFIX, {"id": test_id})
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        test_id = test.id()
        started = self._started_at.pop(test_id, time.perf_counter())
        elapsed = max(0.0, time.perf_counter() - started)
        _emit(TIMING_DONE_PREFIX, {"id": test_id, "seconds": round(elapsed, 3)})
        super().stopTest(test)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run a unittest discover pattern and emit per-test timing markers.")
    parser.add_argument("--pattern", required=True)
    args = parser.parse_args(argv)
    suite = unittest.defaultTestLoader.discover("tests", pattern=Path(args.pattern).name)
    if suite.countTestCases() <= 0:
        _emit(TIMING_SUMMARY_PREFIX, {"testsRun": 0, "failures": 0, "errors": 0, "skipped": 0})
        return 5
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=0, resultclass=TimingResult)
    result = runner.run(suite)
    _emit(
        TIMING_SUMMARY_PREFIX,
        {
            "testsRun": int(result.testsRun),
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
        },
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
