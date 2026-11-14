"""Explicit owner actions. No public candidate/evidence injection endpoint."""
import asyncio
from copy import deepcopy
import re

from .host import ServiceHost

NAME = "customer_service"
_PARAMS = {
    "customer_service_prepare": None,
    "customer_service_test_start": {"suite_id", "budget"},
    "customer_service_detail": {"suite_id"},
    "customer_service_review": {"suite_id", "case_id", "candidate_sha256", "verdict", "note"},
    "customer_service_admit": {"suite_id"},
    "customer_service_revoke": {"suite_id", "reason_code"},
    "customer_service_channel_select": {"suite_id", "budget"},
    "customer_service_channel_pause": {"account_id"},
    "customer_service_channel_replace": {"suite_id", "budget", "expected_record_sha256"},
}


def _error(exc):
    code = str(getattr(exc, "code", "") or str(exc))
    return code if re.fullmatch(r"(?:customer_service|acceptance|autonomy|evaluation)_[a-z0-9_]{1,80}", code) else "customer_service_operation_failed"


async def dispatch(console, ws, data, *, action):
    """Wire payload can request tests/review, never assert a successful trial."""
    ledger, borrowed = None, False
    try:
        if (type(data) is not dict or set(data) - {"type", "action", "project_id", "params", "request_id"}
                or data.get("type") != action or data.get("action", action) != action
                or type(data.get("params")) is not dict):
            raise ValueError("customer_service_request_invalid")
        params = data["params"]
        if _PARAMS[action] is not None and set(params) != _PARAMS[action]:
            raise ValueError("customer_service_request_invalid")
        service = ServiceHost(console, ws, data.get("project_id"))
        state = console.extension_state(NAME)
        job = state.get("job")
        active = state.get("active_service")
        busy = job is not None and not job["task"].done()
        if action == "customer_service_prepare":
            if busy or active is not None:
                raise ValueError("customer_service_test_busy")
            result = {"suite_id": service.prepare(params), "status": "prepared", "model_called": False}
        elif action == "customer_service_channel_pause":
            from .wecom import pause_channel
            result = pause_channel(service, params["account_id"])
        else:
            if busy and job["project_id"] == service.project_id and job["actor"] == service.actor:
                ledger, borrowed = job["ledger"], True
            elif active is not None and active["project_id"] == service.project_id and active["actor"] == service.actor:
                ledger, borrowed = active["ledger"], True
            else:
                ledger = service.ledger()
            if action == "customer_service_test_start":
                if busy or active is not None:
                    raise ValueError("customer_service_test_busy")
                # Validate bounded wall-clock before creating a worker. Money,
                # tariffs and current package are rechecked inside it.
                budget = params["budget"]
                if type(budget) is not dict or type(budget.get("max_wall_clock_s")) is not int or not 1 <= budget["max_wall_clock_s"] <= 600:
                    raise ValueError("customer_service_budget_invalid")
                existing = ledger.get_suite(params["suite_id"])
                if existing["revoked"] or any(value != "pending" for value in existing["case_states"].values()):
                    raise ValueError("customer_service_test_already_started")
                owned_ledger = ledger
                async def run():
                    try:
                        async with asyncio.timeout(budget["max_wall_clock_s"]):
                            await service.run_tests(owned_ledger, params["suite_id"], deepcopy(budget))
                        state["last_run"] = {"suite_id": params["suite_id"], "state": "finished",
                                             "project_id": service.project_id, "actor": service.actor}
                    except asyncio.CancelledError:
                        state["last_run"] = {"suite_id": params["suite_id"], "state": "interrupted",
                                             "project_id": service.project_id, "actor": service.actor}
                        raise
                    except Exception as exc:
                        state["last_run"] = {"suite_id": params["suite_id"], "state": "stopped", "error": _error(exc),
                                             "project_id": service.project_id, "actor": service.actor}
                    finally:
                        owned_ledger.close()
                task = asyncio.create_task(run(), name="customer-service-acceptance")
                # Cancellation before the coroutine's first instruction does
                # not execute its finally. close() is deliberately idempotent.
                task.add_done_callback(lambda completed: owned_ledger.close())
                state["job"] = {"task": task, "ledger": owned_ledger, "project_id": service.project_id, "actor": service.actor}
                ledger = None  # Worker owns this handle until its finally.
                result = {"suite_id": params["suite_id"], "status": "scheduled", "passed": False}
            elif action == "customer_service_detail":
                result = ledger.get_suite(params["suite_id"])
                result["channel_binding"] = None
                if result["request"]["scope"]["channel"] == "wecom_cs":
                    from .wecom import binding_store
                    result["channel_binding"] = binding_store(service).read(result["request"]["scope"]["account_id"])
                last_run = state.get("last_run")
                result["last_run"] = (deepcopy(last_run) if type(last_run) is dict
                    and last_run.get("project_id") == service.project_id
                    and last_run.get("actor") == service.actor else None)
            elif action == "customer_service_review":
                service.current_basis(ledger.get_suite(params["suite_id"]))
                result = ledger.review_case(**params, actor=service.actor)
            elif action == "customer_service_admit":
                result = service.admit(ledger, params["suite_id"])
            elif action in {"customer_service_channel_select", "customer_service_channel_replace"}:
                if busy or active is not None:
                    raise ValueError("customer_service_test_busy")
                if action == "customer_service_channel_replace" and (
                        type(params["expected_record_sha256"]) is not str
                        or re.fullmatch(r"[a-f0-9]{64}", params["expected_record_sha256"]) is None):
                    raise ValueError("customer_service_replacement_refused")
                from .wecom import select_channel
                result = select_channel(service, ledger, **params)
            else:
                ledger.revoke_suite(**params, actor=service.actor)
                result = {"suite_id": params["suite_id"], "status": "revoked"}
        service.check()
        await console.send_ack(ws, action=action, ok=True, project_id=service.project_id,
                               request_id=data.get("request_id"), customer_service=result)
    except Exception as exc:
        await console.send_ack(ws, action=action, ok=False, error=_error(exc))
    finally:
        if ledger is not None and not borrowed:
            ledger.close()


def _handler(action):
    async def handle(console, ws, data):
        await dispatch(console, ws, data, action=action)
    return handle


ACTIONS = {action: _handler(action) for action in _PARAMS}
