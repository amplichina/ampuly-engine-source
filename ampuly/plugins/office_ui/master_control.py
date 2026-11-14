"""Master-control conversational agent for the office UI (总控).

A lightweight system-level assistant bound to the leftmost nav page. It can
answer questions about the live system state (orders, staff, blockers, cost)
from a compact context snapshot, and it can dispatch new orders on the user's
behalf via the existing session service. Control verbs (stop / approve / …)
are deliberately out of scope for v0 — those flow through the established
authorization gates in the workspace.

Plugin-layer module: no engine-kernel changes.
"""

from __future__ import annotations

import asyncio
import re

from datetime import datetime

import json
from typing import Any

from loguru import logger

from ampuly.application.execution_binding import INTERFACE_KEY, MASTER_CONTROL_INTERFACE
from ampuly.i18n_runtime import is_zh
from ampuly.plugins.office_ui.blockers import describe_since, describe_wait, waiting_seconds
from ampuly.plugins.office_ui.business_store import HANDLING_KEY
from ampuly.plugins.office_ui.order_attention import WAITS_OWNER, judge_pauses, load_project_judgement, pause_from_checkpoint
from ampuly.plugins.office_ui.product_name import PRODUCT_NAME_EN, PRODUCT_NAME_ZH
from ampuly.layer2_organization.company_runtime_identity import is_runtime_auxiliary_task
from ampuly.llm.budget import RouteRefused, route_refused_notice
from ampuly.llm.retry import LLMRetryError, call_llm_json_with_retry

MASTER_CHANNEL_PREFIX = "master"

_TERMINAL_STATUSES = {"done", "delivered", "failed", "cancelled"}
_BLOCKED_STATUSES = {"awaiting_human", "awaiting_manager_review", "awaiting_review"}

# Where a run stands, in the owner's words (owner report : 安安's answer wrote pending / idle / done). Every
# order in the context carries one as ``status_zh`` and the prompts tell 安安 to say that, never the code; ``status``
# stays beside it for the rules here that read it (open / blocked / stoppable).
TASK_STATUS_ZH = {
    "pending": "排队中",
    "running": "进行中",
    "idle": "停着，等下一步",
    "blocked": "卡住了",
    "awaiting_peer": "等其他步骤做完",
    "awaiting_manager_review": "等主管审核",
    "awaiting_human": "等人处理",
    "awaiting_review": "等审核",
    "done": "已完成",
    "delivered": "已交付",
    "failed": "出错了",
    "cancelled": "已取消",
}
STATUS_UNKNOWN_ZH = "状态不明"

# A work order's own status in words, for when the judgement says nothing more particular (as 业务工单 says them).
WORK_ORDER_STATUS_ZH = {
    "collecting": "收集需求中",
    "pending_review": "待接单",
    "launching": "正在发起",
    "launched": "执行中",
    "producing": "执行中",
    "delivering": "交付中",
    "closed": "已结案",
    "rejected": "已打回",
}


def status_zh(status: Any) -> str:
    """The run's status as the owner reads it; a status this list does not know is 状态不明, never the code."""
    return TASK_STATUS_ZH.get(str(status or "").strip().lower(), STATUS_UNKNOWN_ZH)


def work_order_status_zh(status: Any, verdict: dict[str, Any] | None = None) -> str:
    """A work order's word from the server's one judgement (``order_attention.judge``, S0-A) when it could be made:
    an ended order says how it ended; a live one what it waits for (待接单 / 等你审核 / 轮到某人 / 等客户确认 ...), else
    what went wrong, else the step its status names; 已逾期 is added when it is. Without a verdict, the status alone."""
    word_of_status = WORK_ORDER_STATUS_ZH.get(str(status or "").strip(), STATUS_UNKNOWN_ZH)
    if not isinstance(verdict, dict):
        return word_of_status
    if verdict.get("ended"):
        return {"cancelled": "已取消", "rejected": "已打回"}.get(str(verdict.get("ended_as", "") or ""), "已结案")
    waiting = str(verdict.get("waiting_kind", "") or "")
    reason = str(verdict.get("abnormal_reason", "") or "")
    word = waiting or reason or word_of_status
    if waiting and reason and reason != waiting:
        word = f"{word}，{reason}"
    if verdict.get("overdue"):
        word = f"{word}，已逾期"
    return word


def is_console_turn_task(task: Any) -> bool:
    """The console's own conversation, filed under a task row so its spend lands in the project total
    (``master_runtime.master_task`` and the single-call path's accounting row): marked ``interface: master_control``,
    the marker the conversation is found again by (``execution_binding.is_master_control_record``) -- never by its
    title. It is not an order: Previously, 安安 counted 「总控」 itself as one of 7 orders."""
    meta = dict(getattr(task, "metadata", {}) or {})
    # A build job's task (总控施工模式, master_build.py: interface ``master_build``) is the console's own work too, not
    # an order: its state lives on its card in the conversation. Counted as an order it was reported to Feishu as
    # 「订单…可能悬死」 once delivered, because its task row stays pending ( live check).
    return str(meta.get(INTERFACE_KEY, "") or "").strip() in {MASTER_CONTROL_INTERFACE, "master_build"}


def master_channel_id(project_id: str, thread_id: str = "") -> str:
    """The chat channel of one 总控 conversation -- the one naming function (policy, 第七节).

    A project's first conversation, the one every project had before conversations were split (「以前的对话」), keeps
    the channel it always had: ``master:{project_id}``, asked for with no ``thread_id``. Every later conversation is
    ``master:{project_id}:{thread_id}``. A project id never holds a colon (``ampuly.project_id``), so a later channel can
    never be taken for another project's first one, and a reader that splits the project id off gets a string that is
    no project id at all instead of the wrong one.
    """
    base = f"{MASTER_CHANNEL_PREFIX}:{project_id}"
    thread = str(thread_id or "").strip()
    return f"{base}:{thread}" if thread else base


def is_master_channel_of(project_id: str, channel_id: Any) -> bool:
    """Whether a channel is one of this project's 总控 conversations (its first one or a later one)."""
    base = master_channel_id(project_id)
    text = str(channel_id or "")
    return text == base or text.startswith(base + ":")


def _task_status(task: Any) -> str:
    status = getattr(task, "status", "")
    return str(getattr(status, "value", status) or "").strip().lower()


def _parent_session_id(task: Any) -> str:
    meta = dict(getattr(task, "metadata", {}) or {})
    return str(getattr(task, "parent_session_id", "") or meta.get("parent_session_id", "") or "").strip()


def _runtime_session_id(task: Any) -> str:
    meta = dict(getattr(task, "metadata", {}) or {})
    runtime_meta = dict(meta.get("runtime_v2", {}) or {})
    return str(runtime_meta.get("runtime_session_id", "") or "").strip()


STALL_AFTER_SECONDS = 15 * 60


def _llm_in_flight_since(task: Any) -> str:
    meta = dict(getattr(task, "metadata", {}) or {})
    runtime_meta = dict(meta.get("runtime_v2", {}) or {})
    return str(runtime_meta.get("llm_in_flight_since", "") or "").strip()


def _order_root_id(task: Any, task_by_session: dict[str, Any]) -> str:
    """Walk parent_session_id up to the order's primary task (bounded)."""
    current = task
    for _ in range(8):
        parent = _parent_session_id(current)
        if not parent:
            return str(getattr(current, "id", "") or "")
        above = task_by_session.get(parent)
        if above is None:
            return str(getattr(current, "id", "") or "")
        current = above
    return str(getattr(current, "id", "") or "")


async def _last_tool_activity(store: Any, tasks: list[Any], order_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Latest tool call per open order, across the order's own turn and every
    role turn under it. A checkpoint explains a silent order; this tells the
    difference between "waiting on you" and "nothing has moved for an hour
    and nobody is asking" — the case the watchdog is for."""
    list_calls = getattr(store, "list_runtime_tool_calls", None)
    if not callable(list_calls) or not order_ids:
        return {}
    task_by_session = {
        str(getattr(task, "session_id", "") or ""): task
        for task in tasks
        if str(getattr(task, "session_id", "") or "")
    }
    sessions_by_order: dict[str, list[str]] = {}
    in_flight_by_order: dict[str, str] = {}
    for task in tasks:
        root = _order_root_id(task, task_by_session)
        if root not in order_ids:
            continue
        runtime_session = _runtime_session_id(task)
        if runtime_session:
            sessions_by_order.setdefault(root, []).append(runtime_session)
        # The runtime stamps a task while a model call is open and clears it
        # after; any task in the tree still stamped means the order is
        # thinking, however quiet it looks from outside.
        since = _llm_in_flight_since(task)
        if since and (waiting_seconds(since) or 0) < 2 * 3600:
            in_flight_by_order[root] = since
    activity: dict[str, dict[str, Any]] = {}
    for order_id in order_ids:
        if order_id in in_flight_by_order and order_id not in sessions_by_order:
            activity[order_id] = {"llm_in_flight": True, "llm_in_flight_since": in_flight_by_order[order_id]}
    for order_id, runtime_sessions in sessions_by_order.items():
        latest_at = ""
        latest_tool = ""
        for runtime_session in runtime_sessions[-12:]:
            try:
                calls = await list_calls(runtime_session)
            except Exception:
                logger.debug(f"master_control: tool calls unavailable for {runtime_session}")
                continue
            for call in calls:
                created = str(call.get("created_at", "") or "")
                if created > latest_at:
                    latest_at = created
                    latest_tool = str(call.get("tool_name", "") or "")
        stamp: dict[str, Any] = {}
        if latest_at:
            seconds = waiting_seconds(latest_at)
            stamp = {
                "last_tool_at": latest_at,
                "last_tool": latest_tool,
                "idle_seconds": seconds,
                "since_last_tool": describe_since(seconds, zh=is_zh()),
            }
        if order_id in in_flight_by_order:
            stamp["llm_in_flight"] = True
            stamp["llm_in_flight_since"] = in_flight_by_order[order_id]
        if stamp:
            activity[order_id] = stamp
    return activity


def stall_verdict(order: dict[str, Any]) -> bool:
    """An order is stalled when nothing explains its silence: no checkpoint
    waiting on a person, no model call in flight, and no tool call for a
    while. Each of those alone is a legitimate quiet; only all three
    together is the case the watchdog exists for. Without the in-flight
    stamp a fifteen-minute generation would trip this — which is why the
    stamp had to exist before the rule."""
    if order.get("waiting_on_human"):
        return False
    if order.get("llm_in_flight"):
        return False
    idle = order.get("idle_seconds")
    if idle is None:
        return False
    return int(idle) >= STALL_AFTER_SECONDS


def _is_primary(task: Any) -> bool:
    meta = dict(getattr(task, "metadata", {}) or {})
    if str(meta.get("parent_session_id", "") or "").strip():
        return False
    return True


# Tools whose point is to leave this machine, or to change something outside
# the conversation. Named one by one because "can you look that up" and "can
# you send it to them" are the questions actually asked; a count of forty
# tools answers neither.
_REACHING_TOOL_NAMES = (
    "web_search", "web_fetch",
    "browser_navigate", "browser_click", "browser_type", "browser_submit_enter",
    "shell_exec", "python_exec",
    "request_customer_acceptance_reminder", "send_dm", "reply_message", "agent_send",
    "image_generate", "file_write",
)


def process_drafts(business: Any, limit: int = 8) -> list[dict[str, Any]]:
    """Descriptions being built, newest first, each with what it still lacks.

    Read-only. Master control can say a description is three questions
    short of runnable. Starting the trial is a separate, owner-gated write
    (`master_run_offline_trial`) because it spends real
    model calls; this listing never does.

    Unreadable means an empty list, never a claim that nothing is being built.
    """
    if business is None:
        return []
    try:
        specs = list(business.list_process_specs())
    except Exception:
        logger.warning("master_control: process specs unreadable; omitting them from context")
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for spec in specs:
        spec_id = str(spec.get("spec_id", "") or "")
        # The listing is every version of every description, newest first.
        # The owner asks about the description, not its history.
        if not spec_id or spec_id in seen:
            continue
        seen.add(spec_id)
        missing = [str(item) for item in (spec.get("missing") or [])]
        out.append({
            "spec_id": spec_id,
            "version": str(spec.get("version", "") or ""),
            "updated_at": str(spec.get("at", "") or ""),
            # The whole point of showing these: "not finished" is the answer,
            # and the questions are what makes it actionable.
            "unanswered": missing[:5],
            "runnable": not missing,
        })
        if len(out) >= limit:
            break
    return out


def capability_snapshot(engine: Any) -> dict[str, Any]:
    """What this installation can actually do, read from the live engine.

    Absent means unreadable, never "no". A field guessed at here becomes a
    sentence the assistant says to the owner as fact, so the rule is that
    everything in this dict was read from something, and anything that could
    not be read is left out.
    """
    snapshot: dict[str, Any] = {}

    registry = getattr(engine, "tool_registry", None)
    if registry is not None:
        try:
            tools = list(registry.list_tools())
        except Exception:
            tools = []
        if tools:
            by_category: dict[str, int] = {}
            for tool in tools:
                key = str(getattr(tool, "category", "") or "general")
                by_category[key] = by_category.get(key, 0) + 1
            names = {str(getattr(tool, "name", "") or "") for tool in tools}
            snapshot["tools"] = {
                "count": len(tools),
                "by_category": dict(sorted(by_category.items())),
                # Only the ones that reach outside, and only those that really
                # are registered here.
                "reaching_out": sorted(n for n in _REACHING_TOOL_NAMES if n in names),
                "needs_approval": sorted(
                    str(getattr(t, "name", "") or "") for t in tools
                    if getattr(t, "requires_confirmation", False)
                )[:12],
            }

    llm = getattr(engine, "llm", None)
    config = getattr(llm, "config", None)
    if config is not None:
        model = str(getattr(config, "default_model", "") or "")
        if model:
            snapshot["model"] = {
                "default": model,
                # Selection is by task type and nothing else today. Saying so
                # keeps the assistant from implying a router that is not there.
                "routed_by": "task_type",
                "routes": dict(getattr(config, "routing", {}) or {}),
            }

    # How a dispatched order actually runs. Read, not assumed: whether the
    # session registry is up decides whether a run leaves a Harness record at
    # all, and the external-agent boundary decides whether a configured
    # external agent runs behind that same contract or calls the broker
    # unrecorded.
    try:
        external_behind_harness = bool(engine._external_harness_boundary_enabled())
    except Exception:
        external_behind_harness = False
    snapshot["execution"] = {
        # The default path for a dispatched order, and the only one this
        # product ships enabled.
        "executor": "native",
        # Whether the session registry is up at all. Without it a run leaves
        # no Harness record, which is the difference between "we can show you
        # what happened" and "we cannot".
        "recorded_through_harness": getattr(engine, "harness_registry", None) is not None,
        # Off by default. When on, a configured external agent runs behind the
        # same contract instead of calling the broker unrecorded.
        "external_agent_behind_harness": external_behind_harness,
        "modes": ["company", "task"],
    }
    return snapshot


async def _recorded_cost(store: Any, project_id: str) -> tuple[dict[str, dict], float | None, int]:
    """What the cost table says: per task, project total, and how many calls.

    The third value is the one that keeps a zero honest. Zero cost across zero
    recorded calls means nothing has run yet; zero cost across many calls would
    mean the recording is broken. A reader that sees only the total cannot tell
    those apart, and neither could the assistant reporting it.
    """
    if store is None:
        return {}, None, 0
    try:
        by_task = await store.get_cost_by_task(project_id=project_id)
    except Exception:
        logger.warning("master_control: cost records unreadable; reporting cost as unknown")
        return {}, None, 0
    by_task = dict(by_task or {})
    total = sum(float(row.get("cost", 0.0) or 0.0) for row in by_task.values())
    calls = sum(int(row.get("calls", 0) or 0) for row in by_task.values())
    return by_task, round(total, 6), calls


def console_turn_meter(engine: Any, *, console_session_id: str) -> Any:
    """A meter that files a console turn's cost under the conversation's task.

    Returns None when there is nothing to file against -- no tracker, or no
    conversation id to key on. A meter that invented an id would produce rows
    that read as recorded and join to nothing, which is worse than none: the
    total would still be wrong and would now look accounted for.

    The task id matches the one the looped path uses, so both paths land in one
    place and turning the loop on does not move the money.
    """
    tracker = getattr(engine, "cost_tracker", None)
    if tracker is None or not console_session_id:
        return None

    task_id = f"master-{console_session_id}"

    async def _meter(*, model: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        from ampuly.layer6_observability.cost_tracker import CostEntry

        await tracker.record(CostEntry(
            task_id=task_id,
            agent_id="master_control",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
        ))

    return _meter


def _work_order_row(business: Any, order: Any) -> dict[str, Any] | None:
    """A work order as the judgement reads it (``api._order_payload``'s shape: the row, its acceptance, its handling).

    The acceptance is read by ``ConsoleAPI._acceptance_payload`` itself, over this store: the record and the acceptance
    rework still pending in the runtime (``runtime_rework``), whose ``last_error`` the judgement says as 返工出错 -- the
    input 业务工单 judges with, so 安安 says what that page says (Z2, review of 5190398d: this read the record alone,
    ``AcceptanceRecord.to_payload()``, and never saw a failed rework). An acceptance that cannot be read is None, as
    before; nothing else is guessed."""
    from types import SimpleNamespace

    from ampuly.plugins.office_ui.api import ConsoleAPI

    to_payload = getattr(order, "to_payload", None)
    if not callable(to_payload):
        return None
    row = dict(to_payload())
    if not str(row.get("order_id", "") or "").strip():
        return None
    try:
        row["acceptance"] = ConsoleAPI._acceptance_payload(
            SimpleNamespace(business=business), row["order_id"], project_id=str(row.get("project_id", "") or "default"),
        )
    except Exception:
        row["acceptance"] = None
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    handling = fields.get(HANDLING_KEY)
    row["handling"] = dict(handling) if isinstance(handling, dict) and handling else None
    return row


async def _stamp_work_orders(
    business: Any, project_id: str, orders: list[dict[str, Any]], checkpoints: list[Any] | None,
) -> None:
    """Each listed order that belongs to a work order carries its number (``order_id``), the name the owner knows it
    by (``order_label``) and the work order's own word (``order_status_zh``), judged by the one rule 业务工单 and
    总控 · 今天 count with (``order_attention.load_project_judgement``) over the pauses already read here.

    Owner report : 安安 described orders by engine status codes. The run's task names its order in its
    metadata (data phase 1); one that does not is looked up as the order's launched run. Nothing here may stop the
    context: an order that cannot be read keeps only what it had, and a judgement that cannot be made leaves the
    status's own word -- nothing is guessed.
    """
    if business is None or not orders:
        return
    get_order = getattr(business, "get_order", None)
    if not callable(get_order):
        return
    by_launch = getattr(business, "order_for_launched_task", None)
    pid = str(project_id or "default")

    def read_rows() -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for order in orders:
            number = str(order.get("order_id", "") or "").strip()
            try:
                if number:
                    found = get_order(number, project_id=pid)
                elif callable(by_launch):
                    found = by_launch(str(order.get("task_id", "") or ""), project_id=pid)
                else:
                    found = None
                row = _work_order_row(business, found) if found is not None else None
            except Exception:
                logger.debug(f"master_control: work order of {order.get('task_id', '')} unreadable")
                row = None
            if row is not None:
                rows[str(order.get("task_id", "") or "")] = row
        return rows

    try:
        rows = await asyncio.to_thread(read_rows)
    except Exception:
        logger.warning("master_control: work orders unreadable; orders keep the run's status only")
        return
    if not rows:
        return

    async def pending(_project_id: str) -> list[Any]:
        if checkpoints is None:
            raise RuntimeError("the pauses could not be read")
        return list(checkpoints)

    verdicts: dict[str, dict[str, Any]] = {}
    try:
        loaded = await load_project_judgement(pid, list(rows.values()), business=business, pending=pending)
        verdicts = dict(loaded.get("verdicts") or {})
    except Exception:
        logger.warning("master_control: work orders could not be judged; each says its own status")
    for order in orders:
        row = rows.get(str(order.get("task_id", "") or ""))
        if row is None:
            continue
        number = str(row["order_id"])
        order["order_id"] = number
        label = str(row.get("customer_name", "") or row.get("brand_name", "") or "").strip()
        if label:
            order["order_label"] = label[:80]
        order["order_status_zh"] = work_order_status_zh(row.get("status"), verdicts.get(number))


async def build_system_context(
    engine: Any,
    project_id: str,
    business: Any = None,
    console_session_id: str = "",
) -> dict[str, Any]:
    """Compact live-state summary — same data family as the dashboard page.

    ``business`` is the console's own store, passed in rather than reached
    for. A caller without one gets a context without the parts that need
    it, rather than this module deciding where files live.
    """
    tasks: list[Any] = []
    store = getattr(engine, "store", None)
    if store is not None:
        try:
            tasks = await store.get_tasks(project_id=project_id)
        except Exception:
            logger.warning("master_control: failed to load tasks for context")
    tasks = [t for t in tasks if not is_runtime_auxiliary_task(t)]

    cost_by_task, total_cost_usd, cost_call_count = await _recorded_cost(store, project_id)

    orders: list[dict[str, Any]] = []
    running = 0
    blocked = 0
    for task in tasks:
        status = _task_status(task)
        cost = cost_by_task.get(str(getattr(task, "id", "") or ""), {}).get("cost")
        if not _is_primary(task):
            continue
        # The console's own turn task is this conversation, not an order (owner report ): not listed, not
        # counted as running, not stoppable from here. Its spend stays in the project total (the cost rows above).
        if is_console_turn_task(task):
            continue
        if status not in _TERMINAL_STATUSES:
            running += 1
        if status in _BLOCKED_STATUSES:
            blocked += 1
        business_order = str(dict(getattr(task, "metadata", {}) or {}).get("order_id", "") or "").strip()
        orders.append({
            "task_id": str(getattr(task, "id", "") or ""),
            # The business order the pin stamped on the run's task (data phase 1): its files are under orders/<it>/.
            **({"order_id": business_order} if business_order else {}),
            "title": str(getattr(task, "title", "") or "")[:80],
            "status": status,
            # What 安安 says (owner report ); ``status`` is for the rules, never for the owner.
            "status_zh": status_zh(status),
            "updated_at": str(getattr(task, "updated_at", "") or ""),
            **({"cost_usd": round(float(cost), 4)} if isinstance(cost, (int, float)) else {}),
        })
    orders.sort(key=lambda item: item.get("updated_at", ""), reverse=True)

    # What is waiting on a person, and for how long. A run parked on a
    # checkpoint emits nothing, so without this the assistant would read a
    # 40-minute approval stall as "running fine". Same projection as the
    # dashboard's blocker list, so the two views cannot disagree.
    pending: list[dict[str, Any]] = []
    pending_index: dict[str, dict[str, Any]] = {}
    # The pauses as read, for the work orders' own judgement below; None = they could not be read (then partial).
    checkpoints_read: list[Any] | None = None
    get_pending = getattr(store, "get_pending_checkpoints", None)
    if callable(get_pending):
        try:
            checkpoints = await get_pending(project_id=project_id)
            checkpoints_read = list(checkpoints or [])
        except Exception:
            logger.warning("master_control: failed to load pending checkpoints for context")
            checkpoints = []
        for checkpoint in checkpoints:
            # The blocker plus ``attention_facts`` (S0-A): what the "who is it waiting on" rule below reads.
            item = pause_from_checkpoint(checkpoint)
            seconds = waiting_seconds(getattr(checkpoint, "created_at", None))
            item["waiting_seconds"] = seconds
            item["waiting_for"] = describe_wait(seconds, zh=is_zh())
            pending_index[item["checkpoint_id"]] = dict(item)
            item.pop("options", None)
            pending.append(item)
    # A19: blockers the owner asked not to be reminded about. They stay in
    # the index -- a reply to one still works -- and out of the list the
    # model answers "what is waiting" from; the count of held ones is said,
    # so silence is never mistaken for nothing pending.
    snoozed: dict[str, dict[str, Any]] = {}
    if business is not None and pending:
        try:
            snoozed = dict(business.active_snoozes(project_id=project_id) or {})
        except Exception:
            logger.warning("master_control: snoozes unreadable; listing every blocker")
            snoozed = {}
    held = [item for item in pending if item["checkpoint_id"] in snoozed]
    for item in held:
        item["snoozed_until"] = snoozed[item["checkpoint_id"]]["until_at"]
        pending_index[item["checkpoint_id"]]["snoozed_until"] = item["snoozed_until"]
    pending = [item for item in pending if item["checkpoint_id"] not in snoozed]
    pending.sort(key=lambda item: item.get("waiting_seconds") or 0, reverse=True)
    # Who each order waits on: the one rule 业务工单 and 项目中心 count with (S0-A, ``order_attention.judge_pauses``).
    # ``waiting_on_human`` is true only for a card addressed to the owner -- never the lesson card, never a step
    # assigned to an employee (that order says ``waiting_on: employee`` / 轮到某人 instead). Judged over the list
    # above, so a held card does not mark its order, as documented there.
    waiting_task_ids: set[str] = set()
    for order in orders:
        waits = [item for item in pending if item.get("task_id") == order["task_id"]]
        verdict = judge_pauses(waits)
        if verdict["waiting_on"]:
            order["waiting_on"] = verdict["waiting_on"]
            order["waiting_kind"] = verdict["waiting_kind"]
            if verdict["waiting_employee_id"]:
                order["waiting_employee_id"] = verdict["waiting_employee_id"]
        if verdict["waiting_on"] == WAITS_OWNER:
            order["waiting_on_human"] = True
            counted = set(verdict["card_ids"])
            longest = next((item for item in waits if item.get("checkpoint_id") in counted), waits[0])
            order["longest_wait"] = longest.get("waiting_for", "")
            waiting_task_ids.add(order["task_id"])
    blocked += sum(
        1 for order in orders
        if order["task_id"] in waiting_task_ids and order["status"] not in _BLOCKED_STATUSES
    )

    open_order_ids = {order["task_id"] for order in orders if order["status"] not in _TERMINAL_STATUSES}
    if store is not None and open_order_ids:
        try:
            activity = await _last_tool_activity(store, tasks, open_order_ids)
        except Exception:
            logger.warning("master_control: failed to load tool activity for context")
            activity = {}
        for order in orders:
            stamp = activity.get(order["task_id"])
            if stamp:
                order.update(stamp)
            if order["task_id"] in open_order_ids and stall_verdict(order):
                order["stalled"] = True

    # The listed orders that belong to a work order: its number and its own word, from the judgement 业务工单 uses.
    await _stamp_work_orders(business, project_id, orders[:15], checkpoints_read)

    agents: list[dict[str, Any]] = []
    org_engine = getattr(engine, "org_engine", None)
    list_agents = getattr(org_engine, "list_agents", None)
    if callable(list_agents):
        try:
            for info in list_agents()[:24]:
                status = getattr(info, "status", "")
                agents.append({
                    "role_id": str(getattr(info, "role_id", "") or ""),
                    "name": str(getattr(info, "name", "") or ""),
                    "status": str(getattr(status, "value", status) or ""),
                })
        except Exception:
            logger.debug("master_control: failed to list org agents for context")

    return {
        "active_order_count": running,
        "blocked_order_count": blocked,
        # None means the record could not be read -- never a zero standing in
        # for an unknown. The call count separates "nothing has run" from
        # "nothing was recorded".
        "total_cost_usd": None if total_cost_usd is None else round(total_cost_usd, 4),
        "cost_call_count": cost_call_count,
        # What this installation can do. Read from the live engine; a field that
        # could not be read is absent rather than guessed at.
        "capabilities": capability_snapshot(engine),
        # Descriptions being built. Reading only: master control cannot start a
        # trial, and the prompt says so, because a trial spends money.
        "process_drafts": process_drafts(business),
        # Whether this conversation has a durable identity. Empty means it does
        # not: the turn still answers, but nothing about it outlives the process,
        # so it must not be described as remembered. Said out loud because a
        # missing field reads as "fine" to every later reader.
        "console_session_id": str(console_session_id or ""),
        "conversation_is_durable": bool(console_session_id),
        "orders": orders[:15],
        "pending_checkpoints": pending[:20],
        "snoozed_checkpoint_count": len(held),
        "snoozed_checkpoints": [
            {"checkpoint_id": item["checkpoint_id"], "task_id": item.get("task_id", ""),
             "subject": item.get("subject", ""), "snoozed_until": item["snoozed_until"]}
            for item in held[:20]
        ],
        "roles": agents,
        # Not for the model: the full blockers (with their decision options)
        # the handler needs to act on a reply_checkpoint action.
        "_pending_index": pending_index,
    }


def _system_prompt() -> str:
    if is_zh():
        return (
            f"你是{PRODUCT_NAME_ZH}引擎的『总控』——面向老板的系统级指挥台助手。\n"
            "你收到的 JSON 上下文包含当前项目的实时状态：订单（会话/任务）、状态、成本、组织角色。\n"
            "你的能力：\n"
            "1. 回答系统状态问题（在跑什么、卡在哪、花了多少钱、谁在干活）——只依据上下文，绝不编造。\n"
            "   pending_checkpoints 是正在等人拍板的事项：checkpoint_type 是类别（tool_permission=工具审批，其余为提问/评审），\n"
            "   subject/detail 是在等什么（如 web_fetch 某个网址），waiting_for 是已经等了多久，task_id 对应哪一单。\n"
            "   用户问「卡在哪 / 为什么不动」时，先报这些：哪一单、等谁做什么、等了多久；有等待项的单不能说成『正常运行』。\n"
            "   orders 里 waiting_on_human=true 的单表示有事项在等用户，longest_wait 是最久的一项。\n"
            "   说一张单在什么状态，只用它的 status_zh；属于工单的，用工单状态 order_status_zh，并用 order_label 或标题称呼它。"
            "绝不写英文状态码。\n"
            "   task_id、checkpoint_id、工单号这类编号只用来填动作，不写进给老板的回复（老板自己先提到某个编号时除外）；"
            "也不要把工具名写给老板。\n"
            "   进行中的单还带 last_tool（最后一次工具动作）与 since_last_tool（距今多久）。没有等待项却很久没动，才是真的悬死，要如实说。\n"
            "   stalled=true 是系统按规则判定的悬死（没等人、没有模型在算、超过 15 分钟没动作），直接告诉用户并建议到工作台「继续」或「停止」。\n"
            "   上下文中的成本单位是美元（usd），取自费用记录表，不是估算。报给用户时换算成人民币（×7.2，保留两位小数），如 ¥0.16。\n"
            "   total_cost_usd 为 null 表示费用记录读不到——直接说『费用暂时读不到』，不要换算，更不要说成 ¥0。\n"
            "   total_cost_usd 为 0 且 cost_call_count 为 0，表示还没有记录到任何模型调用，说『还没有产生费用』；\n"
            "   若 cost_call_count 大于 0 而费用为 0，如实说记录到调用但费用为零，不要替它找解释。\n"
            "   capabilities 是这套系统实际装了什么：tools.reaching_out 是能对外的工具（联网、终端、发消息、生图），\n"
            "   tools.needs_approval 是每次调用都要人批的，model.default 是订单会用的模型，execution.executor 是执行内核。\n"
            "   用户问「你能不能做 X」时只按这里回答；**这里没有的字段表示读不到，不表示不能**，就说读不到，不要猜。\n"
            "   列出一个工具不等于你能调用它：你只有下面三种动作，具体每次调用仍由权限层决定。\n"
            "   process_drafts 是正在搭的流程描述：unanswered 是它还答不上来的问题，runnable=false 表示还不能试跑。\n"
            "   conversation_is_durable=false 表示这次对话没有持久身份、不会被记住；用户问「你记得吗」时如实说，不要假装记得。\n"
            "   用户问「新流程怎么样了」就报这些；**你不能发起试跑**（试跑要花钱），只能告诉用户去工作流页面点。\n"
            "2. 代用户【起草工单】：当用户明确要开一张新单时，输出 create_order 动作（2026-09-28 起只起草，不发单）。\n"
            "   - workflow_id 只能取自 order_flows（这个项目能接单的业务流）；title 用简洁中文工单名；"
            "fields 只按那条业务流 start_fields 的 id 填用户说过的内容，不编造。\n"
            "   - 一次最多一个 create_order。必填项缺了或需求不清就先追问，不要贸然起草。\n"
            "   - 起草的工单在「业务工单」的「待接单」里等老板放行：不会开跑、不开会话、不花钱。"
            "order_flows 里没有合适的业务流就如实说，并建议先起草一条新流程。\n"
            "   - 工单只在它的业务流步骤允许的网站（sites）上操作；用户点名的网站不在里面，就说要先在搭建台改那一步。\n"
            "   - response 里不要自行宣布『已起草』，更不能说『已发单』『已开始处理』——系统会自动附加回执，你只需简短说明理解到的需求。\n"
            "3. 代用户【批准/驳回】一个正在等待的事项：仅当用户在本条消息里明确下令（如「批准」「同意 web_fetch」「拒绝那个 shell」）时，\n"
            "   输出 reply_checkpoint 动作，checkpoint_id 必须取自 pending_checkpoints；decision 为 approve 或 deny。绝不因推测而批准。\n"
            "4. 代用户【停止】一单：仅当用户明确说停止/取消某一单时，输出 stop_order，task_id 取自 orders。指代不清就先问是哪一单。\n"
            "   每次最多一个控制动作。执行结果由系统追加回执，你不要自行宣布『已批准』『已停止』。\n"
            "永远用中文回答，语气干练。只返回严格 JSON：\n"
            "{\n"
            '  "response": "给用户的中文回复",\n'
            '  "actions": [\n'
            '    {"kind": "create_order", "workflow_id": "取自 order_flows", "title": "工单名", "fields": {"开单项 id": "用户说的内容"}},\n'
            '    {"kind": "reply_checkpoint", "checkpoint_id": "…", "decision": "approve"},\n'
            '    {"kind": "stop_order", "task_id": "…"}\n'
            "  ]\n"
            "}\n"
            "没有动作时 actions 为空数组。"
        )
    return (
        f"You are the Master Control of {PRODUCT_NAME_EN} Engine — a system-level command assistant.\n"
        "The JSON context contains the project's live state: orders (sessions/tasks), statuses, cost, org roles.\n"
        "pending_checkpoints lists what is waiting on a person: checkpoint_type (tool_permission = a tool approval, "
        "otherwise a question or review), subject/detail (what for), waiting_for (how long), task_id (which order). "
        "When asked what is stuck, report those first; an order with waiting_on_human=true is not 'running fine'.\n"
        "Say an order's state only in plain words from its status_zh (a work order's from order_status_zh, naming it by "
        "order_label or its title), in the owner's language; never write a raw status code. Ids -- task_id, "
        "checkpoint_id, the order number -- are for actions only: never put one in the reply unless the owner named it "
        "first, and never name a tool to the owner either.\n"
        "Open orders also carry last_tool and since_last_tool; an order with no pending checkpoint and no recent tool call is genuinely stalled — say so.\n"
        "stalled=true is the rule-based verdict (nobody waited on, no model call in flight, no action for 15+ min); say so and suggest continue or stop in the workspace.\n"
        "capabilities is what this installation actually has: tools.reaching_out are the ones that leave the machine, tools.needs_approval need a person every call, model.default is what an order runs on, execution.executor is the executor. Answer \"can you do X\" only from there. A field that is absent means it could not be read, NOT that the answer is no — say you cannot tell. Listing a tool is not permission to call it: you still have exactly the three actions below.\n"
        "conversation_is_durable=false means this conversation has no durable identity and is not remembered across restarts. Say so plainly if asked whether you remember something; do not imply that you do.\n"
        "process_drafts are the process descriptions being built: unanswered lists what each still cannot answer, runnable=false means it cannot be trialled yet. Report those when asked how a new process is coming. You CANNOT start a trial — it spends money — say the owner can run it from the workflows page.\n"
        "Cost comes from the recorded cost rows, not an estimate. total_cost_usd null means the record could not be read — say cost is unavailable; never report it as $0. total_cost_usd 0 with cost_call_count 0 means nothing has run yet; cost_call_count above 0 with zero cost is what the record says — report it, do not explain it away.\n"
        "You can: 1) answer system-state questions strictly from context; 2) draft ONE new work order via a "
        "create_order action when the user clearly asks for one (since 2026-09-28 master control only drafts): "
        "workflow_id only from order_flows (the flows this project takes orders on), a short title, and fields keyed "
        "by that flow's start_fields ids, filled only from what the owner said; at most one per turn; ask first when a "
        "required field is missing or the request is unclear. The draft waits in Orders (待接单) for the owner to "
        "release it: nothing runs, no session, nothing spent -- never say it was dispatched or started. When no flow "
        "fits, say so and suggest drafting a new flow. An order works only on its flow's sites; another site means "
        "changing that step on the builder. Do not announce the draft yourself — the system appends a receipt); "
        "3) answer a waiting checkpoint with a reply_checkpoint action (checkpoint_id from pending_checkpoints, decision approve|deny) "
        "or stop an order with stop_order (task_id from orders) - ONLY when this very message explicitly commands it; never on "
        "inference, at most one control action per turn, and do not announce the outcome yourself (the system appends a receipt).\n"
        "Return strict JSON only:\n"
        '{"response": "reply", "actions": [{"kind": "create_order", "workflow_id": "...", "title": "...", "fields": {"...": "..."}}, '
        '{"kind": "reply_checkpoint", "checkpoint_id": "...", "decision": "approve"}, {"kind": "stop_order", "task_id": "..."}]}\n'
        "Use an empty actions array when there is nothing to do."
    )


async def process_master_message(
    llm: Any,
    *,
    engine: Any,
    project_id: str,
    content: str,
    history: list[dict[str, str]] | None = None,
    business: Any = None,
    console_session_id: str = "",
    order_flows: list[dict[str, Any]] | None = None,
    budget_id: str = "",
    preprocess: Any = None,
) -> dict[str, Any]:
    """Run one master-control turn. Returns {response, actions}.

    ``order_flows`` (policy, M1): the flows this project takes orders on, as master_list_order_flows lists
    them. The single call has no tool to look them up, so a draft's workflow_id and start fields come from here.
    """
    from ampuly.llm.provider import model_credential_scope
    from ampuly.plugins.office_ui.master_runtime import MasterSafetyStop, customer_for

    if budget_id and not str(console_session_id or "").strip():
        raise MasterSafetyStop("Paid turn has no accounting identity; model call not started")
    customer_id = await customer_for(business, project_id or "default")
    accounting_task = None
    if console_session_id:
        from ampuly.core.models import Task
        from ampuly.plugins.office_ui.master_runtime import ensure_master_task_row

        # Accounting identity only, not an execution workspace or tool grant.
        metadata = {"interface": "master_control", "customer_id": customer_id,
                    "execution_source": "direct_task",
                    "ampuly_home": str(getattr(engine, "ampuly_home", "") or "")}
        if budget_id:
            metadata["budget_id"] = str(budget_id)
        accounting_task = Task(
            id=f"master-{console_session_id}", title="总控",
            project_id=project_id or "default", session_id=console_session_id,
            metadata=metadata,
        )
        if not await ensure_master_task_row(getattr(engine, "store", None), accounting_task):
            raise RuntimeError("Master control accounting unavailable; model call not started")
    if preprocess is not None:
        content = await preprocess(accounting_task, content)
    context = await build_system_context(
        engine, project_id, business=business, console_session_id=console_session_id,
    )
    model_state = {key: value for key, value in context.items() if not key.startswith("_")}
    if order_flows is not None:
        model_state["order_flows"] = list(order_flows)
    from ampuly.plugins.office_ui.master_identity import conversation_identity, identity_prompt

    identity = conversation_identity(llm, executor="legacy_json_dispatch", task_type="quick_tasks")
    model_state["current_conversation"] = identity
    payload = json.dumps(
        {
            "project_id": project_id,
            "user_message": content,
            "recent_conversation": (history or [])[-12:],
            "system_state": model_state,
        },
        ensure_ascii=False,
    )
    try:
        from contextlib import ExitStack

        from ampuly.llm.budget import model_budget_scope

        with ExitStack() as budget_stack:
            budget_stack.enter_context(model_credential_scope(customer_id))
            if budget_id:
                # The single-JSON compatibility path spends from the same
                # project allowance as the looped turn; no unbudgeted bypass.
                from ampuly.llm.budget import budget_is_bound
                budget_stack.enter_context(model_budget_scope(accounting_task))
                if not budget_is_bound():
                    raise MasterSafetyStop(
                        "Paid turn has no budget binding; model call not started"
                    )
            parsed = await call_llm_json_with_retry(
                llm,
                system=_system_prompt() + identity_prompt(identity),
                payload=payload,
                task_type="master_control",
                label="master_control",
                # Keep the question's own spend inside the project total.
                meter=console_turn_meter(engine, console_session_id=console_session_id),
            )
    except RouteRefused as exc:
        logger.warning(f"master_control: model call refused before sending: {exc.code}")
        return {"response": route_refused_notice(exc), "actions": []}
    except LLMRetryError as exc:
        fallback = str(exc.last_raw or "").strip()
        logger.warning(f"master_control: LLM returned invalid JSON after retries: {exc}")
        return {"response": fallback or ("总控暂时无法响应，请稍后再试。" if is_zh() else "Master control is temporarily unavailable."), "actions": []}

    response = str(parsed.get("response", "") or "").strip()
    actions = actions_from_model(parsed.get("actions", []), context)
    return {"response": response, "actions": actions, "context": context}


_CHECKPOINT_TYPE_LABELS_ZH = {
    "tool_permission": "工具审批",
    "company_staffing_selection": "人员配置确认",
    "company_reorg_pending": "组织调整确认",
    "human_escalation": "人工升级",
    "delivery_feedback": "交付反馈",
}
_CHECKPOINT_TYPE_LABELS_EN = {
    "tool_permission": "tool approval",
    "company_staffing_selection": "staffing confirmation",
    "company_reorg_pending": "reorg confirmation",
    "human_escalation": "escalation",
    "delivery_feedback": "delivery feedback",
}


def checkpoint_subject(action: dict[str, Any], *, zh: bool) -> str:
    """What the receipt calls the thing that was decided: the tool name when
    there is one, otherwise the kind of checkpoint — never a bare id."""
    subject = str(action.get("subject", "") or "").strip()
    if subject:
        return subject
    kind = str(action.get("checkpoint_type", "") or "").strip()
    labels = _CHECKPOINT_TYPE_LABELS_ZH if zh else _CHECKPOINT_TYPE_LABELS_EN
    return labels.get(kind) or kind or str(action.get("checkpoint_id", "") or "")[:8]


def decision_for(action: dict[str, Any]) -> dict[str, Any]:
    """Shape the decision the way each checkpoint type is validated.

    The coordinator does not take one generic "approve": a tool permission
    wants an option id, a staffing checkpoint wants a staffing_action (and
    selections only when approving a concrete plan), a lesson card wants
    approve|ignore, a question wants text. Sending the wrong shape is
    refused as invalid_* - which is how the first live use of the verb
    reported "not approved: invalid_staffing_action".
    """
    decision = str(action.get("decision", "") or "").strip().lower()
    kind = str(action.get("checkpoint_type", "") or "").strip()
    text = str(action.get("text", "") or "").strip()
    reply_kind = "deny" if decision == "deny" else "approve"
    payload: dict[str, Any] = {
        "text": text or (action.get("option_label") or reply_kind),
        "checkpoint_reply_kind": reply_kind,
    }
    if kind == "company_staffing_selection":
        staffing = dict(action.get("staffing", {}) or {})
        selections = dict(staffing.get("staffing_selections", {}) or {})
        if reply_kind == "deny":
            payload["staffing_action"] = "deny"
        elif selections:
            payload["staffing_action"] = "manual_approve"
            payload["staffing_selections"] = selections
        else:
            # No concrete default plan to approve: approving means letting
            # the runtime recruit, which is also its own recommendation.
            payload["staffing_action"] = "auto_recruit"
        return payload
    if kind == "company_delivery_feedback":
        # The lesson card (留点经验卡): approve is 完全同意 and starts the paid learning pass; a deny from any surface
        # (打回, 拒绝, 不用了) is 忽略 and costs nothing -- never feedback, which would start the paid pass too (owner
        # decision, runtime contract). The words a deny came with are not sent as feedback.
        if reply_kind == "deny":
            payload["checkpoint_reply_kind"] = "ignore"
            payload["text"] = "忽略"
        return payload
    if kind in {"task_user_input", "route_clarification", "company_runtime_selection"}:
        payload["text"] = text or ("同意" if reply_kind == "approve" else "不同意")
        return payload
    if action.get("option_id"):
        payload["option_id"] = action["option_id"]
    if action.get("option_label"):
        payload["option_label"] = action["option_label"]
    return payload


def verb_receipt(action: dict[str, Any], ack: dict[str, Any] | None, *, zh: bool) -> str:
    """Phrase the engine's answer to a control verb for the owner.

    The receipt says what the engine did, not what was asked: a decision the
    checkpoint CAS refused, or one that had already been taken, must not
    read as "approved". An earlier version reported the request as done
    without reading the acknowledgement, and the owner saw "approved" next
    to a checkpoint that was still pending.
    """
    kind = str(action.get("kind", "") or "")
    ack = dict(ack or {})
    duplicated = bool(ack.get("deduplicated") or ack.get("duplicate"))
    # A duplicate is a successful "already done", not a refusal.
    ok = bool(ack.get("ok")) and (duplicated or ack.get("accepted", True) is not False)
    reason = str(ack.get("reason") or ack.get("error") or ack.get("status") or "").strip()
    if kind == "reply_checkpoint":
        decision = str(action.get("decision", "") or "")
        subject = checkpoint_subject(action, zh=zh)
        verb = ("已批准" if decision == "approve" else "已驳回") if zh else (
            "Approved" if decision == "approve" else "Denied"
        )
        if ok and duplicated:
            return (f"ℹ️ {subject}：这条决定之前已经收到过，未重复执行。" if zh
                    else f"ℹ️ {subject}: that decision was already recorded; nothing repeated.")
        if ok:
            return f"✅ {verb}：{subject}" if zh else f"✅ {verb}: {subject}"
        if not ack:
            return (f"⚠️ {subject}：引擎没有回执，无法确认是否生效，请到工作台会话里核对。" if zh
                    else f"⚠️ {subject}: no acknowledgement from the engine; check the workspace session.")
        return (f"⚠️ 未能{verb[1:]}：{subject}（{reason or '引擎拒绝'}），请到工作台会话里处理。" if zh
                else f"⚠️ Not {verb.lower()}: {subject} ({reason or 'refused by the engine'}); handle it in the workspace session.")
    if kind == "stop_order":
        title = str(action.get("title", "") or "") or str(action.get("task_id", "") or "")[:8]
        if ok or not ack:
            return f"⏹ 已发送停止指令：{title}" if zh else f"⏹ Stop requested: {title}"
        return (f"⚠️ 停止未被接受：{title}（{reason or '引擎拒绝'}）" if zh
                else f"⚠️ Stop not accepted: {title} ({reason or 'refused by the engine'})")
    if kind == "interrupt_turn":
        title = str(action.get("title", "") or "") or str(action.get("task_id", "") or "")[:8]
        if reason in {"nothing_running", "nothing_pending"}:
            # Not a failure. Nothing was running, and saying "interrupted"
            # would have the owner believe a turn was stopped that was
            # already over.
            return (f"ℹ️ {title}：当前没有正在跑的回合，订单保持原状。" if zh
                    else f"ℹ️ {title}: no turn is running; the order is untouched.")
        if ok:
            # A request, not a settlement. The turn stops at its next
            # progress event and its own terminal record still decides.
            return (f"⏸ 已请求中止当前回合：{title}（订单仍然开着）" if zh
                    else f"⏸ Interrupt requested for the running turn: {title} (the order stays open)")
        if not ack:
            return (f"⚠️ {title}：引擎没有回执，无法确认回合是否收到中止请求。" if zh
                    else f"⚠️ {title}: no acknowledgement from the engine; the interrupt is unconfirmed.")
        return (f"⚠️ 中止未被接受：{title}（{reason or '引擎拒绝'}），订单未受影响。" if zh
                else f"⚠️ Interrupt not accepted: {title} ({reason or 'refused by the engine'}); "
                     "the order is unaffected.")
    if kind == "steer_turn":
        title = str(action.get("title", "") or "") or str(action.get("task_id", "") or "")[:8]
        if ok:
            return (f"↪️ 当前回合已接收新要求：{title}（尚未确认模型采纳或执行）" if zh
                    else f"↪️ Instruction accepted for the running turn: {title}; model consumption and execution are not confirmed.")
        if not ack or reason == "outcome_unknown":
            return (f"⚠️ {title}：引擎没有回执，不能确认新要求是否送达；不要自动重发。" if zh
                    else f"⚠️ {title}: no acknowledgement; delivery is unknown, so it was not retried.")
        alternative = str(ack.get("alternative", "") or "").strip()
        suffix = f"；{alternative}" if zh and alternative else f"; {alternative}" if alternative else ""
        return (f"⚠️ 未能修改当前回合：{title}（{reason or '引擎拒绝'}）{suffix}" if zh
                else f"⚠️ Could not steer the running turn: {title} ({reason or 'refused by the engine'}){suffix}")
    return ""


def clean_browser_domains(raw: Any, *, limit: int = 20) -> list[str]:
    """Hostnames from what the model passed: a list or a comma/space separated string.

    A URL is reduced to its host; a scheme, path, port or credential never
    survives; anything that is not a hostname is dropped rather than fixed.
    """
    from urllib.parse import urlsplit

    if isinstance(raw, str):
        items: list[Any] = [part for part in raw.replace(",", " ").replace("，", " ").split() if part]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip().lower()
        if not text:
            continue
        host = (urlsplit(text).hostname or "") if "://" in text else text.split("/", 1)[0].split("@")[-1].split(":", 1)[0]
        host = host.strip().strip(".")
        if not host or host in out:
            continue
        if not all(part and all(ch.isalnum() or ch == "-" for ch in part) for part in host.split(".")):
            continue
        if "." not in host and host != "localhost":
            continue
        out.append(host)
        if len(out) >= limit:
            break
    return out


# X05 real run : the master wrote "已发单" without calling the dispatch tool; the
# owner read a receipt that did not exist. A claim the turn did not back is corrected in the
# same reply, by the system, after the model's text.
# policy (M1): master control only drafts a work order, so "drafted" is checked against the draft tool, and
# "dispatched / started" is corrected always -- no master tool dispatches or starts an order any more.
_CLAIMS: tuple[tuple[str, str, str, str], ...] = (
    ("create_order",
     r"已(?:经)?(?:为你|给你|帮你)?(?:起草|建)(?:了|好)?(?:一张|一份)?(?:工单|订单)|(?:工单|订单)已(?:经)?(?:起草|创建|建好)"
     r"|(?:work )?order (?:has been |was )?(?:drafted|created)|drafted (?:an? |the )?(?:work )?order",
     "本回合没有实际起草工单（没有调用起草工单的工具），上面写的「已起草」不算数。",
     "no work order was drafted this turn (the draft tool was not called); the claim above does not count."),
    ("start_order",
     r"已(?:经)?(?:为你|给你|帮你)?(?:发单|下单|派单)|订单已(?:下达|发出|发起)|order (?:has been |was )?dispatched",
     "安安不会发单或开工：工单只起草，在业务工单「待接单」里等你放行，上面说的「已发单」不算数。",
     "master control never dispatches or starts an order: it only drafts one, which waits in Orders for the owner "
     "to release; the claim above does not count."),
    ("reply_checkpoint", r"已(?:经)?(?:批准|同意|放行|驳回|拒绝)(?!.*未)|(?:has been |was )?(?:approved|denied)",
     "本回合没有实际批准或驳回任何事项（未调用审批工具）。", "nothing was approved or denied this turn (the checkpoint tool was not called)."),
    ("stop_order", r"已(?:经)?(?:停止|停单|取消)(?:该|这|此)?单|order (?:has been |was )?(?:stopped|cancelled)",
     "本回合没有实际停单（未调用停单工具）。", "no order was stopped this turn (the stop tool was not called)."),
)


def unbacked_claims(text: str, attempted: list[str] | tuple[str, ...], *, zh: bool = True) -> list[str]:
    """Corrections for what the reply announces but the turn never attempted.

    `attempted` are the write kinds this turn tried (create_order, reply_checkpoint,
    stop_order,...). An attempt that was refused still counts as attempted: the
    handler's own receipt says what happened. Only the model's text is checked.
    ``start_order`` is never attempted -- no master tool dispatches or starts an
    order since the policy decision -- so that claim is always corrected.
    """
    body = str(text or "")
    done = set(str(k) for k in attempted or ())
    lines: list[str] = []
    for kind, pattern, zh_line, en_line in _CLAIMS:
        if kind in done:
            continue
        if re.search(pattern, body, re.IGNORECASE):
            lines.append(("⚠️ 纠正：" if zh else "⚠️ Correction: ") + (zh_line if zh else en_line))
    return lines


def _lease_minutes(raw: Any) -> int | None:
    if raw in (None, ""):
        return 60
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    return minutes if 1 <= minutes <= 480 else None


def actions_from_model(actions_raw: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only actions the live state can honour.

    The model proposes; this function decides what reaches the engine. A
    checkpoint reply must name a checkpoint that is pending right now, a stop
    must name an open order, and each turn carries at most one order and one
    control verb — a chat line is not a batch job.
    """
    actions: list[dict[str, Any]] = []
    if not isinstance(actions_raw, list):
        return actions
    pending_index = dict(context.get("_pending_index", {}) or {})
    open_orders = {
        str(order.get("task_id", "") or ""): order
        for order in list(context.get("orders", []) or [])
        if str(order.get("status", "") or "") not in _TERMINAL_STATUSES
    }
    order_taken = False
    control_taken = False
    for action in actions_raw:
        if not isinstance(action, dict):
            continue
        kind = str(action.get("kind", "") or "").strip()
        if kind == "create_order" and not order_taken:
            # policy (M1): a work-order draft on a flow (the console's draft check decides whether this
            # project takes orders on it and whether the fields are the flow's). No mode, no free description and no
            # domains any more: the flow decides how it runs and which sites it opens.
            workflow_id = str(action.get("workflow_id", "") or "").strip()
            fields = action.get("fields")
            if fields is None:
                fields = {}
            if not workflow_id or not isinstance(fields, dict):
                continue
            actions.append({
                "kind": "create_order",
                "workflow_id": workflow_id,
                "title": str(action.get("title", "") or "").strip()[:80],
                "fields": dict(fields),
            })
            order_taken = True
        elif kind == "reply_checkpoint" and not control_taken:
            checkpoint_id = str(action.get("checkpoint_id", "") or "").strip()
            blocker = pending_index.get(checkpoint_id)
            if blocker is None:
                continue
            decision = str(action.get("decision", "") or "").strip().lower()
            if decision not in {"approve", "deny"}:
                continue
            option = next(
                (opt for opt in list(blocker.get("options", []) or []) if opt.get("kind") == decision),
                None,
            )
            actions.append({
                "kind": "reply_checkpoint",
                "checkpoint_id": checkpoint_id,
                "checkpoint_type": str(blocker.get("checkpoint_type", "") or ""),
                "staffing": dict(blocker.get("staffing", {}) or {}),
                "task_id": str(blocker.get("task_id", "") or ""),
                "session_id": str(blocker.get("session_id", "") or ""),
                "subject": str(blocker.get("subject", "") or ""),
                "decision": decision,
                "option_id": str((option or {}).get("id", "") or ""),
                "option_label": str((option or {}).get("label", "") or ""),
                "text": str(action.get("text", "") or "").strip(),
            })
            control_taken = True
        elif kind == "stop_order" and not control_taken:
            task_id = str(action.get("task_id", "") or "").strip()
            order = open_orders.get(task_id)
            if order is None:
                continue
            actions.append({
                "kind": "stop_order",
                "task_id": task_id,
                "title": str(order.get("title", "") or ""),
            })
            control_taken = True
        elif kind == "snooze_blocker" and not control_taken:
            checkpoint_id = str(action.get("checkpoint_id", "") or "").strip()
            until_at = str(action.get("until_at", "") or "").strip()
            # Pending includes snoozed ones: re-snoozing to a new time is fine.
            if checkpoint_id not in pending_index or not until_at:
                continue
            try:
                datetime.fromisoformat(until_at)
            except ValueError:
                continue
            actions.append({
                "kind": "snooze_blocker",
                "checkpoint_id": checkpoint_id,
                "task_id": str(pending_index[checkpoint_id].get("task_id", "") or ""),
                "until_at": until_at,
                "note": str(action.get("note", "") or "").strip(),
            })
            control_taken = True
        elif kind == "run_offline_trial" and not control_taken:
            spec_id = str(action.get("spec_id", "") or "").strip()
            if not spec_id:
                continue
            actions.append({
                "kind": "run_offline_trial",
                "spec_id": spec_id,
                "version": str(action.get("version", "") or "").strip(),
            })
            control_taken = True
        # No "update_candidate_nodes": since the policy decision master control only proposes a
        # node table (master_propose_candidate_nodes, on the loop), and the owner saves it on the builder canvas.
        # A model that still emits the old action here is dropped like any unknown kind.
        elif kind == "generate_connector_candidate" and not control_taken:
            contract = action.get("contract")
            if not isinstance(contract, dict):
                continue
            actions.append({
                "kind": "generate_connector_candidate",
                "contract": dict(contract),
            })
            control_taken = True
        elif kind == "create_improvement_proposal" and not control_taken:
            proposal = action.get("proposal")
            if not isinstance(proposal, dict):
                continue
            actions.append({
                "kind": "create_improvement_proposal",
                "proposal": dict(proposal),
            })
            control_taken = True
        elif kind == "create_improvement_from_acceptance" and not control_taken:
            candidate = action.get("candidate")
            order_id = str(action.get("order_id", "") or "").strip()
            title = str(action.get("title", "") or "").strip()
            rationale = str(action.get("rationale", "") or "").strip()
            if not isinstance(candidate, dict) or not order_id or not title or not rationale:
                continue
            actions.append({
                "kind": "create_improvement_from_acceptance",
                "order_id": order_id, "title": title, "rationale": rationale, "candidate": dict(candidate),
            })
            control_taken = True
        elif kind == "interrupt_turn" and not control_taken:
            # Same liveness rule as a stop: the order has to be open. An
            # interrupt aimed at a finished order would read as though
            # something had been stopped.
            task_id = str(action.get("task_id", "") or "").strip()
            order = open_orders.get(task_id)
            if order is None:
                continue
            actions.append({
                "kind": "interrupt_turn",
                "task_id": task_id,
                "title": str(order.get("title", "") or ""),
            })
            control_taken = True
        elif kind == "delegate_browser" and not control_taken:
            # C01: the scope is the owner's words, held against the live orders.
            # No open order, no domain, or a lease out of range: nothing reaches
            # the console, and the loop is told which assumption was wrong.
            task_id = str(action.get("task_id", "") or "").strip()
            order = open_orders.get(task_id)
            domains = clean_browser_domains(action.get("domains"))
            lease = _lease_minutes(action.get("lease_minutes"))
            if order is None or not domains or lease is None:
                continue
            actions.append({
                "kind": "delegate_browser",
                "task_id": task_id,
                "title": str(order.get("title", "") or ""),
                "domains": domains,
                "vision": bool(action.get("vision", False)),
                "downloads": bool(action.get("downloads", False)),
                "lease_minutes": lease,
                "visible": bool(action.get("visible", False)),
            })
            control_taken = True
        elif kind == "steer_turn" and not control_taken:
            task_id = str(action.get("task_id", "") or "").strip()
            text = str(action.get("text", "") or "").strip()
            order = open_orders.get(task_id)
            if order is None or not text or len(text) > 4000:
                continue
            actions.append({
                "kind": "steer_turn",
                "task_id": task_id,
                "title": str(order.get("title", "") or ""),
                "text": text,
            })
            control_taken = True
    return actions
