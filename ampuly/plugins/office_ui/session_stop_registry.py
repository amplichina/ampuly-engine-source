"""Notify exact task-bound Harness turns without changing business states.

Call after durable company suspend holds (or legacy Stop marking), using the
already resolved project engine. A durable interrupt is only a request: the
provider's terminal record remains authoritative. No order/run enumeration,
session closing, pending-turn settlement, adoption or automatic retry occurs.
"""
from __future__ import annotations

import asyncio
from typing import Any


MAX_STOP_TASKS = 256
MAX_STOP_SESSIONS = 1024


def _identifier(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip() and not any(ord(char) < 32 for char in value)


async def notify_session_stop_registry(engine: Any, *, project_id: str, task_ids: list[str], actor: str) -> dict[str, Any]:
    """Return scoped request receipts, never a claim that a turn has stopped.

    Only server-resolved task IDs are accepted by the caller. Every registry
    row is rechecked against the exact project/task before even asking for its
    pending turn. CAS prevents an old turn snapshot from interrupting a newer
    turn. Read or write failure is reported honestly and is never retried here.
    """
    result: dict[str, Any] = {"status": "idle", "interrupts": [], "errors": []}

    def error(code: str, task_id: str = "") -> None:
        result["errors"].append({"code": code, "task_id": task_id})

    if not _identifier(project_id) or not _identifier(actor) or type(task_ids) is not list or len(task_ids) > MAX_STOP_TASKS or any(not _identifier(task_id) for task_id in task_ids):
        error("stop_registry_scope_invalid")
        result["status"] = "failed"
        return result
    engine_project = getattr(engine, "project_id", None)
    # The real root AmpulyEngine may retain None; ProjectEngines maps it only to
    # "default", never to whichever project happened to be active in the UI.
    if engine_project in (None, ""):
        engine_project = "default"
    if engine_project != project_id:
        error("stop_registry_project_mismatch")
        result["status"] = "failed"
        return result
    registry = getattr(engine, "harness_registry", None)
    if registry is None:
        result["status"] = "unsupported"
        return result
    if any(not callable(getattr(registry, name, None)) for name in ("sessions_for_task", "pending_turn_id", "request_interrupt")):
        error("stop_registry_api_unavailable")
        result["status"] = "failed"
        return result
    seen: set[str] = set()
    observed = 0
    for task_id in dict.fromkeys(task_ids):
        try:
            records = await asyncio.to_thread(registry.sessions_for_task, task_id)
        except Exception:
            error("stop_registry_read_failed", task_id)
            continue
        if type(records) is not list or len(records) + observed > MAX_STOP_SESSIONS:
            error("stop_registry_session_limit", task_id)
            break
        observed += len(records)
        for record in records:
            # Foreign or incorrectly bound rows disclose no session/turn IDs
            # and receive no interrupt, even if the registry lookup is faulty.
            if getattr(record, "project_id", None) != project_id or getattr(record, "task_id", None) != task_id:
                error("stop_registry_scope_denied", task_id)
                continue
            session_id = getattr(record, "session_id", None)
            if not _identifier(session_id):
                error("stop_registry_session_invalid", task_id)
                continue
            if session_id in seen or getattr(record, "state", None) in ("closed", "lost"):
                continue
            seen.add(session_id)
            try:
                turn_id = await asyncio.to_thread(registry.pending_turn_id, session_id)
            except Exception:
                error("stop_registry_read_failed", task_id)
                continue
            if turn_id == "":
                continue
            if not _identifier(turn_id):
                error("stop_registry_turn_invalid", task_id)
                continue
            try:
                requested = await asyncio.to_thread(registry.request_interrupt, session_id,
                                                     actor=actor, expected_turn_id=turn_id)
            except Exception as exc:
                # HarnessSessionBusy is the expected-turn CAS refusal; another
                # exception may have landed after the durable event was saved.
                error("stop_registry_turn_changed" if type(exc).__name__ == "HarnessSessionBusy" else "stop_registry_outcome_unknown", task_id)
                continue
            if requested == "":
                continue  # The snapshotted turn settled before the request.
            if requested != turn_id:
                error("stop_registry_outcome_unknown", task_id)
                continue
            result["interrupts"].append({"task_id": task_id, "session_id": session_id, "turn_id": requested})
    result["status"] = ("partial" if result["interrupts"] else "failed") if result["errors"] else ("requested" if result["interrupts"] else "idle")
    return result
