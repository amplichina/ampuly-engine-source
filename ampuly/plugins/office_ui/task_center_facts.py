"""Read-only task authority over existing project/task/runtime records."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .ai_employee_contract import AIEmployeeContractError


def _refuse(code: str) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, "Task-center facts unavailable for this scope.")


def _current_owner_allowed(handler: Any, ws: Any) -> bool:
    # WSHandler._ws_is_owner delegates to its fresh socket-principal predicate.
    # Depending on the bound server interface avoids importing the route module
    # (and its application initialization dependencies) into a read helper.
    check = getattr(handler, "_ws_is_owner", None)
    return callable(check) and check(ws) is True


def authorized_project_engine(handler: Any, ws: Any, data: dict[str, Any]) -> tuple[Any, str]:
    if not _current_owner_allowed(handler, ws):
        raise _refuse("task_center_scope_denied")
    project_id = data.get("project_id") or data.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        raise _refuse("task_center_project_invalid")
    project_id = project_id.strip()
    engine = getattr(handler, "engine", None)
    # Never build/attach another project as a side effect of a fact read.
    if engine is None or str(getattr(engine, "project_id", "") or "").strip() != project_id:
        raise _refuse("task_center_scope_denied")
    if getattr(engine, "store", None) is None:
        raise _refuse("task_center_facts_unavailable")
    return engine, project_id


def _value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().casefold()


def _milliseconds(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp() * 1000
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


async def read_task_center_facts(engine: Any, project_id: str, task_id: str) -> dict[str, Any]:
    store = engine.store
    getter = getattr(store, "get_task", None)
    if not callable(getter):
        raise _refuse("task_center_facts_unavailable")
    task = await getter(task_id)
    if task is None:
        raise _refuse("task_center_task_not_found")
    if str(_value(task, "project_id", "") or "") != project_id:
        raise _refuse("task_center_scope_denied")
    status = _status(_value(task, "status"))
    metadata = _value(task, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    runtime = metadata.get("runtime_v2")
    runtime = runtime if isinstance(runtime, dict) else {}
    context = _value(task, "context_snapshot", {})
    context = context if isinstance(context, dict) else {}
    resume = context.get("runtime_resume")
    resume = resume if isinstance(resume, dict) else {}
    pinned = str(runtime.get("runtime_session_id") or resume.get("runtime_session_id") or "").strip()
    facts: dict[str, Any] = {"status": status, "task_id": task_id, "project_id": project_id}
    session = None
    reader = getattr(store, "get_runtime_session", None)
    if pinned and callable(reader):
        session = await reader(pinned)
        if session is not None and (str(session.get("project_id") or "") != project_id
                                    or str(session.get("task_id") or "") != task_id):
            raise _refuse("task_center_scope_denied")
    entries: list[dict[str, Any]] = []
    if isinstance(session, dict):
        facts.update(runtime_session_id=pinned, has_runtime_stamp=True)
        runtime_status = _status(session.get("status"))
        if status not in {"done", "completed", "succeeded", "failed", "cancelled", "canceled"}:
            if runtime_status in {"outcome_unknown", "effect_unknown", "approval_required", "awaiting_human", "cancel_requested", "stop_requested", "blocked", "paused"}:
                facts["status"] = runtime_status
        reader = getattr(store, "list_runtime_events", None)
        if callable(reader):
            for row in await reader(pinned, limit=100):
                if row.get("runtime_session_id") != pinned:
                    raise _refuse("task_center_scope_denied")
                entries.append({"id": row.get("event_id"), "at": row.get("created_at"), "kind": row.get("event_type")})
    # Existing server-persisted identities only; absent identity is not guessed.
    facts["generation"] = resume.get("runtime_session_id")
    facts["current_generation"] = runtime.get("runtime_session_id")
    dispatched = _milliseconds(_value(task, "execution_locked_at"))
    if dispatched is not None:
        facts["dispatched_at_ms"] = dispatched
    # A task can be rebound/deleted while session/event reads await I/O. Do not
    # publish facts whose object scope or current run identity changed mid-read.
    current = await getter(task_id)
    if current is None or str(_value(current, "project_id", "") or "") != project_id:
        raise _refuse("task_center_scope_denied")
    current_meta = _value(current, "metadata", {})
    current_meta = current_meta if isinstance(current_meta, dict) else {}
    current_runtime = current_meta.get("runtime_v2")
    current_runtime = current_runtime if isinstance(current_runtime, dict) else {}
    current_context = _value(current, "context_snapshot", {})
    current_context = current_context if isinstance(current_context, dict) else {}
    current_resume = current_context.get("runtime_resume")
    current_resume = current_resume if isinstance(current_resume, dict) else {}
    current_pin = str(current_runtime.get("runtime_session_id") or current_resume.get("runtime_session_id") or "").strip()
    if current_pin != pinned or _status(_value(current, "status")) != status:
        raise _refuse("task_center_facts_changed")
    return {"run_facts": facts, "timeline_entries": entries, "task_id": task_id,
            "facts_source": "store.task/runtime_session/runtime_events", "scope_verified": True}
