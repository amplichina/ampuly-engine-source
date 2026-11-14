"""Types for the pending trusted console seam; no wire authority or runtime."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .contracts import Scope
from .settings import CRMSettings
from .store import CRMStore


class CRMConsoleHost(Protocol):
    def crm_context(self, ws: Any, project_id: str, *, open_store: bool = True) -> "CRMContext": ...
    async def send_ack(self, ws: Any, **fields: Any) -> None: ...


@dataclass(frozen=True)
class CRMContext:
    """Constructed only by a live authenticated host, never from request params.

    check rereads account role/customer grants and project/customer lifecycle,
    verifies unchanged home/store identity and active project, and fails closed.
    owner_ids come from the existing account registry, never CRM names.
    A source/authorization failure precedes storage construction or disclosure.
    HandlerConsoleHost.crm_context supplies the production factory.

    settings is the persisted per-project switch; data actions require it to
    be enabled with synthetic-only admission. With open_store=False (settings
    actions only) store is None and no SQLite file is created.
    """

    scope: Scope
    store: CRMStore | None
    check: Callable[[], None]
    settings: CRMSettings | None = None
    update_settings: Callable[..., CRMSettings] | None = None


class RuntimeCRMContextResolver(Protocol):
    def __call__(self, task: Any) -> CRMContext:
        """Resolve trusted Task/Run/project and frozen node pins; no fallback.

        Must use the existing saved-workflow/employee loaders and runtime guard,
        current qualifications, permissions, tool grants and data scope. A CRM
        binding dict cannot establish this authority. Not yet wired to runtime.
        """
        ...
