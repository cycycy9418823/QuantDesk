from __future__ import annotations

import sys

from run_data_health_dashboard import main as dashboard_main
from run_data_layer_health import main as layer_main


LAYER_HEALTH_FLAGS = {"--db-path", "--data-root"}


def _is_layer_health_request(argv: list[str]) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in argv for flag in LAYER_HEALTH_FLAGS)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _is_layer_health_request(args):
        return layer_main(args)
    return dashboard_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
