"""QuantDesk Launcher - EXE entry point.

This script is the main entry point for the QuantDesk desktop application.
When packaged as an EXE via PyInstaller, it:
  1. Locates the project root (bundled or portable layout)
  2. Starts the daily research HTTP server as a child process
  3. Opens the web UI in Edge app-mode or the default browser
  4. Keeps the server alive until the launcher is closed

Usage (development):
    python scripts/launcher.py

Usage (packaged EXE):
    double-click QuantDesk.exe
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path


def resolve_project_root() -> Path:
    """Determine the project root directory.

    When running as a PyInstaller bundle, ``sys._MEIPASS`` points to the
    temporary extraction directory.  The project files are placed alongside
    the EXE in a ``project`` sub-folder.

    When running from source, the project root is two levels above this
    script (``scripts/launcher.py`` -> project root).
    """
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir: EXE is in dist/QuantDesk/, project is next to it
        exe_dir = Path(sys.executable).resolve().parent
        project_candidate = exe_dir / "project"
        if project_candidate.is_dir():
            return project_candidate
        # PyInstaller --onefile: everything extracted to _MEIPASS
        meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        project_candidate = meipass / "project"
        if project_candidate.is_dir():
            return project_candidate
        # Fallback: project files in _MEIPASS root
        return meipass
    # Running from source
    return Path(__file__).resolve().parents[1]


def resolve_python(project_root: Path) -> Path:
    """Find the Python interpreter to use for subprocess scripts."""
    # Prefer the project's own virtual environment
    venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    # Fallback to the interpreter running this launcher
    return Path(sys.executable)


def find_free_port(preferred: int = 8000, max_tries: int = 50) -> int:
    """Find a free local TCP port starting from *preferred*.

    With ``max_tries=1`` this binds the canonical port only (no silent drift to
    a neighbouring port), so http://127.0.0.1:8000/ stays the predictable door.
    """
    span = max(1, int(max_tries))
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free local port found in range {preferred}-{preferred + span - 1}")


def port_is_alive(port: int) -> bool:
    """Check whether a QuantDesk server is already listening on *port*."""
    import urllib.request

    try:
        url = f"http://127.0.0.1:{port}/api/daily/overview"
        with urllib.request.urlopen(url, timeout=1.2) as resp:
            return resp.status == 200
    except Exception:
        pass
    return False


def server_source_build_id(project_root: Path) -> str:
    """Build id of all active API-server Python sources on disk."""
    root = Path(project_root).resolve()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from quantdesk.server_build import compute_server_build_id

    return compute_server_build_id(root)


def running_server_build_id(port: int) -> str | None:
    """Ask a running server for the build id it was started with. Returns None
    when the endpoint is missing (an old server predating this machinery) or
    unreachable — both mean 'treat as outdated'."""
    import urllib.request

    try:
        url = f"http://127.0.0.1:{port}/api/launcher/build-id"
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8")).get("build_id")
    except Exception:
        return None
    return None


def server_is_current(port: int, project_root: Path) -> bool:
    """True only when the running server reports the same build id as the
    source on disk. An old server without the build-id endpoint reports None,
    so it is correctly treated as outdated."""
    running = running_server_build_id(port)
    if not running:
        return False
    return running == server_source_build_id(project_root)


def _listener_pids(port: int) -> list[int]:
    """PIDs of processes LISTENING on *port* (Windows netstat)."""
    pids: list[int] = []
    if os.name != "nt":
        return pids
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return pids
    needle = f":{port}"
    for line in (out or "").splitlines():
        parts = line.split()
        # TCP  127.0.0.1:8000  0.0.0.0:0  LISTENING  1234
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(needle):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return pids


def _pid_is_python(pid: int) -> bool:
    """Check whether *pid* belongs to a Python process (not some unrelated service).

    Uses ``tasklist`` on Windows to read the image name.  Returns True when the
    image name contains 'python' (case-insensitive).  When the check itself
    fails (e.g. access denied, tasklist unavailable) we return None to signal
    'unknown' so the caller can decide whether to proceed with caution.
    """
    if os.name != "nt":
        # POSIX: read /proc/<pid>/cmdline or exe link
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text()
            return "python" in cmdline.lower()
        except Exception:
            return True  # can't verify — assume OK
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or "").strip().lower()
        # CSV format: "python.exe","1234","Console","1","12,345 K"
        return "python" in output
    except Exception:
        return True  # can't verify — assume OK


def kill_server_on_port(port: int) -> bool:
    """Force-kill Python processes listening on *port* and wait for it to free up.

    Only kills processes whose image name contains 'python' to avoid killing
    unrelated services that happen to share the port.  Returns True once the
    port is no longer answering.
    """
    pids = set(_listener_pids(port))
    for pid in pids:
        if not _pid_is_python(pid):
            print(
                f"[QuantDesk] WARNING: PID {pid} on port {port} does not look "
                "like a Python process — skipping kill to avoid collateral damage."
            )
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    for _ in range(24):  # up to ~6s for the socket to release
        if not port_is_alive(port):
            return True
        time.sleep(0.25)
    return not port_is_alive(port)


def ensure_runtime_dirs(project_root: Path) -> None:
    """Create empty local runtime directories without bundling runtime data."""
    for relative in (
        "data/daily",
        "data/tracking",
        "data/benchmark",
        "data/cache",
        "data/universe",
        "results",
        "reports",
        "state",
        "logs",
    ):
        (project_root / relative).mkdir(parents=True, exist_ok=True)


def write_startup_status(project_root: Path, payload: dict) -> None:
    """Write launcher-level startup update status for the UI to display."""
    target = project_root / "results" / "startup_update_latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def skipped_startup_status(project_root: Path, reason: str) -> None:
    write_startup_status(
        project_root,
        {
            "task_id": "startup_data_update",
            "status": "skipped",
            "reason": reason,
            "startup_update_attempted": False,
            "provider_network_authorized": False,
            "network": False,
            "dry_run": True,
            "scope": "current_analysis_pool",
            "only_low_price": True,
            "full_scan": False,
            "updated_symbols": 0,
            "failed_symbols": 0,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "latest_local_date_after": "",
            "message": "Startup data update is disabled for this launch.",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "disclaimer": "Research-only local app. No broker route, no live trading, no orders, no investment advice.",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
            "no_buy_sell_advice": True,
            "no_position_advice": True,
            "no_real_money_allocation": True,
        },
    )


def running_startup_status(project_root: Path, *, max_symbols: int) -> None:
    write_startup_status(
        project_root,
        {
            "task_id": "startup_data_update",
            "status": "running",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "network": True,
            "dry_run": False,
            "scope": "current_analysis_pool",
            "only_low_price": True,
            "full_scan": False,
            "max_symbols": max(0, int(max_symbols or 0)),
            "updated_symbols": 0,
            "failed_symbols": 0,
            "stale_count_after": 0,
            "missing_count_after": 0,
            "latest_local_date_after": "",
            "message": "Server is ready. Startup data update is running in the background.",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "disclaimer": "Research-only local app. No broker route, no live trading, no orders, no investment advice.",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
            "no_buy_sell_advice": True,
            "no_position_advice": True,
            "no_real_money_allocation": True,
        },
    )


def run_startup_update(
    project_root: Path,
    *,
    enabled: bool,
    max_symbols: int,
    timeout_seconds: int,
    providers: str,
    generate_candidates: bool = True,
    candidate_max_scan_symbols: int = 300,
    candidate_max_candidates: int = 18,
) -> dict:
    """Run bounded provider update before the UI opens.

    The launcher treats provider/update failures as visible warnings, not as a
    reason to fabricate freshness. A fatal script failure is written to the
    same status JSON and the app still opens with stale-data warnings.
    """
    if not enabled:
        skipped_startup_status(project_root, "update_not_enabled")
        return {"status": "skipped", "message": "Startup update disabled."}

    script = project_root / "scripts" / "startup_data_update.py"
    if not script.exists():
        payload = {
            "task_id": "startup_data_update",
            "status": "failed",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "network": True,
            "dry_run": False,
            "scope": "current_analysis_pool",
            "only_low_price": True,
            "full_scan": False,
            "message": f"Startup update script not found: {script}",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
            "no_buy_sell_advice": True,
            "no_position_advice": True,
            "no_real_money_allocation": True,
        }
        write_startup_status(project_root, payload)
        return payload

    python = resolve_python(project_root)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    command = [
        str(python),
        str(script),
        "--root",
        str(project_root),
        "--enable-network",
        "--max-symbols",
        str(max(0, int(max_symbols or 0))),
        "--rebuild-features",
        "true",
        "--json",
        "true",
    ]
    if generate_candidates:
        command.extend(
            [
                "--generate-candidates",
                "true",
                "--candidate-max-scan-symbols",
                str(max(1, int(candidate_max_scan_symbols or 1))),
                "--candidate-max-candidates",
                str(max(1, int(candidate_max_candidates or 1))),
            ]
        )
    if providers:
        command.extend(["--providers", providers])

    try:
        result = subprocess.run(
            command,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, int(timeout_seconds or 10)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        payload = {
            "task_id": "startup_data_update",
            "status": "timeout",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "network": True,
            "dry_run": False,
            "scope": "current_analysis_pool",
            "only_low_price": True,
            "full_scan": False,
            "max_symbols": max(0, int(max_symbols or 0)),
            "message": f"Startup update timed out after {timeout_seconds} seconds.",
            "stdout_tail": (exc.stdout or "")[-1200:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-1200:] if isinstance(exc.stderr, str) else "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
            "no_buy_sell_advice": True,
            "no_position_advice": True,
            "no_real_money_allocation": True,
        }
        write_startup_status(project_root, payload)
        return payload

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    payload: dict
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except Exception:
        payload = {}
    if not payload:
        payload = {
            "task_id": "startup_data_update",
            "status": "failed" if result.returncode else "completed_with_warnings",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "network": True,
            "dry_run": False,
            "scope": "current_analysis_pool",
            "only_low_price": True,
            "full_scan": False,
            "max_symbols": max(0, int(max_symbols or 0)),
            "message": "Startup update finished without a readable JSON payload.",
            "stdout_tail": stdout[-1200:],
            "stderr_tail": stderr[-1200:],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_live_trading": True,
            "no_auto_execution": True,
            "no_buy_sell_advice": True,
            "no_position_advice": True,
            "no_real_money_allocation": True,
        }
        write_startup_status(project_root, payload)
    if result.returncode != 0:
        payload["launcher_warning"] = f"startup updater exited with code {result.returncode}; app will open with visible data status."
        write_startup_status(project_root, payload)
    return payload


def launch_background_startup_update(
    project_root: Path,
    *,
    enabled: bool,
    max_symbols: int,
    timeout_seconds: int,
    providers: str,
    generate_candidates: bool = True,
    candidate_max_scan_symbols: int = 300,
    candidate_max_candidates: int = 18,
) -> threading.Thread | None:
    if not enabled:
        skipped_startup_status(project_root, "update_not_enabled")
        return None

    running_startup_status(project_root, max_symbols=max_symbols)

    def _worker() -> None:
        update_status = run_startup_update(
            project_root,
            enabled=True,
            max_symbols=max_symbols,
            timeout_seconds=timeout_seconds,
            providers=providers,
            generate_candidates=generate_candidates,
            candidate_max_scan_symbols=candidate_max_scan_symbols,
            candidate_max_candidates=candidate_max_candidates,
        )
        candidate_generation = (
            update_status.get("candidate_generation")
            if isinstance(update_status.get("candidate_generation"), dict)
            else {}
        )
        print(
            "[QuantDesk] Background startup update status: "
            f"{update_status.get('status', '-')}; updated={update_status.get('updated_symbols', 0)}; "
            f"stale_after={update_status.get('stale_count_after', 0)}; "
            f"missing_after={update_status.get('missing_count_after', 0)}; "
            f"generated_candidates={candidate_generation.get('candidate_count', 0)}; "
            f"new_ledger_rows={candidate_generation.get('new_ledger_rows', 0)}"
        )

    thread = threading.Thread(
        target=_worker,
        name="quantdesk-startup-update",
        daemon=True,
    )
    thread.start()
    return thread


def find_edge_exe() -> str | None:
    """Locate Microsoft Edge for app-mode browsing."""
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_key, "")
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.exists():
                return str(candidate)
    return None


def open_app_window(url: str) -> None:
    """Open the QuantDesk web UI.

    A per-launch cache-buster (`?v=<ns>`) is appended so each launch is a NEW
    navigation: Edge app-mode otherwise just re-focuses an already-open window
    for the same URL, leaving the user staring at a stale cached page (the
    recurring "I relaunched but the UI never changed" trap). The server ignores
    the unknown query param.
    """
    sep = "&" if "?" in url else "?"
    fresh_url = f"{url}{sep}v={time.time_ns()}"
    edge = find_edge_exe()
    if edge:
        subprocess.Popen(
            [
                edge,
                f"--app={fresh_url}",
                "--window-size=1460,940",
                "--disable-features=Translate",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        webbrowser.open(fresh_url)


def start_server(project_root: Path, port: int, *, realtime_network: bool = False) -> subprocess.Popen | None:
    """Start the QuantDesk API server as a subprocess.

    Returns the process handle so the launcher can terminate it on exit.
    """
    python = resolve_python(project_root)
    server_script = project_root / "scripts" / "quant_api_server.py"
    if not server_script.exists():
        print(f"[QuantDesk] Server script not found: {server_script}")
        return None

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    command = [str(python), str(server_script), "--port", str(port)]
    if realtime_network:
        command.append("--enable-realtime-network")

    process = subprocess.Popen(
        command,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # Stream server output to console (useful for debugging)
    def _stream_output() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            print(f"[server] {line}", end="", flush=True)

    thread = threading.Thread(target=_stream_output, daemon=True)
    thread.start()
    return process


def wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """Wait until the server responds on *port*."""
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/daily/overview", timeout=1.0
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantDesk Desktop Launcher")
    parser.add_argument("--port", type=int, default=8000, help="Preferred HTTP port (default: 8000)")
    parser.add_argument("--allow-fallback-port", action="store_true", help="If the preferred port is busy and cannot be freed, use the next free port instead of failing. Default off, so http://127.0.0.1:8000/ stays predictable.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--update-on-start", action="store_true", help="Request the full-A Data Close Pipeline when a completed session is available")
    parser.add_argument("--no-update", action="store_true", help="Skip startup data update even when configured")
    parser.add_argument("--update-max-symbols", type=int, default=80, help="Maximum current-pool symbols to update at startup")
    parser.add_argument("--update-timeout", type=int, default=180, help="Startup update timeout in seconds")
    parser.add_argument("--update-providers", default="", help="Comma-separated daily providers, default provider chain if empty")
    parser.add_argument("--no-generate-candidates", action="store_true", help="Skip bounded local daily candidate generation after startup update")
    parser.add_argument("--candidate-max-scan-symbols", type=int, default=300, help="Maximum local daily files to scan for daily recommendations")
    parser.add_argument("--candidate-max-candidates", type=int, default=18, help="Maximum newly generated daily research candidates")
    args = parser.parse_args()

    project_root = resolve_project_root()
    print(f"[QuantDesk] Project root: {project_root}")
    ensure_runtime_dirs(project_root)

    update_enabled = bool(args.update_on_start and not args.no_update)
    if update_enabled:
        print(
            "[QuantDesk] Startup data update enabled: the 8000 service will start first, "
            "then request the full-A Data Close Pipeline."
        )
    else:
        print("[QuantDesk] Startup data update skipped. Use --update-on-start to enable bounded provider refresh.")
    if not update_enabled:
        skipped_startup_status(project_root, "update_not_enabled")

    # --- Port strategy: keep http://127.0.0.1:8000/ the single, predictable door. ---
    # Reuse the running server ONLY when it reports the CURRENT build id. A server
    # that predates the latest edits serves a stale in-memory template no matter how
    # often the browser refreshes, and a foreign (non-QuantDesk) process must never
    # be mistaken for "our server". Anything else: free the port or fail loudly,
    # instead of silently drifting to another port (the old "8000 vs 800x" trap).
    if port_is_alive(args.port):
        if server_is_current(args.port, project_root):
            url = f"http://127.0.0.1:{args.port}/"
            print(f"[QuantDesk] Server already running (current build) at {url}")
            if not args.no_browser:
                open_app_window(url)
            if update_enabled:
                print("[QuantDesk] Existing server detected; running the bounded startup update now.")
                update_status = run_startup_update(
                    project_root,
                    enabled=True,
                    max_symbols=args.update_max_symbols,
                    timeout_seconds=args.update_timeout,
                    providers=args.update_providers,
                    generate_candidates=not args.no_generate_candidates,
                    candidate_max_scan_symbols=args.candidate_max_scan_symbols,
                    candidate_max_candidates=args.candidate_max_candidates,
                )
                candidate_generation = (
                    update_status.get("candidate_generation")
                    if isinstance(update_status.get("candidate_generation"), dict)
                    else {}
                )
                print(
                    "[QuantDesk] Startup update status: "
                    f"{update_status.get('status', '-')}; updated={update_status.get('updated_symbols', 0)}; "
                    f"stale_after={update_status.get('stale_count_after', 0)}; "
                    f"missing_after={update_status.get('missing_count_after', 0)}; "
                    f"generated_candidates={candidate_generation.get('candidate_count', 0)}; "
                    f"new_ledger_rows={candidate_generation.get('new_ledger_rows', 0)}"
                )
            return 0
        # Occupied by an outdated build or a foreign process — try to free the
        # canonical port so the latest UI is served on it.
        print(
            f"[QuantDesk] Port {args.port} is occupied by an outdated or foreign "
            "server; trying to free it so the latest UI is served on the canonical port."
        )
        if not kill_server_on_port(args.port) and not args.allow_fallback_port:
            print(
                f"[QuantDesk] ERROR: port {args.port} is in use and could not be freed. "
                f"Close whatever is using port {args.port}, or relaunch with "
                "--allow-fallback-port to use another port. Refusing to silently "
                "change the URL so http://127.0.0.1:8000/ stays predictable."
            )
            return 2

    # Bind the canonical port. Only drift to a neighbouring port when explicitly
    # allowed via --allow-fallback-port.
    try:
        port = find_free_port(args.port, max_tries=50 if args.allow_fallback_port else 1)
    except RuntimeError:
        print(
            f"[QuantDesk] ERROR: port {args.port} is not available. Close what is "
            "using it, or relaunch with --allow-fallback-port. Not starting on a "
            "different port by default so the URL stays predictable."
        )
        return 2
    url = f"http://127.0.0.1:{port}/"
    if port != args.port:
        print(f"[QuantDesk] NOTE: preferred port {args.port} was busy; using fallback port {port}.")
    print(f"[QuantDesk] Starting server on port {port}...")

    # Start the server
    server_process = start_server(project_root, port, realtime_network=update_enabled)
    if server_process is None:
        print("[QuantDesk] Failed to start server.")
        return 1

    # Wait for server to become ready
    print("[QuantDesk] Waiting for server to be ready...")
    if not wait_for_server(port):
        print("[QuantDesk] Server did not respond within timeout. Check logs above.")
        server_process.terminate()
        return 1

    print(f"[QuantDesk] Server ready at {url}")

    # Open browser
    if not args.no_browser:
        threading.Timer(0.5, lambda: open_app_window(url)).start()

    if update_enabled:
        launch_background_startup_update(
            project_root,
            enabled=True,
            max_symbols=args.update_max_symbols,
            timeout_seconds=args.update_timeout,
            providers=args.update_providers,
            generate_candidates=not args.no_generate_candidates,
            candidate_max_scan_symbols=args.candidate_max_scan_symbols,
            candidate_max_candidates=args.candidate_max_candidates,
        )

    # Keep launcher alive and handle shutdown
    print("[QuantDesk] QuantDesk is running. Close this window or press Ctrl+C to stop.")

    def _shutdown(signum: int = 0, frame: object = None) -> None:
        print("\n[QuantDesk] Shutting down...")
        if server_process.poll() is None:
            # Try graceful shutdown via API
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/shutdown", timeout=3
                )
            except Exception:
                pass
            time.sleep(1)
            # Force kill if still running
            if server_process.poll() is None:
                server_process.terminate()
                try:
                    server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server_process.kill()
        print("[QuantDesk] Goodbye.")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        # Wait for the server process to exit (it runs until shutdown)
        server_process.wait()
    except KeyboardInterrupt:
        _shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
