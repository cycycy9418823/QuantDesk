from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    data_dir: Path
    raw_dir: Path
    daily_dir: Path
    news_dir: Path
    state_dir: Path
    report_dir: Path
    log_dir: Path
    app_dir: Path
    scripts_dir: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> "ProjectPaths":
        base = (root or project_root()).resolve()
        data_dir = base / "data"
        return cls(
            root=base,
            data_dir=data_dir,
            raw_dir=data_dir / "raw",
            daily_dir=data_dir / "daily",
            news_dir=data_dir / "news",
            state_dir=data_dir / "state",
            report_dir=base / "reports",
            log_dir=base / "logs",
            app_dir=base / "app",
            scripts_dir=base / "scripts",
        )


PATHS = ProjectPaths.from_root()

