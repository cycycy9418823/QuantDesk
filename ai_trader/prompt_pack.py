"""Versioned 20:20 prompt and data-menu files.

Content is frozen from commit 53fa41c. Changing the files is a §4 change.
This module only loads them and stamps version numbers onto records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
VERSIONS_FILE = PROMPT_DIR / "versions.json"
PROMPT_VERSION = "v1.0.0"
MENU_VERSION = "v1.0.0"


@dataclass(frozen=True)
class PromptPack:
    prompt_id: str
    prompt_version: str
    menu_id: str
    menu_version: str
    system_template: str
    menu: dict[str, Any]

    def render_system(self, persona_zh: str) -> str:
        return self.system_template.replace("{persona_zh}", str(persona_zh or ""))

    def version_stamp(self) -> dict[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "menu_id": self.menu_id,
            "menu_version": self.menu_version,
        }


def load_prompt_pack(*, prompt_dir: Path | None = None) -> PromptPack:
    root = Path(prompt_dir or PROMPT_DIR)
    versions = json.loads((root / "versions.json").read_text(encoding="utf-8"))
    system = (root / str(versions["prompt_file"])).read_text(encoding="utf-8").replace("\r\n", "\n")
    system = system.rstrip("\n")
    menu = json.loads((root / str(versions["menu_file"])).read_text(encoding="utf-8"))
    return PromptPack(
        prompt_id=str(versions.get("prompt_id") or "evening_system"),
        prompt_version=str(versions.get("prompt_version") or PROMPT_VERSION),
        menu_id=str(versions.get("menu_id") or "evening_data_menu"),
        menu_version=str(versions.get("menu_version") or MENU_VERSION),
        system_template=system,
        menu=menu if isinstance(menu, dict) else {},
    )


__all__ = [
    "MENU_VERSION",
    "PROMPT_DIR",
    "PROMPT_VERSION",
    "PromptPack",
    "load_prompt_pack",
]
