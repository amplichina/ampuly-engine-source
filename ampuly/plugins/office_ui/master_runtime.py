"""The executor behind one master-control conversation.

Master control answers in one shot: build a context, ask the model once, parse
one JSON object, execute at most one action, append a receipt. It cannot look
something up and then decide, because there is no second step -- so it answers
about the system from a snapshot assembled before it knew what was asked.

This gives that conversation the executor the product already has. It does not
add one. The tool loop, the transcript, the exclusive claim, the lease
heartbeat, the cross-process interrupt and the settle-or-wedge rule all live in
NativeRuntimeV2 and NativeHarnessProvider; driving the runtime directly and
writing our own turn rows would duplicate four things that only agree while one
implementation owns them. So the shim here is four lines, and everything else
is the existing path.

What this module is actually careful about:

**The tool list is explicit, always.** The runtime now distinguishes an empty
list (no tools) from an absent restriction. The list comes from
:mod:`master_tools`, which refuses an empty master capability set.

**The workspace is set, never defaulted.** With no workspace on the task the
execution context falls back to the current working directory, which for a
console server is the product's own source tree.

**One task lives across turns.** The same Task object is held between turns
because the loop mutates it (the ledger, the seed-dedupe, the runtime state
all live in its metadata), and a fresh copy each turn would quietly lose them.
It does not carry the conversation: the runtime is given no memory manager, so
it persists and restores no transcript. What the owner said before reaches the
next turn as a bounded recap of the console's stored rows
(:func:`conversation_so_far`, policy, B0).

**The owner can steer a running turn.** The Harness provider gives each turn a
bounded queue on the task; :func:`owner_steers` is what the runtime drains at
its next step, so a 插话 reaches the model instead of stopping at the queue.

**The user's message travels in the description.** When a task is adopted, the
provider stamps the session onto it and changes nothing else -- including not
setting the description from the turn's instruction. A naive adopt-then-run
loses what the person said. Setting it per turn is what the adapter provider
does for the same reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ampuly.core.models import Task
from ampuly.plugins.office_ui.master_tools import (
    MASTER_TOOLS,
    MasterTool,
    allowed_tool_names,
    by_name,
)

# policy (B0): 10 -> 24. Ten steps was enough to look one thing up and
# answer, not to look several things up, compare them and then act -- the owner's
# "master control's limited ability". Every iteration is still a paid call, so it
# stays a bound; one order and one control verb per message are unchanged.
DEFAULT_MAX_ITERATIONS = 24


class MasterSafetyStop(RuntimeError):
    """An authority/accounting refusal must not switch to a weaker executor."""


def fallback_forbidden(error: BaseException) -> bool:
    from ampuly.llm.budget import ModelBudgetStop
    from ampuly.llm.managed_model_route import ModelRouteRefused
    from ampuly.layer3_agent.managed_sdk_attempt import ManagedAttemptRefused
    from ampuly.plugins.office_ui.credential_refs import CredentialUnavailableError

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (MasterSafetyStop, ModelBudgetStop, ModelRouteRefused, ManagedAttemptRefused,
                                CredentialUnavailableError, PermissionError, ValueError)):
            return True
        current = current.__cause__ or current.__context__
    return False


class MasterActions(Protocol):
    """What the console performs on the conversation's behalf.

    Deliberately a Protocol over the console handler rather than an import of
    it: a write must go through the handler method that already implements it,
    and naming that requirement in a type is how it stays true. The reads are
    here too so a test can drive the loop without a console.
    """

    async def read(self, name: str, arguments: dict[str, Any]) -> Any: ...

    async def write(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class MasterTurn:
    """What one turn did, in terms the console can render and record."""

    reply: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    harness_session_id: str = ""
    fell_back: bool = False
    fallback_reason: str = ""

    def performed_writes(self) -> list[str]:
        return [
            str(call.get("name", ""))
            for call in self.tool_calls
            if (by_name(str(call.get("name", ""))) or MasterTool(name="x", summary="y" * 21, writes=False)).writes
        ]


def _parameters_for(tool: MasterTool) -> dict[str, Any]:
    """The argument schema each tool takes.

    Written out per tool rather than generated, because a schema is what the
    model is actually told and a generated one drifts from the handler that
    receives it.
    """
    if tool.name in {"master_resume_flow_draft", "master_propose_component_candidate"}:
        source = {"type": "object", "additionalProperties": False, "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "project_id": {"type": "string"}, "message_id": {"type": "string"},
            "draft_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "required": ["schema_version", "project_id", "message_id", "draft_sha256"]}
        properties = {"source": source}
        required = ["source"]
        if tool.name == "master_propose_component_candidate":
            properties.update(step_id={"type": "string"}, kind={"type": "string", "enum": ["skill", "flow"]},
                details={"type": "object", "description": "skill: {skill_id,method,acceptance}; flow: {intent}. Do not claim tested or ready."})
            required += ["step_id", "kind", "details"]
        return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}
    if tool.name == "master_order_detail":
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "The order to look at."}},
            "required": ["task_id"],
        }
    if tool.name == "master_list_files":
        return {
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "description": "A candidate package id from master_process_drafts or from calling this with no arguments."},
                "task_id": {"type": "string", "description": "An order: its task_id from master_list_orders or its order number (order_id, wo-...); lists that order's deliverables."},
            },
        }
    if tool.name == "master_read_file":
        return {
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "description": "Read from this candidate package."},
                "task_id": {"type": "string", "description": "Read from this order's deliverables: its task_id or its order number (order_id, wo-...)."},
                "path": {"type": "string", "description": "Relative path exactly as master_list_files listed it."},
                "offset": {"type": "integer", "description": "Character offset to start from; use with truncated results."},
                "max_chars": {"type": "integer", "description": "Window size, default 12000."},
            },
            "required": ["path"],
        }
    if tool.name == "master_preflight_candidate":
        return {
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "description": "A candidate package id."},
                "fields": {"type": "object", "description": "Intake field values to preflight with; may be empty."},
            },
            "required": ["package_id"],
        }
    if tool.name == "master_snooze_blocker":
        return {
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "description": "From master_list_blockers; never invented."},
                "until_at": {"type": "string", "description": "ISO timestamp; the reminder is held until then."},
                "note": {"type": "string", "description": "Why, in the owner's words."},
            },
            "required": ["checkpoint_id", "until_at"],
        }
    if tool.name == "master_run_offline_trial":
        return {
            "type": "object",
            "properties": {
                "spec_id": {"type": "string", "description": "A process description id from master_process_drafts."},
                "version": {"type": "string", "description": "Its version; the newest when omitted."},
            },
            "required": ["spec_id"],
        }
    if tool.name == "master_search_components":
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the step needs, in a few words (e.g. 海报 排版, 商品标题 SEO). Empty lists the top ones."},
                "limit": {"type": "integer", "description": "How many per source, 10 by default, at most 25."},
            },
        }
    if tool.name == "master_propose_candidate_nodes":
        return {
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "description": "A draft or testing package id (the kinds the builder canvas edits)."},
                "expected_version": {"type": "string", "description": "The version field you read from its workflow.yaml. Required; a mismatch is refused."},
                "nodes": {
                    "type": "array", "items": {"type": "object"},
                    "description": (
                        "The full node table you propose. Fields: id, name, type, role, assignee, gate, gate_to, "
                        "depends_on, deliverables, skills, tools, responsibility, invoke, canvas. Nothing is saved."
                    ),
                },
                "new_version": {"type": "string", "description": "The version the owner would save it as; must differ from expected_version."},
                "change_note": {"type": "string", "description": "What changes and why, in the owner's words."},
            },
            "required": ["package_id", "expected_version", "nodes", "new_version"],
        }
    if tool.name == "master_propose_flow_draft":
        step = {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short id, lowercase letters/digits/_ (becomes the step id)."},
                "name": {"type": "string", "description": "The step's name, a few words."},
                "kind": {"type": "string", "enum": ["ai", "human", "review"], "description": "Who does it: AI, a person, or a person's review."},
                "does": {"type": "string", "description": "What the step is responsible for."},
                "delivers": {"type": "array", "items": {"type": "string"}, "description": "Files it produces, as paths (brief/需求.md)."},
                "needs": {"type": "array", "items": {"type": "string"}, "description": "Files it needs from earlier steps."},
                "after": {"type": "array", "items": {"type": "string"}, "description": "Keys of the steps it waits for."},
                "capability": {"type": "string", "description": "For an AI step: the capability it needs, in a few words; the console finds the component."},
                "component": {"type": "string", "description": "Optional: an internal-library skill id master_search_components returned. Never invent one."},
                "role": {"type": "string", "description": "Optional: which role of the team does it (a hint; unmatched stays unassigned)."},
                "sign_off": {"type": "boolean", "description": "True when a person must sign the step off."},
                "source": {"type": "string", "description": "Where this step came from, in the owner's language, e.g. 对话 or 需求.docx 第 2 段."},
                "site": {"type": "string", "description": "Optional: the website this step is about (a host, e.g. example.com). Only what the owner named -- never invent one."},
                "external": {"type": "boolean", "description": "Optional: true when the step has to reach a website. A step with no component for it then gets the browser connector, with its settings left for the owner to confirm."},
                "lock_site": {"type": "boolean", "description": "Optional: true only when the owner named this exact site for this step; the console then writes it as the step's only allowed site. Never lock a site the owner did not name, and never invent one."},
            },
            "required": ["key", "name", "kind"],
        }
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The new workflow's name."},
                "summary": {"type": "string", "description": "What it achieves, one or two sentences."},
                "team": {"type": "string", "description": "Optional: the saved team that runs it, if the owner said."},
                "steps": {"type": "array", "items": step, "description": "The steps, at most 30: the whole flow in this one call. A later call in the same reply replaces this draft."},
                "intake": {
                    "type": "array", "description": "Optional: the fields an order is opened with.",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"type": "string"}, "required": {"type": "boolean"}}},
                },
                "open_questions": {"type": "array", "items": {"type": "string"}, "description": "What the requirement leaves undecided; ask, never guess."},
                "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Low-risk assumptions you made to draw the draft."},
            },
            "required": ["name", "steps"],
        }
    if tool.name == "master_generate_connector_candidate":
        return {
            "type": "object",
            "properties": {
                "contract": {
                    "type": "object",
                    "description": (
                        "Formal ConnectorContract: name, version, base_url_env, auth, source and operations. "
                        "Environment variable names only; never include a URL credential or secret value."
                    ),
                },
                "catalog_id": {
                    "type": "string",
                    "description": "Exact component-library id when this contract implements a catalog entry.",
                },
                "catalog_fingerprint": {
                    "type": "string",
                    "description": "Exact 64-character fingerprint shown on that current catalog entry; stale values are refused.",
                },
                "confirmed_inferred_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only field names the owner explicitly confirmed, such as access_mode; omit otherwise.",
                },
            },
            "required": ["contract"],
        }
    if tool.name == "master_create_improvement_from_acceptance":
        return {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order whose customer rejection the improvement answers (from master_list_orders)."},
                "title": {"type": "string"},
                "rationale": {"type": "string", "description": "Why this candidate answers the rejection, in the owner's words."},
                "candidate": {
                    "type": "object",
                    "description": (
                        "The complete replacement candidate: kind and its full payload. The target workflow, its "
                        "fingerprint and the rejection evidence are bound by the server, never supplied here."
                    ),
                },
            },
            "required": ["order_id", "title", "rationale", "candidate"],
        }
    if tool.name == "master_create_improvement_proposal":
        return {
            "type": "object",
            "properties": {
                "proposal": {
                    "type": "object",
                    "description": (
                        "The complete governed proposal: title, rationale, target, evidence and candidate. "
                        "target binds kind/id/current_fingerprint; evidence entries bind kind/id/sha256; "
                        "candidate binds kind and its complete payload. Exact schema validation is fail-closed. "
                        "Or, when the owner only says in one sentence what to improve on a published flow, a "
                        "library skill or a registered connector: {\"target\": {\"kind\": \"process\"|\"skill\"|"
                        "\"connector\", \"id\": <id>}, \"request\": <the owner's sentence>} -- the server drafts "
                        "and materializes the inert candidate itself."
                    ),
                },
            },
            "required": ["proposal"],
        }
    if tool.name == "master_create_order":
        # policy (M1): a draft of one work order. No domains -- an order works on its flow's own browser
        # steps; no mode or free description -- the flow decides how it runs and its start fields carry the request.
        return {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "A flow from master_list_order_flows (order_flows). Never invented."},
                "title": {"type": "string", "description": "A short name for the order, said back to the owner."},
                "fields": {
                    "type": "object",
                    "description": (
                        "The flow's start fields, keyed by the start_fields ids master_list_order_flows gave, filled only "
                        "from what the owner said. An unknown id or a missing required one is refused; ask instead of guessing."
                    ),
                },
            },
            "required": ["workflow_id"],
        }
    if tool.name == "master_interrupt_turn":
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The order whose running turn should stop. The order stays open.",
                },
            },
            "required": ["task_id"],
        }
    if tool.name == "master_steer_turn":
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The order whose live turn should receive the correction.",
                },
                "text": {
                    "type": "string",
                    "description": "The owner's exact correction or new constraint; never invent one.",
                },
            },
            "required": ["task_id", "text"],
        }
    if tool.name == "master_delegate_browser":
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The open order (from master_list_orders) that will work in a browser."},
                "domains": {"type": "array", "items": {"type": "string"},
                            "description": "Hostnames the owner named, e.g. shop.example. Only these and their subdomains may be opened."},
                "vision": {"type": "boolean", "description": "Whether the agent may take masked screenshots. Off unless the owner allowed it."},
                "downloads": {"type": "boolean", "description": "Whether the agent may save downloads. Off unless the owner allowed it."},
                "lease_minutes": {"type": "integer", "description": "How long the agent holds control before the owner must renew (1-480, default 60)."},
                "visible": {"type": "boolean", "description": "Open the browser on the screen so the owner can log in or take over on it. Off unless the owner asked."},
            },
            "required": ["task_id", "domains"],
        }
    if tool.name == "master_reply_checkpoint":
        return {
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "description": "From the pending list; never invented."},
                "decision": {"type": "string", "description": "approve or deny."},
                "note": {"type": "string", "description": "Why, in the owner's words."},
            },
            "required": ["checkpoint_id", "decision"],
        }
    if tool.name == "master_stop_order":
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "The order to stop."}},
            "required": ["task_id"],
        }
    # 总控施工模式 (policy, B1).
    if tool.name == "master_start_build":
        return {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the owner wants built or changed, in the owner's own words."},
                "line_id": {"type": "string", "description": "The line to change (line_id from master_list_builds); leave empty for a new line."},
                "name": {"type": "string", "description": "Optional: a short name for the draft."},
                "team": {"type": "string", "description": "Optional, new line only: the saved team that runs it, if the owner named one."},
            },
            "required": ["goal"],
        }
    if tool.name == "master_continue_build":
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "From master_list_builds; never invented."},
                "instruction": {"type": "string", "description": "The owner's next instruction, in the owner's words."},
            },
            "required": ["job_id", "instruction"],
        }
    return {"type": "object", "properties": {}}


def build_tool_registry(actions: MasterActions, *, include_writes: bool = True) -> Any:
    """A registry holding exactly the master tools and nothing else.

    Built per conversation rather than shared, so a change to what one
    conversation may reach cannot leak into another. Every tool routes back
    through ``actions``; nothing here touches a domain service, which is what
    keeps one command on one path.
    """
    from ampuly.layer4_tools.registry import ToolDefinition, ToolRegistry

    registry = ToolRegistry()
    for tool in MASTER_TOOLS:
        if tool.writes and not include_writes:
            continue

        def _make(bound: MasterTool):
            async def _call(task: Any | None = None, **arguments: Any) -> Any:
                import asyncio

                async def notify(state: str) -> None:
                    observer = getattr(actions, "notify_tool_state", None)
                    if callable(observer):
                        try:
                            await asyncio.wait_for(observer(bound.name, state), timeout=0.5)
                        except Exception:
                            # Display transport failure must never replay a business action.
                            pass

                await notify("started")
                try:
                    result = await (actions.write(bound.name, dict(arguments)) if bound.writes
                                    else actions.read(bound.name, dict(arguments)))
                except asyncio.CancelledError:
                    await notify("outcome_unknown" if bound.writes else "cancelled")
                    raise
                except Exception:
                    await notify("outcome_unknown" if bound.writes else "failed")
                    raise
                refused = isinstance(result, dict) and (result.get("ok") is False or "refused" in result or "error" in result)
                await notify("refused" if refused else "finished")
                return result

            return _call

        registry.register(ToolDefinition(
            name=tool.name,
            description=tool.summary,
            parameters=_parameters_for(tool),
            func=_make(tool),
            category="business",
            # A write here is already gated by the handler it routes to, which
            # is the same gate the console button passes. Asking again in the
            # runtime would be a second, differently-worded prompt for one
            # decision the owner has already been asked about.
            requires_confirmation=False,
            read_only=not tool.writes,
            # Declared, not defaulted. An undeclared tool reaches the policy as
            # "unknown", which asks the operator a question that names nothing
            # and then runs the call with no confinement at all.
            permission_effects=list(tool.effects),
            company_effect_kind="runtime_internal",
        ))
    return registry


def master_task(
    *,
    project_id: str,
    console_session_id: str,
    workspace_root: str,
    customer_id: str = "",
    environment_id: str = "",
) -> Task:
    """The one task a master conversation runs on, across every turn.

    ``session_id`` is the console conversation's own id, and ``runtime_resume``
    names it as the runtime session every turn reports under. Neither restores
    anything by itself: the runtime restores its transcript only through a
    memory manager, which this loop is deliberately not given (see
    :func:`conversation_so_far`). What turn N+1 knows of turn N is the recap
    :func:`build_agent` is handed (policy, B0).
    """
    if not console_session_id:
        raise ValueError("a master task without a session id would persist nothing and restore nothing")
    if not workspace_root:
        raise ValueError("a master task without a workspace falls back to the server's own directory")
    return Task(
        id=f"master-{console_session_id}",
        title="总控",
        description="",
        project_id=project_id or "default",
        session_id=console_session_id,
        metadata={
            "workspace_root": str(workspace_root),
            "customer_id": customer_id,
            "environment_id": environment_id,
            "interface": "master_control",
            "execution_source": "direct_task",
        },
        context_snapshot={
            "runtime_resume": {"runtime_session_id": console_session_id},
        },
    )


async def customer_for(business: Any, project_id: str) -> str:
    """Resolve authority from the server's business store, never model input.

    Standalone internal callers without a business layer remain unbound. A
    configured layer that cannot be read is not equivalent to no customer.
    """
    if business is None:
        return ""
    try:
        customer = await asyncio.to_thread(business.project_customer, project_id)
    except Exception as exc:
        raise MasterSafetyStop("Master customer binding unavailable; model call not started") from exc
    return str(customer or "").strip()


async def remember_unfinished_master_goal(store: Any, task: Any, *, goal: str, reason: str) -> bool:
    """Keep the original ask on the task. This does not start another model call."""
    if store is None or task is None or not callable(getattr(store, "save_task", None)):
        return False
    recovery = {
        "schema": 1,
        "state": "recoverable",
        "goal": str(goal or "")[:4000],
        "reason": str(reason or "")[:500],
        "consult_started": False,
    }
    metadata = dict(getattr(task, "metadata", {}) or {})
    metadata["master_turn_recovery"] = recovery
    task.metadata = metadata
    try:
        await store.save_task(task)
        getter = getattr(store, "get_task", None)
        saved = await getter(task.id) if callable(getter) else task
    except Exception:
        return False
    recorded = dict(getattr(saved, "metadata", {}) or {}).get("master_turn_recovery")
    return (
        type(recorded) is dict
        and recorded.get("consult_started") is False
        and recorded.get("goal") == recovery["goal"]
        and recorded.get("state") == "recoverable"
    )


async def ensure_master_task_row(store: Any, task: Task) -> bool:
    """Make sure the conversation's task exists as a row, once.

    Not bookkeeping. ``get_cost_by_task`` joins ``cost_records`` to ``tasks``
    and filters on the project, so a cost row keyed to a task that was never
    written is silently dropped from the project total -- the exact total the
    owner asks this conversation for. Recording the spend and never writing
    the task would look fixed and change nothing.

    Returns whether the row exists in the same project and conversation.
    Callers must check False before starting a paid turn.
    """
    if store is None:
        return False
    try:
        existing = await store.get_task(task.id)
        if existing is None:
            await store.save_task(task)
        elif (existing.project_id != task.project_id
              or existing.session_id != task.session_id):
            return False
        elif any(str((existing.metadata or {}).get(key, "") or "")
                 != str((task.metadata or {}).get(key, "") or "")
                 for key in ("customer_id", "environment_id")):
            return False
        return True
    except Exception:
        return False


def instruct(task: Task, message: str) -> Task:
    """Put this turn's message where the executor will read it.

    The provider stamps its session onto an adopted task and changes nothing
    else -- it does not set the description from the instruction. A turn that
    skipped this would run with the previous turn's message, or with none.
    """
    task.description = str(message or "")
    return task


# The runtime shows at most six inbox messages per notice and drops the rest, so
# no more are taken per step; the others wait in the queue for the next step.
_STEERS_PER_STEP = 6


def conversation_so_far(history: Any, message: str = "") -> str:
    """What this conversation said before this message, bounded (policy, B0).

    The console's stored rows (``master_history_rows``), recapped by the
    managed path's own ``conversation_recap`` -- not the runtime's transcript.
    Given a memory manager the runtime would restore its transcript, but it
    would also start a paid rolling-summary call every few messages
    (``background_session_memory``, on by default), write that call's cost and
    the summary onto the conversation's own record, and restore every earlier
    tool call and result with no bound but the model's context window.

    The owner's message is stored before its turn runs, so the newest row is
    normally this very message: it is left out rather than shown twice (a row
    that is only the start of this message says nothing this message does
    not). Of the rest the NEWEST rows are kept whole, as many as fit the
    recap's own bound (``RECAP_TURNS`` rows, ``RECAP_CHARS`` characters): the
    recap fills from the oldest row it is handed, and handed everything it
    would cut the previous answer first.
    """
    import sys

    from ampuly.plugins.office_ui.master_managed_entry import RECAP_TURNS, conversation_recap

    rows = [
        row for row in list(history or [])
        if isinstance(row, dict) and str(row.get("role", "") or "") in {"user", "assistant"}
        and str(row.get("content", "") or "").strip()
    ]
    if rows and rows[-1]["role"] == "user" and str(message or "").startswith(str(rows[-1]["content"])):
        rows = rows[:-1]
    if not rows:
        return ""
    kept = 1
    for count in range(2, min(len(rows), RECAP_TURNS) + 1):
        tail = rows[-count:]
        if conversation_recap(tail, turns=count) != conversation_recap(tail, turns=count, chars=sys.maxsize):
            break
        kept = count
    return conversation_recap(rows[-kept:], turns=kept)


async def owner_steers(task: Any) -> list[dict[str, Any]]:
    """The owner's 插话 on this turn, handed to the runtime at its next step (policy, B0).

    NativeHarnessProvider gives a root turn a bounded queue on the task
    (``_runtime_inbox_queue``) and its ``steer`` puts the owner's words there.
    The runtime reads an inbox only through the provider it is passed, and the
    master turn passed none: a steer was acknowledged and never reached the
    model. Nothing is taken when the turn has no queue.
    """
    queue = getattr(task, "_runtime_inbox_queue", None)
    steers: list[dict[str, Any]] = []
    while queue is not None and len(steers) < _STEERS_PER_STEP:
        try:
            entry = queue.get_nowait()
        except Exception:
            break
        body = str((entry.get("body", "") if isinstance(entry, dict) else entry) or "").strip()
        if body:
            steers.append({"from": "owner", "subject": "the owner's correction to this turn", "body": body})
    return steers


@dataclass
class MasterRuntimeConfig:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    include_writes: bool = True


def build_agent(
    llm: Any,
    registry: Any,
    system_prompt: str,
    config: MasterRuntimeConfig | None = None,
    *,
    cost_tracker: Any = None,
    event_store: Any = None,
    conversation: str = "",
) -> Any:
    """The four-line shim the Harness drives.

    Everything that makes a turn safe -- the claim, the lease, the interrupt,
    the settle -- belongs to the provider above this. All this does is run the
    loop the product already has, with the tools this conversation may reach.

    ``conversation`` is :func:`conversation_so_far` for this turn. It travels
    as its own system message after the prompt, not inside it, so the prompt
    and the tool list stay byte-identical from turn to turn and a cached
    prefix is not thrown away because the owner said something new.
    """
    from ampuly.core.config import AmpulyConfig
    from ampuly.layer3_agent.runtime_v2.runtime import NativeRuntimeV2

    settings = config or MasterRuntimeConfig()
    allowed = allowed_tool_names(include_writes=settings.include_writes)
    recap = str(conversation or "").strip()
    context_messages = [{"role": "system", "content": recap}] if recap else []

    runtime_config = AmpulyConfig()
    # The runtime's verification contract is for turns that edit code: it appends an English status line
    # ("Verification: not required ...") to the final text. A master reply is read by the owner and edits no code;
    # the line leaked into 总控 once a write tool with a workspace effect ran (master_start_build, live check).
    runtime_config.system.native_runtime.verification_contract.enabled = False
    runtime = NativeRuntimeV2(
        llm=llm,
        tool_registry=registry,
        config=runtime_config,
        max_iterations=settings.max_iterations,
        # Without this the runtime's only record_cost call is skipped and the
        # whole conversation spends invisibly -- while master control answers
        # "what has this cost" from the very table it is missing from.
        cost_tracker=cost_tracker,
        # Observe requests without opting this conversation into memory.
        event_store=event_store,
    )

    class _MasterAgent:
        async def execute(self, task: Task, on_progress: Any = None) -> Any:
            from ampuly.plugins.office_ui.master_identity import conversation_identity, identity_prompt

            identity = conversation_identity(llm, executor="native_runtime_v2")
            return await runtime.run(
                system_prompt + identity_prompt(identity),
                task.description,
                context_messages=[dict(message) for message in context_messages] or None,
                task=task,
                # Explicit, always; never rely on an absent restriction.
                allowed_tools=list(allowed),
                on_progress=on_progress,
                # The owner's 插话 on this turn, read at every step.
                inbox_interrupt_provider=owner_steers,
            )

    return _MasterAgent()


def workspace_for(
    engine: Any, *, project_id: str = "default", customer_id: str = "",
    environment_id: str = "",
) -> str:
    """Name a dedicated scope directory, never the configuration/database home.

    This does not create directories or grant file tools. Hashing the complete
    scope prevents path traversal and ambiguous sanitized-name collisions.
    """
    home = getattr(engine, "ampuly_home", None)
    if not home or not Path(home).is_absolute():
        raise ValueError("master workspace requires an explicit absolute ampuly_home")
    root = Path(home).resolve()
    identity = json.dumps(
        [str(project_id or "default"), str(customer_id), str(environment_id)],
        ensure_ascii=True, separators=(",", ":"),
    )
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    base = root / "master_workspaces"
    workspace = base / key
    for path in (base, workspace):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("master workspace must not be a redirected directory")
    if not workspace.resolve().is_relative_to(root):
        raise ValueError("master workspace escapes ampuly_home")
    return str(workspace)


def summarize_calls(result: Any) -> list[dict[str, Any]]:
    """The tool calls a turn made, as the console records them.

    Arguments are carried because a receipt that says only "called
    master_create_order" cannot be checked against what was actually drafted.
    """
    calls: list[dict[str, Any]] = []
    for entry in list(getattr(result, "tool_calls", None) or []):
        if isinstance(entry, dict):
            name = str(entry.get("name", "") or entry.get("tool", "") or "")
            arguments = entry.get("arguments")
        else:
            name = str(getattr(entry, "name", "") or "")
            arguments = getattr(entry, "arguments", None)
        if not name:
            continue
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {"raw": arguments[:400]}
        calls.append({"name": name, "arguments": arguments if isinstance(arguments, dict) else {}})
    return calls
