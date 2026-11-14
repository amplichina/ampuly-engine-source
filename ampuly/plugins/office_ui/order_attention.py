"""One judgement per order: who it waits on, whether it is abnormal, whether it is overdue (S0-A, 2026-09-27).

Until now the console counted 「等人处理」 in the frontend (orderQueues / projectOverview / orderSpread / attention)
from the order's status plus the pauses matched to it, and the master's context had a second rule of its own
(``waiting_on_human``: any un-snoozed pending checkpoint on the run).  The two disagreed in both directions the
design warned about: a step assigned to an employee was counted as waiting on the owner, and the lesson card
(company_delivery_feedback, 留点经验) counted as waiting on a person -- on 2026-09-27 项目中心 said 等人处理 2 for two of them.

The owner's decision (design 第二轮定稿, row 「等谁」): an order waits on the OWNER only when a live card is addressed
to the owner or is inherently the owner's -- a tool or effect permission, the allowance stopping, a paid call whose
receipt never came, a parked (interrupted / stopped) run, an intake to accept (待接单), the final delivery to accept
(acceptance submitted).  A human step assigned to an employee makes the order 在做 · 轮到某人, not waiting on the
owner.  An order in 客户验收中 with no owner card waits on the customer.  The lesson card never makes an order wait on
anyone.  A snoozed card still waits (``snoozed`` says so; the page may de-emphasise, the count must not drop it).  An
ended order waits on nobody and is never abnormal or overdue.

The pure functions here take what the callers already hold (the order row, its pauses, its budgets); ``judge_project``
is the one loader, and it fails open: when a project's pauses or budgets cannot be read, every verdict of that project
says ``waiting_on: ''`` with ``partial: True`` and the caller says so in its ack -- never a wrong number silently.

Plugin-layer module: no engine-kernel changes, no I/O in the judgement.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Collection, Iterable, Mapping

from loguru import logger

from ampuly.plugins.office_ui.blockers import blocker_from_checkpoint, blocker_kind, hide_pauses_of_suspended_runs
from ampuly.plugins.office_ui.business_store import ACCEPTANCE_PASSING, ENDED_ORDER_STATUSES

# Whom an order waits on.
WAITS_OWNER = "owner"
WAITS_EMPLOYEE = "employee"
WAITS_CUSTOMER = "customer"

# What it waits for, in the owner's words (one short word per row; the page shows it as the row's status word).
KIND_INTAKE = "待接单"
KIND_REVIEW = "等你审核"
KIND_ANSWER = "等你回答"
KIND_TOOL = "要用工具"
KIND_ALLOWANCE = "钱不够了"
KIND_NO_RECEIPT = "没收到回执"
KIND_STOPPED = "已停"
KIND_ACCEPTANCE = "等你验收"
KIND_CUSTOMER = "等客户确认"
KIND_CLOSE = "等你确认收货"      # accepted, not yet closed: only the owner's 确认收货并结案 is left
KIND_EMPLOYEE = "轮到某人"

# Why an order is abnormal, one word; when several apply the first of this order is said.
REASON_FAILED = "出错了"
REASON_STOPPED = "已停"
REASON_REWORK = "返工出错"
REASON_NO_RECEIPT = "没收到回执"
REASON_ALLOWANCE = "钱不够了"
REASON_BUDGET_CLOSED = "预算已关"
_REASON_ORDER = (REASON_FAILED, REASON_STOPPED, REASON_REWORK, REASON_NO_RECEIPT, REASON_ALLOWANCE, REASON_BUDGET_CLOSED)

# How an ended order ended.
ENDED_CLOSED = "closed"
ENDED_REJECTED = "rejected"
ENDED_CANCELLED = "cancelled"

# The four numbers every page shows, computed here and nowhere else.
STANDING_KEYS = ("waiting_on_me", "abnormal_or_overdue", "in_flight", "delivered")


# ── one pause ─────────────────────────────────────────────────────────────


def pause_from_checkpoint(checkpoint: Any) -> dict[str, Any]:
    """A pending checkpoint as a pause the judgement reads: the console's blocker plus ``attention_facts``.

    The facts come from the checkpoint's own payload, so a project whose engine is not loaded (its store read as a
    file) is judged by the same rule as the one on screen: the person a human step is assigned to
    (``pause_request.assignee_employee_id``, as the kernel records it; '' or 'owner' is the owner), whether the step
    is a human node, whether the allowance stopped this item (``budget_stop``), whether a browser step's check did
    not pass (``connector_done``), and whether it is the final delivery review.
    """
    item = blocker_from_checkpoint(checkpoint)
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    request = dict(payload.get("pause_request", {}) or {})
    item["attention_facts"] = {
        "assignee": str(request.get("assignee_employee_id", "") or "").strip(),
        "human_node": bool(request.get("human_node")),
        # The stop's own kind (exhausted / unavailable / refused / outcome_unknown / ...), '' when not a budget stop.
        "budget_stop": str(request.get("budget_stop", "") or "").strip(),
        "connector_done": bool(str(request.get("connector_done", "") or "").strip()),
        "final_delivery_review": isinstance(payload.get("final_delivery_review"), dict),
        # A step whose last turn ended in an unknown state at a console restart (restart_unknown_wait): the owner
        # is asked to redo or skip because no receipt came back.
        "no_receipt": str(request.get("request_id", "") or "").startswith("restart-unknown:"),
    }
    return item


def classify_pause(pause: dict[str, Any]) -> tuple[str, str, str]:
    """``(addressee, waiting_kind, abnormal_reason)`` of one pause; addressee '' when it waits on nobody.

    A blockers_list item (``kind`` / ``gate``) and a ``pause_from_checkpoint`` item (``attention_facts``) are both
    read.  A ``company_work_item_gate`` is the owner's: its payload names a reviewer role, not a person, so the
    only pause that names an employee is a human node (``task_user_input`` with ``assignee_employee_id``, or the
    ``gate.assignee`` blockers_list resolved for that same node).
    """
    kind = str(pause.get("kind", "") or "") or blocker_kind(str(pause.get("checkpoint_type", "") or ""))
    facts = pause.get("attention_facts") if isinstance(pause.get("attention_facts"), dict) else {}
    gate = pause.get("gate") if isinstance(pause.get("gate"), dict) else {}
    checkpoint_type = str(pause.get("checkpoint_type", "") or "")
    assignee = str(facts.get("assignee", "") or "").strip()
    if not assignee and checkpoint_type == "task_user_input":
        assignee = str(gate.get("assignee", "") or "").strip()
    if kind == "delivery":
        return "", "", ""  # 留点经验: the lesson card never makes an order wait on anyone
    if kind == "wait":
        return "", "", ""  # a peer wait is the run's own, not a person's
    if kind == "suspended":
        return WAITS_OWNER, KIND_STOPPED, REASON_STOPPED
    if kind == "failure":
        return WAITS_OWNER, KIND_ANSWER, REASON_FAILED
    if kind == "permission":
        return WAITS_OWNER, KIND_TOOL, ""
    if facts.get("budget_stop"):
        # A paid call that went out and never answered is not the allowance's doing: the row says 没收到回执, the
        # same word the owner card (owner_card.py) puts on it.
        if str(facts.get("budget_stop", "") or "") == "outcome_unknown":
            return WAITS_OWNER, KIND_NO_RECEIPT, REASON_NO_RECEIPT
        return WAITS_OWNER, KIND_ALLOWANCE, REASON_ALLOWANCE
    if facts.get("no_receipt"):
        return WAITS_OWNER, KIND_NO_RECEIPT, REASON_NO_RECEIPT
    if facts.get("final_delivery_review"):
        return WAITS_OWNER, KIND_ACCEPTANCE, ""
    # A person's step: a work-item gate, a human node, or a blockers_list item resolved to a signer.  Only there is
    # the word 等你审核 / 轮到某人; a browser step's check or a restart question addressed to "owner" is 等你回答.
    if assignee and assignee.lower() != WAITS_OWNER:
        return WAITS_EMPLOYEE, KIND_EMPLOYEE, ""
    if kind == "gate" or facts.get("human_node") or bool(gate):
        return WAITS_OWNER, KIND_REVIEW, ""
    # A question, a staffing or reorg confirmation, a browser step's check, anything new: a decision only the
    # owner takes.  Unknown pauses lean towards "someone should look", never towards silence.
    return WAITS_OWNER, KIND_ANSWER, ""


def pauses_of_order(order_row: dict[str, Any], pauses: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pauses that belong to this order: the same match the pages make (its run's task, or the order's own id,
    against the pause's anchor task, anchor session or waiting task)."""
    ids = {str(order_row.get("launched_task_id", "") or "").strip(), str(order_row.get("order_id", "") or "").strip()} - {""}
    if not ids:
        return []
    return [
        pause for pause in pauses
        if isinstance(pause, dict) and (
            str(pause.get("task_id", "") or "") in ids
            or str(pause.get("session_id", "") or "") in ids
            or str(pause.get("waiting_task_id", "") or "") in ids
        )
    ]


def _created_key(pause: dict[str, Any]) -> tuple[bool, str]:
    stamp = str(pause.get("created_at", "") or "")
    return (stamp == "", stamp)  # the oldest first; one with no time last, never first


def judge_pauses(pauses: Iterable[dict[str, Any]], *, snoozed_ids: Collection[str] = ()) -> dict[str, Any]:
    """Whom these pauses (of one order) make it wait on, and what for.

    The owner wins over an employee: while any card addressed to the owner is open the order waits on the owner.
    Among the chosen addressee's cards the oldest speaks (its kind, its time), as the pages sort them.  ``snoozed``
    is true when every card of the chosen addressee is held.  ``card_ids`` names the cards counted, in the order
    given.  ``abnormal_reason`` is the first reason of ``_REASON_ORDER`` any card carries, or ''.
    """
    held = {str(item or "").strip() for item in snoozed_ids} - {""}
    owner_cards: list[tuple[dict[str, Any], str]] = []
    employee_cards: list[tuple[dict[str, Any], str]] = []
    reasons: set[str] = set()
    for pause in pauses:
        if not isinstance(pause, dict):
            continue
        addressee, kind, reason = classify_pause(pause)
        if reason:
            reasons.add(reason)
        if addressee == WAITS_OWNER:
            owner_cards.append((pause, kind))
        elif addressee == WAITS_EMPLOYEE:
            employee_cards.append((pause, kind))
    waiting_on = WAITS_OWNER if owner_cards else (WAITS_EMPLOYEE if employee_cards else "")
    chosen = owner_cards or employee_cards
    oldest = sorted(chosen, key=lambda pair: _created_key(pair[0]))
    first, first_kind = oldest[0] if oldest else ({}, "")
    facts = first.get("attention_facts") if isinstance(first.get("attention_facts"), dict) else {}
    gate = first.get("gate") if isinstance(first.get("gate"), dict) else {}
    employee = str(facts.get("assignee", "") or gate.get("assignee", "") or "").strip() if waiting_on == WAITS_EMPLOYEE else ""
    return {
        "waiting_on": waiting_on,
        "waiting_employee_id": employee,
        "waiting_kind": first_kind if waiting_on else "",
        "waiting_since": str(first.get("created_at", "") or "") if waiting_on else "",
        "snoozed": bool(chosen) and all(str(pause.get("checkpoint_id", "") or "") in held for pause, _kind in chosen),
        "abnormal_reason": next((reason for reason in _REASON_ORDER if reason in reasons), ""),
        "card_ids": [str(pause.get("checkpoint_id", "") or "") for pause, _kind in chosen],
    }


# ── one order ─────────────────────────────────────────────────────────────


def due_days(due_at: Any, now: datetime | date | None = None) -> int | None:
    """Calendar days from today to ``due_at`` (``YYYY-MM-DD``, or a stamp starting with one); negative = past.
    None when nothing is set or it cannot be read as a date."""
    text = str(due_at or "").strip()[:10]
    if not text:
        return None
    try:
        due = date.fromisoformat(text)
    except ValueError:
        return None
    moment = now if now is not None else datetime.now()
    today = moment.date() if isinstance(moment, datetime) else moment
    return (due - today).days


def judge(
    order_row: dict[str, Any],
    *,
    pauses: Iterable[dict[str, Any]] = (),
    budgets: Iterable[dict[str, Any]] = (),
    acceptance: dict[str, Any] | None = None,
    now: datetime | None = None,
    snoozed_ids: Collection[str] = (),
    cancelled: bool = False,
    run_failed: bool = False,
    partial: bool = False,
) -> dict[str, Any]:
    """The judgement of one order.  Pure: reads the row, the project's pauses and budgets handed in, nothing else.

    ``pauses`` may be the whole project's (matched here) or the order's own.  ``budgets`` are the project's run
    budgets as the store lists them (``state``: open / closed / pending_verification).  ``acceptance`` overrides the
    row's.  ``cancelled`` is the closing event's fact for a closed order (the row itself does not carry it).
    ``run_failed`` is the run's own status, known to callers that hold the task; an order row does not carry it.
    ``partial``: the pauses or budgets could not be read -- only what the row alone says is judged.
    """
    row = order_row if isinstance(order_row, dict) else {}
    status = str(row.get("status", "") or "")
    order_id = str(row.get("order_id", "") or "").strip()
    accepted = acceptance if isinstance(acceptance, dict) else (row.get("acceptance") if isinstance(row.get("acceptance"), dict) else {})
    handling = row.get("handling") if isinstance(row.get("handling"), dict) else {}
    ended = status in ENDED_ORDER_STATUSES
    if status == "rejected":
        ended_as = ENDED_REJECTED
    elif status == "closed":
        ended_as = ENDED_CANCELLED if cancelled else ENDED_CLOSED
    else:
        ended_as = ""
    days = due_days(handling.get("due_at"), now)
    verdict: dict[str, Any] = {
        "waiting_on": "",
        "waiting_employee_id": "",
        "waiting_kind": "",
        "waiting_since": "",
        "snoozed": False,
        "abnormal": False,
        "abnormal_reason": "",
        "due_days": days,
        "overdue": bool(not ended and days is not None and days < 0),
        "ended": ended,
        "ended_as": ended_as,
        "delivered": bool(status == "closed" and not cancelled),
    }
    if partial:
        verdict["partial"] = True
    if ended or partial:
        return verdict

    cards = judge_pauses(pauses_of_order(row, pauses), snoozed_ids=snoozed_ids)
    own_budgets = [budget for budget in budgets if isinstance(budget, dict) and str(budget.get("order_id", "") or "").strip() == order_id]
    states = {str(budget.get("state", "") or "open") for budget in own_budgets}
    # The allowance's third state (pending_verification): a paid call nobody can say went out or not.
    no_receipt = any(state not in {"open", "closed"} for state in states)
    budget_closed = "closed" in states

    reasons: set[str] = set()
    if run_failed:
        reasons.add(REASON_FAILED)
    if cards["abnormal_reason"]:
        reasons.add(cards["abnormal_reason"])
    rework = accepted.get("runtime_rework") if isinstance(accepted.get("runtime_rework"), dict) else {}
    if str(rework.get("last_error", "") or "").strip():
        reasons.add(REASON_REWORK)
    if no_receipt:
        reasons.add(REASON_NO_RECEIPT)
    if budget_closed:
        reasons.add(REASON_BUDGET_CLOSED)
    reason = next((word for word in _REASON_ORDER if word in reasons), "")
    verdict["abnormal"] = bool(reason)
    verdict["abnormal_reason"] = reason

    updated = str(row.get("updated_at", "") or "")
    if status == "pending_review":
        verdict.update(waiting_on=WAITS_OWNER, waiting_kind=KIND_INTAKE, waiting_since=updated)
    elif str(accepted.get("status", "") or "") == "submitted":
        verdict.update(waiting_on=WAITS_OWNER, waiting_kind=KIND_ACCEPTANCE, waiting_since=updated)
    elif cards["waiting_on"] == WAITS_OWNER:
        verdict.update(waiting_on=WAITS_OWNER, waiting_kind=cards["waiting_kind"], waiting_since=cards["waiting_since"], snoozed=cards["snoozed"])
    elif no_receipt:
        verdict.update(waiting_on=WAITS_OWNER, waiting_kind=KIND_NO_RECEIPT)
    elif cards["waiting_on"] == WAITS_EMPLOYEE:
        verdict.update(
            waiting_on=WAITS_EMPLOYEE, waiting_employee_id=cards["waiting_employee_id"], waiting_kind=KIND_EMPLOYEE,
            waiting_since=cards["waiting_since"], snoozed=cards["snoozed"],
        )
    elif status == "delivering" and str(accepted.get("status", "") or "") in ACCEPTANCE_PASSING:
        # Accepted but not closed: the customer has nothing left to do; only the owner's 确认收货并结案 remains.
        verdict.update(waiting_on=WAITS_OWNER, waiting_kind=KIND_CLOSE, waiting_since=updated)
    elif status == "delivering":
        verdict.update(waiting_on=WAITS_CUSTOMER, waiting_kind=KIND_CUSTOMER, waiting_since=updated)
    return verdict


def judge_rows(
    rows: Iterable[dict[str, Any]],
    *,
    pauses: Iterable[dict[str, Any]] = (),
    budgets: Iterable[dict[str, Any]] = (),
    now: datetime | None = None,
    snoozed_ids: Collection[str] = (),
    cancelled_ids: Collection[str] = (),
    partial: bool = False,
) -> dict[str, dict[str, Any]]:
    """``judge`` over many rows of one project, by order id."""
    pause_list = [pause for pause in pauses if isinstance(pause, dict)]
    budget_list = [budget for budget in budgets if isinstance(budget, dict)]
    cancelled = {str(item or "").strip() for item in cancelled_ids} - {""}
    verdicts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("order_id", "") or "").strip()
        if not order_id:
            continue
        verdicts[order_id] = judge(
            row, pauses=pause_list, budgets=budget_list, now=now, snoozed_ids=snoozed_ids,
            cancelled=order_id in cancelled, partial=partial,
        )
    return verdicts


# ── the four numbers ──────────────────────────────────────────────────────


def count_standing(rows: Iterable[dict[str, Any]], verdicts: dict[str, dict[str, Any]]) -> dict[str, int]:
    """The four numbers over these rows, from their verdicts: waiting on me (the owner; a held card still counts),
    abnormal or overdue (live orders only), in flight (live and not waiting on me -- 轮到某人 and 客户验收中 are
    in flight), delivered (closed, not cancelled)."""
    counts = {key: 0 for key in STANDING_KEYS}
    for row in rows:
        if not isinstance(row, dict):
            continue
        verdict = verdicts.get(str(row.get("order_id", "") or "").strip()) or {}
        ended = bool(verdict.get("ended"))
        if verdict.get("waiting_on") == WAITS_OWNER:
            counts["waiting_on_me"] += 1
        if not ended and (verdict.get("abnormal") or verdict.get("overdue")):
            counts["abnormal_or_overdue"] += 1
        if not ended and verdict.get("waiting_on") != WAITS_OWNER:
            counts["in_flight"] += 1
        if verdict.get("delivered"):
            counts["delivered"] += 1
    return counts


def standing_of(rows: Iterable[dict[str, Any]], verdicts: dict[str, dict[str, Any]], *, truncated: bool, partial: bool = False) -> dict[str, Any]:
    """A project's standing: the four numbers, whether the rows were cut (every number then a floor), and whether
    the judgement was partial (waiting and abnormal then unknown, not zero)."""
    return {**count_standing(rows, verdicts), "truncated": bool(truncated), "partial": bool(partial)}


def flows_of(rows: Iterable[dict[str, Any]], verdicts: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    """The same four numbers per flow (``workflow_id``), for the flows that have an order here."""
    by_flow: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        flow_id = str(row.get("workflow_id", "") or "").strip()
        if flow_id:
            by_flow.setdefault(flow_id, []).append(row)
    return {flow_id: count_standing(flow_rows, verdicts) for flow_id, flow_rows in sorted(by_flow.items())}


# ── the loader ────────────────────────────────────────────────────────────


async def pending_checkpoints_readonly(db_path: Path | str, project_id: str) -> list[Any]:
    """The pending checkpoints of a project whose engine this process does not run, read in its store file
    (``mode=ro``, closed after) -- the C4-FIX pattern; nothing is loaded for it.  Raises when the file cannot be
    read, so the caller can say the judgement is partial rather than count nothing."""
    import sqlite3

    from ampuly.core.sqlite_open import opened
    from ampuly.database.store import AmpulyStore

    def read() -> list[Any]:
        with opened(db_path, read_only=True, timeout=2.0, row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT * FROM execution_checkpoints WHERE project_id = ? AND status = 'pending' ORDER BY updated_at DESC",
                (str(project_id or "default"),),
            ).fetchall()
        return [AmpulyStore._execution_checkpoint_from_data(dict(row)) for row in rows]

    return await asyncio.to_thread(read)


def _cancelled_closes(business: Any, rows: list[dict[str, Any]]) -> set[str]:
    """Which of these closed orders were cancelled (the closing event says; ``order_liveness.order_ended_as``)."""
    from ampuly.core.order_liveness import ORDER_CANCELLED
    from ampuly.plugins.office_ui.order_liveness import order_ended_as

    found: set[str] = set()
    for row in rows:
        order_id = str(row.get("order_id", "") or "").strip()
        if order_ended_as(business, SimpleNamespace(status="closed", order_id=order_id)) == ORDER_CANCELLED:
            found.add(order_id)
    return found


async def judge_project(
    project_id: str,
    rows: list[dict[str, Any]],
    *,
    business: Any,
    pending: Callable[[str], Awaitable[list[Any]]],
    now: datetime | None = None,
    cancelled_ids: Collection[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """The verdicts of one project's rows, and whether they are partial.

    ``pending(project_id)`` hands back the project's pending checkpoints (the caller decides how: the loaded
    engine's store, or the store file read-only) and raises when it cannot.  ``business`` is the console's store:
    budgets, snoozes and the closing events of closed rows.  Unreadable pauses, budgets or closing events make the
    whole project partial (waiting '' and no abnormal claim, ``partial: True`` on every verdict); unreadable snoozes
    only lose the ``snoozed`` flag (every card then counts as live, as blockers_list lists them).
    ``cancelled_ids``: which closed rows were cancelled, when the caller already read it (the order rows carry
    ``ended_as``); given, the closing events are not read again.
    """
    loaded = await load_project_judgement(project_id, rows, business=business, pending=pending, now=now, cancelled_ids=cancelled_ids)
    return loaded["verdicts"], loaded["partial"]


async def load_project_judgement(
    project_id: str,
    rows: list[dict[str, Any]],
    *,
    business: Any,
    pending: Callable[[str], Awaitable[list[Any]]],
    now: datetime | None = None,
    cancelled_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """What ``judge_project`` reads, kept: ``verdicts`` and ``partial`` exactly as it returns them, plus the ``pauses``
    the verdicts were judged from (the pauses under a parked run already hidden), the ``checkpoints`` they were built
    from by id, and the ``snoozed_ids`` -- for a caller that also shows the card an order waits on (S3-3A 总控) without
    reading the project's store a second time."""
    partial = False
    checkpoints: list[Any] | None = None
    try:
        checkpoints = list(await pending(project_id) or [])
    except Exception:
        logger.opt(exception=True).warning("order attention: the pauses of project {} could not be read; its orders are judged partially", project_id)
        partial = True
    budgets: list[dict[str, Any]] = []
    snoozed_ids: set[str] = set()
    known_cancelled = cancelled_ids is not None
    cancelled_ids = {str(item or "").strip() for item in (cancelled_ids or ())} - {""}
    closed = [] if known_cancelled else [row for row in rows if isinstance(row, dict) and str(row.get("status", "") or "") == "closed"]
    if business is None:
        partial = True
    else:
        try:
            budgets = list(await asyncio.to_thread(business.list_run_budgets, project_id=project_id) or [])
        except Exception:
            logger.opt(exception=True).warning("order attention: the budgets of project {} could not be read; its orders are judged partially", project_id)
            partial = True
        try:
            snoozed_ids = set(await asyncio.to_thread(business.active_snoozes, project_id=project_id) or {})
        except Exception:
            logger.warning("order attention: snoozes of project {} unreadable; every card counted as live", project_id)
        if closed:
            try:
                cancelled_ids = await asyncio.to_thread(_cancelled_closes, business, closed)
            except Exception:
                logger.opt(exception=True).warning("order attention: the closing events of project {} could not be read; its orders are judged partially", project_id)
                partial = True
    pauses: list[dict[str, Any]] = []
    if checkpoints is not None:
        pauses = [pause_from_checkpoint(checkpoint) for checkpoint in checkpoints]
        # A pause under a parked run cannot be answered until the run resumes; the park itself is what waits.
        closed_task_ids = {
            str(row.get("launched_task_id", "") or "").strip()
            for row in rows if isinstance(row, dict) and str(row.get("status", "") or "") in ENDED_ORDER_STATUSES
        } - {""}
        pauses, _hidden = hide_pauses_of_suspended_runs(pauses, closed_task_ids=closed_task_ids)
    verdicts = judge_rows(rows, pauses=pauses, budgets=budgets, now=now, snoozed_ids=snoozed_ids, cancelled_ids=cancelled_ids, partial=partial)
    return {
        "verdicts": verdicts,
        "partial": partial,
        "pauses": pauses,
        "checkpoints": {str(getattr(checkpoint, "checkpoint_id", "") or ""): checkpoint for checkpoint in (checkpoints or [])},
        "snoozed_ids": snoozed_ids,
    }


# ── S3-3A 总控: 「等你处理」 across projects, and where a card lives ───────────
#
# The owner's decision (design 「总控：今天」 and 「和引擎的对应」 row 总控): 总控's first screen is one
# cross-project query -- the orders that wait on the owner, 逾期在前、其次等得最久, at most a few of them -- with the
# 状态句's three numbers over every row read.  Read-only; the pages that already judge one project (S0-A) are not
# changed, and no engine is loaded for a project this process does not run.

ATTENTION_LIMIT_DEFAULT = 3
ATTENTION_LIMIT_MAX = 20
# A project the owner archived or destroyed: its orders are not 等你处理, and its store is not read for them.
_ATTENTION_SKIPPED_LIFECYCLES = frozenset({"archived", "destroyed"})


def clamp_attention_limit(value: Any) -> int:
    """How many rows the query hands back: ``value`` within 1..``ATTENTION_LIMIT_MAX``; the default when unreadable."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = ATTENTION_LIMIT_DEFAULT
    return max(1, min(ATTENTION_LIMIT_MAX, limit))


def needs_owner(verdict: dict[str, Any]) -> bool:
    """Whether an order belongs on 「等你处理」: it waits on the owner, or it is live and abnormal or overdue (the 状态句
    counts those separately; the list shows them so the owner sees them)."""
    if not isinstance(verdict, dict):
        return False
    if verdict.get("waiting_on") == WAITS_OWNER:
        return True
    return bool(not verdict.get("ended") and (verdict.get("abnormal") or verdict.get("overdue")))


def _stamp_key(text: Any) -> str:
    """A time stamp as a sortable string.  The rows stamp ``updated_at`` as ``datetime.isoformat`` (a ``T``), a
    checkpoint's ``created_at`` is a datetime said as text (a space): the two must not be compared as they are."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).isoformat()
    except ValueError:
        return raw


def attention_order_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """逾期在前、其次等得最久: an overdue order first; then the one that has waited longest (``waiting_since``; an order
    with none after those with one); then the most overdue; then by project and order, so the list is stable."""
    verdict = item.get("attention") if isinstance(item.get("attention"), dict) else {}
    since = _stamp_key(verdict.get("waiting_since"))
    days = verdict.get("due_days")
    return (
        0 if verdict.get("overdue") else 1,
        since == "",
        since,
        days if isinstance(days, int) else 0,
        str(item.get("project_id", "") or ""),
        str(item.get("order_id", "") or ""),
    )


def open_card_of(order_row: dict[str, Any], pauses: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The pause an order's verdict speaks for -- the oldest card of the addressee it waits on, as ``judge_pauses``
    chooses it -- or None when no card is open on it (an intake, a delivery to accept, a run with nothing open)."""
    owner_cards: list[dict[str, Any]] = []
    employee_cards: list[dict[str, Any]] = []
    for pause in pauses_of_order(order_row, pauses):
        addressee, _kind, _reason = classify_pause(pause)
        if addressee == WAITS_OWNER:
            owner_cards.append(pause)
        elif addressee == WAITS_EMPLOYEE:
            employee_cards.append(pause)
    chosen = owner_cards or employee_cards
    return sorted(chosen, key=_created_key)[0] if chosen else None


async def collect_owner_attention(
    projects: Iterable[Mapping[str, Any]],
    *,
    rows_of: Callable[[str], list[dict[str, Any]]],
    judgement: Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]],
    limit: Any = ATTENTION_LIMIT_DEFAULT,
    zh: bool = True,
    employee_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """「等你处理」 across the given projects (the visible entries, ``id`` / ``project_id`` and ``name``), in one read each.

    ``rows_of(project_id)`` hands back the project's order rows (run in a thread; raises when the orders cannot be
    read) and ``judgement(project_id, rows)`` what ``load_project_judgement`` returns for them.  The answer:
    ``items`` -- at most ``limit`` of the rows ``needs_owner`` keeps, sorted by ``attention_order_key``, each
    ``{project_id, project_name, order_id, order_label, attention, owner_card, due_days}`` where ``owner_card`` is the
    S1 projection (``owner_card.project``) of the card the order waits on, None when the wait is not a card;
    ``total``, how many rows there were before the cut; ``counts``, the 状态句's ``waiting_on_me`` / ``in_progress`` /
    ``abnormal_or_overdue`` over every row read; ``partial_projects``, the projects whose orders or store could not
    be read -- their waiting is unknown, never said to be zero.
    """
    from ampuly.plugins.office_ui.owner_card import _order_words, project as owner_card_of

    limit = clamp_attention_limit(limit)
    items: list[dict[str, Any]] = []
    counts = {"waiting_on_me": 0, "in_progress": 0, "abnormal_or_overdue": 0}
    partial_projects: list[str] = []
    seen: set[str] = set()
    for entry in projects:
        if not isinstance(entry, Mapping):
            continue
        project_id = str(entry.get("id", "") or entry.get("project_id", "") or "").strip()
        if not project_id or project_id in seen:
            continue
        seen.add(project_id)
        if str(entry.get("lifecycle_status", "") or "") in _ATTENTION_SKIPPED_LIFECYCLES:
            continue
        project_name = str(entry.get("name", "") or "").strip() or project_id
        try:
            rows = [row for row in (await asyncio.to_thread(rows_of, project_id) or []) if isinstance(row, dict)]
        except Exception:
            logger.opt(exception=True).warning("owner attention: the orders of project {} could not be read; it is left out and named", project_id)
            partial_projects.append(project_id)
            continue
        if not rows:
            continue
        try:
            loaded = await judgement(project_id, rows)
        except Exception:
            logger.opt(exception=True).warning("owner attention: project {} could not be judged; its orders are judged partially", project_id)
            loaded = {"verdicts": judge_rows(rows, partial=True), "partial": True}
        verdicts = loaded.get("verdicts") if isinstance(loaded.get("verdicts"), dict) else {}
        if loaded.get("partial"):
            partial_projects.append(project_id)
        standing = count_standing(rows, verdicts)
        counts["waiting_on_me"] += standing["waiting_on_me"]
        counts["in_progress"] += standing["in_flight"]
        counts["abnormal_or_overdue"] += standing["abnormal_or_overdue"]
        pauses = [pause for pause in list(loaded.get("pauses") or []) if isinstance(pause, dict)]
        checkpoints = loaded.get("checkpoints") if isinstance(loaded.get("checkpoints"), dict) else {}
        for row in rows:
            order_id = str(row.get("order_id", "") or "").strip()
            verdict = verdicts.get(order_id)
            if not order_id or not isinstance(verdict, dict) or not needs_owner(verdict):
                continue
            card = open_card_of(row, pauses)
            owner_card = None
            if card is not None:
                owner_card = owner_card_of(
                    card, order=row, zh=zh, checkpoint=checkpoints.get(str(card.get("checkpoint_id", "") or "")),
                    employee_names=employee_names,
                )
            _order_id, order_label = _order_words(row, card or {})
            items.append({
                "project_id": project_id,
                "project_name": project_name,
                "order_id": order_id,
                "order_label": order_label,
                "attention": dict(verdict),
                "owner_card": owner_card,
                "due_days": verdict.get("due_days"),
            })
    items.sort(key=attention_order_key)
    return {"items": items[:limit], "total": len(items), "limit": limit, "counts": counts, "partial_projects": partial_projects}


def order_of_pause(business: Any, item: Mapping[str, Any], project_id: str) -> Any:
    """The order a pause belongs to, found as the pages match a pause to one: the run's task (``task_id`` or
    ``waiting_task_id`` is the order's launched task), else the order's own id (``task_id`` or ``session_id``).
    None when it belongs to none.  Reads the console's business store only."""
    for key in ("task_id", "waiting_task_id"):
        run_task_id = str(item.get(key, "") or "").strip()
        if run_task_id:
            found = business.order_for_launched_task(run_task_id, project_id=project_id)
            if found is not None:
                return found
    for key in ("task_id", "session_id"):
        order_key = str(item.get(key, "") or "").strip()
        if order_key:
            found = business.get_order(order_key, project_id=project_id)
            if found is not None:
                return found
    return None


def _order_of_card_row(business: Any, row: Mapping[str, Any]) -> Any:
    """The order a gate card row was sent for (its ``order_id``, else the run task it names); None when neither is known."""
    order_id = str(row.get("order_id", "") or "").strip()
    order = business.get_order(order_id) if order_id else None
    task_id = str(row.get("task_id", "") or "").strip()
    if order is None and task_id:
        order = business.order_for_launched_task(task_id)
    return order


async def locate_checkpoint(
    checkpoint_id: str,
    *,
    business: Any,
    project_ids: Iterable[str],
    checkpoint_in: Callable[[str, str], Awaitable[Any]],
) -> dict[str, Any] | None:
    """``{project_id, order_id}`` of a card (S3-3A ``checkpoint_locate``), or None when it is nowhere.

    Its card row answers first (``gate_cards``: the order a card was sent for, read in the console's business store);
    else each of ``project_ids`` in turn: ``checkpoint_in(project_id, checkpoint_id)`` hands back the checkpoint from
    that project's store (the engine this process runs, or the file read-only), None when it is not there, and raises
    when the store cannot be read -- that project is then skipped, never loaded.  ``order_id`` is '' for a checkpoint
    that belongs to no order.  Only a project among ``project_ids`` (the visible ones) is answered.
    """
    clean = str(checkpoint_id or "").strip()
    if not clean:
        return None
    visible = [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]
    if business is not None:
        try:
            row = await asyncio.to_thread(business.gate_card, clean)
            order = await asyncio.to_thread(_order_of_card_row, business, row) if row else None
        except Exception:
            logger.opt(exception=True).debug("checkpoint locate: the card row of {} could not be read", clean[:8])
            order = None
        project_id = str(getattr(order, "project_id", "") or "").strip() if order is not None else ""
        if project_id and (not visible or project_id in visible):
            return {"project_id": project_id, "order_id": str(getattr(order, "order_id", "") or "")}
    for project_id in visible:
        try:
            checkpoint = await checkpoint_in(project_id, clean)
        except Exception:
            logger.opt(exception=True).debug("checkpoint locate: the store of project {} could not be read for {}", project_id, clean[:8])
            continue
        if checkpoint is None:
            continue
        item = blocker_from_checkpoint(checkpoint)
        order = None
        if business is not None:
            try:
                order = await asyncio.to_thread(order_of_pause, business, item, project_id)
            except Exception:
                logger.opt(exception=True).debug("checkpoint locate: the order of {} could not be read", clean[:8])
        return {"project_id": project_id, "order_id": str(getattr(order, "order_id", "") or "")}
    return None
