"""WS dispatch for pending-approval badge read + blockers-channel push plan (default off)."""
from __future__ import annotations

import os
import asyncio
import inspect
from typing import Any

from ampuly.plugins.office_ui.ai_employee_contract import AIEmployeeContractError
from ampuly.plugins.office_ui.employee_usability import (
    pending_approval_badge_read_api,
    plan_pending_approval_ws_push,
)


def _env_on(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def badge_api_enabled(data: dict[str, Any] | None = None) -> bool:
    # Test injection belongs in test construction, never in a production frame.
    return _env_on("AMPULY_PENDING_APPROVAL_BADGE")


async def read_visible_pending_blockers(handler: Any, ws: Any, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Read the owner's attached-project pending facts, sharing blocker projection.

    Never consumes request items, scope flags, timestamps or preferences. The
    project/owner predicate is rechecked here even when called outside WS routing.
    """
    from ampuly.plugins.office_ui.task_center_facts import authorized_project_engine
    from ampuly.plugins.office_ui.blockers import blocker_from_checkpoint, hide_pauses_of_suspended_runs

    engine, project_id = authorized_project_engine(handler, ws, data)
    store = getattr(engine, "store", None)
    read = getattr(store, "get_pending_checkpoints", None)
    if not callable(read):
        raise AIEmployeeContractError("pending_approval_unavailable", "Pending approval store is unavailable.")
    checkpoints = read(project_id=project_id)
    if inspect.isawaitable(checkpoints):
        checkpoints = await checkpoints
    items: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        if str(getattr(checkpoint, "project_id", "") or "").strip() != project_id:
            raise AIEmployeeContractError("pending_approval_scope_denied", "Pending fact does not belong to the authorized project.")
        item = blocker_from_checkpoint(checkpoint)
        item["status"] = str(getattr(checkpoint, "status", "") or "").strip()
        if not item["status"]:
            raise AIEmployeeContractError("pending_approval_fact_invalid", "Pending fact is missing its authoritative status.")
        # The canonical store returns pending rows. Revisions, if provided by a
        # store implementation, order duplicates without trusting the client.
        revision = getattr(checkpoint, "revision", None)
        if type(revision) is int:
            item["revision"] = revision
        items.append(item)
    closed_task_ids: set[str] = set()
    accounts = getattr(handler, "_accounts", None)
    if callable(accounts):
        business = getattr(accounts(), "business", None)
        list_orders = getattr(business, "list_orders", None)
        if callable(list_orders):
            orders = await asyncio.to_thread(list_orders, project_id=project_id, status="closed")
            closed_task_ids = {str(getattr(order, "launched_task_id", "") or "").strip() for order in orders} - {""}
    items, _ = hide_pauses_of_suspended_runs(items, closed_task_ids=closed_task_ids)
    confirmed_engine, confirmed_project = authorized_project_engine(handler, ws, data)
    if confirmed_engine is not engine or confirmed_project != project_id:
        raise AIEmployeeContractError("pending_approval_scope_denied", "Pending view scope changed during the read.")
    return project_id, items


async def dispatch_pending_approval_badge_read(handler: Any, ws: Any, data: dict[str, Any]) -> dict[str, Any]:
    enabled = badge_api_enabled(data)
    if not enabled:
        return {**pending_approval_badge_read_api(enabled=False), "action": "pending_approval_badge_read"}
    try:
        project_id, items = await read_visible_pending_blockers(handler, ws, data)
        badge = pending_approval_badge_read_api(enabled=True, pending_items=items)
        push = None
        if enabled and not badge.get("skipped"):
            push = plan_pending_approval_ws_push(badge=badge, existing_ws_channel="blockers")
    except AIEmployeeContractError as exc:
        return {
            "ok": False,
            "action": "pending_approval_badge_read",
            "error": exc.code,
            "message": str(exc),
            "details": getattr(exc, "details", None),
        }
    except Exception:
        # Never turn read failure into a false zero/no-approval result.
        return {"ok": False, "action": "pending_approval_badge_read", "error": "pending_approval_unavailable", "limited": True}
    out = dict(badge)
    out["action"] = "pending_approval_badge_read"
    out["project_id"] = project_id
    out["fact_source"] = "server_pending_checkpoints"
    actor_reader = getattr(handler, "_ws_actor_login", None)
    actor_login = str(actor_reader(ws) or "").strip() if callable(actor_reader) else ""
    owner_check = getattr(handler, "_ws_is_owner", None)
    if not actor_login or not callable(owner_check) or owner_check(ws) is not True:
        return {"ok": False, "action": "pending_approval_badge_read", "error": "pending_approval_scope_denied"}
    out["visibility_scope"] = {"project_id": project_id, "role": "owner", "actor": actor_login}
    out["actor_login"] = actor_login
    if push is not None:
        push["payload"].update(project_id=project_id, actor_login=out["actor_login"],
                               visibility_scope=out["visibility_scope"])
        out["ws_push"] = push
    return out


def badge_from_blocker_items(
    items: list[dict[str, Any]], preferences: dict[str, Any] | None = None, *,
    project_id: str = "", actor_login: str = "",
) -> dict[str, Any] | None:
    """Optional attachment for blockers_list when AMPULY_PENDING_APPROVAL_BADGE=1."""
    if not _env_on("AMPULY_PENDING_APPROVAL_BADGE"):
        return None
    # Existing blockers are an authoritative pending-query projection; legacy
    # blocker shapes have checkpoint_id/kind but no explicit status field.
    pending = [{**row, **({"status": "pending"} if row.get("checkpoint_id") and not row.get("status") and not row.get("state") else {})} for row in items]
    badge = pending_approval_badge_read_api(enabled=True, pending_items=pending, preferences=preferences or {})
    badge.update(project_id=project_id, actor_login=actor_login,
                 visibility_scope={"project_id": project_id, "role": "owner", "actor": actor_login},
                 fact_source="server_blockers_projection")
    return badge
