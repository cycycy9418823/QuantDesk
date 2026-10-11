"""One bounded, resumable, research-only bootstrap job over an explicit CSV.

The selected external job directory must survive a platform rebuild for resume
to work. No market-event replay, scheduler, provider, or ledger is involved.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import math
import os
import platform
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quantdesk import shared
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.research.backtest_statistical_validation import bootstrap_metric_confidence_intervals

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "resumable_bootstrap_job_v1"
IMPLEMENTATION_VERSION = "1"
DEFAULT_ITERATIONS = 500
DEFAULT_SEED = 42
MAX_INPUT_BYTES = 1_048_576
MAX_ROWS = 10_000
MAX_ITERATIONS = 5_000
MAX_WORK = 2_000_000
MAX_INTERNAL_BYTES = 4_194_304
STAGES = ("validate", "compute", "publish")
STAGE_OUTPUTS = {"validate": "validate.json", "compute": "compute.json", "publish": "result.json"}
SOURCE_PATHS = (
    "quantdesk/__init__.py", "quantdesk/research/__init__.py",
    "quantdesk/research/risk_attribution.py", "quantdesk/research/factor_attribution.py",
    "quantdesk/research/backtest_statistical_validation.py",
    "quantdesk/research/resumable_bootstrap_job.py", "scripts/run_resumable_bootstrap_job.py",
    "quantdesk/shared.py", "quantdesk/interprocess_lock.py", "quantdesk/time_utils.py",
    "quantdesk/version.py",
)
_FILES = {"checkpoint.json", "input_snapshot.json", "attempts.json", ".job.lock", *STAGE_OUTPUTS.values()}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class JobError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class JobBusy(JobError):
    pass


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _plain(path: Path, *, directory: bool = False) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise JobError("linked_job_path", str(path))
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise JobError("job_directory_required", str(path))
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise JobError("linked_or_nonregular_job_file", str(path))


def _check_job_tree(job_dir: Path) -> None:
    _plain(job_dir, directory=True)
    entries = list(job_dir.iterdir())
    if len(entries) > 128:
        raise JobError("job_directory_budget_exceeded")
    for item in entries:
        _plain(item)
        if item.name not in _FILES and not any(
            item.name.startswith(name + ".") and item.name.endswith(".tmp") for name in _FILES
        ):
            raise JobError("unexpected_job_file", item.name)


def _read_bytes(path: Path, limit: int = MAX_INTERNAL_BYTES) -> bytes:
    _plain(path)
    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > limit:
                raise JobError("file_budget_exceeded", path.name)
            raw = handle.read(limit + 1)
    except FileNotFoundError as exc:
        raise JobError("required_file_missing", path.name) from exc
    if len(raw) > limit:
        raise JobError("file_budget_exceeded", path.name)
    return raw


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if _canonical(result).encode("ascii") != raw:
            raise ValueError("noncanonical JSON")
        return result
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise JobError("malformed_json", label) from exc


def _atomic_json(path: Path, payload: Any) -> None:
    _plain(path)
    # Compact JSON has no literal newlines, including inside escaped strings;
    # shared.write_text therefore produces identical bytes on Windows/Linux.
    shared.write_text(path, _canonical(payload), trailing_newline=False)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _descriptor(raw: bytes) -> dict[str, Any]:
    return {"sha256": _sha(raw), "bytes": len(raw)}


def _valid_descriptor(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"sha256", "bytes"}
        and isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and type(value["bytes"]) is int and 0 < value["bytes"] <= MAX_INTERNAL_BYTES
    )


def _source_hashes() -> dict[str, str]:
    return {name: _sha((SOURCE_ROOT / name).read_bytes()) for name in SOURCE_PATHS}


def _python_binding() -> dict[str, str]:
    return {"implementation": platform.python_implementation(), "version": sys.version}


def _parameters(iterations: int, seed: int) -> None:
    if type(iterations) is not int or not 1 <= iterations <= MAX_ITERATIONS:
        raise JobError("iterations_out_of_budget", f"expected 1..{MAX_ITERATIONS}")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise JobError("seed_out_of_range", "expected 0..4294967295")


def _binding(raw: bytes, iterations: int, seed: int) -> dict[str, Any]:
    return {
        "input_sha256": _sha(raw), "input_bytes": len(raw),
        "iterations": iterations, "seed": seed,
        "schema_version": SCHEMA_VERSION, "implementation_version": IMPLEMENTATION_VERSION,
        "source_hashes": _source_hashes(), "python": _python_binding(),
    }


def _validate_checkpoint(checkpoint: Any, expected: dict[str, Any], job_id: str) -> None:
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"schema_version", "job_id", "binding", "snapshot", "completed"}:
        raise JobError("malformed_checkpoint")
    if checkpoint["job_id"] != job_id:
        raise JobError("job_id_mismatch")
    if checkpoint["schema_version"] != SCHEMA_VERSION or not _valid_descriptor(checkpoint["snapshot"]):
        raise JobError("checkpoint_schema_mismatch")
    actual = checkpoint["binding"]
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise JobError("malformed_binding")
    for key, value in expected.items():
        if type(actual[key]) is not type(value) or actual[key] != value:
            raise JobError("binding_mismatch", key)
    completed = checkpoint["completed"]
    if not isinstance(completed, dict) or set(completed) not in [set(STAGES[:n]) for n in range(4)]:
        raise JobError("malformed_stage_order")
    if not all(_valid_descriptor(value) for value in completed.values()):
        raise JobError("malformed_stage_descriptor")


def _snapshot_payload(raw: bytes) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "input_sha256": _sha(raw), "base64": base64.b64encode(raw).decode("ascii")}


def _input_from_snapshot(job_dir: Path, checkpoint: dict[str, Any]) -> bytes:
    raw = _read_bytes(job_dir / "input_snapshot.json")
    if _descriptor(raw) != checkpoint.get("snapshot"):
        raise JobError("snapshot_integrity_mismatch")
    payload = _decode_json(raw, "input_snapshot.json")
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "input_sha256", "base64"} or payload["schema_version"] != SCHEMA_VERSION:
        raise JobError("malformed_snapshot")
    try:
        source = base64.b64decode(payload["base64"], validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise JobError("malformed_snapshot") from exc
    binding = checkpoint.get("binding")
    if not isinstance(binding, dict) or len(source) > MAX_INPUT_BYTES or (
        _sha(source) != payload["input_sha256"] or _sha(source) != binding.get("input_sha256")
        or len(source) != binding.get("input_bytes")
    ):
        raise JobError("snapshot_input_mismatch")
    return source


def _validate_returns(raw: bytes, iterations: int) -> list[float]:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise JobError("input_size_out_of_budget")
    try:
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader)
        if (
            not 1 <= len(header) <= 32 or len(set(header)) != len(header)
            or "return" not in header or any(not name or len(name) > 128 for name in header)
        ):
            raise JobError("input_csv_schema", "a unique return column is required")
        column = header.index("return")
        values: list[float] = []
        for row in reader:
            if len(row) != len(header) or any(len(cell) > 256 for cell in row):
                raise JobError("input_csv_schema", "row width or cell length")
            try:
                value = float(row[column])
            except ValueError as exc:
                raise JobError("invalid_return") from exc
            if not math.isfinite(value) or not -1.0 <= value <= 1.0:
                raise JobError("return_out_of_range", "this bounded job supports finite decimal returns in [-1,1]; no rows are clipped or dropped")
            values.append(value)
            if len(values) > MAX_ROWS or len(values) * iterations > MAX_WORK:
                raise JobError("computation_budget_exceeded")
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise JobError("input_csv_schema", str(exc)) from exc
    if not values:
        raise JobError("empty_return_sample")
    if len(values) * math.log1p(max(0.0, max(values))) > 600:
        raise JobError("compounding_overflow_budget")
    return values


def _existing_outputs(job_dir: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for stage in STAGES:
        if stage not in checkpoint["completed"]:
            continue
        name = STAGE_OUTPUTS[stage]
        raw = _read_bytes(job_dir / name)
        if _descriptor(raw) != checkpoint["completed"][stage]:
            raise JobError("stage_output_integrity_mismatch", stage)
        value = _decode_json(raw, name)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or value.get("binding") != checkpoint["binding"]:
            raise JobError("stage_output_schema_mismatch", stage)
        if stage != "publish" and value.get("stage") != stage:
            raise JobError("stage_output_schema_mismatch", stage)
        outputs[stage] = value
    return outputs


def _validate_bootstrap(value: Any, iterations: int, rows: int) -> None:
    intervals = ("annual_return_ci", "sharpe_ci", "max_drawdown_ci", "win_rate_ci")
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "iterations", "status", "warnings", *intervals}
        or value["enabled"] is not True or type(value["iterations"]) is not int
        or value["iterations"] != iterations
        or value["status"] != ("warning" if rows < 8 else "passed")
        or value["warnings"] != (["insufficient_sample"] if rows < 8 else [])
    ):
        raise JobError("computation_output_schema_mismatch")
    for name in intervals:
        bounds = value[name]
        if (
            not isinstance(bounds, list) or len(bounds) != 2
            or any(type(bound) not in (int, float) or not math.isfinite(bound) for bound in bounds)
            or bounds[0] > bounds[1]
        ):
            raise JobError("computation_output_schema_mismatch", name)
    if not -1 <= value["max_drawdown_ci"][0] <= value["max_drawdown_ci"][1] <= 0:
        raise JobError("computation_output_schema_mismatch", "max_drawdown_ci")
    if not 0 <= value["win_rate_ci"][0] <= value["win_rate_ci"][1] <= 1:
        raise JobError("computation_output_schema_mismatch", "win_rate_ci")


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    expected = _canonical(payload).encode("ascii")
    # An output left by a crash before checkpoint publication is reused only
    # when it exactly equals a newly derived deterministic stage result.
    if path.exists() and _read_bytes(path) == expected:
        return
    _atomic_json(path, payload)


def _record_stage(job_dir: Path, checkpoint: dict[str, Any], stage: str, output_path: Path) -> None:
    updated = {**checkpoint, "completed": {**checkpoint["completed"], stage: _descriptor(_read_bytes(output_path))}}
    _atomic_json(job_dir / "checkpoint.json", updated)
    checkpoint.update(updated)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _begin_attempt(job_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = job_dir / "attempts.json"
    receipts = _decode_json(_read_bytes(path), path.name) if path.exists() else {"schema_version": SCHEMA_VERSION, "attempts": []}
    if not isinstance(receipts, dict) or set(receipts) != {"schema_version", "attempts"} or receipts["schema_version"] != SCHEMA_VERSION or not isinstance(receipts["attempts"], list):
        raise JobError("malformed_attempt_receipts")
    if len(receipts["attempts"]) >= 1024:
        raise JobError("attempt_budget_exceeded")
    now = _now()
    for previous in receipts["attempts"]:
        if not isinstance(previous, dict) or previous.get("status") not in {"running", "completed", "failed", "unfinished"}:
            raise JobError("malformed_attempt_receipts")
        if previous["status"] == "running":
            previous.update(status="unfinished", recovered_at=now, ended_at=None)
    attempt = {"attempt": len(receipts["attempts"]) + 1, "started_at": now, "status": "running"}
    receipts["attempts"].append(attempt)
    _atomic_json(path, receipts)
    return receipts, attempt


def _result(binding: dict[str, Any], values: list[float], bootstrap: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "status": "completed", "binding": binding,
        "row_count": len(values), "bootstrap": bootstrap,
        "research_only": True, "validation_status": "研究输出·未验证",
        "interpretation": {
            "annual_return_ci": "Cumulative sample return interval; not annualized.",
            "sharpe_ci": "Sample mean divided by sample population standard deviation; not annualized.",
            "passed": "Computation completed; not evidence of proven strategy effectiveness or future returns.",
            "persistence": "Resume requires the selected job directory to survive; temporary-directory persistence is not guaranteed.",
            "scope": "Fixed input research only; no missed-market-event replay, ledger writes, or broker route.",
        },
    }


def _run_locked(job_dir: Path, input_path: Path | None, iterations: int, seed: int) -> dict[str, Any]:
    _check_job_tree(job_dir)
    supplied = _read_bytes(Path(input_path), MAX_INPUT_BYTES) if input_path is not None else None
    checkpoint_path = job_dir / "checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = _decode_json(_read_bytes(checkpoint_path), checkpoint_path.name)
        if not isinstance(checkpoint, dict):
            raise JobError("malformed_checkpoint")
        if not (job_dir / "input_snapshot.json").exists():
            # Initialization commits the binding first. If interrupted before
            # the snapshot, only the identical explicitly supplied input can
            # finish initialization; no completed stage may exist at this point.
            if supplied is None:
                raise JobError("snapshot_missing_input_required")
            binding = _binding(supplied, iterations, seed)
            _validate_checkpoint(checkpoint, binding, job_dir.name)
            snapshot = _snapshot_payload(supplied)
            if checkpoint["completed"] or checkpoint["snapshot"] != _descriptor(_canonical(snapshot).encode("ascii")):
                raise JobError("snapshot_missing_from_started_job")
            _atomic_json(job_dir / "input_snapshot.json", snapshot)
        source = _input_from_snapshot(job_dir, checkpoint)
        if supplied is not None and _sha(supplied) != _sha(source):
            raise JobError("input_binding_mismatch")
        binding = _binding(source, iterations, seed)
        _validate_checkpoint(checkpoint, binding, job_dir.name)
    else:
        if any((job_dir / name).exists() for name in _FILES - {".job.lock"}):
            raise JobError("checkpoint_missing_from_existing_job")
        if supplied is None:
            raise JobError("input_required_for_new_job")
        source = supplied
        binding = _binding(source, iterations, seed)
        snapshot = _snapshot_payload(source)
        checkpoint = {
            "schema_version": SCHEMA_VERSION, "job_id": job_dir.name, "binding": binding,
            "snapshot": _descriptor(_canonical(snapshot).encode("ascii")), "completed": {},
        }
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(job_dir / "input_snapshot.json", snapshot)
    outputs = _existing_outputs(job_dir, checkpoint)
    receipts, attempt = _begin_attempt(job_dir)
    try:
        # Parsing the bounded frozen input also checks budget and validates a
        # recorded validation payload before its values can reach computation.
        values = _validate_returns(source, iterations)
        validation = {"schema_version": SCHEMA_VERSION, "stage": "validate", "binding": binding, "returns": values}
        if "validate" in outputs and outputs["validate"] != validation:
            raise JobError("validation_output_mismatch")
        if "validate" not in outputs:
            path = job_dir / STAGE_OUTPUTS["validate"]
            _write_output(path, validation)
            _record_stage(job_dir, checkpoint, "validate", path)
        if "compute" in outputs:
            computation = outputs["compute"]
            if set(computation) != {"schema_version", "stage", "binding", "bootstrap"} or not isinstance(computation["bootstrap"], dict):
                raise JobError("computation_output_schema_mismatch")
        else:
            computation = {
                "schema_version": SCHEMA_VERSION, "stage": "compute", "binding": binding,
                "bootstrap": bootstrap_metric_confidence_intervals(values, iterations=iterations, seed=seed),
            }
            _validate_bootstrap(computation["bootstrap"], iterations, len(values))
            path = job_dir / STAGE_OUTPUTS["compute"]
            _write_output(path, computation)
            _record_stage(job_dir, checkpoint, "compute", path)
        _validate_bootstrap(computation["bootstrap"], iterations, len(values))
        result = _result(binding, values, computation["bootstrap"])
        if "publish" in outputs:
            if outputs["publish"] != result:
                raise JobError("result_output_mismatch")
        else:
            path = job_dir / STAGE_OUTPUTS["publish"]
            _write_output(path, result)
            _record_stage(job_dir, checkpoint, "publish", path)
        attempt.update(status="completed", ended_at=_now())
        _atomic_json(job_dir / "attempts.json", receipts)
        return result
    except Exception as exc:
        attempt.update(status="failed", ended_at=_now(), error_code=getattr(exc, "code", type(exc).__name__))
        try:
            _atomic_json(job_dir / "attempts.json", receipts)
        except OSError:
            pass
        raise


def run_job(
    *, job_root: Path, job_id: str, input_path: Path | None = None,
    iterations: int = DEFAULT_ITERATIONS, seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Create or resume this exact job; parameter/source/runtime drift fails closed."""

    _parameters(iterations, seed)
    if not isinstance(job_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job_id) is None or job_id.upper() in _RESERVED:
        raise JobError("invalid_job_id")
    unresolved = Path(job_root).absolute()
    for ancestor in (*reversed(unresolved.parents), unresolved):
        _plain(ancestor, directory=True)
    root = unresolved.resolve()
    if root == SOURCE_ROOT or SOURCE_ROOT in root.parents:
        raise JobError("job_root_must_be_external")
    root.mkdir(parents=True, exist_ok=True)
    job_dir = root / job_id
    _plain(job_dir, directory=True)
    job_dir.mkdir(exist_ok=True)
    _check_job_tree(job_dir)
    try:
        with interprocess_file_lock(job_dir / ".job.lock", timeout_seconds=0):
            return _run_locked(job_dir, input_path, iterations, seed)
    except TimeoutError as exc:
        raise JobBusy("job_busy", job_id) from exc


__all__ = ["run_job", "JobError", "JobBusy"]
