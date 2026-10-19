"""Explicit offline prompt comparison; no model, ledger, market fetch or task."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ai_trader.llm import _build_prompt
from ai_trader.prompt_pack import CANDIDATE_ID, load_prompt_pack
from quantdesk.utils import atomic_write_json


def compare_prompt_packs(context: dict[str, Any], *, candidate: str = CANDIDATE_ID) -> dict[str, Any]:
    """Return both exact rendered inputs and structural differences only."""
    packs = {"active": load_prompt_pack(), "candidate": load_prompt_pack(candidate=candidate)}
    variants: dict[str, Any] = {}
    for name, pack in packs.items():
        system, user = _build_prompt(context, pack=pack)
        encoded = json.dumps({"system": system, "user": user}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        variants[name] = {**pack.version_stamp(), "system": system, "user": user,
                          "effective_prompt_sha256": sha256(encoded).hexdigest(),
                          "slots": [slot["id"] for slot in pack.menu["slots"]]}
    active_slots, candidate_slots = set(variants["active"]["slots"]), set(variants["candidate"]["slots"])
    return {
        "schema": "ai_prompt_comparison.v1", "kind": "offline_structural_comparison",
        "model_called": False, "ledger_changed": False, "active_selection_changed": False,
        "deployment_eligible": False, "variants": variants,
        "removed_slots": sorted(active_slots - candidate_slots),
        "added_slots": sorted(candidate_slots - active_slots),
        "remaining_acceptance": ["frozen_historical_baseline", "predeclared_holdout_window",
                                  "predeclared_acceptance_metrics", "owner_approval"],
        "limitation": "结构比较与合成回归不能证明真实收益、样本外优势或判断质量；候选未切换活动菜单。",
    }


def write_prompt_comparison(context: dict[str, Any], out_dir: Path,
                            *, candidate: str = CANDIDATE_ID) -> dict[str, Any]:
    """Save reviewable offline artifacts only in the caller's explicit directory."""
    report = compare_prompt_packs(context, candidate=candidate)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, variant in report["variants"].items():
        (out / f"{name}_system.txt").write_text(variant["system"] + "\n", encoding="utf-8")
        (out / f"{name}_user.txt").write_text(variant["user"] + "\n", encoding="utf-8")
    atomic_write_json(out / "comparison.json", report)
    return {"ok": True, "comparison": str(out / "comparison.json"),
            "kind": report["kind"], "active_selection_changed": False,
            "deployment_eligible": False, "limitation": report["limitation"]}
