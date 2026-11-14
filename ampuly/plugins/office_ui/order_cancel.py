"""Cancel a whole order, and say exactly what that did and did not do.

An order in production has three things going at once: a company run
dispatching work items, harness turns executing them (possibly in another
process), and effects some of those turns may already have caused. A cancel
that only flips the order's status leaves the first two running and lies
about the third. This does what can be done and records the rest:

- the run is suspended (no further dispatch), then marked ended, so no restart parks it again;
- every harness turn recorded against the order is asked to stop, through
  the registry, so a turn in another process hears it too; the request is
  recorded per turn, not claimed as a stop;
- the browser of each of its work items is cancelled too (Q05 B5): an action in
  flight ends cancelled_unknown, a decider loop stops before its next step,
  and nothing more is sent until someone looks at the page again;
- the pauses its run raised and nobody answered yet (a tool asking leave, a
  question, a restart's park) are closed with the order, so a cancelled order
  waits on nobody -- not in 待审批, not in the group, not on its own row; the
  park the cancel's own suspend leaves is closed too: a cancelled order's run
  is never resumed, so it needs no resume entry and no 运行已停放 row;
- the order moves to ``closed`` (or ``rejected`` when it never launched), with
  the reason and the whole outcome in its hash-chained event;
- effects already caused are not rolled back, and the event says so.

Everything is checked before anything is stopped: an order that cannot be
cancelled from its state is refused with nothing touched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ampuly.core.interaction_protocol import OWNER_INTERACTION_CHECKPOINT_TYPES
from ampuly.plugins.office_ui.blockers import blocker_from_checkpoint, root_session_id
from ampuly.plugins.office_ui.business_store import ORDER_TRANSITIONS, OrderTransitionError

NOT_YET_RUNNING = frozenset({"collecting", "pending_review", "launching"})

# Written into every pause the cancel closes, and into the order's event: the owner reads why a card went away.
CANCELLED_PAUSE_REASON = "整单已取消"
# 确认收货并结案 closes what the order still has waiting (runtime contract); the delivery's
# lesson card, still open then, says the order was closed with no lesson left for the AI employees.
CLOSED_ORDER_PAUSE_REASON = "订单已结案"
NO_LESSON_LEFT_REASON = "订单已结案，没留经验"

# Nothing pending that belongs to the order's run is kept -- not even the park the cancel's own suspend leaves. That
# park is how a stopped run is resumed, and a cancelled order's run is never resumed; kept, it read 运行已停放 in
# 待审批 for an order already closed (Regression case).


async def close_order_pauses(
    engine: Any,
    *,
    project_id: str,
    order_id: str,
    task_ids: set[str],
    session_ids: set[str],
    reason: str = CANCELLED_PAUSE_REASON,
    reasons_by_type: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Close every pending pause that belongs to the order's run; say which, and which could not be.

    A pause belongs to the order when it is anchored (the same reading the console's rows make,
    ``blocker_from_checkpoint``) on the order itself, on one of its run's tasks, or on the run's own
    session. Owner interactions (a tool asking leave, a question, a gate, a delivery card) are closed
    through their typed administrative closure, which never consumes a decision: one answered in the
    meantime is left to its answer and reported as skipped. A restart's park is resolved directly.
    ``reasons_by_type`` gives a pause of that checkpoint type its own reason instead of ``reason``.
    """
    store = getattr(engine, "store", None)
    ready = getattr(store, "is_ready", None)
    if store is None or (callable(ready) and not ready()) or ready is False:
        return {"reason": reason, "closed": [], "skipped": [], "failed": [], "store": "unavailable"}
    pending = await store.get_pending_checkpoints(project_id=project_id)
    anchors = {str(item or "").strip() for item in {order_id, *task_ids}} - {""}
    roots = {root_session_id(item) for item in session_ids} - {""}
    closed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    now = datetime.now().isoformat()
    coordinator = getattr(engine, "interaction_coordinator", None)
    patch_non_owner = getattr(store, "patch_execution_checkpoint_payload", None)
    for checkpoint in list(pending or []):
        checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        if not checkpoint_id:
            continue
        blocker = blocker_from_checkpoint(checkpoint)
        raw_session = str(getattr(checkpoint, "session_id", "") or "").strip()
        own = {
            str(blocker.get("task_id", "") or ""), str(blocker.get("session_id", "") or ""),
            str(blocker.get("waiting_task_id", "") or ""), str(getattr(checkpoint, "task_id", "") or "").strip(), raw_session,
        } - {""}
        if not (own & anchors) and root_session_id(raw_session) not in roots and root_session_id(blocker.get("session_id")) not in roots:
            continue
        entry = {"checkpoint_id": checkpoint_id, "checkpoint_type": checkpoint_type}
        said = str(dict(reasons_by_type or {}).get(checkpoint_type) or reason)
        patch = {"checkpoint_resolution_reason": said, "administrative_reason": said, "administrative_closed_at": now}
        try:
            if checkpoint_type in OWNER_INTERACTION_CHECKPOINT_TYPES:
                if coordinator is not None:
                    _, applied = await coordinator.close_pending_owner_checkpoint(
                        checkpoint_id, checkpoint_type=checkpoint_type, status="cancelled", payload_patch=patch,
                    )
                else:
                    _, applied = await store.close_pending_owner_interaction_checkpoint(
                        checkpoint_id, project_id=project_id, checkpoint_type=checkpoint_type, status="cancelled", payload_patch=patch,
                    )
            elif callable(patch_non_owner):
                # The run's own park (a stop, a restart, the cancel's suspend) closes with the reason on its row, and
                # only while still pending: a resume that beat the cancel is left to its own record.
                _, applied = await patch_non_owner(
                    checkpoint_id, project_id=project_id, checkpoint_type=checkpoint_type,
                    expected_statuses=("pending",), payload_patch=patch, status="cancelled",
                )
            else:
                await store.resolve_execution_checkpoint(checkpoint_id, status="cancelled")
                applied = True
        except Exception as exc:  # noqa: BLE001 - one pause that cannot be closed must not hide the others, or stop the cancel
            failed.append({**entry, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        (closed if applied else skipped).append(entry)
    return {"reason": reason, "closed": closed, "skipped": skipped, "failed": failed}


def cancel_target(status: str) -> str:
    """Where a cancelled order ends up: rejected before it ran, closed after."""
    return "rejected" if status in NOT_YET_RUNNING else "closed"


async def cancel_order(
    business: Any,
    order_id: str,
    *,
    actor: str,
    reason: str,
    registry: Any = None,
    suspend: Any = None,
    project_id: str | None = None,
    browsers: Any = None,
    checkpoints: Any = None,
    end_runs: Any = None,
) -> dict[str, Any]:
    """Stop what can be stopped, then close the order with the record of it.

    ``checkpoints`` closes the run's pending pauses (``close_order_pauses`` bound to the order's engine);
    without it they are left as they are, and the record says nothing about them.

    ``end_runs`` marks the order's run ended once it is suspended (runtime contract:
    ``AmpulyEngine.end_company_runs_of_ended_order``), so no restart parks it again. Without it, the engine whose
    suspend stopped the run ends it; with neither, the next start does (its reconcile asks whether the order ended).
    """
    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 2:
        raise OrderTransitionError("取消要写原因；这句话会写进工单事件")
    # Every store call leaves the loop. This coroutine is awaited by the
    # console's websocket handler, and BusinessStore waits up to five seconds
    # for a lock: on the loop, one contended write froze every socket.
    order = await asyncio.to_thread(business.get_order, order_id, project_id=project_id)
    if order is None:
        raise OrderTransitionError(f"order {order_id!r} not found")
    if order.status in {"closed", "rejected"}:
        raise OrderTransitionError(f"order {order_id} is already {order.status}; there is nothing to cancel")
    target = cancel_target(order.status)
    if target not in ORDER_TRANSITIONS.get(order.status, frozenset()):
        raise OrderTransitionError(f"order {order_id} cannot be cancelled from {order.status}")

    outcome: dict[str, Any] = {
        "reason": clean_reason,
        "from_status": order.status,
        "runtime_suspended": False,
        "interrupts_requested": [],
        # Said here, in the record, so nobody reads "closed" as "undone".
        "effects": "not_rolled_back",
    }
    launched = str(getattr(order, "launched_task_id", "") or "")
    # The order's work items, as far as they are known: the launched task and what the suspend reached.
    task_ids: set[str] = {launched} if launched else set()
    # The run's own session, as the suspend resolved it from the launched task: a pause raised under it is the order's.
    session_ids: set[str] = set()
    if launched and suspend is not None:
        try:
            result = await suspend(origin_task_id=launched, reason="order_cancelled")
            outcome["runtime_suspended"] = result is not None
            if isinstance(result, dict):
                task_ids |= {str(t) for t in list(result.get("task_ids", []) or []) if str(t or "").strip()}
                if str(result.get("session_id", "") or "").strip():
                    session_ids.add(str(result.get("session_id", "")).strip())
        except Exception as exc:  # the cancel goes on; the failure is recorded
            outcome["runtime_suspend_error"] = f"{type(exc).__name__}: {exc}"[:200]
    ender = end_runs if end_runs is not None else getattr(
        getattr(suspend, "__self__", None), "end_company_runs_of_ended_order", None,
    )
    if launched and callable(ender):
        # The suspended run is over with its order: ended, never parked again by a restart. A run a live controller
        # still holds is left to the next start. A failure is recorded; the cancel goes on.
        try:
            outcome["runs"] = dict(await ender(
                origin_task_id=launched, order_id=order_id, ended_as="cancelled", actor=actor, reason=clean_reason,
            ) or {})
        except Exception as exc:  # noqa: BLE001
            outcome["runs_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if registry is not None:
        sessions = await asyncio.to_thread(registry.sessions_for_order, order_id)
        for session in list(sessions or []):
            session_id = str(getattr(session, "session_id", "") or "")
            if not session_id:
                continue
            turn_id = await asyncio.to_thread(registry.request_interrupt, session_id, actor=actor)
            if turn_id:
                outcome["interrupts_requested"].append({"session_id": session_id, "turn_id": turn_id})
            session_task = str(getattr(session, "task_id", "") or "").strip()
            if session_task:
                task_ids.add(session_task)
    if browsers is not None and task_ids:
        # In the loop, not a thread: the browser Gateway's bookkeeping lives on it. A failure is recorded; the cancel goes on.
        try:
            outcome["browsers"] = dict(browsers(str(project_id or getattr(order, "project_id", "") or ""), task_ids,
                                                f"order {order_id} cancelled") or {})
        except Exception as exc:  # noqa: BLE001
            outcome["browsers_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if checkpoints is not None:
        # The pauses nobody answered yet are closed with the order (a cancelled order still read 等人：browser_navigate
        # on its row, and its card still waited in 待审批 and the group). A failure is recorded; the cancel goes on.
        try:
            outcome["checkpoints"] = dict(await checkpoints(
                project_id=str(project_id or getattr(order, "project_id", "") or "default"), order_id=order_id,
                task_ids=set(task_ids), session_ids=set(session_ids), reason=CANCELLED_PAUSE_REASON,
            ) or {})
        except Exception as exc:  # noqa: BLE001
            outcome["checkpoints_error"] = f"{type(exc).__name__}: {exc}"[:200]
    # Shielded: by now the run may be suspended and turns asked to stop, and
    # the transition is the record that says so. A socket dropping here must
    # not leave those done and this undone.
    updated = await asyncio.shield(asyncio.to_thread(
        business.transition,
        order_id, target, actor=actor, note=clean_reason, extra={"cancel": outcome}, project_id=project_id,
    ))
    return {"order": updated.to_payload(), "cancel": outcome, "final_status": target}
