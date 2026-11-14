"""Volatile server-owned Ego resource bindings, without business authorization.

The composition root supplies an unshared object capability and frozen scope
from the existing execution envelope. This registry does not read model
arguments/task metadata, issue budget, grant tools, open a browser or replay an
Effect. The original Effect journal remains authoritative for business writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
import math
import threading
import time
from typing import Any, Callable


class EgoBindingRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class EgoBindingState(str, Enum):
    ACTIVE = "active"
    UNKNOWN = "unknown"
    REVOKED = "revoked"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class EgoNativeScope:
    task_id: str
    project_id: str
    run_id: str
    order_id: str
    batch_id: str
    budget_grant_id: str
    profile_id: str
    space_id: int
    expires_at: float
    # A snapshot of the original capability intersection, never an added grant.
    allowed_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EgoNativeBinding:
    scope: EgoNativeScope
    task: Any = field(repr=False, compare=False)


@dataclass(slots=True)
class _Entry:
    binding: EgoNativeBinding
    state: EgoBindingState = EgoBindingState.ACTIVE


def _identifier(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 256 and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class EgoNativeBindingRegistry:
    """Keep strong task identities and tombstones for this process lifetime.

    Only a never-bound task returns None. Expired/revoked/unknown bindings must
    fail closed rather than route the task to a different browser. Capacity is
    bounded; no automatic eviction can erase that distinction.
    """
    def __init__(self, *, registration_key: object, clock: Callable[[], float] = time.time,
                 max_bindings: int = 128, max_lease_seconds: float = 7200.0) -> None:
        if type(registration_key) is not object:
            raise ValueError("explicit_server_registration_capability_required")
        if type(max_bindings) is not int or not 1 <= max_bindings <= 1024:
            raise ValueError("bounded_binding_capacity_required")
        if (type(max_lease_seconds) not in {int, float} or not math.isfinite(max_lease_seconds)
                or not 1 <= max_lease_seconds <= 7200):
            raise ValueError("bounded_binding_lease_required")
        if not callable(clock):
            raise TypeError("clock_required")
        self._key = registration_key
        self._clock = clock
        self._maximum = max_bindings
        self._maximum_lease = float(max_lease_seconds)
        self._lock = threading.RLock()
        self._entries: dict[int, _Entry] = {}
        self._task_ids: dict[str, Any] = {}
        self._spaces: dict[int, tuple[str, ...]] = {}

    def _now(self) -> float:
        now = self._clock()
        if type(now) not in {int, float} or not math.isfinite(now):
            raise EgoBindingRefused("ego_clock_unavailable")
        return float(now)

    def _control(self, registration_key: object) -> None:
        if registration_key is not self._key:
            raise EgoBindingRefused("ego_registration_capability_required")

    @staticmethod
    def _task_identity(task: Any) -> tuple[str, str]:
        if task is None:
            raise EgoBindingRefused("ego_trusted_task_required")
        try:
            task_id = getattr(task, "id", None)
            project = getattr(task, "project_id", None)
        except Exception:
            raise EgoBindingRefused("ego_task_identity_invalid") from None
        if not _identifier(task_id) or not _identifier(project):
            raise EgoBindingRefused("ego_task_identity_invalid")
        return task_id, project

    def _lookup(self, task: Any) -> _Entry | None:
        if task is None:
            return None
        entry = self._entries.get(id(task))
        if entry is None:
            # An existing non-Ego task may be opaque or only partially built.
            # Reading no model metadata here preserves its original path.
            try:
                task_id = getattr(task, "id", None)
            except Exception:
                return None
            if type(task_id) is str and task_id in self._task_ids:
                raise EgoBindingRefused("ego_task_object_mismatch")
            return None
        task_id, project = self._task_identity(task)
        previous = self._task_ids.get(task_id)
        if previous is not None and previous is not task:
            raise EgoBindingRefused("ego_task_object_mismatch")
        binding = entry.binding
        if binding.task is not task:
            raise EgoBindingRefused("ego_task_object_mismatch")
        if (task_id, project) != (binding.scope.task_id, binding.scope.project_id):
            raise EgoBindingRefused("ego_task_scope_changed")
        # The run is supplied by the frozen server envelope. If the runtime
        # object also exposes a direct run_id, it may not contradict that run.
        current_run = getattr(task, "run_id", None)
        if current_run is not None and current_run != binding.scope.run_id:
            raise EgoBindingRefused("ego_task_scope_changed")
        return entry

    def _usable(self, entry: _Entry, *, readback: bool = False) -> EgoNativeBinding:
        if self._now() >= entry.binding.scope.expires_at:
            raise EgoBindingRefused("ego_binding_expired")
        if entry.state is EgoBindingState.UNKNOWN and not readback:
            raise EgoBindingRefused("ego_binding_unknown")
        if entry.state not in ({EgoBindingState.ACTIVE, EgoBindingState.UNKNOWN}
                               if readback else {EgoBindingState.ACTIVE}):
            raise EgoBindingRefused("ego_binding_" + entry.state.value)
        return entry.binding

    @_synchronized
    def bind(self, task: Any, scope: EgoNativeScope, *, registration_key: object) -> EgoNativeBinding:
        self._control(registration_key)
        task_id, project = self._task_identity(task)
        if type(scope) is not EgoNativeScope:
            raise EgoBindingRefused("ego_scope_invalid")
        for value in (scope.task_id, scope.project_id, scope.run_id, scope.order_id,
                      scope.batch_id, scope.budget_grant_id, scope.profile_id):
            if not _identifier(value):
                raise EgoBindingRefused("ego_scope_invalid")
        if (task_id, project) != (scope.task_id, scope.project_id):
            raise EgoBindingRefused("ego_task_scope_mismatch")
        direct_run = getattr(task, "run_id", None)
        if direct_run is not None and direct_run != scope.run_id:
            raise EgoBindingRefused("ego_task_scope_mismatch")
        if scope.profile_id.lower() == "default":
            raise EgoBindingRefused("ego_dedicated_profile_required")
        if type(scope.space_id) is not int or not 0 <= scope.space_id <= 9007199254740991:
            raise EgoBindingRefused("ego_space_identity_invalid")
        if (type(scope.expires_at) not in {int, float} or not math.isfinite(scope.expires_at)
                or not 0 < scope.expires_at - self._now() <= self._maximum_lease):
            raise EgoBindingRefused("ego_bounded_expiry_required")
        if (type(scope.allowed_tools) is not frozenset
                or any(not _identifier(name) or not name.startswith("browser_")
                       for name in scope.allowed_tools)):
            raise EgoBindingRefused("ego_original_tool_capabilities_required")
        existing = self._lookup(task)
        if existing is not None:
            if existing.binding.scope != scope:
                raise EgoBindingRefused("ego_binding_conflict")
            return self._usable(existing)
        if len(self._entries) >= self._maximum:
            raise EgoBindingRefused("ego_binding_capacity")
        space_key = scope.space_id
        owner = (scope.profile_id, scope.project_id, scope.run_id, scope.order_id,
                 scope.batch_id, scope.budget_grant_id)
        if space_key in self._spaces and self._spaces[space_key] != owner:
            raise EgoBindingRefused("ego_space_scope_conflict")
        if any(entry.binding.scope.space_id == space_key and entry.state is EgoBindingState.UNKNOWN
               for entry in self._entries.values()):
            raise EgoBindingRefused("ego_space_unknown")
        binding = EgoNativeBinding(scope, task)
        self._entries[id(task)] = _Entry(binding)
        self._task_ids[task_id] = task
        self._spaces[space_key] = owner
        return binding

    @_synchronized
    def select_binding(self, task: Any) -> EgoNativeBinding | None:
        entry = self._lookup(task)
        return None if entry is None else self._usable(entry)

    @_synchronized
    def require_action(self, task: Any, binding: EgoNativeBinding, tool_name: str) -> EgoNativeBinding:
        entry = self._lookup(task)
        if entry is None or entry.binding is not binding:
            raise EgoBindingRefused("ego_binding_identity_mismatch")
        current = self._usable(entry)
        if tool_name not in current.scope.allowed_tools:
            raise EgoBindingRefused("ego_tool_not_in_original_capabilities")
        return current

    @_synchronized
    def readback_binding(self, task: Any) -> EgoNativeBinding | None:
        entry = self._lookup(task)
        if entry is None:
            return None
        current = self._usable(entry, readback=True)
        if "browser_observe" not in current.scope.allowed_tools:
            raise EgoBindingRefused("ego_readback_not_in_original_capabilities")
        return current

    def _controlled_entry(self, task: Any, key: object) -> _Entry:
        self._control(key)
        entry = self._lookup(task)
        if entry is None:
            raise EgoBindingRefused("ego_task_not_bound")
        return entry

    @_synchronized
    def mark_unknown(self, task: Any, *, registration_key: object) -> None:
        entry = self._controlled_entry(task, registration_key)
        if entry.state not in {EgoBindingState.ACTIVE, EgoBindingState.UNKNOWN}:
            raise EgoBindingRefused("ego_binding_" + entry.state.value)
        # Members of the same frozen run can share a native space. An unknown
        # action makes that resource unknown for every such member, not just
        # the task that happened to dispatch it.
        for same_space in self._entries.values():
            if (same_space.binding.scope.space_id == entry.binding.scope.space_id
                    and same_space.state in {EgoBindingState.ACTIVE, EgoBindingState.UNKNOWN}):
                same_space.state = EgoBindingState.UNKNOWN

    @_synchronized
    def revoke(self, task: Any, *, registration_key: object) -> None:
        entry = self._controlled_entry(task, registration_key)
        if entry.state is EgoBindingState.CLOSED:
            raise EgoBindingRefused("ego_binding_closed")
        entry.state = EgoBindingState.REVOKED

    @_synchronized
    def close(self, task: Any, *, registration_key: object) -> None:
        self._controlled_entry(task, registration_key).state = EgoBindingState.CLOSED

    @_synchronized
    def state_of(self, task: Any) -> EgoBindingState | None:
        entry = self._lookup(task)
        return None if entry is None else entry.state
