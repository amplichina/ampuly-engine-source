"""Everything currently waiting on a person, in one shape.

A run that pauses for a checkpoint produces no logs and no runtime activity,
so from the outside it is indistinguishable from a hang (observed twice on
2026-09-01: a web_fetch approval and a list_dir approval each stalled a
station for tens of minutes while the console reported zero blockers). The
dashboard's blocker list and the master-control context both need the same
reading of a pending checkpoint; keeping it here means they cannot drift.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any


# What kind of human pause a checkpoint is.  The console used to label
# everything that was not a business gate as "tool authorization", so a
# suspended run's restart marker and a delivery-feedback card both read as
# permission requests (seen in the approval center on 2026-09-06).
_CHECKPOINT_KINDS = {
    "company_work_item_gate": "gate",
    "tool_permission": "permission",
    "action_permission": "permission",
    "task_user_input": "question",
    "route_clarification": "question",
    "company_runtime_selection": "question",
    "human_escalation": "question",
    "company_delivery_feedback": "delivery",
    "company_staffing_selection": "staffing",
    "company_recruitment_confirmation": "staffing",
    "company_runtime_interrupted": "suspended",
    "company_runtime_suspended": "suspended",
    "company_reorg_pending": "reorg",
    "company_run_failure_review": "failure",
    "task_peer_wait": "wait",
}


SUSPEND_CHECKPOINT_TYPES = frozenset({"company_runtime_suspended", "company_runtime_interrupted"})


def root_session_id(session_id: Any) -> str:
    """``root`` for ``root``, ``root:child`` and ``root:child:grandchild``."""
    return str(session_id or "").strip().split(":", 1)[0]


def hide_pauses_of_suspended_runs(
    items: list[dict[str, Any]],
    *,
    closed_task_ids: Collection[str] = (),
) -> tuple[list[dict[str, Any]], int]:
    """Drop the human pauses that belong to a suspended or stopped run.

    A run that a person stopped, or that a console restart parked, still has
    its gates and questions pending in the store, but answering them does
    nothing until the run resumes.  The suspension marker itself stays (it
    is how the operator resumes); the pauses underneath it are hidden and
    counted so the approval center can say how many are waiting behind it.

    Except for a closed order: its run is never resumed, so its park (the one
    the cancel's suspend left, or one a later restart re-parked) is not a
    pause anyone can act on and is not listed at all.  ``closed_task_ids``
    names the run tasks of the orders already closed; a park anchored on one
    of them is dropped, not counted, and hides nothing (review of 2026-09-25:
    a cancelled order still read 运行已停放 in 待审批).
    """
    closed = {str(task_id or "").strip() for task_id in closed_task_ids} - {""}

    def _park_of_closed_order(item: dict[str, Any]) -> bool:
        if not closed or str(item.get("checkpoint_type", "") or "") not in SUSPEND_CHECKPOINT_TYPES:
            return False
        anchors = {str(item.get("task_id", "") or "").strip(), str(item.get("waiting_task_id", "") or "").strip()}
        return bool(anchors & closed)

    suspended_roots = {
        root_session_id(item.get("session_id"))
        for item in items
        if str(item.get("checkpoint_type", "") or "") in SUSPEND_CHECKPOINT_TYPES and not _park_of_closed_order(item)
    }
    suspended_roots.discard("")
    kept: list[dict[str, Any]] = []
    hidden = 0
    for item in items:
        if str(item.get("checkpoint_type", "") or "") in SUSPEND_CHECKPOINT_TYPES:
            if not _park_of_closed_order(item):
                kept.append(item)
            continue
        if root_session_id(item.get("session_id")) in suspended_roots:
            hidden += 1
            continue
        kept.append(item)
    return kept, hidden


def blocker_kind(checkpoint_type: str) -> str:
    """``gate`` / ``permission`` / ``question`` / ``delivery`` / ``staffing`` / ``suspended`` / ... / ``other``."""
    return _CHECKPOINT_KINDS.get(str(checkpoint_type or "").strip(), "other")


def _without_outer_quotes(text: str) -> str:
    """「X」 -> X and “X” -> X; 「X」的汇报 and anything else stay as they are."""
    for left, right in (("「", "」"), ("“", "”")):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1]
    return text


def owner_turn_name(title: Any, metadata: Any = None, *, in_brackets: bool = False, limit: int = 80) -> str:
    """A task's title as the owner reads it: 「X」的汇报, 最终交付, 「X」 -- never "Report #1: X" (2026-09-26).

    ``company_mode.owner_facing_turn_title`` names the turn; the runtime title itself stays what it is for logs and
    audit. ``in_brackets`` is for a name set inside 【…】 (a Feishu card's title): the outer 「」 / “” the helper puts
    round a plain title are dropped there. ``limit`` bounds the quoted part. "" for an empty title.
    """
    raw = " ".join(str(title or "").split())
    if not raw:
        return ""
    from ampuly.layer2_organization.company_mode import owner_facing_turn_title

    name = owner_facing_turn_title(raw, metadata if isinstance(metadata, dict) else None, limit=limit)
    return _without_outer_quotes(name) if in_brackets else name


def _owner_words(text: str, waiting_task: Any) -> str:
    """``text`` with the waiting task's runtime title, where it appears, put in the owner's words.

    Only an internal turn title is replaced (``owner_turn_name`` changes more than its quotes); a plain title, a tool
    name and every other word stay as they are.
    """
    if not text or waiting_task is None:
        return text
    raw = str(getattr(waiting_task, "title", "") or "").strip()
    collapsed = " ".join(raw.split())
    if not collapsed:
        return text
    # Not cut short here: a long plain title is still plain, and the row cuts the whole subject anyway.
    owner = owner_turn_name(collapsed, getattr(waiting_task, "metadata", None), limit=len(collapsed) + 1)
    if not owner or _without_outer_quotes(owner) == collapsed:
        return text
    for form in dict.fromkeys((raw, collapsed)):
        if form and form in text:
            text = text.replace(form, owner)
    return text


def blocker_from_checkpoint(checkpoint: Any, *, waiting_task: Any = None) -> dict[str, Any]:
    """Project one pending ``ExecutionCheckpoint`` into a console blocker.

    Answering a checkpoint is authorized against the order's UI anchor, not
    the delegated work item that raised it. Reporting the raw runtime task
    fails authorization and navigates to a child session the operator cannot
    see, so the recorded anchor wins whenever the payload carries one.

    ``waiting_task`` (the Task the checkpoint waits on, read by a caller that
    has a store) puts its runtime title into the owner's words where the
    subject quotes it ("Report #1: X" -> 「X」的汇报); tool names stay as
    they are. This function reads nothing itself.
    """
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    tool_call = dict(payload.get("tool_call", {}) or {})
    approval = dict(payload.get("approval", {}) or {})
    checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "")
    subject = str(tool_call.get("name") or approval.get("action_name") or "").strip()
    detail = ""
    arguments = tool_call.get("arguments")
    if isinstance(arguments, dict):
        for key in ("url", "path", "command", "query"):
            if arguments.get(key):
                detail = str(arguments[key])[:160]
                break
    interaction = dict(payload.get("interaction", {}) or {})
    if not subject:
        subject = _owner_words(str(interaction.get("title") or interaction.get("prompt") or "").strip(), waiting_task)[:80]
    raw_options = interaction.get("options") or approval.get("options") or []
    if not raw_options:
        # A question with fixed choices and nothing to type (the order-allowance stop: continue / fail this item) can
        # be answered where it is listed. Anything that takes free text, or asks more than one thing, is answered on
        # its card in the conversation, as before.
        questions = [item for item in list(dict(payload.get("pause_request", {}) or {}).get("input_questions", []) or []) if isinstance(item, dict)]
        if len(questions) == 1 and questions[0].get("allow_freeform") is False:
            raw_options = list(questions[0].get("options", []) or [])
    options = [
        {
            "id": str(option.get("id", "") or ""),
            "label": str(option.get("label", "") or option.get("id", "") or ""),
            "kind": "deny" if "deny" in str(option.get("id", "")).lower() else "approve",
        }
        for option in raw_options
        if isinstance(option, dict) and str(option.get("id", "") or "").strip()
    ]
    ownership = dict(interaction.get("ownership", {}) or {})
    anchor_task_id = str(
        ownership.get("ui_anchor_task_id") or getattr(checkpoint, "task_id", "") or ""
    ).strip()
    anchor_session_id = str(
        ownership.get("ui_anchor_session_id")
        or ownership.get("root_session_id")
        or getattr(checkpoint, "session_id", "")
        or ""
    ).strip()
    # A staffing checkpoint is answered with an action, not an option: the
    # runtime's recommendation and any default selections travel with it.
    staffing = {}
    if checkpoint_type == "company_staffing_selection":
        defaults = dict(payload.get("staffing_defaults", {}) or {})
        selections = {k: v for k, v in defaults.items() if isinstance(v, dict) and str(v.get("kind", "") or "")}
        staffing = {
            "recommended_action": str(payload.get("recommended_action", "") or "").strip(),
            "staffing_selections": selections,
        }
    return {
        "project_id": str(getattr(checkpoint, "project_id", "") or "default").strip() or "default",
        "checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
        "checkpoint_type": checkpoint_type,
        "kind": blocker_kind(checkpoint_type),
        **({"staffing": staffing} if staffing else {}),
        "task_id": anchor_task_id,
        "session_id": anchor_session_id,
        "waiting_task_id": str(ownership.get("waiting_task_id") or getattr(checkpoint, "task_id", "") or "").strip(),
        "subject": subject,
        "detail": detail,
        "options": options,
        "created_at": str(getattr(checkpoint, "created_at", "") or ""),
    }


def waiting_seconds(created_at: Any, *, now: datetime | None = None) -> int | None:
    """How long a checkpoint has been waiting, or ``None`` if unparseable.

    The store stamps checkpoints with naive local ``datetime.now()``; a naive
    timestamp is therefore compared against naive local time, and an aware
    one against aware UTC. Mixing the two would report an eight-hour wait on
    a checkpoint created a minute ago on a UTC+8 laptop.
    """
    if isinstance(created_at, datetime):
        started = created_at
    else:
        text = str(created_at or "").strip()
        if not text:
            return None
        try:
            started = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if started.tzinfo is None:
        current = now.replace(tzinfo=None) if now is not None else datetime.now()
    else:
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    return max(0, int((current - started).total_seconds()))


def describe_wait(seconds: int | None, *, zh: bool) -> str:
    """"等了 12 分钟" / "waiting 12 min" — for the master-control context."""
    if seconds is None:
        return ""
    minutes = seconds // 60
    if minutes < 1:
        return "刚刚" if zh else "just now"
    if minutes < 60:
        return f"等了 {minutes} 分钟" if zh else f"waiting {minutes} min"
    hours, rest = divmod(minutes, 60)
    if zh:
        return f"等了 {hours} 小时 {rest} 分钟" if rest else f"等了 {hours} 小时"
    return f"waiting {hours} h {rest} min" if rest else f"waiting {hours} h"


def describe_since(seconds: int | None, *, zh: bool) -> str:
    """"12 分钟前" / "12 min ago" — for last-activity stamps."""
    if seconds is None:
        return ""
    minutes = seconds // 60
    if minutes < 1:
        return "刚刚" if zh else "just now"
    if minutes < 60:
        return f"{minutes} 分钟前" if zh else f"{minutes} min ago"
    hours, rest = divmod(minutes, 60)
    if zh:
        return f"{hours} 小时 {rest} 分钟前" if rest else f"{hours} 小时前"
    return f"{hours} h {rest} min ago" if rest else f"{hours} h ago"
