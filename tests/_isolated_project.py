from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path


_SOURCE_DIRECTORIES = (
    "quantdesk",
    "scripts",
    "config",
    "app",
    "templates",
    "schemas",
    "examples",
)

_EMPTY_RUNTIME_DIRECTORIES = (
    "data/daily",
    "data/intraday",
    "data/universe",
    "data/features",
    "data/metadata",
    "data/benchmark",
    "data/calendar",
    "data/knowledge",
    "data/tracking",
    "results",
    "reports",
    "state",
    "logs",
    "test_runtime",
)


class IsolatedProject:
    """Disposable source clone for CLI tests that write project-relative state."""

    def __init__(self, source_root: Path, *, temp_parent: Path) -> None:
        self.source_root = source_root.resolve()
        self.temp_parent = temp_parent.resolve()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None

    def start(self) -> "IsolatedProject":
        if self.root is not None:
            return self
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="isolated_project_",
            dir=self.temp_parent,
        )
        self.root = Path(self._temporary_directory.name).resolve()
        if self.root == self.source_root:
            raise AssertionError("isolated project root must differ from the real project root")

        ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        for relative in _SOURCE_DIRECTORIES:
            source = self.source_root / relative
            if source.is_dir():
                shutil.copytree(source, self.root / relative, ignore=ignored)
        for relative in _EMPTY_RUNTIME_DIRECTORIES:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        return self

    def cleanup(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
        self._temporary_directory = None
        self.root = None

    def subprocess_env(self) -> dict[str, str]:
        if self.root is None:
            raise RuntimeError("isolated project has not been started")
        pycache = self.root / "test_runtime" / "pycache" / uuid.uuid4().hex
        pycache.mkdir(parents=True, exist_ok=False)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(pycache),
                "PYTHONPATH": str(self.root),
            }
        )
        return env

    def __enter__(self) -> "IsolatedProject":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()
