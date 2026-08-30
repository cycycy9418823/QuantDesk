from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.research.formal_alpha_adjudication import (  # noqa: E402
    adjudicate_formal_alpha,
    write_formal_alpha_artifact,
)
from quantdesk.research.formal_alpha_evaluator import (  # noqa: E402
    build_formal_alpha_evaluation_source,
    write_formal_alpha_evaluation_source,
)


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


@contextmanager
def _temporary_source(payload: Mapping[str, Any]) -> Iterator[Path]:
    """Yield a temporary evaluator source and remove it on every exit path."""

    # The adjudication reader intentionally rejects source paths outside the
    # project root.  Keep the no-write fixture inside the ignored test runtime
    # area, then remove it before returning so this mode remains side-effect
    # free for results/state/data.
    runtime_dir = ROOT / "test_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quantdesk_formal_alpha_", dir=runtime_dir) as directory:
        path = Path(directory) / "source.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        yield path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run local fail-closed formal alpha preflight/adjudication. Full "
            "multi-symbol evidence must pass independent holdout, PIT, cost, "
            "tradability, and provenance gates before an explicit publish step; "
            "the command never changes strategy weights, paper state, or broker boundaries."
        )
    )
    parser.add_argument(
        "--source",
        default="results/round22_factor_adjudication/adjudication_latest.json",
        help="Frozen local evidence envelope. Round22/23 diagnostics block by design.",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help=(
            "Build a fresh local evaluator/preflight source first. It remains "
            "blocked when the authoritative chain/PIT/holdout or dual-track "
            "contract is incomplete."
        ),
    )
    parser.add_argument(
        "--preregistration",
        default="docs/internal/formal_alpha_adjudication_preregistration.json",
    )
    parser.add_argument("--publish", action="store_true", help="Approve only if every formal gate passes.")
    parser.add_argument("--no-write", action="store_true", help="Evaluate without writing dated/latest artifacts.")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_paths: dict[str, str] = {}
    source_override: Path | None = None
    if args.evaluate:
        preregistration = _resolve(args.preregistration)
        chain_path = ROOT / "results" / "data_chain" / "daily_data_chain_latest.json"
        try:
            chain = json.loads(chain_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            chain = {}
        source_payload = build_formal_alpha_evaluation_source(
            ROOT,
            preregistration_path=preregistration,
            daily_chain=chain,
        )
        if not args.no_write:
            written_source = write_formal_alpha_evaluation_source(ROOT, source_payload)
            source_paths = {
                key: str(path.relative_to(ROOT)).replace("\\", "/")
                for key, path in written_source.items()
            }
            source_override = written_source["latest"]
        else:
            # Keep the no-write path side-effect free while still exercising the
            # evaluator contract through a temporary local input file.
            with _temporary_source(source_payload) as temporary_source:
                payload = adjudicate_formal_alpha(
                    ROOT,
                    source_path=temporary_source,
                    preregistration_path=_resolve(args.preregistration),
                    publish=bool(args.publish),
                )
            source_override = None
            paths: dict[str, str] = {}
            output = {"ok": True, "payload": payload, "paths": paths, "evaluator_source": source_paths}
            if args.json:
                print(json.dumps(output, ensure_ascii=True, sort_keys=True))
            else:
                print(f"formal alpha status: {payload['status']}")
                print(f"publication_status: {payload['publication_status']}")
                print(f"adjudication_id: {payload['adjudication_id']}")
                if payload.get("reason_codes"):
                    print("reason_codes: " + ", ".join(payload["reason_codes"]))
            return 0
    payload = adjudicate_formal_alpha(
        ROOT,
        source_path=source_override or _resolve(args.source),
        preregistration_path=_resolve(args.preregistration),
        publish=bool(args.publish),
    )
    paths: dict[str, str] = {}
    if not args.no_write:
        written = write_formal_alpha_artifact(ROOT, payload)
        paths = {key: str(path.relative_to(ROOT)).replace("\\", "/") for key, path in written.items()}
    output = {"ok": True, "payload": payload, "paths": paths, "evaluator_source": source_paths}
    if args.json:
        print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    else:
        print(f"formal alpha status: {payload['status']}")
        print(f"publication_status: {payload['publication_status']}")
        print(f"adjudication_id: {payload['adjudication_id']}")
        if payload.get("reason_codes"):
            print("reason_codes: " + ", ".join(payload["reason_codes"]))
        for key, value in paths.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
