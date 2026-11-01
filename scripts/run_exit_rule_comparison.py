"""Refuse the incomplete exit-rule replay until its evidence gate is repaired.

The prior draft manufactured an equity result from position summaries, assumed
take-profit happened first when both thresholds were touched on a daily bar,
and did not verify the original price basis. Do not use its report as evidence.
This entry point intentionally writes nothing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Keep the draft's options so an old command fails safely, rather than
    # silently writing its old report.
    parser.add_argument("--portfolio", type=Path)
    parser.add_argument("--daily-dir", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.parse_args(argv)
    print(
        "离场规则对比未完成，未计算收益或写入报告。"
        "原草稿无法判断同日先触及止盈还是止损，"
        "也未验证完整成交、价格口径与账本对账。"
        "需要逐笔原始流水、可信价格与样本外区间后重新比较。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
