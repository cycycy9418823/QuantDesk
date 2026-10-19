from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


_SOURCE_DIRECTORIES = (
    "quantdesk",
    "ai_trader",
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
        self._startup_fixture: Path | None = None

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
        self._startup_fixture = None

    def subprocess_env(self) -> dict[str, str]:
        if self.root is None:
            raise RuntimeError("isolated project has not been started")
        pycache = self.root / "test_runtime" / "pycache" / uuid.uuid4().hex
        pycache.mkdir(parents=True, exist_ok=False)
        env = os.environ.copy()
        bootstrap = env.get("QUANTDESK_TEST_FENCE_BOOTSTRAP", "")
        entries = ([bootstrap] if bootstrap else []) + [str(self.root)]
        entries += [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                    if entry and entry not in entries and Path(entry).resolve() != self.source_root]
        if self._startup_fixture is not None:
            env["QUANTDESK_TEST_STARTUP_FIXTURE"] = str(self._startup_fixture)
        else:
            env.pop("QUANTDESK_TEST_STARTUP_FIXTURE", None)
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(pycache),
                "PYTHONPATH": os.pathsep.join(entries),
            }
        )
        return env

    def install_startup_fixture(self, fixture: Path) -> None:
        """Load the fixture after any inherited process-isolation hooks."""
        if self.root is None:
            raise RuntimeError("isolated project has not been started")
        copied = self.root / "_quantdesk_startup_fixture.py"
        if self._startup_fixture is not None:
            raise RuntimeError("isolated project already has a startup fixture")
        shutil.copyfile(fixture, copied)
        self._startup_fixture = copied
        if os.environ.get("QUANTDESK_TEST_FENCE_BOOTSTRAP"):
            # The inherited bootstrap restores every fence and then loads this
            # fixture in every descendant. Do not shadow it with sitecustomize.
            return
        inherited = getattr(sys.modules.get("sitecustomize"), "__file__", None)
        lines = ["import runpy"]
        if inherited:
            lines.append(f"runpy.run_path({str(Path(inherited).resolve())!r})")
        lines.append(f"runpy.run_path({str(copied)!r})")
        (self.root / "sitecustomize.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def __enter__(self) -> "IsolatedProject":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()
