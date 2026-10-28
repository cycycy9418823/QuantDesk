"""Audit constraints describe only public declared packages and one tested target."""
from pathlib import Path
import re


def test_audit_constraints_cover_declared_direct_dependencies_without_private_packages() -> None:
    root = Path(__file__).resolve().parents[1]
    declared = set()
    for name in ("requirements.txt", "requirements-data.txt", "requirements-dev.txt"):
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                declared.add(re.split(r"[<>=!~;\[]", line.strip())[0].lower().replace("_", "-"))
    constrained = set()
    for line in (root / "requirements-audit.constraints.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r'([A-Za-z0-9-]+)==([0-9.]+) ; python_version == "3.13" and sys_platform == "win32"', line)
        assert match, "Every pin must name a public direct package and the measured target"
        constrained.add(match[1].lower().replace("_", "-"))
    assert constrained == declared
    assert 'requires-python = ">=3.10"' in (root / "pyproject.toml").read_text(encoding="utf-8")
