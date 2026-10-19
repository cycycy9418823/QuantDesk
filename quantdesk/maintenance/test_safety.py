"""Prevent tests and their Python children from modifying real runtime evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlsplit

_RUNTIME_DIRS = {"data", "state", "results", "reports", "logs", "dist"}
# The startup loader imports this file without importing the application
# package. A later normal package import must share the same process registry.
_REGISTRY = getattr(sys, "_quantdesk_test_fence_registry", None)
if _REGISTRY is None:
    _REGISTRY = {"installed": set(), "configs": [], "bootstrap_dir": None, "fixture_path": None}
    setattr(sys, "_quantdesk_test_fence_registry", _REGISTRY)
_INSTALLED: set[tuple[str, ...]] = _REGISTRY["installed"]
_FENCE_CONFIGS: list[dict] = _REGISTRY["configs"]
_BOOTSTRAP_DIR: str | None = _REGISTRY["bootstrap_dir"]


def _run_startup_fixture() -> None:
    """Load a test-owned fixture once per interpreter, after all fences exist."""
    fixture = os.environ.get("QUANTDESK_TEST_STARTUP_FIXTURE", "")
    if not fixture:
        return
    path = str(Path(fixture).resolve(strict=True))
    previous = _REGISTRY["fixture_path"]
    if previous == path:
        return
    if previous is not None:
        raise ValueError("test_startup_fixture_changed_in_process")
    _REGISTRY["fixture_path"] = path
    import runpy
    runpy.run_path(path)


def _publish_child_bootstrap() -> None:
    """Direct pytest and isolated runs both propagate the same Python guard."""
    global _BOOTSTRAP_DIR
    _BOOTSTRAP_DIR = _REGISTRY["bootstrap_dir"]
    if _BOOTSTRAP_DIR is None:
        inherited = os.environ.get("QUANTDESK_TEST_FENCE_BOOTSTRAP")
        if inherited and (Path(inherited) / "sitecustomize.py").is_file():
            _BOOTSTRAP_DIR = inherited
        else:
            directory = Path(tempfile.mkdtemp(prefix="quantdesk-test-fence-"))
            (directory / "sitecustomize.py").write_text(
                "import importlib.util,sys\nsys.dont_write_bytecode=True\n"
                "try:\n"
                f" spec=importlib.util.spec_from_file_location('_quantdesk_test_fence_runtime', {str(Path(__file__).resolve())!r})\n"
                " guard=importlib.util.module_from_spec(spec)\n sys.modules[spec.name]=guard\n"
                " spec.loader.exec_module(guard)\n guard.restore_test_fence()\n guard._run_startup_fixture()\n"
                "except BaseException as error:\n raise SystemExit('test_fence_bootstrap_failed:'+type(error).__name__)\n",
                encoding="utf-8")
            _BOOTSTRAP_DIR = str(directory)
        _REGISTRY["bootstrap_dir"] = _BOOTSTRAP_DIR
    os.environ["QUANTDESK_TEST_FENCE_BOOTSTRAP"] = _BOOTSTRAP_DIR
    os.environ["QUANTDESK_TEST_FENCE_STATE"] = json.dumps(_FENCE_CONFIGS, sort_keys=True)
    entries = [_BOOTSTRAP_DIR]
    for value in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if value and value not in entries:
            entries.append(value)
    source = str(Path(__file__).resolve().parents[2])
    if source not in entries:
        entries.append(source)
    os.environ["PYTHONPATH"] = os.pathsep.join(value for value in entries if value)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def restore_test_fence() -> None:
    """Fail closed if a Python child cannot restore the parent test protection."""
    configs = json.loads(os.environ["QUANTDESK_TEST_FENCE_STATE"])
    if not isinstance(configs, list) or not configs:
        raise ValueError("test_fence_state_missing")
    for config in configs:
        install_test_write_fence([Path(value) for value in config["roots"]], all_files=config["all_files"])


def _process_command(executable: object, argv: object) -> tuple[str, list[str]]:
    """Decode the actual Windows audit event, including executable=None."""
    if isinstance(argv, (list, tuple)):
        tokens = [os.fsdecode(value) for value in argv]
    elif isinstance(argv, (str, bytes)):
        command = os.fsdecode(argv)
        if os.name == "nt":
            import ctypes
            count = ctypes.c_int()
            shell = ctypes.WinDLL("shell32", use_last_error=True)
            parse = shell.CommandLineToArgvW
            parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
            allocated = parse(command, ctypes.byref(count))
            if not allocated:
                raise PermissionError("test_process_command_line_unreadable")
            try:
                tokens = [allocated[index] for index in range(count.value)]
            finally:
                free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
                free.argtypes = [ctypes.c_void_p]
                free.restype = ctypes.c_void_p
                free(ctypes.cast(allocated, ctypes.c_void_p))
        else:
            import shlex
            tokens = shlex.split(command)
    else:
        raise PermissionError("test_process_arguments_unreadable")
    program = os.fsdecode(executable) if executable is not None else (tokens[0] if tokens else "")
    return Path(program).stem.lower(), tokens


def install_test_write_fence(roots: Iterable[Path], *, all_files: bool = False) -> None:
    """Install an additive CPython audit hook; existing tripwires stay enabled."""
    protected = tuple(sorted({str(Path(root).resolve()) for root in roots}))
    key = (*protected, str(all_files))
    if not protected or key in _INSTALLED:
        return
    _INSTALLED.add(key)
    _FENCE_CONFIGS.append({"roots": list(protected), "all_files": all_files})
    resolved_roots = [Path(root) for root in protected]

    def check(value: object) -> None:
        if isinstance(value, int) or value is None:
            return
        try:
            path = Path(os.fsdecode(value)).resolve()
        except (TypeError, ValueError, OSError):
            return
        for root in resolved_roots:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if all_files or (relative.parts and relative.parts[0].lower() in _RUNTIME_DIRS):
                raise PermissionError(
                    f"test_runtime_write_blocked:{path}; inject tmp_path or run scripts/run_tests_isolated.py"
                )

    def audit(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args[:3]
            writing = bool(isinstance(mode, str) and set(mode) & set("wax+"))
            writing = writing or bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                check(path)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            check(args[0])
        elif event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
            check(args[0])
            check(args[1])
        elif event == "sqlite3.connect":
            path = args[0]
            if isinstance(path, str) and path.startswith("file:"):
                uri = urlsplit(path)
                mode = parse_qs(uri.query).get("mode", [])
                if mode in (["ro"], ["memory"]):
                    return
                path = unquote(uri.path)
                if uri.netloc and uri.netloc.lower() != "localhost":
                    path = "//" + uri.netloc + path
                elif re.match(r"^/[A-Za-z]:/", path):
                    path = path[1:]
            if path != ":memory:":
                check(path)
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            host = args[1][0] if event == "socket.connect" and isinstance(args[1], tuple) else args[0] if event == "socket.getaddrinfo" else None
            if host is not None and str(host).lower() not in {"localhost", "::1"} and not str(host).startswith("127."):
                raise PermissionError("test_external_network_blocked")
        elif event == "os.system":
            raise PermissionError("test_os_system_blocked_use_guarded_subprocess")
        elif event == "subprocess.Popen":
            executable, argv = args[:2]
            name, tokens = _process_command(executable, argv)
            text = " ".join(tokens)
            lower = text.lower()
            if name in {"ssh", "scp", "sftp", "curl", "wget", "plink", "pscp", "taskkill"}:
                raise PermissionError("test_external_process_blocked")
            mutations = ("register-scheduledtask", "unregister-scheduledtask", "start-scheduledtask", "stop-scheduledtask", "enable-scheduledtask", "disable-scheduledtask", "set-scheduledtask", "invoke-webrequest", "invoke-restmethod", "stop-process")
            if name in {"powershell", "pwsh"} and any(word in lower for word in mutations):
                raise PermissionError("test_task_or_external_action_blocked")
            if name == "schtasks" and any(token in lower.split() for token in ("/run", "/change", "/create", "/delete", "/end")):
                raise PermissionError("test_task_mutation_blocked")
            if name in {"cmd", "powershell", "pwsh", "sh", "bash"}:
                forbidden = r"(?<![\w-])(?:taskkill|stop-process|pkill|killall|register-scheduledtask|unregister-scheduledtask|start-scheduledtask|stop-scheduledtask|enable-scheduledtask|disable-scheduledtask|set-scheduledtask)(?:\.exe)?(?![\w-])"
                if re.search(forbidden, lower) or re.search(r"schtasks(?:\.exe)?\b.*?/(?:run|change|create|delete|end)\b", lower):
                    raise PermissionError("test_indirect_task_or_process_mutation_blocked")
                if re.search(r"(?:^|\s)-(?:enc|encodedcommand)\b", lower):
                    raise PermissionError("test_encoded_shell_command_blocked")
            if name == "py" or name.startswith("python"):
                skip_value = False
                for token in tokens[1:]:
                    token = str(token)
                    if skip_value:
                        skip_value = False
                        continue
                    if token in {"-c", "-m", "--"} or not token.startswith("-"):
                        break
                    if token == "--check-hash-based-pycs":
                        skip_value = True
                        continue
                    if token.startswith("--"):
                        continue
                    end_options = False
                    options = token[1:]
                    for index, option in enumerate(options):
                        if option in "ISE":
                            raise PermissionError("test_python_bootstrap_bypass_blocked")
                        if option in "XW":
                            # Python also accepts clusters such as -BX utf8.
                            # Everything after X/W in the same token is its
                            # value, otherwise the next token is the value.
                            skip_value = index == len(options) - 1
                            break
                        if option in "cm":
                            end_options = True
                            break
                    if end_options:
                        break
                environment = args[3] if len(args) > 3 and args[3] is not None else os.environ
                if _BOOTSTRAP_DIR and (environment.get("QUANTDESK_TEST_FENCE_STATE") != os.environ.get("QUANTDESK_TEST_FENCE_STATE")
                        or not str(environment.get("PYTHONPATH", "")).split(os.pathsep)[0] == _BOOTSTRAP_DIR):
                    raise PermissionError("test_python_child_missing_fence_bootstrap")

    sys.addaudithook(audit)
    _publish_child_bootstrap()


def configure_pytest_fence(root: Path) -> None:
    """Direct pytest refuses real writes; a runner-created disposable tree may write."""
    root = root.resolve()
    extra = [Path(value) for value in os.environ.get("QUANTDESK_TEST_PROTECTED_ROOTS", "").split(os.pathsep) if value]
    sandbox = os.environ.get("QUANTDESK_TEST_SANDBOX", "")
    isolated = False
    if sandbox and Path(sandbox).resolve() == root:
        try:
            marker = json.loads((root / ".quantdesk-test-sandbox.json").read_text(encoding="utf-8"))
            source = Path(marker["source_root"]).resolve()
            isolated = source != root and marker.get("sandbox_root") == str(root)
            if isolated:
                extra.append(source)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    install_test_write_fence(extra, all_files=True)
    if not isolated:
        install_test_write_fence([root])
