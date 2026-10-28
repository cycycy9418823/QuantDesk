"""Repeatable local read-only viewer; no market, account or scheduled writes.

Run with the active interpreter. Network stays off unless the owner explicitly
opens the selected-stock quote endpoint and requests a quote in the page.
"""
from __future__ import annotations

import argparse
import contextvars
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import uuid

SOURCE_ROOT = Path(__file__).resolve().parents[1]
QUOTE_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar("selected_quote", default=False)


def new_run_directory(log_root: Path) -> Path:
    """Every invocation gets an independent receipt; the command is reusable."""
    log_root.mkdir(parents=True, exist_ok=True)
    run = log_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
    run.mkdir()
    return run


def readonly_lock_probe(path: Path) -> bool:
    """Check the existing OS lock without opening its bytes for writing."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False
        except OSError:
            return True
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remap_runtime_inputs(source: Path, data_root: Path, *, app: Any = None) -> None:
    """Point read adapters, including captured route deps, at one data root."""
    visited: set[int] = set()
    def path_value(value: Any, *, root_allowed: bool = False) -> Any:
        if not isinstance(value, Path) or not value.is_absolute():
            return value
        try:
            rel = value.resolve().relative_to(source.resolve())
        except ValueError:
            return value
        if (rel.parts and rel.parts[0] in {"data", "state", "results", "reports", "logs"}) or (not rel.parts and root_allowed):
            return data_root / rel
        return value
    def captured(value: Any) -> Any:
        mapped = path_value(value, root_allowed=True)
        if mapped is not value:
            return mapped
        if is_dataclass(value) and not isinstance(value, type):
            changes = {field.name: captured(getattr(value, field.name)) for field in fields(value)}
            return replace(value, **changes)
        if callable(value) and getattr(value, "__module__", "").startswith(("quantdesk", "scripts.", "ai_trader")):
            if id(value) in visited:
                return value
            visited.add(id(value))
            for cell in getattr(value, "__closure__", None) or ():
                try:
                    old = cell.cell_contents
                except ValueError:
                    continue
                new = captured(old)
                if new is not old:
                    cell.cell_contents = new
        return value
    for module in list(sys.modules.values()):
        if module is None or not module.__name__.startswith(("quantdesk", "scripts.", "ai_trader")):
            continue
        for key, value in list(vars(module).items()):
            mapped = path_value(value, root_allowed=key in {"PROJECT_ROOT", "ROOT", "BASE_DIR", "project_root", "REPO_ROOT"})
            if mapped is not value:
                setattr(module, key, mapped)
    if app is not None:
        for view in app.view_functions.values():
            captured(view)


class ReadOnlyGuard:
    def __init__(self, run: Path, *, port: int, allow_quotes: bool) -> None:
        self.run, self.port, self.allow_quotes = run.resolve(), port, allow_quotes
        self.probe_command: str | None = None

    def writable(self, value: Any) -> bool:
        if isinstance(value, int):
            return True
        try:
            return Path(os.fsdecode(value)).resolve().is_relative_to(self.run)
        except (TypeError, ValueError, OSError):
            return False

    def deny(self, event: str) -> None:
        # Paths, commands and request data may contain secrets; log the category.
        with (self.run / "guard_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "blocked": True}) + "\n")
        raise PermissionError("Read-only research viewer blocked: " + event)

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            path, mode, flags = args
            writes = isinstance(mode, str) and any(c in mode for c in "wax+")
            writes = writes or isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writes and not self.writable(path):
                self.deny(event)
        elif event in {"os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime", "os.link", "os.symlink"}:
            if not self.writable(args[0]):
                self.deny(event)
        elif event in {"os.rename", "os.replace"}:
            if not self.writable(args[0]) or not self.writable(args[1]):
                self.deny(event)
        elif event == "os.mkdir":
            if not self.writable(args[0]) and not Path(args[0]).is_dir():
                self.deny(event)
        elif event == "socket.connect":
            address = args[1]
            if not (self.allow_quotes and QUOTE_SCOPE.get() and isinstance(address, tuple) and address[1] == 443):
                self.deny(event)
        elif event == "socket.getaddrinfo":
            host = str(args[0])
            if host not in {"127.0.0.1", "localhost", "::1", "None"} and not (
                    self.allow_quotes and QUOTE_SCOPE.get() and host == "push2.eastmoney.com"):
                self.deny(event)
        elif event == "socket.bind":
            if args[1] != ("127.0.0.1", self.port):
                self.deny(event)
        elif event == "subprocess.Popen":
            command = args[1]
            text = subprocess.list2cmdline(command) if isinstance(command, (list, tuple)) else str(command)
            if not self.probe_command or text != self.probe_command:
                self.deny(event)
        elif event in {"os.system", "os.startfile", "os.exec", "os.posix_spawn"}:
            self.deny(event)
        elif event == "sqlite3.connect":
            value = str(args[0])
            if value != ":memory:" and not self.writable(value.split("?", 1)[0].removeprefix("file:")):
                if not value.startswith("file:") or "mode=ro" not in value or "immutable=1" not in value:
                    self.deny(event)

    def install(self) -> None:
        original_connect = sqlite3.connect
        def readonly_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
            value = os.fspath(database)
            if value != ":memory:":
                path = value.split("?", 1)[0].removeprefix("file:")
                if not self.writable(path):
                    if not Path(path).is_file():
                        raise PermissionError("Read-only viewer does not create databases")
                    value = Path(path).resolve().as_uri().replace("file:///", "file:/") + "?mode=ro&immutable=1"
                    kwargs["uri"] = True
            return original_connect(value, *args, **kwargs)
        sqlite3.connect = readonly_connect
        sys.addaudithook(self.audit)


def build_app(data_root: Path, guard: ReadOnlyGuard) -> Any:
    sys.path.insert(0, str(SOURCE_ROOT))
    server = importlib.import_module("scripts.quant_api_server")
    # These adapters otherwise import their path constants only on the first
    # request, after remapping has already finished.
    for name in ("ai_trader.paths", "ai_trader.dashboard", "ai_trader.chat", "ai_trader.decision_log"):
        importlib.import_module(name)
    remap_runtime_inputs(SOURCE_ROOT, data_root, app=server.app)
    chain = importlib.import_module("quantdesk.daily_data_chain")
    chain._lock_is_held = readonly_lock_probe
    scheduler = importlib.import_module("quantdesk.daily_scheduler_status")
    health = importlib.import_module("quantdesk.data_health_dashboard")
    remap_runtime_inputs(SOURCE_ROOT, data_root, app=server.app)
    query = scheduler._probe_command(scheduler.CURRENT_TASK_NAME)
    if os.name == "nt":
        query[0] = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    guard.probe_command = subprocess.list2cmdline(query)
    original_status = health.daily_data_scheduler_status
    def readonly_status(**kwargs: Any) -> dict[str, Any]:
        def runner(command: Any, **options: Any) -> Any:
            if command != scheduler._probe_command(scheduler.CURRENT_TASK_NAME):
                raise PermissionError("Unexpected scheduler query")
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            return subprocess.run(query, **options)
        return original_status(**kwargs, runner=runner)
    health.daily_data_scheduler_status = readonly_status
    app = server.app
    app.config.update(RESEARCH_READ_ONLY=True, REALTIME_NETWORK_ENABLED=False,
                      STOCK_QUOTE_REFRESH_ALLOWED=guard.allow_quotes,
                      # Only the selected-stock quote path may use the network,
                      # and only when the owner started the viewer with its flag.
                      STOCK_QUOTE_NETWORK_ENABLED=guard.allow_quotes,
                      QUANTDESK_PROJECT_ROOT=str(data_root),
                      RUNTIME_ENTRY_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    original_fetch = server.fetch_eastmoney_single_quote
    def scoped_fetch(symbol: str) -> dict[str, Any]:
        token = QUOTE_SCOPE.set(True)
        try:
            return original_fetch(symbol)
        finally:
            QUOTE_SCOPE.reset(token)
    server.fetch_eastmoney_single_quote = scoped_fetch
    from flask import request
    @app.before_request
    def _open_quote_scope_for_selected_stock() -> None:
        # The workbench deps captured their fetcher at import time, before the
        # wrapper above existed, so authorize the scope for this request itself.
        if guard.allow_quotes and request.method == "POST" and request.path.endswith("/quote-refresh"):
            QUOTE_SCOPE.set(True)
    @app.after_request
    def _close_quote_scope(response: Any) -> Any:
        QUOTE_SCOPE.set(False)
        return response
    @app.after_request
    def viewer_headers(response: Any) -> Any:
        response.headers["X-QuantDesk-Mode"] = "guarded-read-only"
        response.headers["Content-Security-Policy"] = "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:"
        return response
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--log-root", type=Path, default=Path(tempfile.gettempdir()) / "QuantDesk" / "viewer")
    parser.add_argument("--enable-selected-quotes", action="store_true")
    parser.add_argument("--check", action="store_true", help="Read-only preflight; do not serve")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not args.data_root.is_dir():
        parser.error("A valid local data directory and port are required")
    run = new_run_directory(args.log_root)
    for key in ("TMP", "TEMP", "MPLCONFIGDIR"):
        os.environ[key] = str(run)
    sys.dont_write_bytecode = True
    guard = ReadOnlyGuard(run, port=args.port, allow_quotes=args.enable_selected_quotes)
    guard.install()
    app = build_app(args.data_root.resolve(), guard)
    info = {"source_root": str(SOURCE_ROOT), "data_root": str(args.data_root.resolve()),
            "read_only": True, "selected_quotes": args.enable_selected_quotes,
            "port": args.port, "pid": os.getpid(), "run_directory": str(run)}
    (run / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.check:
        with app.test_client() as client:
            result = client.get("/api/runtime-capabilities")
            assert result.status_code == 200 and result.get_json()["read_only"] is True
            assert client.post("/api/backtest/run", json={}).status_code == 423
            assert client.get("/").status_code == 200
        print(json.dumps({**info, "check": "passed"}, ensure_ascii=False), flush=True)
        return 0
    print(json.dumps(info, ensure_ascii=False), flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
