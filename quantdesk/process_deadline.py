"""Run independent tasks in killable worker processes with a hard deadline."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from queue import Empty
import time
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class ProcessDeadlineResult:
    results: dict[int, Any]
    failures: dict[int, str]
    unfinished_indices: list[int]
    timed_out: bool
    worker_pids: list[int]


def _worker_main(
    worker: Callable[[Any, dict[str, Any]], Any],
    context: dict[str, Any],
    task_queue: Any,
    result_queue: Any,
) -> None:
    while True:
        item = task_queue.get()
        if item is None:
            return
        index, task = item
        try:
            result = worker(task, context)
        except BaseException as exc:  # noqa: BLE001 - isolate provider failures
            result_queue.put(
                (
                    index,
                    False,
                    f"{type(exc).__name__}:{exc}",
                )
            )
        else:
            result_queue.put((index, True, result))


def _terminate_and_join(processes: Sequence[Any]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=2.0)
    for process in processes:
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
    for process in processes:
        if process.is_alive():
            process.join(timeout=2.0)
    still_alive = [process.pid for process in processes if process.is_alive()]
    if still_alive:
        raise RuntimeError(
            "process_deadline_workers_still_alive:"
            + ",".join(str(pid) for pid in still_alive)
        )


def run_process_tasks_until_deadline(
    worker: Callable[[Any, dict[str, Any]], Any],
    tasks: Sequence[Any],
    *,
    context: dict[str, Any],
    max_workers: int,
    timeout_seconds: float,
) -> ProcessDeadlineResult:
    """Return only after every spawned worker has exited or been killed.

    ``spawn`` is used on every platform so the behavior exercised by tests is
    the same process-isolation model used on Windows. The worker must therefore
    be a module-level picklable callable.
    """

    task_list = list(tasks)
    if not task_list:
        return ProcessDeadlineResult({}, {}, [], False, [])
    timeout = max(0.0, float(timeout_seconds))
    if timeout == 0.0:
        return ProcessDeadlineResult(
            {},
            {},
            list(range(len(task_list))),
            True,
            [],
        )

    process_context = multiprocessing.get_context("spawn")
    task_queue = process_context.Queue()
    result_queue = process_context.Queue()
    worker_count = max(1, min(int(max_workers or 1), len(task_list)))
    for index, task in enumerate(task_list):
        task_queue.put((index, task))
    for _ in range(worker_count):
        task_queue.put(None)

    processes = [
        process_context.Process(
            target=_worker_main,
            args=(worker, context, task_queue, result_queue),
            name=f"quantdesk-provider-{index + 1}",
        )
        for index in range(worker_count)
    ]
    results: dict[int, Any] = {}
    failures: dict[int, str] = {}
    pending = set(range(len(task_list)))
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        for process in processes:
            process.start()
        worker_pids = [
            int(process.pid)
            for process in processes
            if process.pid is not None
        ]
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                index, ok, value = result_queue.get(
                    timeout=min(0.05, remaining)
                )
            except Empty:
                if not any(process.is_alive() for process in processes):
                    break
                continue
            if index not in pending:
                continue
            pending.remove(index)
            if ok:
                results[index] = value
            else:
                failures[index] = str(value)

        if pending and time.monotonic() >= deadline:
            timed_out = True
        if timed_out or pending:
            _terminate_and_join(processes)
        else:
            for process in processes:
                process.join(timeout=2.0)
            _terminate_and_join(processes)
        return ProcessDeadlineResult(
            results=results,
            failures=failures,
            unfinished_indices=sorted(pending),
            timed_out=timed_out,
            worker_pids=worker_pids,
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
        for process in processes:
            try:
                process.close()
            except (OSError, ValueError):
                pass
        for queue in (task_queue, result_queue):
            try:
                queue.cancel_join_thread()
                queue.close()
            except (AttributeError, OSError, ValueError):
                pass


__all__ = ["ProcessDeadlineResult", "run_process_tasks_until_deadline"]
