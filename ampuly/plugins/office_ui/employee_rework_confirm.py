"""One current-owner confirmation through the original Company controller.

No independent worker, model call, token cache, bootstrap owner or permission
fallback. The handler retains the original socket identity for this request;
the runtime host rechecks it before and after its current execution checks.
"""
from __future__ import annotations

from ampuly.layer2_organization.human_review_rework import (
    ACTION, HumanReviewReworkError, validate_confirm_request, _fail,
)
from ampuly.layer2_organization.human_review_rework_executor import restart_human_review_rework


async def confirm_owner_rework(handler, ws, data):
    # Late import prevents a module cycle without constructing another host.
    from .ws_handler import (
        _socket_behind, _socket_principal_state, _socket_principal_entry,
        _current_socket_principal,
    )
    from .business_store import ENDED_ORDER_STATUSES
    from ampuly.core.company_controller import CompanyRunControllerBusy

    request = validate_confirm_request(data)
    reply = {"action": ACTION, "request_id": request["request_id"],
             "project_id": request["project_id"], "order_id": request["order_id"],
             "rework_record_id": request["rework_record_id"]}
    try:
        original_state = _socket_principal_state(handler)
        account_store, manager, entries = original_state
        raw_ws = _socket_behind(ws)
        original_entry = _socket_principal_entry(entries, raw_ws)
        if original_entry is None:
            _fail("rework_forbidden")
        binding = original_entry[1]
        current = _current_socket_principal(handler, raw_ws, expected_binding=binding)
        if current is None or current.role != "owner" or current.disabled:
            _fail("rework_forbidden")
        owner_login = current.login
        business = account_store.business

        async def recheck_owner_async():
            if (_socket_principal_state(handler) is not original_state
                    or _socket_principal_entry(entries, raw_ws) is not original_entry
                    or manager._store is not account_store or account_store.business is not business):
                _fail("rework_forbidden")
            actor = _current_socket_principal(handler, raw_ws, expected_binding=binding)
            if (actor is None or actor.role != "owner" or actor.disabled
                    or actor.login != owner_login):
                _fail("rework_forbidden")
            # Scope is taken from the owned BusinessStore, not the child Task
            # or browser's metadata. Stop/order lifecycle is checked again by
            # the owned runtime host on each confirm_rework invocation.
            order = business.get_order(request["order_id"], project_id=request["project_id"])
            if (order is None or order.project_id != request["project_id"]
                    or order.launched_task_id != request["task_id"]
                    or order.status in ENDED_ORDER_STATUSES):
                _fail("rework_stale")
            return actor.login

        await recheck_owner_async()
        engine = await handler._engine_for_project(request["project_id"])
        if engine is None or engine.project_id != request["project_id"]:
            _fail("rework_source_unavailable")
        target = await handler._resolve_company_runtime_target(request["task_id"], engine=engine)
        if target is None:
            _fail("rework_source_unavailable")
        config_task = target.get("config_task") or target.get("task")
        run_id = (getattr(config_task, "metadata", {}) or {}).get("delegation_run_id")
        session_id = target.get("runtime_session_id")
        if type(run_id) is not str or not run_id or type(session_id) is not str or not session_id:
            _fail("rework_source_unavailable")
        snapshot = await engine._load_company_runtime_snapshot(session_id, delegation_run_id=run_id)
        executor = getattr(engine, "company_executor", None)
        host = getattr(engine, "employee_runtime_host", None)
        if (snapshot is None or executor is None or host is None
                or getattr(executor, "employee_runtime_host", None) is not host):
            _fail("rework_source_unavailable")
        plan, tasks = snapshot
        request_host = host.for_owner_request(owner_login=owner_login,
                                              recheck_owner_async=recheck_owner_async)
        await recheck_owner_async()
        result = await restart_human_review_rework(executor, plan, tasks, request=request,
                                                   host=request_host, schedule_execution=handler._track)
        # A revoked or disconnected session after a commit is unknown to this
        # client, not proof that the command did not happen. Keep request_id.
        try:
            await recheck_owner_async()
        except Exception:
            return {**reply, "ok": False, "error": "rework_outcome_unknown"}
        return {**reply, "ok": True, **result}
    except HumanReviewReworkError as exc:
        return {**reply, "ok": False, "error": exc.code}
    except CompanyRunControllerBusy:
        return {**reply, "ok": False, "error": "rework_busy"}
    except Exception:
        # The native transaction/continuation may already have committed.
        # Never automatically retry or expose raw exceptions/credentials.
        return {**reply, "ok": False, "error": "rework_outcome_unknown"}
