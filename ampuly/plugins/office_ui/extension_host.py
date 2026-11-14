"""The console as a console extension sees it (``extensions``): the one adapter over the websocket handler.

An extension declares what it needs from the console (``ampuly.plugins.wecom_service.host.ConsoleHost`` is one such
declaration) and gets it through :class:`HandlerConsoleHost`. This is the only place an extension's calls reach the
handler's own members; everything here reads what the handler already has and adds nothing to it except the
extensions' own per-console state (``extension_state``).
"""

from __future__ import annotations

from pathlib import Path
import re
import stat
from typing import Any, Callable


class CustomerServiceContextError(ValueError):
    """Stable host-authority refusal; never include paths or account details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _customer_service_path_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    """Inspect only this fixed path and its parents; never follow a link."""
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CustomerServiceContextError("customer_service_authority_unavailable")
        if current != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise CustomerServiceContextError("customer_service_authority_unavailable")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CustomerServiceContextError("customer_service_authority_unavailable")
    return info.st_dev, info.st_ino


class HandlerConsoleHost:
    """The console's websocket handler (``WSHandler``, or a stand-in with the same members), as an extension's host."""

    # How an extension tells a host from a handler it still has to wrap (``console_host``).
    is_console_host = True

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    # ── what the console holds ──

    @property
    def business(self) -> Any:
        """The business store."""
        return self._handler._accounts().business

    def packages(self) -> list[Any]:
        """The workflow packages the console knows."""
        return list(self._handler._workflow_registry())

    def console_api(self) -> Any:
        """The console API (orders, acceptance, the audit)."""
        return self._handler._console_api()

    async def request_engine(self, project_id: str) -> tuple[Any, str]:
        """(engine, project id) the console answers a request naming ``project_id`` with."""
        return await self._handler._engine_for_request({"project_id": project_id})

    async def crm_context(self, ws: Any, project_id: str, *, open_store: bool = True) -> Any:
        """Admit CRM from current socket and project sources, never wire scope.

        CRM is default OFF per project. Only settings actions pass
        ``open_store=False``; data actions fail ``crm_not_enabled`` before any
        SQLite file is created unless an owner enabled CRM for this project.
        """
        from ampuly.plugins.crm_agent.contracts import Scope, fail, identifier
        from ampuly.plugins.crm_agent.host_contract import CRMContext
        from ampuly.plugins.crm_agent.settings import ADMISSION_SYNTHETIC, admit_real_data, read_settings, write_settings
        from ampuly.plugins.crm_agent.store import CRMStore
        from .ws_handler import _current_socket_principal, ws_customer_scope

        identifier(project_id)
        handler = self._handler
        business = self.business
        services_context = handler.services_context
        home = Path(services_context.ampuly_home)
        project_root = services_context.project_dir(project_id)
        if not home.is_absolute() or project_root != home / "projects" / project_id:
            fail("source_unavailable")

        def observe():
            if self.shutting_down or handler.services_context is not services_context or self.business is not business:
                fail("source_changed")
            principal = _current_socket_principal(handler, ws)
            if not self.is_owner(ws):
                fail("owner_required")
            actor = self.actor_login(ws)
            allowed = ws_customer_scope(handler, ws)
            if principal is not None and (principal.disabled or principal.role != "owner"):
                fail("owner_required")
            if not business.project_is_registered(project_id):
                fail("project_not_registered")
            if not project_root.is_dir():
                fail("source_unavailable")
            lifecycle = business.project_lifecycle(project_id)
            customer = str(lifecycle.customer_id or "")
            if lifecycle.status != "active" or lifecycle.reason == "project_destroy_started" or not customer:
                fail("project_customer_binding_required")
            identifier(customer)
            if allowed and customer not in allowed:
                fail("customer_forbidden")
            if Path(services_context.ampuly_home) != home or Path(business.path) != home / "business.db":
                fail("source_changed")
            # Record owners are owner-role accounts only (CRM v1 is owner-only).
            owners = tuple(sorted({actor, *(account.login for account in handler._accounts().list_accounts()
                if account.role == "owner" and not account.disabled
                and (not account.customer_ids or customer in account.customer_ids))}))
            return Scope(project_id, customer, actor, True, owners)

        initial = observe()
        def check():
            if observe() != initial:
                fail("source_changed")
        check()
        crm_dir = project_root / "plugins" / "crm"
        settings = read_settings(crm_dir)

        def update_settings(*, enabled, admission, expected_revision):
            check()
            return write_settings(crm_dir, enabled=enabled, admission=admission, expected_revision=expected_revision,
                                  actor=initial.actor, ampuly_home=home, project_id=project_id)

        if not open_store:
            return CRMContext(scope=initial, store=None, check=check, settings=settings, update_settings=update_settings)
        if not settings.enabled:
            fail("not_enabled")
        if settings.admission != ADMISSION_SYNTHETIC:
            admit_real_data(home, project_id)  # closed in this build: always raises
        # The engine's existing project lifecycle owns this directory. Each
        # request uses short-lived SQLite connections, with no CRM worker.
        # Tombstones live outside the project tree so restoring an older
        # project backup cannot overwrite them; they are re-applied here.
        store = CRMStore(crm_dir / "sales.sqlite", admission=ADMISSION_SYNTHETIC,
                         tombstones=home / "plugins" / "crm" / "tombstones" / f"{project_id}.json")
        store.assert_scope(initial)
        check()
        store.apply_tombstones(initial)
        check()
        return CRMContext(scope=initial, store=store, check=check, settings=settings)

    async def project_engine(self, project_id: str) -> Any:
        """The engine of ``project_id`` as the console's services load it."""
        return await self._handler.services_context.engine_for_project(project_id)

    def normalize_project_id(self, value: Any) -> str:
        return self._handler._normalize_project_id(value)

    def customer_service_context(self, ws: Any, project_id: str) -> tuple[Any, Any, str, Path]:
        """Current owner/project sources for explicit customer-service actions.

        Return (existing engine, existing business store, current actor, home).
        This is an observation, not a retained permission: callers recheck at
        every write/model/send boundary and compare the actor and source objects.
        No constructing getter, engine attachment, configuration or DB content
        is read here, except the existing store's project-lifecycle lookup.
        """
        def principal(_accounts: Any) -> str:
            if self._handler._ws_is_owner(ws) is not True:
                raise ValueError
            return self._handler._ws_actor_login(ws)

        return self._customer_service_existing_context(project_id, principal)

    def customer_service_binding_home(self, project_id: str) -> Path:
        """Locate durable bindings, without granting model or send authority.

        Host-only, read-only bootstrap for a channel that has no bound actor
        yet. The caller must verify the persisted binding, obtain its actor,
        then call ``customer_service_background_context`` before any action.
        This reads no account record and creates/loads no store or engine.
        Do not require a project runtime directory or active lifecycle before
        discovering whether the legacy lane ever selected this new service.
        Project admission (including the bounded ID) stays in the background
        context; this locator uses the existing canonical path-safe ID shape.
        """
        if type(project_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project_id) is None:
            raise CustomerServiceContextError("customer_service_project_invalid")
        if self.shutting_down:
            raise CustomerServiceContextError("customer_service_shutting_down")
        handler = self._handler
        accounts = getattr(handler, "_account_store", None)
        context = getattr(handler, "services_context", None)
        if accounts is None or context is None:
            raise CustomerServiceContextError("customer_service_authority_unavailable")

        def absolute_path(value: Any) -> Path:
            if not isinstance(value, (str, Path)):
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            return path

        try:
            home = absolute_path(getattr(context, "ampuly_home", None))
            business = getattr(accounts, "business", None)
            database = home / "business.db"
            if (absolute_path(getattr(accounts, "home", None)) != home
                    or absolute_path(getattr(business, "path", None)) != database):
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            checked_paths = [(home, True), (database, False)]
            identities = [_customer_service_path_identity(path, directory=is_dir)
                          for path, is_dir in checked_paths]
            if self.shutting_down:
                raise CustomerServiceContextError("customer_service_shutting_down")
            if (getattr(handler, "_account_store", None) is not accounts
                    or getattr(handler, "services_context", None) is not context
                    or getattr(accounts, "business", None) is not business
                    or absolute_path(getattr(context, "ampuly_home", None)) != home
                    or absolute_path(getattr(accounts, "home", None)) != home
                    or absolute_path(getattr(business, "path", None)) != database
                    or identities != [_customer_service_path_identity(path, directory=is_dir)
                                      for path, is_dir in checked_paths]):
                raise CustomerServiceContextError("customer_service_context_changed")
            return home
        except CustomerServiceContextError:
            raise
        except Exception:
            raise CustomerServiceContextError("customer_service_authority_unavailable") from None

    def customer_service_background_context(self, project_id: str, actor: str) -> tuple[Any, Any, str, Path]:
        """Recheck the retained acceptance owner without inventing a socket.

        Host-only seam: the caller must take ``actor`` from its verified durable
        acceptance binding, never channel/wire input, and compare that binding
        at every boundary. A string alone cannot prove its persistence origin.
        Each observation rereads the existing account twice. No local-console
        bootstrap owner, account-store constructor or engine loader is used.
        """
        if (type(actor) is not str or not 1 <= len(actor) <= 128
                or actor != actor.strip() or any(ord(c) < 32 or ord(c) == 127 for c in actor)):
            raise CustomerServiceContextError("customer_service_owner_required")

        def principal(accounts: Any) -> str:
            lookup = getattr(accounts, "get", None)
            if not callable(lookup):
                raise ValueError
            current = lookup(actor)
            # Read only non-secret authority fields; do not expose or copy the
            # account record. A scoped owner is not authority for background CS.
            customer_ids = getattr(current, "customer_ids", None)
            if (getattr(current, "login", None) != actor
                    or getattr(current, "role", None) != "owner"
                    or getattr(current, "disabled", None) is not False
                    or type(customer_ids) not in (tuple, list) or customer_ids):
                raise ValueError
            return actor

        return self._customer_service_existing_context(project_id, principal)

    def _customer_service_existing_context(self, project_id: str, resolve_actor: Callable[[Any], str]
                                          ) -> tuple[Any, Any, str, Path]:
        """Shared read-only source checks; the resolver is installed host code."""
        if type(project_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_id) is None:
            raise CustomerServiceContextError("customer_service_project_invalid")
        if self.shutting_down:
            raise CustomerServiceContextError("customer_service_shutting_down")
        handler = self._handler
        accounts = getattr(handler, "_account_store", None)
        context = getattr(handler, "services_context", None)
        if accounts is None or context is None:
            raise CustomerServiceContextError("customer_service_authority_unavailable")

        def principal() -> str:
            try:
                actor = resolve_actor(accounts)
                if (type(actor) is not str or not 1 <= len(actor) <= 128
                        or actor != actor.strip() or any(ord(c) < 32 or ord(c) == 127 for c in actor)):
                    raise ValueError
                return actor
            except Exception:
                raise CustomerServiceContextError("customer_service_owner_required") from None

        def absolute_path(value: Any) -> Path:
            if not isinstance(value, (str, Path)):
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            return path

        try:
            home = absolute_path(getattr(context, "ampuly_home", None))
            business = getattr(accounts, "business", None)
            database = home / "business.db"
            if (absolute_path(getattr(accounts, "home", None)) != home
                    or absolute_path(getattr(business, "path", None)) != database):
                raise CustomerServiceContextError("customer_service_authority_unavailable")
            checked_paths = [(home, True), (database, False)]
            if project_id != "default":
                checked_paths.append((home / "projects" / project_id, True))
            identities = [_customer_service_path_identity(path, directory=is_dir)
                          for path, is_dir in checked_paths]
            # Refuse foreign/missing authority paths before an account query.
            actor = principal()
            lifecycle = getattr(business, "project_lifecycle", None)
            if not callable(lifecycle):
                raise CustomerServiceContextError("customer_service_authority_unavailable")

            def project_active() -> None:
                state = lifecycle(project_id)
                if (getattr(state, "project_id", None) != project_id
                        or getattr(state, "status", None) != "active"):
                    raise CustomerServiceContextError("customer_service_project_unavailable")

            project_active()
            engines = (getattr(context, "active_engine", None), getattr(context, "root_engine", None))
            engine = next((candidate for candidate in engines if candidate is not None
                           and getattr(candidate, "project_id", None) == project_id
                           and absolute_path(getattr(candidate, "ampuly_home", None)) == home), None)
            if engine is None:
                raise CustomerServiceContextError("customer_service_engine_unavailable")
            # ProjectEngines.loaded() attaches services, so it is deliberately
            # not a fallback for this observation-only, existing-engine seam.
            project_active()
            if principal() != actor:
                raise CustomerServiceContextError("customer_service_owner_required")
            if self.shutting_down:
                raise CustomerServiceContextError("customer_service_shutting_down")
            current_engines = (getattr(context, "active_engine", None), getattr(context, "root_engine", None))
            if (getattr(handler, "_account_store", None) is not accounts
                    or getattr(handler, "services_context", None) is not context
                    or getattr(accounts, "business", None) is not business
                    or absolute_path(getattr(context, "ampuly_home", None)) != home
                    or absolute_path(getattr(accounts, "home", None)) != home
                    or absolute_path(getattr(business, "path", None)) != database
                    or not any(candidate is engine for candidate in current_engines)
                    or getattr(engine, "project_id", None) != project_id
                    or absolute_path(getattr(engine, "ampuly_home", None)) != home
                    or identities != [_customer_service_path_identity(path, directory=is_dir)
                                      for path, is_dir in checked_paths]):
                raise CustomerServiceContextError("customer_service_context_changed")
            return engine, business, actor, home
        except CustomerServiceContextError:
            raise
        except Exception:
            raise CustomerServiceContextError("customer_service_authority_unavailable") from None

    # ── the Feishu gate group ──

    def gate_configured(self) -> bool:
        return bool(self._handler._gate_configured())

    def gate_service(self) -> Any:
        return self._handler._gate_service()

    def ensure_feishu_listener(self) -> None:
        """Start the group's listener when the console has one and it is not running; nothing otherwise."""
        ensure = getattr(self._handler, "_ensure_feishu_listener", None)
        if callable(ensure):
            ensure()

    # ── the duty round ──

    def frozen_project_check(self) -> Callable[[list[str]], Any] | None:
        """The console's check of which projects their owner archived or destroyed (ids -> the frozen ones), or None
        when this console has none."""
        check = getattr(self._handler, "_duty_frozen_project_ids", None)
        return check if callable(check) else None

    def outbound_refusal(self) -> Any:
        """The console's own outbound check (connector switches), for a notifier; None when it has none."""
        return getattr(self._handler, "_connector_refusal", None)

    def note_duty(self, entry: dict[str, Any]) -> None:
        """Add one entry to the duty log the project page reads (the newest 50 kept); nothing when there is none."""
        log = getattr(self._handler, "_duty_log", None)
        if isinstance(log, list):
            log.append(entry)
            del log[:-50]

    @property
    def shutting_down(self) -> bool:
        return bool(getattr(self._handler, "_shutting_down", False))

    def extension_state(self, name: str) -> dict[str, Any]:
        """The per-console place of extension ``name`` for what it holds while the console runs (a link, a runtime)."""
        state = getattr(self._handler, "_extension_state", None)
        if not isinstance(state, dict):
            state = {}
            self._handler._extension_state = state
        return state.setdefault(str(name), {})

    # ── one websocket request ──

    def is_owner(self, ws: Any) -> bool:
        return bool(self._handler._ws_is_owner(ws))

    def actor_login(self, ws: Any) -> str:
        return self._handler._ws_actor_login(ws)

    async def send_ack(self, ws: Any, **fields: Any) -> None:
        await self._handler._send_ack(ws, **fields)


def console_host(console: Any) -> Any:
    """``console`` as an extension's host: itself when it is one already, else the handler behind the adapter."""
    if getattr(console, "is_console_host", False) is True:
        return console
    return HandlerConsoleHost(console)
