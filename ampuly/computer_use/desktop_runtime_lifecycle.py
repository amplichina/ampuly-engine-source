"""Execution-attempt ownership for desktop runtime resources.

Ephemeral desktop objects belong to an existing engine execution attempt.
This module starts no process, reads no screen and changes no durable facts.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect


_DRAIN_TIMEOUT_SEC = 30.0


class DesktopLifecycleRefused(PermissionError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(eq=False)
class _Owner:
    attempt_token: str
    project_id: str | None
    task_id: str
    active: int = 0
    closing: bool = False
    contexts: dict = field(default_factory=dict)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_CURRENT_OWNER: ContextVar[tuple[object, _Owner] | None] = ContextVar(
    "ampuly_desktop_execution_owner", default=None
)


class DesktopRuntimeLifecycle:
    """One binding's resource ownership; not execution authorization.

    Callers must still pass the original scope, permission and current channel
    checks. The attempt token comes from Engine's ActiveTaskRunRegistry, never
    from model arguments or mutable Task metadata.
    """

    def __init__(self, project_id, *, max_contexts=64, on_owner_closing=None):
        self.project_id = project_id
        self._on_owner_closing = on_owner_closing
        self.max_contexts = max_contexts
        self.admission_closed = False
        self.contexts = {}
        self._owners = {}
        # Takeover / fresh-look hold. Independent of owner close: a human
        # holding the desktop must fail this fence even while the attempt is open.
        self._input_hold = ""

    @asynccontextmanager
    async def owner(self, task, attempt_token):
        task_id = getattr(task, "id", None)
        if (self.admission_closed
                or type(task_id) is not str or not task_id
                or type(attempt_token) is not str or len(attempt_token) != 32
                or any(c not in "0123456789abcdef" for c in attempt_token)
                or attempt_token in self._owners):
            raise DesktopLifecycleRefused("desktop_execution_owner_invalid")
        # A non-desktop task can lack the desktop binding's project. Merely
        # entering the execution lifetime must not reject such ordinary work;
        # actual borrowing still requires the exact projected project below.
        owner = _Owner(attempt_token, getattr(task, "project_id", None), task_id)
        owner.idle.set()
        self._owners[attempt_token] = owner
        token = _CURRENT_OWNER.set((self, owner))
        try:
            yield
        finally:
            # Keep ownership through drain and teardown. On cleanup failure,
            # retain the exact owner/objects for Engine.shutdown() to retry.
            try:
                await self._close_owner(owner)
            finally:
                _CURRENT_OWNER.reset(token)

    @contextmanager
    def borrow(self, task, scope_key, factory):
        binding = _CURRENT_OWNER.get()
        if binding is None or binding[0] is not self:
            raise DesktopLifecycleRefused("desktop_execution_owner_missing")
        owner = binding[1]
        if (self.admission_closed or owner.closing
                or owner.project_id != self.project_id
                or self._owners.get(owner.attempt_token) is not owner
                or getattr(task, "project_id", None) != owner.project_id
                or getattr(task, "id", None) != owner.task_id):
            raise DesktopLifecycleRefused("desktop_execution_owner_invalid")
        key = (owner.attempt_token, scope_key)
        context = owner.contexts.get(scope_key)
        if context is None:
            if len(self.contexts) >= self.max_contexts:
                raise DesktopLifecycleRefused("desktop_binding_retention_limit")
            context = factory()
            self.contexts[key] = context
            owner.contexts[scope_key] = context
        owner.active += 1
        owner.idle.clear()
        try:
            yield context
        finally:
            owner.active -= 1
            if owner.active == 0:
                owner.idle.set()

    def hold_input(self, reason: str) -> None:
        """Block OS input for this binding until ``release_input``.

        Used when a person holds the desktop, and again after they hand it
        back until a fresh look replaces the old frame. Not a product TTL.
        """
        text = str(reason or "held")
        self._input_hold = text[:80]

    def release_input(self) -> None:
        self._input_hold = ""

    def input_allowed(self):
        """Last input fence for the currently borrowed, still-open owner.

        False while the attempt is closing, and while takeover or a required
        fresh look is holding input. Callers that are about to reach the OS
        stop here.
        """
        binding = _CURRENT_OWNER.get()
        if binding is None or binding[0] is not self:
            return False
        owner = binding[1]
        return (not self.admission_closed and not owner.closing
                and not self._input_hold
                and owner.active > 0
                and self._owners.get(owner.attempt_token) is owner)

    def observed_owner(self, task, scope_key):
        """Read an existing, still-open attempt; never borrow/create a helper."""
        if self.admission_closed or getattr(task, 'project_id', None) != self.project_id:
            return None
        rows = [owner.attempt_token for owner in self._owners.values()
                if not owner.closing and owner.project_id == self.project_id
                and owner.task_id == getattr(task, 'id', None) and scope_key in owner.contexts]
        return rows[0] if len(rows) == 1 else None

    def has_attempt(self, attempt_token):
        """A closing/failed cleanup still owns its resources and visible notice."""
        return attempt_token in self._owners

    async def _close_owner(self, owner):
        owner.closing = True
        if self._on_owner_closing is not None:
            self._on_owner_closing(owner.attempt_token)
        async with owner.close_lock:
            if self._owners.get(owner.attempt_token) is not owner:
                return
            if owner.active:
                # Timeout reports incomplete cleanup; it does not erase the
                # owner, unlock an active operation or kill a newer helper.
                await asyncio.wait_for(owner.idle.wait(), _DRAIN_TIMEOUT_SEC)
            for scope_key, context in list(owner.contexts.items()):
                # No tool operation can still use this exact helper. A new
                # attempt has its own key and objects, even for the same task.
                kill = getattr(context[0], "kill", None)
                if not callable(kill):
                    raise DesktopLifecycleRefused("desktop_cleanup_unavailable")
                result = kill()
                if inspect.isawaitable(result):
                    await result
                key = (owner.attempt_token, scope_key)
                if self.contexts.get(key) is context:
                    self.contexts.pop(key)
                if owner.contexts.get(scope_key) is context:
                    owner.contexts.pop(scope_key)
            if self._owners.get(owner.attempt_token) is owner:
                self._owners.pop(owner.attempt_token)

    def begin_shutdown(self):
        self.admission_closed = True
        for owner in self._owners.values():
            owner.closing = True

    async def close(self):
        self.begin_shutdown()
        for owner in list(self._owners.values()):
            await self._close_owner(owner)

