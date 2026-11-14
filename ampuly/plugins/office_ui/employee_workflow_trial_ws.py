"""Current-owner WS admission for the existing employee workflow launch.

No bootstrap principal, project activation, worker, execution grant or new
ledger lives here. The callback retained by the service must also run inside
the original ConsoleAPI launch lock and immediately before its durable claim.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from ampuly.project_id import is_valid_project_id
from .ai_employee_actions import AuthenticatedActor, AuthorizedProject
from .ai_employee_safe_files import AIEmployeeSafeFiles
from .api import ConsoleAPI
from .business_store import OrderTransitionError
from .employee_workflow_trial import EmployeeWorkflowTrialService


PREPARE = "employee_workflow_trial_prepare"
START = "employee_workflow_trial_start"
READ = "employee_workflow_trial_read"
ACTIONS = frozenset({PREPARE, START, READ})
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ERROR = re.compile(r"employee_trial_[a-z_]{1,100}\Z")
_COMMON = {"type", "request_id", "project_id"}
_START_FIELDS = {"workflow_id", "workflow_version", "manifest_sha256", "environment_id",
                 "fields", "budget_limits"}


def _deny(code: str = "employee_trial_forbidden") -> None:
    raise OrderTransitionError(code)


def _request(data: Any) -> tuple[str, dict[str, Any]]:
    if type(data) is not dict or any(type(key) is not str for key in data):
        _deny("employee_trial_request_invalid")
    action = data.get("type")
    if type(action) is not str or action not in ACTIONS:
        _deny("employee_trial_request_invalid")
    allowed = _COMMON if action == READ else _COMMON | _START_FIELDS
    if set(data) != allowed:
        _deny("employee_trial_request_invalid")
    project_id, request_id = data.get("project_id"), data.get("request_id")
    if (type(project_id) is not str or len(project_id) > 128 or not is_valid_project_id(project_id)
            or type(request_id) is not str or not _REQUEST_ID.fullmatch(request_id)):
        _deny("employee_trial_request_invalid")
    return action, {key: value for key, value in data.items() if key != "type"}


async def dispatch_owner_employee_workflow_trial(handler: Any, ws: Any, data: Any) -> dict[str, Any]:
    """Return one ACK payload; the caller sends it once without replaying work.

Only authenticated, unrestricted owners use this entry. A request project is
a selector, never an authority. Readback needs no engine; a new trial requires
the already active engine and its request-local API view to own the same host.
The cached API still owns the shared BusinessStore and claim/dispatch locks.
"""
    from .ws_handler import (
        _socket_behind, _socket_principal_state, _socket_principal_entry,
        _current_socket_principal, SocketPrincipalBindingError,
    )

    reply: dict[str, Any] = {"automatic_retry": False}
    started = False
    recheck_authority = None
    try:
        action, request = _request(data)
        project_id = request["project_id"]
        reply.update(action=action, project_id=project_id, request_id=request["request_id"])
        original_state = _socket_principal_state(handler)
        account_store, manager, entries = original_state
        raw_ws = _socket_behind(ws)
        original_entry = _socket_principal_entry(entries, raw_ws)
        if original_entry is None:
            _deny()  # No local-owner/bootstrap fallback, including empty stores.
        binding = original_entry[1]
        current = _current_socket_principal(handler, raw_ws, expected_binding=binding)
        if (current is None or current.role != "owner" or current.disabled is not False
                or current.customer_ids):
            _deny()
        owner_login = current.login
        business = account_store.business
        home = Path(account_store.home)
        business_path = business.path
        if not home.is_absolute() or Path(business_path) != home / "business.db":
            _deny()
        api = None
        source_api = None
        engine = getattr(handler, "engine", None) if action != READ else None
        host = getattr(engine, "employee_runtime_host", None) if engine is not None else None

        def recheck_authority() -> None:
            # This function is deliberately synchronous: no await is inserted
            # between the final current-source read and the original claim.
            try:
                if (_socket_principal_state(handler) is not original_state
                        or _socket_principal_entry(entries, raw_ws) is not original_entry
                        or manager._store is not account_store or account_store.business is not business
                        or Path(account_store.home) != home or business.path != business_path):
                    _deny()
                principal = _current_socket_principal(handler, raw_ws, expected_binding=binding)
                if (principal is None or principal.login != owner_login or principal.role != "owner"
                        or principal.disabled is not False or principal.customer_ids):
                    _deny()
                lifecycle = business.project_lifecycle(project_id)
                allowed_status = {"active", "archived"} if action == READ else {"active"}
                if lifecycle.project_id != project_id or lifecycle.status not in allowed_status:
                    _deny("employee_trial_project_inactive")
                if project_id != "default":
                    projects = AIEmployeeSafeFiles(home).list_entries("projects", limit=2001)
                    if not any(row == {"name": project_id, "kind": "directory"} for row in projects):
                        _deny("employee_trial_project_unavailable")
                if api is not None and (type(api) is not ConsoleAPI
                        or getattr(handler, "_console_api_cache", None) is not source_api
                        or api.business is not business or Path(api.ampuly_home) != home):
                    _deny()
                if action != READ:
                    if (engine is None or getattr(handler, "engine", None) is not engine
                            or getattr(engine, "project_id", None) != project_id
                            or Path(getattr(engine, "ampuly_home", "")) != home
                            or host is None or getattr(engine, "employee_runtime_host", None) is not host
                            or getattr(host, "engine", None) is not engine
                            or api is not None and api.employee_runtime_host is not host):
                        _deny("employee_trial_runtime_project_mismatch")
                # A source callback above must not replace the original
                # binding/store while returning an otherwise plausible row.
                if (_socket_principal_state(handler) is not original_state
                        or _socket_principal_entry(entries, raw_ws) is not original_entry
                        or account_store.business is not business or business.path != business_path):
                    _deny()
            except OrderTransitionError:
                raise
            except Exception:
                _deny()

        recheck_authority()
        # Use the handler's existing ConsoleAPI and original BusinessStore.
        # Its lazy accessor is reached only after a current-owner/scope check.
        source_api = handler._console_api()
        if type(source_api) is not ConsoleAPI:
            _deny()
        api = source_api
        if action != READ:
            # A request owns its host view; never mutate the shared ConsoleAPI
            # or handler.engine when the active project changed since caching.
            from copy import copy
            api = copy(source_api)
            api.employee_runtime_host = host
            if getattr(engine, "config", None) is not None:
                api._model_plan_source = lambda: engine.config.llm
            if getattr(engine, "adapter_registry", None) is not None:
                api._provider_resolver = lambda name: engine.adapter_registry.get(name)
            api._harness_registry = getattr(engine, "harness_registry", source_api._harness_registry)
            # Locks and original BusinessStore/launcher remain shared, so a
            # request-local view cannot create a second claim/dispatch lane.
        recheck_authority()
        service = EmployeeWorkflowTrialService(api, recheck_authority=recheck_authority)
        actor = AuthenticatedActor(owner_login)
        scope = AuthorizedProject(project_id, True)
        if action == PREPARE:
            result = service.prepare(request, actor=actor, scope=scope)
        elif action == READ:
            result = service.read(request_id=request["request_id"], actor=actor, scope=scope)
        else:
            # The original launch may commit before its await fails. From
            # here a lost authority/source cannot disclose an order receipt.
            started = True
            result = await service.start(request, actor=actor, scope=scope)
        recheck_authority()
        # Keep server-validated correlation outside the service result. A
        # result cannot replace the original request/project/action fields.
        if action != READ:
            reply.update(workflow_id=request["workflow_id"], workflow_version=request["workflow_version"],
                         manifest_sha256=request["manifest_sha256"])
        return {**reply, "ok": True, "result": result}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if started:
            try:
                if recheck_authority is None:
                    _deny()
                recheck_authority()
            except Exception:
                return {**reply, "ok": False, "error": "employee_trial_outcome_unknown",
                        "execution_succeeded": None, "delivery_confirmed": None}
        code = ("employee_trial_forbidden" if isinstance(exc, SocketPrincipalBindingError)
                else str(exc) if type(exc) is OrderTransitionError else "")
        if not _ERROR.fullmatch(code):
            code = "employee_trial_outcome_unknown" if started else "employee_trial_unavailable"
        return {**reply, "ok": False, "error": code}
