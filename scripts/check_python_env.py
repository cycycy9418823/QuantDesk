from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.shared import now_text, truthy, write_json  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

WINDOWS_LOCAL_MINICONDA_HINT = Path("E:/miniconda/python.exe")
RESULT_JSON = PROJECT_ROOT / "results" / "python_env_latest.json"


def _probe_command(command: str) -> dict[str, Any]:
    executable = shutil.which(command)
    if not executable:
        return {
            "command": command,
            "available": False,
            "executable": "",
            "version": "",
            "failed_reason": "not_found_in_path",
        }
    try:
        completed = subprocess.run(  # noqa: S603 - command comes from fixed allowlist.
            [executable, "--version"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive OS-level failure.
        return {
            "command": command,
            "available": False,
            "executable": executable,
            "version": "",
            "failed_reason": f"{type(exc).__name__}: {exc}",
        }
    output = (completed.stdout or completed.stderr or "").strip()
    return {
        "command": command,
        "available": completed.returncode == 0,
        "executable": executable,
        "version": output,
        "failed_reason": "" if completed.returncode == 0 else f"exit_code={completed.returncode}",
    }


def collect_python_env() -> dict[str, Any]:
    path_python = _probe_command("python")
    py_launcher = _probe_command("py") if os.name == "nt" else {
        "command": "py",
        "available": False,
        "executable": "",
        "version": "",
        "failed_reason": "py_launcher_windows_only",
    }
    warnings: list[str] = []
    if not path_python["available"]:
        warnings.append("python is not available from PATH; use an activated environment, py launcher, or QUANTDESK_PYTHON.")
    stdout_encoding = sys.stdout.encoding or ""
    if stdout_encoding and "utf" not in stdout_encoding.lower():
        warnings.append(f"stdout encoding is {stdout_encoding}; UTF-8 is recommended for QuantDesk logs and Chinese UI text.")
    preferred_encoding = locale.getpreferredencoding(False) or ""
    if preferred_encoding and "utf" not in preferred_encoding.lower() and os.name != "nt":
        warnings.append(f"locale preferred encoding is {preferred_encoding}; UTF-8 locale is recommended.")

    suggested = [
        "Activate your conda/venv environment, then run python scripts/qd.py env-doctor --json true.",
        "On Windows, you can also run py scripts/qd.py env-doctor --json true if the py launcher is installed.",
        "For helper .cmd scripts, set QUANTDESK_PYTHON to the full python.exe path when python is not stable in PATH.",
    ]
    if os.name == "nt":
        suggested.append(r"Example only: set QUANTDESK_PYTHON=E:\miniconda\python.exe")

    return {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "python_env_doctor",
        "generated_at": now_text(),
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "network": False,
        "executable": sys.executable,
        "version": sys.version.splitlines()[0],
        "version_info": {
            "major": sys.version_info.major,
            "minor": sys.version_info.minor,
            "micro": sys.version_info.micro,
        },
        "path_python_available": bool(path_python["available"]),
        "path_python_executable": path_python["executable"],
        "path_python_version": path_python["version"],
        "path_python_failed_reason": path_python["failed_reason"],
        "py_launcher_available": bool(py_launcher["available"]),
        "py_launcher_executable": py_launcher["executable"],
        "py_launcher_version": py_launcher["version"],
        "py_launcher_failed_reason": py_launcher["failed_reason"],
        "local_miniconda_hint_path": str(WINDOWS_LOCAL_MINICONDA_HINT),
        "local_miniconda_hint_exists": WINDOWS_LOCAL_MINICONDA_HINT.exists(),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "locale_preferred_encoding": preferred_encoding,
        "stdout_encoding": stdout_encoding,
        "stderr_encoding": sys.stderr.encoding or "",
        "utf8_mode": int(getattr(sys.flags, "utf8_mode", 0)),
        "cwd": str(Path.cwd()),
        "project_root": str(PROJECT_ROOT),
        "warnings": warnings,
        "suggested_next_action": suggested,
        "output_paths": {"json": str(RESULT_JSON.relative_to(PROJECT_ROOT)).replace("\\", "/")},
        "disclaimer": "Local runtime diagnostics only; no network, no broker connection, no trading action.",
    }


def _print_human(payload: dict[str, Any]) -> None:
    print("QuantDesk Python Environment Doctor")
    print(f"status: {payload.get('status')}")
    print(f"current executable: {payload.get('executable')}")
    print(f"current version: {payload.get('version')}")
    print(f"python from PATH: {payload.get('path_python_available')} {payload.get('path_python_version')}")
    print(f"py launcher: {payload.get('py_launcher_available')} {payload.get('py_launcher_version')}")
    print(f"stdout encoding: {payload.get('stdout_encoding')}")
    print(f"filesystem encoding: {payload.get('filesystem_encoding')}")
    print(f"cwd: {payload.get('cwd')}")
    warnings = payload.get("warnings") or []
    if warnings:
        print("warnings:")
        for item in warnings:
            print(f"  - {item}")
    print("suggested_next_action:")
    for item in payload.get("suggested_next_action") or []:
        print(f"  - {item}")
    print(f"json: {payload.get('output_paths', {}).get('json')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnose the local Python runtime used by QuantDesk.")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--output", default=str(RESULT_JSON.relative_to(PROJECT_ROOT)).replace("\\", "/"))
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; env-doctor never uses network.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect_python_env()
    if truthy(args.network):
        payload.setdefault("warnings", []).append("network flag ignored: env-doctor is local-only.")
        payload["status"] = "warning"
    output = Path(str(args.output))
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    write_json(output, payload)
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
