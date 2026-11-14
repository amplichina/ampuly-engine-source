"""The duty officer's rules: what deserves a message, and when.

Given the master-control context (orders with their waits, last actions and
stall verdicts; pending checkpoints) and what has already been announced,
decide which messages to send now. Pure: the WebSocket handler owns the
clock, the state and the sending; this module only decides.

Three things earn a message, each once:
- a checkpoint that appeared and is waiting on a person;
- an order that reached a terminal status;
- an order the stall rule judged stuck — repeated at most hourly, because a
  stall that persists is still one stall.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ampuly.plugins.office_ui.notifier import compose_finished, compose_stalled, compose_waiting


def compose_reminder_resumed(waiting_text: str) -> str:
    """The hold the owner asked for has run out; the same pause is still waiting."""
    import os

    zh = str(os.environ.get("AMPULY_RESPONSE_LANGUAGE", "") or "").lower().startswith("zh")
    prefix = "【暂缓到期】" if zh else "[hold expired] "
    return prefix + waiting_text

TERMINAL_STATUSES = {"done", "delivered", "failed", "cancelled"}
STALL_REPEAT_SECONDS = 60 * 60
SILENT_CHECKPOINT_TYPES = {"company_runtime_interrupted"}


@dataclass
class DutyState:
    """What has been announced. Kept in memory on purpose: after a restart a
    checkpoint still pending is worth one more message, not silence."""

    notified_checkpoints: set[str] = field(default_factory=set)
    # A19: checkpoints whose reminder the owner held. When one comes back
    # into the pending list the hold has expired, and that is news once,
    # even though the checkpoint itself was announced before the hold.
    held_checkpoints: set[str] = field(default_factory=set)
    last_status: dict[str, str] = field(default_factory=dict)
    stall_notified_at: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


@dataclass
class DutyMessage:
    kind: str  # waiting | finished | stalled
    order_id: str
    text: str
    action: str = ""
    result: str = ""
    failure_reason: str = ""


def plan_messages(context: dict[str, Any], state: DutyState, *, base_url: str, now: float | None = None) -> list[DutyMessage]:
    """Decide what to send now and record it in ``state``."""
    now = time.time() if now is None else now
    orders = {str(o.get("task_id", "") or ""): o for o in list(context.get("orders", []) or [])}
    messages: list[DutyMessage] = []

    # The master context lists held reminders apart from the pending ones.
    # Remember them, so their return to the pending list is recognised.
    for item in list(context.get("snoozed_checkpoints", []) or []):
        held_id = str(item.get("checkpoint_id", "") or "")
        if held_id:
            state.held_checkpoints.add(held_id)

    for item in list(context.get("pending_checkpoints", []) or []):
        checkpoint_id = str(item.get("checkpoint_id", "") or "")
        if not checkpoint_id:
            continue
        resumed = checkpoint_id in state.held_checkpoints
        if resumed:
            state.held_checkpoints.discard(checkpoint_id)
        elif checkpoint_id in state.notified_checkpoints:
            continue
        # A suspended run leaves an "interrupted" checkpoint behind; nobody is
        # being asked anything, the owner stopped it. Not worth a page.
        if str(item.get("checkpoint_type", "") or "") in SILENT_CHECKPOINT_TYPES:
            continue
        # A checkpoint left behind by an order that already ended asks nobody.
        owner_status = str(orders.get(str(item.get("task_id", "") or ""), {}).get("status", "") or "")
        if owner_status in TERMINAL_STATUSES:
            continue
        state.notified_checkpoints.add(checkpoint_id)
        order = orders.get(str(item.get("task_id", "") or ""), {})
        title = str(order.get("title", "") or item.get("task_id", "") or "")[:40]
        text = compose_waiting(title, str(item.get("subject", "") or ""), str(item.get("waiting_for", "") or ""), base_url=base_url)
        if resumed:
            text = compose_reminder_resumed(text)
        messages.append(DutyMessage(
            "waiting",
            str(item.get("task_id", "") or ""),
            text,
            action="reminder_resumed" if resumed else "approval_requested",
            result="waiting",
        ))

    for order_id, order in orders.items():
        status = str(order.get("status", "") or "")
        previous = state.last_status.get(order_id)
        state.last_status[order_id] = status
        # Only a transition seen by this process counts: an order that was
        # already finished when the watch started is history, not news.
        if previous is not None and previous != status and status in TERMINAL_STATUSES:
            messages.append(DutyMessage(
                "finished", order_id,
                compose_finished(str(order.get("title", "") or order_id)[:40], status, base_url=base_url),
                action="status_changed",
                result=status,
            ))

        if order.get("stalled") and status not in TERMINAL_STATUSES:
            last = state.stall_notified_at.get(order_id)
            if last is None or now - last >= STALL_REPEAT_SECONDS:
                state.stall_notified_at[order_id] = now
                messages.append(DutyMessage(
                    "stalled",
                    order_id,
                    compose_stalled(
                        str(order.get("title", "") or order_id)[:40],
                        str(order.get("since_last_tool", "") or ""),
                        str(order.get("last_tool", "") or ""),
                        base_url=base_url,
                    ),
                    action="stall_detected",
                    result="stalled",
                    failure_reason=str(order.get("stall_reason", "") or "idle_without_progress").strip(),
                ))
        elif not order.get("stalled"):
            # Recovered: the next stall is news again.
            state.stall_notified_at.pop(order_id, None)

    return messages
