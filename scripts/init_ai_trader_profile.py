"""Fork the AI paper trader into a new profile (a second, isolated book).

The new profile shares ONLY the incoming market data with the source book.
Its account state and trader memory are cloned once at the fork instant so
both brains start identical; after that the two directories are isolated and
neither can read the other's conclusions.

The per-profile ``llm.env`` is rewritten with the profile's model but the
token is left blank on purpose: credentials are the owner's to place. Never
touches ``state/auto_paper_portfolio.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trader.paths import (  # noqa: E402
    PROFILE_NAMES,
    ledger_id_for_profile,
    profile_env_file,
    profile_paths,
    profile_results_dir,
    profile_state_dir,
)

SKIP_NAMES = {"llm.env", "chat.jsonl", "PROFILE_FORK.json"}
SKIP_PREFIXES = ("daytime_",)
SKIP_SUFFIXES = {".ps1", ".cmd", ".bat", ".log"}


def _rewrite_ledger_id(path: Path, ledger_id: str) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"ledger is not an object: {path}")
    payload["ledger_id"] = ledger_id
    mandate = payload.get("mandate")
    if isinstance(mandate, dict):
        mandate["ledger_id"] = ledger_id
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _write_profile_env(target: Path, source_env: Path, model: str, provider: str) -> None:
    base_url = ""
    if source_env.is_file():
        match = re.search(r'export\s+ANTHROPIC_BASE_URL\s*=\s*"?([^"\n]+)"?', source_env.read_text(encoding="utf-8"))
        base_url = match.group(1).strip() if match else ""
    lines = [
        "# Per-profile AI trader credentials. Runtime only; state/ is gitignored.",
        "# The token is intentionally blank: paste it here (or export ANTHROPIC_AUTH_TOKEN).",
        f'export ANTHROPIC_BASE_URL = "{base_url or "https://integrate.api.nvidia.com/v1"}"',
        'export ANTHROPIC_AUTH_TOKEN = ""',
        f'export AI_TRADER_LLM_MODEL = "{model}"',
        f'export AI_TRADER_LLM_PROVIDER = "{provider}"',
        "export AI_TRADER_EXTERNAL_LLM_ALLOWED = 1",
        "",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def fork_profile(
    *,
    profile: str,
    source_profile: str = "",
    model: str = "z-ai/glm-5.3",
    provider: str = "openai",
    force: bool = False,
) -> dict:
    name = profile.strip().lower()
    if not name or name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile: {profile!r}; known: {list(PROFILE_NAMES)}")
    source = profile_paths(source_profile)
    target_state = profile_state_dir(name)
    target_results = profile_results_dir(name)
    if target_state.exists() and not force:
        existing = sorted(item.name for item in target_state.iterdir())
        raise FileExistsError(f"target profile state already exists: {target_state} ({existing})")

    source_state = source.ledger.parent
    if not source_state.is_dir():
        raise FileNotFoundError(f"source state directory missing: {source_state}")

    target_state.mkdir(parents=True, exist_ok=True)
    target_results.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for item in sorted(source_state.iterdir()):
        if not item.is_file() or item.name in SKIP_NAMES or item.suffix.lower() in SKIP_SUFFIXES:
            continue
        if item.name.startswith(SKIP_PREFIXES):
            continue
        shutil.copy2(item, target_state / item.name)
        copied.append(item.name)

    ledger_id = ledger_id_for_profile(name)
    ledger_target = target_state / "ledger.json"
    if ledger_target.is_file():
        _rewrite_ledger_id(ledger_target, ledger_id)

    _write_profile_env(profile_env_file(name), profile_env_file(source_profile), model, provider)

    marker = {
        "schema_version": "ai_trader_profile_fork_v1",
        "profile": name,
        "ledger_id": ledger_id,
        "forked_from": source_profile or "default",
        "forked_at": datetime.now().astimezone().isoformat(),
        "model": model,
        "provider": provider,
        "state_dir": str(target_state),
        "results_dir": str(target_results),
        "shares_only": ["data/daily", "data/intraday/1m", "data/snapshots"],
        "copied_files": copied,
        "note": "Token is blank in the new llm.env; place it before the first run.",
    }
    (target_state / "PROFILE_FORK.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--from-profile", default="")
    parser.add_argument("--model", default="z-ai/glm-5.3")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        marker = fork_profile(
            profile=args.profile,
            source_profile=args.from_profile,
            model=args.model,
            provider=args.provider,
            force=args.force,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(
        {key: marker[key] for key in
         ("profile", "ledger_id", "forked_from", "model", "provider", "state_dir", "results_dir", "copied_files")},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
