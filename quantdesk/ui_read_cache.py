"""Bounded, byte-identified caches and projections for local read-only UI views."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import json
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable, Hashable, TypeVar

T = TypeVar("T")


class InputChangedDuringRead(RuntimeError):
    """An input changed while its result was being constructed."""


class ReadCacheWaitTimeout(TimeoutError):
    """A follower stopped waiting; the original read still owns its work."""


@dataclass
class _Entry:
    value: Any
    expires: float


class ReadCache:
    """Single-flight per exact identity, bounded TTL, and no cached exceptions."""

    def __init__(self, *, max_entries: int = 32, ttl_seconds: float = 5.0,
                 wait_timeout_seconds: float = 10.0) -> None:
        if not math.isfinite(wait_timeout_seconds) or wait_timeout_seconds <= 0:
            raise ValueError("read_cache_wait_timeout_must_be_finite_and_positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self._lock = RLock()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._pending: dict[Hashable, Future] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key: Hashable, build: Callable[[], T], *,
            admit: Callable[[T], bool] | None = None, copy: bool = True) -> T:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires > time.monotonic():
                self._entries.move_to_end(key)
                return deepcopy(entry.value) if copy else entry.value
            self._entries.pop(key, None)
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._pending[key] = future
        if not owner:
            try:
                result = future.result(timeout=self.wait_timeout_seconds)
            except FutureTimeoutError as exc:
                # Do not cancel the shared owner or cache a timeout as data.
                raise ReadCacheWaitTimeout("ui_read_cache_wait_timeout") from exc
            return deepcopy(result) if copy else result
        try:
            result = build()
            with self._lock:
                if admit is None or admit(result):
                    self._entries[key] = _Entry(deepcopy(result) if copy else result,
                        time.monotonic() + self.ttl_seconds)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
                future.set_result(result)
            return deepcopy(result) if copy else result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)


UI_READ_CACHE = ReadCache()
# Parsed file values stay useful across a slow response build. Every lookup
# still hashes source bytes before selecting this cache entry.
UI_FILE_CACHE = ReadCache(max_entries=64, ttl_seconds=60.0)


def byte_identity(paths: list[Path], *, target_date: str = "", account: str = "",
                  scope: str = "", code_id: str = "", config_id: str = "") -> dict[str, Any]:
    """Hash explicit input bytes on EACH lookup, including same-stat rewrites.

    No directory is recursively read, and secret config/keys are not inputs.
    A run's persisted JSON hash binds its recorded run ID and configuration.
    """
    rows = []
    for path in sorted(set(paths), key=str):
        if path.is_dir():
            before = path.stat()
            names = sorted(item.name for item in path.iterdir())
            after = path.stat()
            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                raise InputChangedDuringRead("directory_changed_during_hash:" + str(path))
            rows.append({"path": str(path), "exists": True, "type": "directory",
                "sha256": hashlib.sha256(json.dumps(names).encode()).hexdigest(), "entry_count": len(names)})
            continue
        before = path.stat() if path.is_file() else None
        if before is None:
            rows.append({"path": str(path), "exists": False, "sha256": ""})
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise InputChangedDuringRead("source_changed_during_hash:" + str(path))
        rows.append({"path": str(path), "exists": True, "sha256": digest.hexdigest(), "bytes": after.st_size})
    identity = {"target_date": target_date, "account": account, "scope": scope,
        "code_id": code_id, "config_id": config_id, "inputs": rows}
    identity["sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return identity


def cached_file_value(paths: list[Path], build: Callable[[], T], *, scope: str,
                      target_date: str = "", code_id: str = "", config_id: str = "") -> T:
    identity = byte_identity(paths, scope=scope, target_date=target_date, code_id=code_id, config_id=config_id)
    def verified_build() -> T:
        value = build()
        if byte_identity(paths, scope=scope, target_date=target_date, code_id=code_id, config_id=config_id) != identity:
            raise InputChangedDuringRead("source_changed_during_build")
        return value
    return UI_FILE_CACHE.get((scope, identity["sha256"]), verified_build)


def summary_projection(payload: dict[str, Any], *, max_items: int = 8,
                       max_depth: int = 5) -> dict[str, Any]:
    """Keep status/scalars; explicitly identify every deferred collection/text.

    Detail pointers address the original payload, so missing evidence never
    becomes a fabricated normal value. Warnings carry counts and a detail path.
    """
    omissions: list[dict[str, Any]] = []
    def child(path: str, key: Any) -> str:
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")
    def compact(value: Any, path: str, depth: int) -> Any:
        if isinstance(value, dict):
            keys = list(value)
            if depth >= max_depth or len(keys) > 100:
                scalar = {k: v for k, v in value.items() if not isinstance(v, (dict, list))}
                kept = [k for k in scalar if len(keys) <= 100 or k in {"ok", "status", "reason", "reason_code", "run_id",
                    "target_date", "as_of", "date", "source", "sha256", "generated_at", "updated_at"}
                    or str(k).endswith(("_count", "_ratio", "_date", "_status"))][:100]
                omissions.append({"pointer": path, "type": "object", "total": len(keys), "returned": len(kept)})
                return {k: compact(scalar[k], child(path, k), depth+1) for k in kept}
            return {k: compact(v, child(path, k), depth+1) for k, v in value.items()}
        if isinstance(value, list):
            limit = max_items if depth < max_depth else 0
            if len(value) > limit:
                omissions.append({"pointer": path, "type": "array", "total": len(value), "returned": limit})
            return [compact(v, child(path, i), depth+1) for i,v in enumerate(value[:limit])]
        if isinstance(value, str) and len(value) > 2000:
            omissions.append({"pointer": path, "type": "string", "total": len(value), "returned": 2000})
            return value[:2000]
        return value
    result = compact(payload, "", 0)
    result["summary_only"] = True
    result["summary_omissions"] = omissions
    return result


def public_read_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Expose one stable scope hash without duplicating hundreds of input paths."""
    return {key:value for key,value in identity.items() if key != "inputs"} | {
        "input_file_count":len(identity["inputs"]),
        "input_bytes":sum(row.get("bytes",0) for row in identity["inputs"]),
        "identity_basis":"declared file bytes and directory entry names hashed on this request"}


def detail_page(payload: Any, *, pointer: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Bounded shallow JSON-pointer paging; nested values have their own pointer."""
    if offset < 0 or not 1 <= limit <= 200 or (pointer and not pointer.startswith("/")):
        raise ValueError("invalid_detail_pagination")
    value = payload
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    def descriptor(key: Any, item: Any) -> dict[str, Any]:
        path = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
        if isinstance(item, (dict,list)) or (isinstance(item,str) and len(item)>2000):
            return {"key":key,"pointer":path,"type":"object" if isinstance(item,dict) else "array" if isinstance(item,list) else "string",
                "total":len(item),"deferred":True}
        return {"key":key,"pointer":path,"value":item,"deferred":False}
    if isinstance(value, dict):
        keys = list(value)
        items = [descriptor(k,value[k]) for k in keys[offset:offset+limit]]
        total, kind = len(keys), "object"
    elif isinstance(value,list):
        items = [descriptor(i,value[i]) for i in range(offset,min(len(value),offset+limit))]
        total, kind = len(value), "array"
    elif isinstance(value,str):
        items = value[offset:offset+limit]
        total, kind = len(value), "string"
    else:
        items, total, kind = value, 1, "scalar"
    return {"detail_pointer":pointer,"kind":kind,"items":items,
        "pagination":{"offset":offset,"limit":limit,"total":total,
            "next_offset":offset+limit if kind!='scalar' and offset+limit<total else None},
        "detail_only":True,"read_only":True}
