"""Host-owned recipient reads, bounded results and honest cancellation.

Adapters are trusted host code, not a sandbox for arbitrary Python. A sync
adapter must have finite underlying I/O; async adapters must not block the loop
or suppress cancellation indefinitely. The deadline rejects late evidence. It
does not kill a Python thread: cancellation/deadline first drains this exact
invocation, so no abandoned read can become an unobserved background operation.
No threads are spawned until an explicitly typed adapter is actually called.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import math
import threading
import time
from typing import Any, Callable

MAX_READBACK_ROWS = 128
MAX_READBACK_ROW_BYTES = 1024 * 1024
MAX_READBACK_TOTAL_BYTES = 2 * 1024 * 1024
MAX_READBACK_SECONDS = 10.0
MAX_READBACK_JSON_NODES = 8192
MAX_READBACK_JSON_DEPTH = 16
_ERROR_CODES = frozenset({
    "invalid_readback_policy", "invalid_readback_request", "invalid_readback_adapter",
    "invalid_readback_batch", "external_agent_readback_limit", "external_agent_readback_incomplete",
    "external_agent_readback_busy", "external_agent_readback_timeout", "external_agent_readback_unavailable",
})


class ExternalAgentReadbackError(ValueError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _ERROR_CODES else "external_agent_readback_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class ReadbackPolicy:
    """A host may tighten these ceilings, never raise them through a request."""

    max_rows: int = MAX_READBACK_ROWS
    max_row_bytes: int = MAX_READBACK_ROW_BYTES
    max_total_bytes: int = MAX_READBACK_TOTAL_BYTES
    deadline_s: float = MAX_READBACK_SECONDS

    def __post_init__(self):
        for value, ceiling in ((self.max_rows, MAX_READBACK_ROWS),
                               (self.max_row_bytes, MAX_READBACK_ROW_BYTES),
                               (self.max_total_bytes, MAX_READBACK_TOTAL_BYTES)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ExternalAgentReadbackError("invalid_readback_policy")
        if (self.max_row_bytes > self.max_total_bytes
                or type(self.deadline_s) not in (int, float)
                or not math.isfinite(self.deadline_s)
                or not 0 < self.deadline_s <= MAX_READBACK_SECONDS):
            raise ExternalAgentReadbackError("invalid_readback_policy")


@dataclass(frozen=True)
class ReadbackRequest:
    """Exact query scope and limits, supplied before any recipient I/O.

    The reader must apply the deadline and limits to its transport/enumeration,
    not read an unbounded dataset and then slice it. No credentials or mutable
    Effect/Task object is passed to the adapter.
    """

    effect_id: str
    principal_id: str
    project_id: str
    channel: str
    user_id: str
    session_id: str
    task_ref: str
    artifact_sha256: str
    binding_sha256: str
    protocol: str
    policy: ReadbackPolicy
    deadline_monotonic: float

    def __post_init__(self):
        for key in ("effect_id", "principal_id", "project_id", "channel", "user_id",
                    "session_id", "task_ref", "artifact_sha256", "binding_sha256", "protocol"):
            value = getattr(self, key)
            if (type(value) is not str or not value or len(value) > 512
                    or any(ord(char) < 32 for char in value)):
                raise ExternalAgentReadbackError("invalid_readback_request")
        if (type(self.policy) is not ReadbackPolicy
                or type(self.deadline_monotonic) not in (int, float)
                or not math.isfinite(self.deadline_monotonic)):
            raise ExternalAgentReadbackError("invalid_readback_request")

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


@dataclass(frozen=True)
class ReadbackBatch:
    """Materialized independent records. False completeness never proves delivery."""

    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    complete: bool


class ReadbackAdapter:
    """Explicit host trust and cancellation contract; not callable auto-detection.

    An instance owns at most one read at a time on the host's event loop. A second
    request is refused instead of accumulating queued reads during cancellation.
    """

    __slots__ = ("_callback", "_mode", "_policy", "_active")

    def __init__(self, callback: Callable, *, mode: str, policy: ReadbackPolicy | None = None):
        call = callback if inspect.isfunction(callback) else getattr(callback, "__call__", callback)
        is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(call)
        if (not callable(callback) or type(mode) is not str or mode not in {"trusted_sync", "async_io"}
                or inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call)
                or (mode == "async_io") != is_async):
            raise ExternalAgentReadbackError("invalid_readback_adapter")
        if policy is not None and type(policy) is not ReadbackPolicy:
            raise ExternalAgentReadbackError("invalid_readback_policy")
        self._callback, self._mode = callback, mode
        self._policy = policy if policy is not None else ReadbackPolicy()
        self._active = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def policy(self) -> ReadbackPolicy:
        return self._policy

    @property
    def active(self) -> bool:
        return self._active


def require_readback_adapter(value: Any) -> ReadbackAdapter:
    if type(value) is not ReadbackAdapter:
        raise ExternalAgentReadbackError("invalid_readback_adapter")
    return value


def _checked_batch(value: Any, policy: ReadbackPolicy) -> ReadbackBatch:
    if type(value) is not ReadbackBatch or type(value.complete) is not bool:
        # A mistakenly returned coroutine is never scheduled as a second read.
        if inspect.iscoroutine(value):
            value.close()
        raise ExternalAgentReadbackError("invalid_readback_batch")
    if type(value.rows) not in (list, tuple):
        raise ExternalAgentReadbackError("invalid_readback_batch")
    if len(value.rows) > policy.max_rows:
        raise ExternalAgentReadbackError("external_agent_readback_limit")
    if not value.complete:
        raise ExternalAgentReadbackError("external_agent_readback_incomplete")

    remaining = policy.max_total_bytes
    nodes = MAX_READBACK_JSON_NODES
    active: set[int] = set()

    def charge(count: int):
        nonlocal remaining
        remaining -= count
        if remaining < 0:
            raise ExternalAgentReadbackError("external_agent_readback_limit")

    def copy_json(item: Any, depth: int):
        nonlocal nodes
        nodes -= 1
        if nodes < 0 or depth > MAX_READBACK_JSON_DEPTH:
            raise ExternalAgentReadbackError("external_agent_readback_limit")
        if type(item) is str:
            # Reject before allocating a UTF-8/escaped copy of an oversized string.
            if len(item) > remaining:
                raise ExternalAgentReadbackError("external_agent_readback_limit")
            try:
                charge(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))
            except UnicodeError:
                raise ExternalAgentReadbackError("invalid_readback_batch") from None
            return item
        if item is None or type(item) in (bool, int, float):
            if ((type(item) is int and item.bit_length() > 256)
                    or (type(item) is float and not math.isfinite(item))):
                raise ExternalAgentReadbackError("invalid_readback_batch")
            charge(len(json.dumps(item, allow_nan=False)))
            return item
        if type(item) not in (dict, list):
            raise ExternalAgentReadbackError("invalid_readback_batch")
        if id(item) in active:
            raise ExternalAgentReadbackError("invalid_readback_batch")
        if len(item) > nodes:
            raise ExternalAgentReadbackError("external_agent_readback_limit")
        active.add(id(item))
        try:
            charge(2 + max(0, len(item) - 1))  # braces/brackets and commas
            if type(item) is list:
                return [copy_json(child, depth + 1) for child in item]
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise ExternalAgentReadbackError("invalid_readback_batch")
                charge(1)  # colon
                result[copy_json(key, depth + 1)] = copy_json(child, depth + 1)
            return result
        finally:
            active.remove(id(item))

    rows = []
    charge(2 + max(0, len(value.rows) - 1))
    for row in value.rows:
        if type(row) is not dict:
            raise ExternalAgentReadbackError("invalid_readback_batch")
        start = remaining
        copied = copy_json(row, 0)
        if start - remaining > policy.max_row_bytes:
            raise ExternalAgentReadbackError("external_agent_readback_limit")
        if (any(type(copied.get(key)) is not str for key in
                ("channel", "user_id", "session_id", "task_ref", "content"))
                or type(copied.get("metadata")) is not dict
                or type(copied["metadata"].get("external_agent")) is not dict):
            raise ExternalAgentReadbackError("invalid_readback_batch")
        rows.append(copied)
    return ReadbackBatch(rows=tuple(rows), complete=True)


async def read_recipient_batch(adapter: ReadbackAdapter, request: ReadbackRequest) -> ReadbackBatch:
    """Never abandon an invocation or accept its result after cancellation/deadline."""
    adapter = require_readback_adapter(adapter)
    if type(request) is not ReadbackRequest or request.policy != adapter.policy:
        raise ExternalAgentReadbackError("invalid_readback_request")
    # Compare absolute deadlines. Subtracting a large monotonic value can make
    # an exactly constructed 20 ms interval round slightly above 0.02 seconds.
    if request.deadline_monotonic > time.monotonic() + adapter.policy.deadline_s:
        raise ExternalAgentReadbackError("invalid_readback_request")
    if adapter.active:
        raise ExternalAgentReadbackError("external_agent_readback_busy")
    if request.remaining_seconds <= 0:
        raise ExternalAgentReadbackError("external_agent_readback_timeout")
    adapter._active = True
    worker = None
    cancellation = None
    expired = False
    stop_requested = threading.Event()

    def sync_read():
        # A queued worker must not begin new I/O after this request was stopped.
        if stop_requested.is_set() or request.remaining_seconds <= 0:
            raise ExternalAgentReadbackError("external_agent_readback_timeout")
        return _checked_batch(adapter._callback(request), request.policy)

    async def invoke():
        if adapter.mode == "trusted_sync":
            # Validation runs in this same owned worker too, not on the loop.
            return await asyncio.to_thread(sync_read)
        if request.remaining_seconds <= 0:
            raise ExternalAgentReadbackError("external_agent_readback_timeout")
        return _checked_batch(await adapter._callback(request), request.policy)

    try:
        worker = asyncio.create_task(invoke())
        try:
            done, _ = await asyncio.wait({worker}, timeout=request.remaining_seconds)
            expired = not done or request.remaining_seconds <= 0
        except asyncio.CancelledError as exc:
            cancellation = exc
        if cancellation is not None or expired:
            stop_requested.set()
            if adapter.mode == "async_io" and not worker.done():
                worker.cancel()
            # Sync threads cannot be killed. Async cancellation is cooperative.
            # Retain ownership until termination, even after repeated stop requests.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError as exc:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        cancellation = exc
                except Exception:
                    break
        try:
            result = worker.result()
        except asyncio.CancelledError:
            if cancellation is not None:
                raise cancellation
            if expired:
                raise ExternalAgentReadbackError("external_agent_readback_timeout") from None
            raise
        except Exception:
            if cancellation is not None:
                raise cancellation
            if expired or request.remaining_seconds <= 0:
                raise ExternalAgentReadbackError("external_agent_readback_timeout") from None
            raise
        if cancellation is not None:
            raise cancellation
        if expired or request.remaining_seconds <= 0:
            raise ExternalAgentReadbackError("external_agent_readback_timeout")
        return result
    except ExternalAgentReadbackError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never echo channel exceptions (which may contain credentials or bodies).
        raise ExternalAgentReadbackError("external_agent_readback_unavailable") from None
    finally:
        adapter._active = False
