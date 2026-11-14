"""One console read request reads the project's task list once (2026-09-26, 4.7 wave 10 AV).

A console sync and a session's detail used to load the whole project task list
(78 rows and ~16 MB of JSON on a real project, 0.14-0.26 s per load, all on the
event loop) three to ten times each: the sync itself, every live session's
pending-card lookup, every owner card's visibility check and the runtime
controls each rebuilt the company-runtime identity index from a fresh load.

Inside ``request_read_scope()`` the list and the index built from it are loaded
at most once per store and project, and shared by everything the request's own
task does (a task it starts reads the store itself, see ``current_request_reads``).
Outside a scope nothing changes: ``project_tasks`` and ``identity_index`` load
as before. There is no caching across requests -- the scope ends with the
request and what it held is dropped, so a write is never answered with data
read before it by a later request. Callers must treat the shared list and its
Task objects as read-only.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

from ampuly.layer2_organization.company_runtime_identity import (
    load_company_runtime_identity_index,
)


class RequestReads:
    """What one request has already read. ``loads`` counts real task-list reads."""

    def __init__(self) -> None:
        self.active = True
        # The task handling the request. Only it reads through the scope: a
        # task it starts copies the context (and with it this scope), but may
        # run on after a write the request makes, so it reads the store itself.
        try:
            self.owner: asyncio.Task[Any] | None = asyncio.current_task()
        except RuntimeError:
            self.owner = None
        self.task_loads = 0
        self.index_loads = 0
        self._tasks: dict[tuple[int, str], tuple[Any, list[Any]]] = {}
        self._indexes: dict[tuple[int, str], tuple[Any, Any]] = {}

    async def tasks(self, store: Any, project_id: str) -> list[Any]:
        key = (id(store), str(project_id))
        held = self._tasks.get(key)
        if held is None or held[0] is not store:
            loaded = list(await store.get_tasks(project_id=project_id) or [])
            self.task_loads += 1
            if not self.active:
                return loaded
            held = (store, loaded)
            self._tasks[key] = held
        return list(held[1])

    async def identity_index(self, store: Any, project_id: str) -> Any:
        key = (id(store), str(project_id))
        held = self._indexes.get(key)
        if held is None or held[0] is not store:
            index = await load_company_runtime_identity_index(
                _TasksReadOnce(store, self), project_id
            )
            self.index_loads += 1
            if not self.active:
                return index
            held = (store, index)
            self._indexes[key] = held
        return held[1]

    def close(self) -> None:
        self.active = False
        self._tasks.clear()
        self._indexes.clear()


class _TasksReadOnce:
    """A store whose project task list comes from the request (everything else is the store's own)."""

    def __init__(self, store: Any, reads: RequestReads) -> None:
        self._store = store
        self._reads = reads

    async def get_tasks(self, project_id: str | None = None, **kwargs: Any) -> list[Any]:
        if kwargs or not project_id:
            return await self._store.get_tasks(project_id=project_id, **kwargs)
        return await self._reads.tasks(self._store, project_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


_CURRENT: contextvars.ContextVar[RequestReads | None] = contextvars.ContextVar(
    "ampuly_office_request_reads", default=None
)


def current_request_reads() -> RequestReads | None:
    """The scope of the request being handled, or None.

    Only the task that opened the scope reads through it. A task started from
    inside a request inherits the context, and with it the scope object, but
    not its reads -- neither while the request still runs nor after it ended:
    it may run on after a write, and reads the store itself (it may open a
    scope of its own).
    """
    scope = _CURRENT.get()
    if scope is None or not scope.active:
        return None
    if scope.owner is not None:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is not scope.owner:
            return None
    return scope


@contextmanager
def request_read_scope() -> Iterator[RequestReads]:
    """Share project reads for the duration of one request (nested scopes join the outer one)."""
    outer = current_request_reads()
    if outer is not None:
        yield outer
        return
    scope = RequestReads()
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        scope.close()
        _CURRENT.reset(token)


async def project_tasks(store: Any, project_id: str) -> list[Any]:
    """``store.get_tasks(project_id=...)``, read once per request inside a scope."""
    scope = current_request_reads()
    if scope is None:
        return await store.get_tasks(project_id=project_id)
    return await scope.tasks(store, project_id)


async def identity_index(store: Any, project_id: str) -> Any:
    """``load_company_runtime_identity_index(store, project_id)``, built once per request inside a scope."""
    scope = current_request_reads()
    if scope is None:
        return await load_company_runtime_identity_index(store, project_id)
    return await scope.identity_index(store, project_id)


def accepts_keyword(method: Any, name: str) -> bool:
    """Whether ``method`` declares ``name`` itself (a ``**kwargs`` catch-all or a test double does not count)."""
    import inspect

    try:
        parameter = inspect.signature(method).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
