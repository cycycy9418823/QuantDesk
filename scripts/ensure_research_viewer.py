"""Start or reuse the verified local read-only viewer; never stop a port owner."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _capabilities(port: int) -> dict[str, Any] | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
    try:
        # Loopback only: ignore proxy settings and do not follow redirects.
        connection.request("GET", "/api/runtime-capabilities")
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = json.loads(response.read(65536).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def viewer_identity_matches(
    payload: dict[str, Any] | None, root: Path, *, enable_selected_quotes: bool = False,
) -> bool:
    from scripts.launcher import server_source_build_id

    if not isinstance(payload, dict):
        return False
    identity = payload.get("source_identity")
    if not isinstance(identity, dict):
        return False
    try:
        same_root = bool(identity.get("data_root")) and Path(str(identity["data_root"])).resolve() == root.resolve()
        entry_hash = hashlib.sha256((root / "scripts/run_research_viewer.py").read_bytes()).hexdigest()
    except (OSError, ValueError):
        return False
    return bool(same_root and payload.get("ok") is True
                and payload.get("mode") == "read_only_research"
                and payload.get("read_only") is True and payload.get("mutations_enabled") is False
                and payload.get("selected_quote_refresh") is enable_selected_quotes
                and identity.get("entry_sha256") == entry_hash
                and identity.get("server_build_id") == server_source_build_id(root))


def ensure_research_viewer(
    project_root: Path, log_root: Path, *, port: int = 8000, wait_seconds: float = 30,
    enable_selected_quotes: bool = False,
    start: Callable[..., Any] = subprocess.Popen,
    capabilities: Callable[[int], dict[str, Any] | None] = _capabilities,
    port_open: Callable[[int], bool] = _port_open,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    root, logs = project_root.resolve(), log_root.resolve()
    if not 1 <= port <= 65535 or not (root / "scripts/run_research_viewer.py").is_file():
        raise ValueError("viewer_source_or_port_invalid")
    if logs == root or logs.is_relative_to(root):
        raise ValueError("viewer_logs_must_be_outside_project")
    from quantdesk.interprocess_lock import interprocess_file_lock

    identity = hashlib.sha256(f"{os.path.normcase(str(root))}:{port}".encode("utf-8")).hexdigest()[:24]
    # The installer supplies a stable external log root. Hold the OS lock through
    # readiness so simultaneous clicks cannot both spawn a listener.
    acquired = False
    try:
        with interprocess_file_lock(logs / f"start-{identity}.lock", timeout_seconds=0):
            acquired = True
            return _ensure_research_viewer_locked(
                root, logs, port=port, wait_seconds=wait_seconds,
                enable_selected_quotes=enable_selected_quotes, start=start,
                capabilities=capabilities, port_open=port_open, clock=clock, sleep=sleep,
            )
    except TimeoutError:
        if acquired:
            raise
        return {"status": "startup_in_progress", "ok": False, "port": port, "process_stopped": False}


def _ensure_research_viewer_locked(
    root: Path, logs: Path, *, port: int, wait_seconds: float, enable_selected_quotes: bool,
    start: Callable[..., Any], capabilities: Callable[[int], dict[str, Any] | None],
    port_open: Callable[[int], bool], clock: Callable[[], float], sleep: Callable[[float], None],
) -> dict[str, Any]:
    if viewer_identity_matches(capabilities(port), root, enable_selected_quotes=enable_selected_quotes):
        return {"status": "reused", "ok": True, "port": port, "read_only": True,
                "selected_quote_refresh": enable_selected_quotes}
    if port_open(port):
        return {"status": "foreign_or_unverified_port_owner", "ok": False, "port": port,
                "process_stopped": False}
    configured = os.environ.get("QUANTDESK_PYTHON", "").strip()
    python = Path(configured) if configured else root / ".venv/Scripts/python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    run = logs / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    command = [str(python), "-B", "-X", "utf8", str(root / "scripts/run_research_viewer.py"),
               "--port", str(port), "--data-root", str(root), "--log-root", str(run / "runtime")]
    if enable_selected_quotes:
        command.append("--enable-selected-quotes")
    try:
        with (run / "stdout.log").open("wb") as stdout, (run / "stderr.log").open("wb") as stderr:
            process = start(command, cwd=str(root), stdout=stdout, stderr=stderr,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        result = {"status": "spawn_failed", "ok": False, "port": port, "read_only": True,
                  "error_type": type(exc).__name__, "run_directory": str(run),
                  "selected_quote_refresh": enable_selected_quotes}
        (run / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    result: dict[str, Any] = {"status": "starting", "ok": False, "port": port,
                             "pid": process.pid, "read_only": True, "run_directory": str(run),
                             "selected_quote_refresh": enable_selected_quotes}
    deadline = clock() + max(0.0, wait_seconds)
    while clock() < deadline:
        if viewer_identity_matches(capabilities(port), root, enable_selected_quotes=enable_selected_quotes):
            result.update(status="ready", ok=True)
            break
        if process.poll() is not None:
            result.update(status="exited_before_ready", exit_code=process.poll())
            break
        sleep(min(0.25, max(0.0, deadline - clock())))
    if result["status"] == "starting":
        result["status"] = "readiness_timeout"
    # Keep the receipt outside the data workspace; never mutate launcher runtime state.
    (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--log-root", type=Path, default=Path(tempfile.gettempdir()) / "QuantDesk/viewer-start")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--enable-selected-quotes", action="store_true",
                        help="Explicitly allow only the currently selected stock quote refresh")
    args = parser.parse_args(argv)
    result = ensure_research_viewer(args.project_root, args.log_root, port=args.port,
                                    enable_selected_quotes=args.enable_selected_quotes)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
