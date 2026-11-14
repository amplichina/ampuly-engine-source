"""Default-off task-center read over authoritative server records."""
from __future__ import annotations
import os
import time
from typing import Any
from .ai_employee_contract import AIEmployeeContractError
from .employee_task_center import task_center_read_api
from .task_center_facts import authorized_project_engine, read_task_center_facts


def task_center_api_enabled(data: dict[str, Any] | None = None) -> bool:
    """Only server configuration can enable the production path."""
    return str(os.environ.get("AMPULY_TASK_CENTER_API", "") or "").strip().lower() in {"1", "true", "yes", "on"}


async def dispatch_task_center_read(handler: Any, ws: Any, data: dict[str, Any]) -> dict[str, Any]:
    if not task_center_api_enabled():
        return {"ok": True, "skipped": True, "reason": "api_off", "effects_produced": False, "action": "employee_task_center_read"}
    try:
        engine, project_id = authorized_project_engine(handler, ws, data)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise AIEmployeeContractError("task_center_task_invalid", "A task identity is required.")
        source = await read_task_center_facts(engine, project_id, task_id.strip())
        current, current_project = authorized_project_engine(handler, ws, data)
        if current is not engine or current_project != project_id:
            raise AIEmployeeContractError("task_center_scope_denied", "Project authority changed.")
        out = task_center_read_api(enabled=True, project_id=project_id, actor_scope_ok=True,
                                  timeline_entries=source["timeline_entries"], run_facts=source["run_facts"], now_ms=time.time()*1000)
        out.update(task_id=source["task_id"], facts_source=source["facts_source"])
    except AIEmployeeContractError as exc:
        out = {"ok": False, "error": exc.code, "message": str(exc)}
    except Exception:
        out = {"ok": False, "error": "task_center_facts_unavailable", "message": "Task facts unavailable."}
    return {**out, "action": "employee_task_center_read"}
