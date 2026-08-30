from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.chanlun_knowledge_base import DATA_KNOWLEDGE_DIR, build_chanlun_knowledge_base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local Chanlun learning knowledge base without saving full text.")
    parser.add_argument("--source-pdf", default=str(DATA_KNOWLEDGE_DIR / "sources" / "chanlun_108.pdf"), help="Optional local PDF path. Only metadata is read by default.")
    parser.add_argument("--output-dir", default=str(DATA_KNOWLEDGE_DIR), help="Output directory for structured knowledge JSON files.")
    parser.add_argument("--report", action=argparse.BooleanOptionalAction, default=True, help="Write Markdown summary report.")
    parser.add_argument("--no-fulltext", action=argparse.BooleanOptionalAction, default=True, help="Do not save PDF full text. Defaults to true.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_chanlun_knowledge_base(
        source_pdf=args.source_pdf,
        output_dir=args.output_dir,
        report=bool(args.report),
        no_fulltext=bool(args.no_fulltext),
        write=True,
    )
    print(json.dumps({
        "ok": payload.get("ok"),
        "schema_version": payload.get("schema_version"),
        "lesson_count": payload.get("lesson_count"),
        "concept_count": payload.get("concept_count"),
        "programmable_count": payload.get("programmable_count"),
        "implementation_status": payload.get("implementation_status"),
        "paths": payload.get("paths"),
        "disclaimer": payload.get("disclaimer"),
    }, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

