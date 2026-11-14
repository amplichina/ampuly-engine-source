"""Native Runtime V2: streaming LLM loop + structured tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from contextlib import aclosing, nullcontext
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from ampuly.core.company_controller import CompanyRunControllerLeaseLost
from ampuly.core.config import AmpulyConfig
from ampuly.core.events import EventBus
from ampuly.core.models import AmpulyEvent, PermissionResolution, Task, TaskResult, TaskStatus, VerificationEvidence
from ampuly.layer0_interaction.coordinator import InteractionCoordinator
from ampuly.layer2_organization.collaboration_policy import ownership_guard_violation
from ampuly.layer2_organization.company_runtime_identity import is_company_runtime_task
from ampuly.layer2_organization.work_item_identity import (
    result_delivery_identity_payload_for_task,
    turn_type_for_task,
    work_item_identity_payload_for_task,
)
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
from ampuly.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from ampuly.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from ampuly.layer3_agent.runtime_v2.subagents import ChildAgentFactory, SubagentManager
from ampuly.layer3_agent.runtime_v2.tool_hooks import (
    RuntimeCompanyControllerToolFence,
    RuntimeToolHookBus,
    RuntimeToolHookContext,
)
from ampuly.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from ampuly.layer3_agent.prompt_harness import (
    render_runtime_artifact_messages,
    strip_runtime_artifact_messages,
)
from ampuly.layer3_agent.prompt_harness.artifacts import build_runtime_artifact_record
from ampuly.layer3_agent.prompt_harness.types import RuntimeArtifact
from ampuly.layer4_tools.execution_context import ensure_task_execution_context
from ampuly.layer4_tools.opaque_execution import (
    OpaqueExecutionEnvelopeError,
    build_company_opaque_execution_plan,
    company_opaque_execution_plan_for_permit,
    company_opaque_execution_identity,
    exact_tool_call_fingerprint,
    opaque_execution_envelope_digest,
)
from ampuly.layer4_tools.output_budget import clip_text
from ampuly.layer4_tools.registry import ToolRegistry
from ampuly.layer3_agent.step_snapshot_projection import (
    prepare_tool_offer, snapshot_main_request, persist_step_snapshot,
)
from ampuly.llm.provider import current_model_plan
from ampuly.layer6_observability.cost_tracker import CostEntry
from ampuly.layer6_observability.metering import meter_for_task
from ampuly.llm.provider import LLMProvider, ProviderQuotaExhaustedError, ProviderRejected, received_rejection
from ampuly.llm.budget import BUDGET_STOP_ARTIFACT, ModelBudgetStop, RouteRefused, model_budget_scope, model_retry_scope, stop_in_tool_results


ApprovalCallback = Callable[..., Awaitable[tuple[bool, Any]]]
PrefetchProvider = Callable[[Task, str, list[dict[str, Any]]], Awaitable[dict[str, str]]]

# AE04a: the model calls a work item has made over all of its turns -- one Task, however many turns ran on it (a
# permission released and resumed, a rework, a retry) -- kept on the task so the engine can give the next turn only
# what the role has left. Missing means 0: a task from before this key behaves as it did. Written once per turn, when
# the turn ends, whichever way it ends. A subagent runs on its own task and counts there, never here (decision D18).
NATIVE_STEPS_USED_KEY = "native_steps_used"


def native_steps_used(task: Task | None) -> int:
    """The steps a task's turns have used so far (``NATIVE_STEPS_USED_KEY``); 0 when missing or unreadable."""
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    try:
        return max(0, int(metadata.get(NATIVE_STEPS_USED_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class _RuntimePrefetchHandle:
    task: asyncio.Task[dict[str, str]]
    query: str
    consumed: bool = False


class _TurnEarlyTools:
    """The turn's task group for tools started before the model stream ends."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[Any]] = []

    def create_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def cancel_join(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _subagent_model_check(llm: Any) -> Callable[[str], str]:
    """Refuse a subagent model the catalogue does not list, before anything is set up for it (routing phase 0).

    Model routing phase 1 adds the second half: a sub-agent of an order runs inside that order's plan, so a model
    the plan's tiers do not hold is refused too. A sub-agent's model is text the model itself wrote, which is
    exactly why it is checked before a worktree, a state file or an event exists.

    With an empty tier table this is the catalogue's check alone, exactly as it was.
    """

    def check(model: str) -> str:
        chosen = str(model or "").strip()
        default = str(getattr(getattr(llm, "config", None), "default_model", "") or "")
        if not chosen or chosen == default:
            return ""
        listed = getattr(llm, "model_listed", None)
        if not (callable(listed) and listed(chosen) is True):
            return "subagent_model_not_listed"
        from ampuly.llm.provider import current_model_plan

        plan = current_model_plan() or {}
        tiers = dict(plan.get("tiers") or {})
        if tiers and chosen not in set(tiers.values()):
            return "subagent_outside_plan"
        return ""

    return check


class NativeRuntimeV2:
    """Ampuly native runtime with structured events and tool batches."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        event_bus: EventBus | None = None,
        cost_tracker: Any | None = None,
        memory_manager: Any | None = None,
        event_store: Any | None = None,
        history_compactor: Any | None = None,
        max_iterations: int = 50,
        compression_threshold: float = 0.85,
        config: AmpulyConfig | None = None,
        child_agent_factory: ChildAgentFactory | None = None,
        approval_callback: ApprovalCallback | None = None,
        permission_policy: Any | None = None,
        prefetch_provider: PrefetchProvider | None = None,
        interaction_coordinator: InteractionCoordinator | None = None,
        external_outcome_unknown: Any | None = None,
    ) -> None:
        self.llm = llm
        # A12: called with (checkpoint, execution) when a tool that writes
        # outside the product was interrupted while executing and its
        # result never came back. The engine binds it to the same budget
        # hold the effect gateway uses; the runtime only reports.
        self.external_outcome_unknown = external_outcome_unknown
        self.tools = tool_registry
        self.event_bus = event_bus
        self.cost_tracker = cost_tracker
        self.memory_manager = memory_manager
        # Snapshot persistence is independent from transcript/memory opt-in.
        # Keep None unresolved so an existing memory manager's Store rebind
        # remains visible. An explicit Store is never silently replaced.
        self._event_store = event_store
        self.history_compactor = history_compactor
        self.max_iterations = max_iterations
        self.compression_threshold = compression_threshold
        self.config = config or AmpulyConfig()
        self.child_agent_factory = child_agent_factory
        self.approval_callback = approval_callback
        # The single permission policy (ApprovalEngine). Its sync predict()
        # gates every tool call; ASK routes into approval_callback.
        self.permission_policy = permission_policy
        self.prefetch_provider = prefetch_provider
        self.interaction_coordinator = interaction_coordinator or getattr(
            permission_policy, "interaction_coordinator", None
        )
        self._pre_tool_hooks: list[tuple[str, Any]] = []
        self._post_tool_hooks: list[tuple[str, Any]] = []
        self._failure_tool_hooks: list[tuple[str, Any]] = []
        # AE04a: model calls made by the turn running on this runtime (its subagents cap their own step budgets by
        # what is left), and the subagent managers still alive here -- one per turn -- which are cancelled when the
        # turn ends, however it ends.
        self._iterations_used = 0
        self._live_subagent_managers: list[SubagentManager] = []
        self._turn_early_tools = _TurnEarlyTools()

    @property
    def iterations_used(self) -> int:
        """Model calls the turn on this runtime has made so far (read-only; 0 before its loop starts)."""
        return self._iterations_used

    @property
    def remaining_iterations(self) -> int:
        """Steps the turn on this runtime still has: ``max_iterations`` less the model calls made, never below 0."""
        return max(0, int(self.max_iterations) - self._iterations_used)

    def _step_snapshot_event_store(self) -> Any:
        """Resolve the existing snapshot writer without enabling memory.

        The explicit writer serves StepSnapshot only. Legacy event/session,
        transcript, compaction and resume paths retain their current wiring.
        Validation/fail-closed behavior belongs to persist_step_snapshot.
        """
        if self._event_store is not None:
            return self._event_store
        return getattr(self.memory_manager, "store", None)

    def register_pre_tool_hook(self, name: str, hook: Any) -> None:
        self._pre_tool_hooks.append((name, hook))

    def register_post_tool_hook(self, name: str, hook: Any) -> None:
        self._post_tool_hooks.append((name, hook))

    def register_failure_tool_hook(self, name: str, hook: Any) -> None:
        self._failure_tool_hooks.append((name, hook))

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict[str, Any]] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        task: Task | None = None,
        allowed_tools: list[str] | None = None,
        on_progress: Any = None,
        inbox_interrupt_provider: Any = None,
    ) -> TaskResult:
        """Run one native turn with a durable permission failure boundary.

        The turn declares whose account its model calls spend. The task
        already carries the customer; without this the provider can only
        use the key the process started with, which is the wrong account
        the moment two customers are in flight together.
        """
        from ampuly.llm.provider import model_credential_scope
        from ampuly.llm.task_model_scope import plan_scope_for

        metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        with model_credential_scope(
            str(metadata.get("customer_id", "") or ""),
            str(metadata.get("environment_id", "") or ""),
        ), plan_scope_for(task), model_budget_scope(task):
            return await self._run_with_cleanup(
                system_prompt,
                user_message,
                context_messages=context_messages,
                attachment_refs=attachment_refs,
                task=task,
                allowed_tools=allowed_tools,
                on_progress=on_progress,
                inbox_interrupt_provider=inbox_interrupt_provider,
            )

    async def _run_with_cleanup(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict[str, Any]] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        task: Task | None = None,
        allowed_tools: list[str] | None = None,
        on_progress: Any = None,
        inbox_interrupt_provider: Any = None,
    ) -> TaskResult:
        # Reset here, not only at the top of the loop: a turn that fails before its loop starts must not add a
        # previous turn's count to the task (a runtime object is reused by tests, and by nothing else).
        self._iterations_used = 0
        try:
            return await self._run_impl(
                system_prompt,
                user_message,
                context_messages=context_messages,
                attachment_refs=attachment_refs,
                task=task,
                allowed_tools=allowed_tools,
                on_progress=on_progress,
                inbox_interrupt_provider=inbox_interrupt_provider,
            )
        except BaseException as exc:
            if task is not None:
                cleanup = asyncio.create_task(
                    self._settle_aborted_runtime(
                        task=task,
                        error=exc,
                    )
                )
                # Runtime owners are cancelled during ordered Engine
                # shutdown.  Keep the Store-backed cleanup joined even if a
                # second cancel arrives; shutdown must be able to prove there
                # is no live runtime projection before it closes the Store.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
            raise
        finally:
            # AE04a: the steps this turn walked join the task's running total -- once, here, on every way out (a
            # return, an escaping error, a cancellation) -- and no background or resident subagent of it keeps
            # running. The returns inside _run_impl already cancelled theirs; this catches the exits with no return.
            # Early read-only tools are in the same group: a cancel must be able to finish the join.
            self._record_native_steps_used(task)
            join = asyncio.create_task(self._join_early_tools())
            while not join.done():
                try:
                    await asyncio.shield(join)
                except asyncio.CancelledError:
                    continue
            await self._cancel_live_subagents(task)

    def _record_native_steps_used(self, task: Task | None) -> None:
        """Add this turn's model calls to the task's total (``NATIVE_STEPS_USED_KEY``); in place, so every holder of
        the task's metadata sees it, and the engine's one write of the turn's result persists it."""
        if task is None or self._iterations_used <= 0:
            return
        if task.metadata is None:
            task.metadata = {}
        task.metadata[NATIVE_STEPS_USED_KEY] = native_steps_used(task) + int(self._iterations_used)

    async def _cancel_live_subagents(self, task: Task | None) -> None:
        """Cancel the subagents of every manager this runtime still holds for ``task``, then let the managers go."""
        managers = [manager for manager in self._live_subagent_managers if manager.parent_task is task]
        for manager in managers:
            try:
                await manager.cancel_all()
            finally:
                if manager in self._live_subagent_managers:
                    self._live_subagent_managers.remove(manager)

    async def _run_impl(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict[str, Any]] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        task: Task | None = None,
        allowed_tools: list[str] | None = None,
        on_progress: Any = None,
        inbox_interrupt_provider: Any = None,
    ) -> TaskResult:
        if task is not None:
            ensure_task_execution_context(task, self.config)
            await self._refresh_durable_tool_permits(task)
            self._assert_durable_tool_permits_resumable(task)
        runtime_session_id = self._runtime_session_id(task)
        conversation_turn_id = self._conversation_turn_id(task, runtime_session_id)
        continue_after_tool_result = bool(
            self._runtime_resume_payload(task).get("continue_after_tool_result")
        )
        attempt_user_seed_required = bool(
            task is not None
            and (task.metadata or {}).get(
                "_runtime_v2_attempt_user_seed_required", False
            )
        )
        attempt_user_seed_receipt: dict[str, Any] = {}
        if (
            attempt_user_seed_required
            and task is not None
            and not self._approved_resume_tool_call(task)
            and not continue_after_tool_result
        ):
            attempt_user_seed_receipt = await self._ensure_attempt_user_turn_seed(
                task,
                user_message,
                runtime_session_id=runtime_session_id,
                conversation_turn_id=conversation_turn_id,
            )
            user_message = str(
                attempt_user_seed_receipt.get("content", "") or user_message
            )
        user_content = self.llm.prepare_user_message_content(
            user_message,
            attachment_refs=attachment_refs,
        )
        # One WARNING per turn when tool-call history sent to the model had to
        # be paired (restored transcript or any later model call).
        tool_pairing_log: dict[str, Any] = {}
        messages, base_prefix_len = await self._bootstrap_messages(
            system_prompt=system_prompt,
            user_content=user_content,
            user_message=user_message,
            context_messages=context_messages,
            task=task,
            suppress_resume_user_append=(
                attempt_user_seed_required or bool(attempt_user_seed_receipt)
            ),
            tool_pairing_log=tool_pairing_log,
        )
        if continue_after_tool_result and task is not None:
            store = getattr(self.memory_manager, "store", None)
            clear_marker = getattr(
                store, "set_task_runtime_continuation_marker", None
            )
            if not callable(clear_marker):
                raise RuntimeError(
                    "durable runtime continuation marker store is unavailable"
                )
            persisted = await clear_marker(
                task.id,
                runtime_session_id=runtime_session_id,
                enabled=False,
            )
            task.context_snapshot = dict(persisted.context_snapshot or {})
            task.metadata = dict(persisted.metadata or {})
        tool_offer = prepare_tool_offer(
            self.tools,
            self.llm.get_tool_definitions(self.tools.get_schemas(allowed=allowed_tools)),
        )
        tool_schemas = tool_offer.wire_tools()
        planner = ToolPlanner(
            self.tools,
            max_parallel_read_tools=self.config.system.native_runtime.max_parallel_read_tools,
        )
        permission_resolver = RuntimePermissionAdapter(
            self.permission_policy,
            guardian=self.config.autonomy.permissions_v2.guardian,
        )
        todo_state: list[dict[str, Any]] = self._restore_task_ledger(task)
        runtime_status: dict[str, Any] = {
            "current_tool": None,
            "queue_depth": 0,
            "drain_mode": "idle",
            "tool_elapsed_ms": 0,
            "last_tool_summary": "",
            "context_tokens": 0,
            "context_window": self._context_window_limit(),
            "context_remaining_pct": 100,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "turn_cost_usd": 0.0,
            "session_cost_usd": 0.0,
            "pending_permission_count": 0,
        }
        runtime_notes: dict[str, Any] = {
            "observed_risky_tools": [],
            "mutating_tools": [],
            "verification": {},
            "prefetch_hits": [],
            "task_ledger": list(todo_state),
            "artifact_manifest": list(self._boot_artifact_manifest(task)),
            "artifact_hashes": dict(self._boot_artifact_hashes(task)),
        }
        pending_prefetch: _RuntimePrefetchHandle | None = None
        self._iterations_used = 0
        subagents = SubagentManager(
            parent_task=task,
            config=self.config,
            child_agent_factory=self.child_agent_factory,
            event_bus=self.event_bus,
            store=getattr(self.memory_manager, "store", None),
            memory_manager=self.memory_manager,
            runtime_session_id=runtime_session_id,
            model_check=_subagent_model_check(self.llm),
            parent_remaining_iterations=lambda: self.remaining_iterations,
        )
        # Held on the runtime so the exit paths without a return (an escaping error, a cancelled turn) can still
        # cancel its subagents; _run_with_cleanup lets go of it when the turn is over.
        self._live_subagent_managers.append(subagents)
        hook_bus = self._build_tool_hook_bus(
            runtime_session_id=runtime_session_id,
            task=task,
            permission_resolver=permission_resolver,
            on_progress=on_progress,
        )

        async def _emit_executor_event(event_type: str, payload: dict[str, Any]) -> None:
            payload = dict(payload or {})
            execution_turn_id = self._runtime_iteration_turn_id(conversation_turn_id, iteration)
            payload.setdefault("iteration", iteration + 1)
            payload.setdefault("turn_id", execution_turn_id)
            payload.setdefault("canonical_turn_id", conversation_turn_id)
            payload.setdefault("conversation_turn_id", conversation_turn_id)
            payload.setdefault("execution_turn_id", execution_turn_id)
            tool_call_id = str(payload.get("tool_call_id", "") or "").strip()
            if tool_call_id:
                prefix = "permission" if event_type.startswith("permission_") else "tool"
                payload.setdefault("item_id", f"{execution_turn_id}:{prefix}:{tool_call_id}")
                payload.setdefault("stream_id", f"{execution_turn_id}:{prefix}:{tool_call_id}")
            if event_type.startswith("permission_"):
                group_key = self._permission_group_key(
                    str(payload.get("tool_name", "") or ""),
                    dict(payload.get("arguments", {}) or {}),
                )
                if group_key:
                    payload.setdefault("permission_group_key", group_key)
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                event_type,
                payload,
            )
            if event_type == "tool_started":
                runtime_status["current_tool"] = str(payload.get("tool_name", "") or "") or None
                runtime_status["queue_depth"] = 1
                runtime_status["drain_mode"] = "smooth"
                runtime_status["tool_elapsed_ms"] = 0
            elif event_type == "tool_progress":
                runtime_status["current_tool"] = str(payload.get("tool_name", "") or runtime_status.get("current_tool") or "") or None
                runtime_status["queue_depth"] = 1 if runtime_status.get("current_tool") else 0
                runtime_status["drain_mode"] = "smooth" if runtime_status.get("current_tool") else "idle"
                runtime_status["tool_elapsed_ms"] = int(payload.get("elapsed_ms", 0) or 0)
                runtime_status["last_tool_summary"] = str(
                    payload.get("message", "") or payload.get("text", "") or runtime_status.get("last_tool_summary", "")
                ).strip()
            elif event_type == "tool_completed":
                runtime_status["current_tool"] = None
                runtime_status["queue_depth"] = 0
                runtime_status["drain_mode"] = "idle"
                runtime_status["tool_elapsed_ms"] = int(payload.get("elapsed_ms", 0) or 0)
                runtime_status["last_tool_summary"] = str(payload.get("result_summary", "") or "").strip()
            elif event_type == "tool_skipped":
                runtime_status["queue_depth"] = 0
                runtime_status["drain_mode"] = "idle"
                runtime_status["last_tool_summary"] = str(payload.get("reason", "") or "").strip()
            elif event_type == "permission_requested":
                runtime_status["pending_permission_count"] = int(runtime_status.get("pending_permission_count", 0) or 0) + 1
            elif event_type == "permission_resolved":
                runtime_status["pending_permission_count"] = max(
                    0,
                    int(runtime_status.get("pending_permission_count", 0) or 0) - 1,
                )
            if event_type in {
                "tool_started",
                "tool_progress",
                "tool_completed",
                "tool_skipped",
                "permission_requested",
                "permission_resolved",
            }:
                await self._emit_status_snapshot(runtime_session_id, task, runtime_status)

        executor = StreamingToolExecutor(
            registry=self.tools,
            planner=planner,
            permission_resolver=permission_resolver,
            hook_bus=hook_bus,
            controller_tool_fence=RuntimeCompanyControllerToolFence(
                store=self._controller_tool_fence_store()
            ),
            runtime_tool_handler=lambda tool_name, arguments: self._handle_runtime_tool(
                subagents=subagents,
                tool_name=tool_name,
                arguments=arguments,
                task=task,
                todo_state=todo_state,
            ),
            emit_event=_emit_executor_event,
            max_parallel_read_tools=self.config.system.native_runtime.max_parallel_read_tools,
            converge_on_parallel_failure=self.config.system.native_runtime.converge_on_parallel_failure,
            # The exact names in the immutable Runtime-to-provider offer.
            # For allowed=None this intentionally NARROWS the previous
            # wildcard gate to the names actually offered. [] stays zero.
            # This is not a permission grant or an implementation lease.
            offered_tools=tool_offer.names,
        )

        total_cost = 0.0
        total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        last_observed_prompt_tokens = 0
        aggregated_artifacts: dict[str, Any] = {}
        overflow_retries = 0
        max_overflow_retries = max(
            1,
            int(self.config.system.native_runtime.reactive_compaction.max_overflow_retries or 1),
        ) if self.config.system.native_runtime.reactive_compaction.enabled else 1
        # Unclassified provider failures (content filters, transient rejects)
        # get bounded retries with the provider's error text fed back into the
        # conversation so the model can adapt; the counter resets after every
        # successful stream so long runs are not penalized for sporadic blips.
        stream_error_feedback_retries = 0
        max_stream_error_feedback_retries = 2
        stream_error_context_reset_attempted = False
        # The next call re-asks after a failed one (compacted, truncated, error fed back): under a bound budget it is
        # counted as a retry (model-routing phase 0, C9). Cleared after every stream that answered.
        reasking = False
        request_sequence = 0

        async def prepare_request_tools(
            step_number: int, retrying: bool,
        ) -> list[dict[str, Any]]:
            # One sequence and one original offer for this Runtime invocation,
            # including synchronous protocol-repair requests. Never re-read the
            # registry to widen an in-flight turn's tool offer.
            nonlocal request_sequence
            tool_offer.assert_executor_names(executor.offered_tools)
            request_sequence += 1
            snapshot = snapshot_main_request(
                task=task, runtime_session_id=runtime_session_id,
                canonical_turn_id=conversation_turn_id, iteration=step_number,
                request_sequence=request_sequence, retrying=retrying,
                model_plan=current_model_plan(), offer=tool_offer,
            )
            await persist_step_snapshot(
                self._step_snapshot_event_store(),
                runtime_session_id=runtime_session_id, snapshot=snapshot,
            )
            return tool_offer.wire_tools()

        compaction_boundaries: list[dict[str, Any]] = []

        await self._save_runtime_session(
            runtime_session_id,
            task,
            "running",
            {
                "allowed_tools": allowed_tools or [],
                "task_ledger": list(todo_state),
                "prefetch_hits": [],
                "artifact_manifest": list(runtime_notes.get("artifact_manifest", []) or []),
            },
        )
        if (
            not attempt_user_seed_required
            and not self._approved_resume_tool_call(task)
            and not continue_after_tool_result
        ):
            await self._seed_user_turn(
                task,
                user_message,
                runtime_session_id=runtime_session_id,
                conversation_turn_id=conversation_turn_id,
            )
        await self._emit_prompt_prefix_state(
            runtime_session_id=runtime_session_id,
            task=task,
            messages=messages,
            tool_schemas=tool_schemas,
            base_prefix_len=base_prefix_len,
        )
        await self._emit_status_snapshot(runtime_session_id, task, runtime_status)
        for iteration in range(self.max_iterations):
            self._iterations_used = iteration + 1
            await self._refresh_durable_tool_permits(task)
            self._assert_durable_tool_permits_resumable(task)
            approved_resume_tool_call = self._approved_resume_tool_call(
                task,
                state="ready",
            )
            if approved_resume_tool_call:
                resume_call = {
                    "id": str(approved_resume_tool_call.get("id", "") or ""),
                    "function": str(approved_resume_tool_call.get("function", "") or ""),
                    "arguments": dict(approved_resume_tool_call.get("arguments", {}) or {}),
                    "_approval_permit": dict(approved_resume_tool_call),
                }
                if bool(approved_resume_tool_call.get("approved")):
                    effect_started = await self._begin_durable_tool_permission_effect(
                        task,
                        approved_resume_tool_call,
                    )
                    if not effect_started:
                        raise RuntimeError(
                            "durable ToolCall permission lease was lost before execution"
                        )
                    # The atomic begin command persisted both the checkpoint
                    # effect fence and Task permit state. Refresh that fact;
                    # never follow it with a stale second ledger write.
                    await self._refresh_durable_tool_permits(task)
                    executing_permit = self._approved_resume_tool_call(
                        task,
                        tool_call_id=str(resume_call.get("id", "") or ""),
                        state="executing",
                    )
                    if not executing_permit:
                        raise RuntimeError(
                            "durable ToolCall permit did not enter executing state"
                        )
                    resume_call["_approval_permit"] = executing_permit
                    resumed_results = await executor.execute(
                        [resume_call],
                        task=task,
                        on_progress=on_progress,
                    )
                    if not resumed_results:
                        raise RuntimeError("approved ToolCall produced no execution result")
                    resumed_item = resumed_results[0]
                else:
                    denied_result = self._canonical_exact_tool_call_denial_result(
                        permit=approved_resume_tool_call,
                    )
                    denied_decision = permission_resolver.decision_from_result(
                        resume_call["function"],
                        resume_call["arguments"],
                        denied_result,
                    )
                    resumed_item = {
                        "tool_call": resume_call,
                        "result": denied_result,
                        "permission_decision": denied_decision,
                        "stop_batch_on_failure": False,
                        "hook_metadata": {"durable_tool_permission": True},
                    }
                self._update_runtime_notes(runtime_notes, [resumed_item])
                resumed_result = dict(resumed_item.get("result", {}) or {})
                clipped_resume_result = self._clip_tool_result_for_history(
                    resume_call["function"],
                    resumed_result,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": resume_call["id"],
                    "content": json.dumps(
                        clipped_resume_result,
                        ensure_ascii=False,
                        default=str,
                    ),
                })
                result_payload = resumed_result.get("result", {})
                if isinstance(result_payload, dict):
                    aggregated_artifacts = self._merge_artifacts(
                        aggregated_artifacts,
                        result_payload,
                    )
                permission_finished = await self._persist_tool_result(
                    task,
                    resume_call,
                    clipped_resume_result,
                    resumed_item.get("permission_decision"),
                    runtime_session_id=runtime_session_id,
                    hook_metadata={
                        **dict(resumed_item.get("hook_metadata", {}) or {}),
                        "durable_tool_permission": True,
                        "approval_checkpoint_id": str(
                            approved_resume_tool_call.get("checkpoint_id", "") or ""
                        ),
                    },
                )
                if not permission_finished:
                    await self._set_approved_resume_tool_call_state(
                        task,
                        {
                            **approved_resume_tool_call,
                            "state": "result_persisted",
                        },
                    )
                    await self._complete_durable_tool_permission(
                        task=task,
                        item=resumed_item,
                        runtime_session_id=runtime_session_id,
                    )
                await self._emit_runtime_event(
                    runtime_session_id,
                    task,
                    "approved_tool_call_resumed",
                    {
                        "tool_call_id": resume_call["id"],
                        "tool_name": resume_call["function"],
                        "approval_checkpoint_id": str(
                            approved_resume_tool_call.get("checkpoint_id", "") or ""
                        ),
                    },
                )
                await self._refresh_durable_tool_permits(task)
                self._assert_durable_tool_permits_resumable(task)
                approved_resume_tool_call = self._approved_resume_tool_call(
                    task,
                    state="ready",
                )
                if approved_resume_tool_call:
                    # A task can own several independently approved calls.  The
                    # keyed permit ledger preserves all of them; drain each
                    # exact call before asking the LLM for another command.
                    continue
            # A second controller can durably append another approved call
            # while this runner is finishing the first one.  Refresh at the
            # model boundary so the original assistant command is always
            # drained before a new LLM turn can be generated.
            await self._refresh_durable_tool_permits(task)
            self._assert_durable_tool_permits_resumable(task)
            if self._approved_resume_tool_call(task, state="ready"):
                continue
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "turn_started",
                {
                    "iteration": iteration + 1,
                    "turn_id": conversation_turn_id,
                    "canonical_turn_id": conversation_turn_id,
                    "conversation_turn_id": conversation_turn_id,
                    "execution_turn_id": self._runtime_iteration_turn_id(conversation_turn_id, iteration),
                },
            )
            await self._emit_status_snapshot(runtime_session_id, task, {**runtime_status, "drain_mode": "smooth"})

            messages, base_prefix_len, consumed_hits = await self._consume_ready_prefetch(
                pending_prefetch,
                messages=messages,
                base_prefix_len=base_prefix_len,
                runtime_session_id=runtime_session_id,
                task=task,
            )
            if consumed_hits:
                runtime_notes["prefetch_hits"] = [
                    *list(runtime_notes.get("prefetch_hits", []) or []),
                    *consumed_hits,
                ]
                await self._save_runtime_session(
                    runtime_session_id,
                    task,
                    "running",
                    self._build_runtime_state_metadata(
                        task=task,
                        messages=messages,
                        todo_state=todo_state,
                        runtime_notes=runtime_notes,
                        compaction_boundaries=compaction_boundaries,
                        active_subagents=subagents.list_agents().get("agents", []),
                    ),
                )
                pending_prefetch = None

            if pending_prefetch is None:
                pending_prefetch = self._start_runtime_prefetch(task, messages)

            if inbox_interrupt_provider is not None and task is not None:
                try:
                    inbox_messages = await inbox_interrupt_provider(task)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(f"Runtime inbox provider failed: {exc}")
                    inbox_messages = []
                if inbox_messages:
                    messages.append({
                        "role": "system",
                        "content": self._format_interrupt_notice(inbox_messages),
                    })

            messages = await self._apply_context_pipeline(
                messages,
                tool_schemas=tool_schemas,
                task=task,
                base_prefix_len=base_prefix_len,
                runtime_session_id=runtime_session_id,
                compaction_boundaries=compaction_boundaries,
                todo_state=todo_state,
                runtime_notes=runtime_notes,
                active_subagents=subagents.list_agents().get("agents", []),
                observed_tokens=last_observed_prompt_tokens,
            )
            context_usage = await self._emit_context_usage(
                runtime_session_id=runtime_session_id,
                task=task,
                messages=messages,
                tool_schemas=tool_schemas,
                phase="pre_llm",
            )
            runtime_status["context_tokens"] = int(context_usage.get("context_tokens", 0) or 0)
            runtime_status["context_window"] = int(context_usage.get("context_window", 0) or 0)
            runtime_status["context_remaining_pct"] = int(context_usage.get("context_remaining_pct", 0) or 0)
            await self._emit_status_snapshot(runtime_session_id, task, runtime_status)

            assistant_text = ""
            assistant_delta_seq = 0
            thinking_delta_seq = 0
            tool_call_chunks: dict[int, dict[str, Any]] = {}
            early_tool_runs: dict[int, dict[str, Any]] = {}
            # Only the outgoing copy is paired: ``messages`` and the stored
            # transcript keep what really happened.
            outbound_messages = self._pair_tool_calls_for_model(
                messages,
                task=task,
                warn_state=tool_pairing_log,
            )
            # Outside the model retry handler: persistence is not a vendor
            # rejection, and a failed write cannot start a model request.
            tool_schemas = await prepare_request_tools(iteration + 1, reasking)
            await self._stamp_llm_in_flight(task, True)
            try:
                from contextlib import nullcontext
                from ampuly.computer_use.desktop_frames import desktop_frame_task_scope, has_desktop_frame_task
                frame_scope = desktop_frame_task_scope(task, self.llm) if has_desktop_frame_task(task) else nullcontext()
                with model_retry_scope() if reasking else nullcontext(), frame_scope:
                    # The event protocol also carries complete replies. Keep
                    # the default call shape for existing model adapters.
                    reply_options = {} if self.config.system.native_runtime.stream_llm else {"stream": False}
                    async with aclosing(self.llm.chat_stream(outbound_messages, tools=tool_schemas, **reply_options)) as model_events:
                        async for event in model_events:
                            if event.event_type == "assistant_delta":
                                delta_text = str(event.payload.get("text", "") or "")
                                if delta_text:
                                    assistant_text += delta_text
                                    turn_id = self._runtime_iteration_turn_id(conversation_turn_id, iteration)
                                    assistant_delta_seq += 1
                                    await self._emit_runtime_event(
                                        runtime_session_id,
                                        task,
                                        "assistant_delta",
                                        {
                                            "iteration": iteration + 1,
                                            "turn_id": turn_id,
                                            "canonical_turn_id": conversation_turn_id,
                                            "conversation_turn_id": conversation_turn_id,
                                            "execution_turn_id": turn_id,
                                            "item_id": f"{turn_id}:assistant",
                                            "stream_id": f"{turn_id}:assistant",
                                            "seq": assistant_delta_seq,
                                            "text": delta_text,
                                        },
                                    )
                            elif event.event_type == "thinking_delta":
                                thinking_text = str(event.payload.get("text", "") or "")
                                if thinking_text:
                                    # Keep the full thinking stream for the final
                                    # transcript metadata; the UI renders it as a
                                    # collapsed block after the turn completes.
                                    runtime_notes["latest_thinking_text"] = (
                                        str(runtime_notes.get("latest_thinking_text", "") or "") + thinking_text
                                    )
                                    turn_id = conversation_turn_id
                                    execution_turn_id = self._runtime_iteration_turn_id(conversation_turn_id, iteration)
                                    thinking_delta_seq += 1
                                    await self._emit_runtime_event(
                                        runtime_session_id,
                                        task,
                                        "thinking_delta",
                                        {
                                            "iteration": iteration + 1,
                                            "turn_id": turn_id,
                                            "canonical_turn_id": conversation_turn_id,
                                            "conversation_turn_id": conversation_turn_id,
                                            "execution_turn_id": execution_turn_id,
                                            # Keyed per iteration (like assistant_delta):
                                            # thinking_delta_seq resets every iteration, so a
                                            # turn-scoped stream id makes downstream seq guards
                                            # drop iteration>=2 deltas and collapses all
                                            # iterations into one entry (no tool interleaving).
                                            "item_id": f"{execution_turn_id}:thinking",
                                            "stream_id": f"{execution_turn_id}:thinking",
                                            "seq": thinking_delta_seq,
                                            "text": thinking_text,
                                        },
                                    )
                            elif event.event_type == "tool_call_delta":
                                index = int(event.payload.get("index", 0) or 0)
                                bucket = tool_call_chunks.setdefault(index, {
                                    "id": "",
                                    "function": "",
                                    "arguments_chunks": [],
                                })
                                if event.payload.get("id"):
                                    bucket["id"] = event.payload["id"]
                                if event.payload.get("name"):
                                    bucket["function"] = event.payload["name"]
                                arguments_chunk = str(event.payload.get("arguments", "") or "")
                                if arguments_chunk:
                                    bucket["arguments_chunks"].append(arguments_chunk)
                                await self._maybe_start_streaming_tool_calls(
                                    upto_index=index,
                                    tool_call_chunks=tool_call_chunks,
                                    early_tool_runs=early_tool_runs,
                                    executor=executor,
                                    planner=planner,
                                    permission_resolver=permission_resolver,
                                    task=task,
                                    on_progress=on_progress,
                                    runtime_session_id=runtime_session_id,
                                )
                            elif event.event_type == "usage":
                                prompt_tokens = int(event.payload.get("prompt_tokens", 0) or 0)
                                completion_tokens = int(event.payload.get("completion_tokens", 0) or 0)
                                estimated_cost_delta = float(event.payload.get("estimated_cost_delta", 0.0) or 0.0)
                                if prompt_tokens:
                                    last_observed_prompt_tokens = prompt_tokens
                                total_usage["prompt_tokens"] += prompt_tokens
                                total_usage["completion_tokens"] += completion_tokens
                                total_cost += estimated_cost_delta
                                runtime_status["input_tokens"] = total_usage["prompt_tokens"]
                                runtime_status["output_tokens"] = total_usage["completion_tokens"]
                                runtime_status["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
                                if self.cost_tracker and (prompt_tokens or completion_tokens or estimated_cost_delta):
                                    entry = CostEntry(
                                        task_id=task.id if task else None,
                                        agent_id=task.assigned_to if task else None,
                                        org_id=task.org_id if task else None,
                                        model=str(event.payload.get("model", "") or event.model or ""),
                                        tokens_in=prompt_tokens,
                                        tokens_out=completion_tokens,
                                        cost=estimated_cost_delta,
                                    )
                                    await self.cost_tracker.record(entry)
                                runtime_status["turn_cost_usd"] = round(total_cost, 6)
                                runtime_status["session_cost_usd"] = round(
                                    float(getattr(self.cost_tracker, "session_total", total_cost) if self.cost_tracker else total_cost),
                                    6,
                                )
                                runtime_status["context_window"] = int(
                                    event.payload.get("context_window", 0) or runtime_status.get("context_window", 0) or 0
                                )
                                await self._emit_runtime_event(
                                    runtime_session_id,
                                    task,
                                    "cost_update",
                                    {
                                        "iteration": iteration + 1,
                                        "model": str(event.payload.get("model", "") or event.model or ""),
                                        "tokens_in": prompt_tokens,
                                        "tokens_out": completion_tokens,
                                        "tokens_total": prompt_tokens + completion_tokens,
                                        "input_tokens_total": runtime_status["input_tokens"],
                                        "output_tokens_total": runtime_status["output_tokens"],
                                        "turn_cost_usd": runtime_status["turn_cost_usd"],
                                        "session_cost_usd": runtime_status["session_cost_usd"],
                                        "estimated_cost_delta": estimated_cost_delta,
                                    },
                                )
                                await self._emit_status_snapshot(runtime_session_id, task, runtime_status)
                            elif event.event_type == "error":
                                message = str(event.payload.get("message", "Unknown LLM stream error"))
                                status = event.payload.get("status")
                                if isinstance(status, int) and not isinstance(status, bool):
                                    # A received rejection, typed: the quota park and the overflow compaction read it.
                                    raise ProviderRejected(status, message,
                                                           error_class=str(event.payload.get("error_class", "") or ""))
                                raise RuntimeError(message)
                            if event.event_type not in {"assistant_delta", "thinking_delta"}:
                                await self._emit_runtime_event(
                                    runtime_session_id,
                                    task,
                                    event.event_type,
                                    {
                                        **event.payload,
                                        "iteration": iteration + 1,
                                        "model": event.model,
                                    },
                                )
                await self._stamp_llm_in_flight(task, False)
            except Exception as exc:
                await self._stamp_llm_in_flight(task, False)
                if isinstance(exc, ModelBudgetStop):
                    await self._cancel_early_tool_runs(early_tool_runs)
                    raise
                quota_error = self._provider_quota_error(exc)
                if quota_error is not None:
                    # Quota/rate-limit rejections never reach the model, so
                    # conversation-feedback retries cannot help — surface a
                    # typed error for the dispatcher to park on instead of
                    # burning retries and failing the work item (OBS-6).
                    await self._cancel_early_tool_runs(early_tool_runs)
                    await self._emit_provider_quota_exhausted(
                        runtime_session_id=runtime_session_id,
                        task=task,
                        iteration=iteration,
                        error=exc,
                    )
                    if quota_error is exc:
                        raise
                    raise quota_error from exc
                if self.llm.is_context_overflow_error(exc) and overflow_retries < max_overflow_retries:
                    await self._join_early_tools()
                    early_tool_runs.clear()
                    overflow_retries += 1
                    messages = await self._apply_context_pipeline(
                        messages,
                        tool_schemas=tool_schemas,
                        task=task,
                        base_prefix_len=base_prefix_len,
                        runtime_session_id=runtime_session_id,
                        compaction_boundaries=compaction_boundaries,
                        force_compact=True,
                        todo_state=todo_state,
                        runtime_notes=runtime_notes,
                        active_subagents=subagents.list_agents().get("agents", []),
                        observed_tokens=last_observed_prompt_tokens,
                    )
                    reasking = True
                    continue
                recovered_turn = await self._recover_tool_protocol_stream_error(
                    exc=exc,
                    messages=messages,
                    prepare_request_tools=prepare_request_tools,
                    base_prefix_len=base_prefix_len,
                    runtime_session_id=runtime_session_id,
                    task=task,
                    iteration=iteration,
                    early_tool_runs=early_tool_runs,
                )
                recovery_unknown = (recovered_turn or {}).get("outcome_unknown") if isinstance(recovered_turn, dict) else None
                if recovery_unknown is not None:
                    exc = recovery_unknown
                    recovered_turn = None
                if recovered_turn is not None:
                    messages = list(recovered_turn["messages"])
                    assistant_text = str(recovered_turn["assistant_text"] or "")
                    tool_call_chunks = dict(recovered_turn["tool_call_chunks"] or {})
                    usage = dict(recovered_turn.get("usage", {}) or {})
                    total_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                    total_usage["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
                    await self._emit_runtime_event(
                        runtime_session_id,
                        task,
                        "tool_protocol_recovered",
                        {
                            "iteration": iteration + 1,
                            "strategy": str(recovered_turn.get("strategy", "") or ""),
                            "tool_call_count": len(tool_call_chunks),
                            "model": str(recovered_turn.get("model", "") or ""),
                        },
                    )
                else:
                    await self._cancel_early_tool_runs(early_tool_runs)
                    # Only a vendor's received "no" is asked again (model-routing phase 0, C10). A request whose outcome
                    # is not known -- a timeout, a 5xx, a reset -- may have been served: the turn fails, marked, and is
                    # not re-run by anyone automatically. A text-classified protocol or overflow error is the vendor's
                    # refusal of the transcript, not an unknown outcome.
                    rejected = received_rejection(exc) is not None
                    outcome_unknown = recovery_unknown is not None or (
                        not rejected and not self.llm.is_tool_protocol_error(exc) and not self.llm.is_context_overflow_error(exc))
                    if recovery_unknown is not None:
                        pass
                    elif self.llm.is_tool_protocol_error(exc):
                        truncated = self._truncate_to_last_clean_user_turn(messages, base_prefix_len)
                        if truncated and len(truncated) > base_prefix_len:
                            await self._emit_runtime_event(
                                runtime_session_id,
                                task,
                                "tool_protocol_retry",
                                {
                                    "iteration": iteration + 1,
                                    "strategy": "truncate",
                                    "message": str(exc),
                                },
                            )
                            messages = truncated
                            reasking = True
                            continue
                    elif rejected and stream_error_feedback_retries < max_stream_error_feedback_retries:
                        stream_error_feedback_retries += 1
                        messages.append(self._provider_error_feedback_message(exc))
                        await self._emit_runtime_event(
                            runtime_session_id,
                            task,
                            "tool_protocol_retry",
                            {
                                "iteration": iteration + 1,
                                "strategy": "provider_error_feedback",
                                "attempt": stream_error_feedback_retries,
                                "message": str(exc),
                            },
                        )
                        reasking = True
                        continue
                    elif rejected and not stream_error_context_reset_attempted:
                        stream_error_context_reset_attempted = True
                        truncated = self._truncate_to_last_clean_user_turn(messages, base_prefix_len)
                        if truncated and base_prefix_len < len(truncated) < len(messages):
                            truncated.append(
                                self._provider_error_feedback_message(exc, context_reset=True)
                            )
                            await self._emit_runtime_event(
                                runtime_session_id,
                                task,
                                "tool_protocol_retry",
                                {
                                    "iteration": iteration + 1,
                                    "strategy": "provider_error_context_reset",
                                    "message": str(exc),
                                },
                            )
                            messages = truncated
                            reasking = True
                            continue
                    await self._emit_runtime_event(
                        runtime_session_id,
                        task,
                        "turn_failed",
                        {
                            "iteration": iteration + 1,
                            "turn_id": conversation_turn_id,
                            "canonical_turn_id": conversation_turn_id,
                            "conversation_turn_id": conversation_turn_id,
                            "execution_turn_id": self._runtime_iteration_turn_id(conversation_turn_id, iteration),
                            "message": str(exc),
                        },
                    )
                    runtime_status["current_tool"] = None
                    runtime_status["queue_depth"] = 0
                    runtime_status["drain_mode"] = "idle"
                    await self._emit_status_snapshot(runtime_session_id, task, runtime_status)
                    await self._save_runtime_session(runtime_session_id, task, "failed", {"error": str(exc)})
                    self._cancel_prefetch(pending_prefetch)
                    return TaskResult(
                        status=TaskStatus.FAILED,
                        content=f"LLM stream failed: {exc}",
                        artifacts={"runtime_session_id": runtime_session_id,
                                   **({"model_outcome_unknown": True} if outcome_unknown else {})},
                        cost=total_cost,
                        token_usage=total_usage,
                    )

            stream_error_feedback_retries = 0
            reasking = False
            tool_calls = self._finalize_tool_calls(tool_call_chunks)
            assistant_message = {"role": "assistant", "content": assistant_text}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": item["id"],
                        "type": "function",
                        "function": {
                            "name": item["function"],
                            "arguments": json.dumps(item["arguments"], ensure_ascii=False, default=str),
                        },
                    }
                    for item in tool_calls
                ]
            messages.append(assistant_message)
            await self._persist_assistant_turn(
                task,
                assistant_text,
                tool_calls,
                runtime_session_id=runtime_session_id,
                turn_id=(
                    self._runtime_iteration_turn_id(conversation_turn_id, iteration)
                    if tool_calls
                    else conversation_turn_id
                ),
                conversation_turn_id=conversation_turn_id,
                iteration=iteration + 1,
                thinking_text=str(runtime_notes.get("latest_thinking_text", "") or ""),
            )
            await self._maybe_update_background_session_memory(
                runtime_session_id=runtime_session_id,
                task=task,
                messages=messages,
            )

            if not tool_calls:
                # A correction can arrive while the final response streams.
                # Give it another bounded iteration before declaring success.
                # Root Harness inboxes then seal synchronously, so cleanup's
                # awaits cannot acknowledge another message that nobody reads.
                if inbox_interrupt_provider is not None and task is not None:
                    inbox_messages = await inbox_interrupt_provider(task)
                    if inbox_messages:
                        messages.append({
                            "role": "system",
                            "content": self._format_interrupt_notice(inbox_messages),
                        })
                        if iteration + 1 >= self.max_iterations:
                            runtime_notes["pending_inbox_messages"] = list(inbox_messages)
                        continue
                    close_inbox = getattr(getattr(task, "_runtime_inbox_queue", None), "close_if_empty", None)
                    if callable(close_inbox) and not close_inbox():
                        continue
                if on_progress and assistant_text:
                    await self._emit_progress(on_progress, assistant_text, task)
                active_subagents = subagents.list_agents().get("agents", [])
                await self._run_verification_audit(
                    runtime_session_id=runtime_session_id,
                    task=task,
                    subagents=subagents,
                    messages=messages,
                    todo_state=todo_state,
                    runtime_notes=runtime_notes,
                )
                extraction_artifacts = await self._extract_durable_memory(
                    task=task,
                    user_message=user_message,
                    assistant_text=assistant_text,
                    runtime_session_id=runtime_session_id,
                )
                if extraction_artifacts:
                    aggregated_artifacts = self._merge_artifacts(aggregated_artifacts, extraction_artifacts)
                final_content, verification_verdict = self._apply_verification_contract(
                    assistant_text,
                    task=task,
                    todo_state=todo_state,
                    runtime_notes=runtime_notes,
                )
                artifacts = {
                    **aggregated_artifacts,
                    "runtime_session_id": runtime_session_id,
                    **result_delivery_identity_payload_for_task(
                        task,
                        canonical_turn_id=conversation_turn_id,
                    ),
                    "permission_requests": self._permission_requests_from_results([]),
                    "active_subagents": active_subagents,
                    "compaction_boundaries": list(compaction_boundaries),
                    "compaction_records": list(compaction_boundaries),
                    "resume_cursor": len(messages),
                    "worktree_path": self._primary_worktree_path(active_subagents),
                    "task_ledger": list(todo_state),
                    "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
                    "verification": dict(runtime_notes.get("verification", {}) or {}),
                    "verification_evidence": dict((runtime_notes.get("verification", {}) or {}).get("evidence", {}) or {}),
                    "verification_verdict": verification_verdict,
                    "artifact_manifest": list(self._compose_runtime_artifact_manifest(
                        task=task,
                        messages=messages,
                        todo_state=todo_state,
                        runtime_notes=runtime_notes,
                        compaction_boundaries=compaction_boundaries,
                        active_subagents=active_subagents,
                    )),
                    "resume_state": self._build_resume_state(
                        messages=messages,
                        todo_state=todo_state,
                        runtime_notes=runtime_notes,
                        compaction_boundaries=compaction_boundaries,
                        active_subagents=active_subagents,
                    ),
                }
                await self._emit_runtime_event(
                    runtime_session_id,
                    task,
                    "turn_completed",
                    {
                        "iteration": iteration + 1,
                        "turn_id": conversation_turn_id,
                        "canonical_turn_id": conversation_turn_id,
                        "conversation_turn_id": conversation_turn_id,
                        "execution_turn_id": self._runtime_iteration_turn_id(conversation_turn_id, iteration),
                        "content_preview": final_content[:500],
                    },
                )
                runtime_status["current_tool"] = None
                runtime_status["queue_depth"] = 0
                runtime_status["drain_mode"] = "idle"
                await self._emit_status_snapshot(runtime_session_id, task, runtime_status)
                # The turn is over: its background and resident subagents end with it (AE04a).
                await subagents.cancel_all()
                await self._save_runtime_session(runtime_session_id, task, "completed", artifacts)
                self._cancel_prefetch(pending_prefetch)
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=final_content,
                    artifacts=artifacts,
                    cost=total_cost,
                    token_usage=total_usage,
                )

            execution_results = await self._collect_execution_results(
                tool_calls=tool_calls,
                early_tool_runs=early_tool_runs,
                executor=executor,
                task=task,
                on_progress=on_progress,
            )
            self._update_runtime_notes(runtime_notes, execution_results)
            runtime_notes["task_ledger"] = list(todo_state)
            active_subagents = subagents.list_agents().get("agents", [])
            await self._persist_task_ledger(
                runtime_session_id=runtime_session_id,
                task=task,
                todo_state=todo_state,
                runtime_notes=runtime_notes,
                messages=messages,
                compaction_boundaries=compaction_boundaries,
                active_subagents=active_subagents,
            )
            early_return = self._handle_pause_or_peer_wait(
                execution_results,
                aggregated_artifacts,
                runtime_session_id,
                task=task,
                compaction_boundaries=compaction_boundaries,
                active_subagents=active_subagents,
                messages=messages,
                todo_state=todo_state,
                runtime_notes=runtime_notes,
            )
            if early_return is not None:
                # Parking for a person or a peer: the background helpers stop first, so nothing keeps working -- or
                # spending -- while the turn waits (AE04a). The suspended state is written after they are gone.
                await subagents.cancel_all()
                await self._save_runtime_session(runtime_session_id, task, early_return.status.value, early_return.artifacts)
                self._cancel_prefetch(pending_prefetch)
                return early_return

            for item in execution_results:
                call = item["tool_call"]
                result = item["result"]
                decision = item.get("permission_decision")
                clipped_result = self._clip_tool_result_for_history(
                    str(call.get("function", "") or ""),
                    result,
                )
                result_text = json.dumps(clipped_result, ensure_ascii=False, default=str)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result_text,
                })
                result_payload = result.get("result", {})
                if isinstance(result_payload, dict):
                    aggregated_artifacts = self._merge_artifacts(aggregated_artifacts, result_payload)
                permission_finished = await self._persist_tool_result(
                    task,
                    call,
                    clipped_result,
                    decision,
                    runtime_session_id=runtime_session_id,
                    hook_metadata=item.get("hook_metadata", {}),
                )
                if not permission_finished:
                    durable_permit = self._approved_resume_tool_call(
                        task,
                        tool_call_id=str(call.get("id", "") or ""),
                    )
                    if durable_permit:
                        await self._set_approved_resume_tool_call_state(
                            task,
                            {**durable_permit, "state": "result_persisted"},
                        )
                    await self._complete_durable_tool_permission(
                        task=task,
                        item=item,
                        runtime_session_id=runtime_session_id,
                    )

            # The order's allowance refused a paid tool: the results are recorded above, and the turn ends here the way
            # a refused model call ends it -- for the owner, not for the model to work around.
            paid_tool_stop = stop_in_tool_results(execution_results)
            if paid_tool_stop is not None:
                await self._save_runtime_session(runtime_session_id, task, "failed", {"reason": "budget_stop"})
                self._cancel_prefetch(pending_prefetch)
                raise paid_tool_stop

        pending_inbox = list(runtime_notes.get("pending_inbox_messages", []) or [])
        close_inbox = getattr(getattr(task, "_runtime_inbox_queue", None), "close_and_take_pending", None)
        if callable(close_inbox):
            pending_inbox.extend(close_inbox())
        limit_artifacts: dict[str, Any] = {
            "runtime_session_id": runtime_session_id, BUDGET_STOP_ARTIFACT: "step_limit",
            "native_iteration_limit_reached": True,
        }
        if pending_inbox:
            # Keep accepted corrections recoverable when the existing step
            # allowance has no room to consume them. Never add an extra call.
            limit_artifacts["pending_inbox_messages"] = pending_inbox
        await subagents.cancel_all()
        await self._save_runtime_session(runtime_session_id, task, "failed", {"reason": "max_iterations", **limit_artifacts})
        self._cancel_prefetch(pending_prefetch)
        return TaskResult(
            status=TaskStatus.FAILED,
            content=f"Exceeded maximum iterations ({self.max_iterations})",
            artifacts=limit_artifacts,
            cost=total_cost,
            token_usage=total_usage,
        )

    def _controller_tool_fence_store(self) -> Any:
        """Resolve the canonical Store without coupling the executor to memory."""

        candidates = (
            getattr(self.memory_manager, "store", None),
            getattr(self.interaction_coordinator, "store", None),
        )
        for store in candidates:
            if callable(
                getattr(store, "delegation_run_controller_lease_is_current", None)
            ):
                return store
        return next((store for store in candidates if store is not None), None)

    async def _handle_runtime_tool(
        self,
        *,
        subagents: SubagentManager,
        tool_name: str,
        arguments: dict[str, Any],
        task: Task | None = None,
        todo_state: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if tool_name == "todo_write":
            todos_payload = arguments.get("todos", [])
            if isinstance(todos_payload, str):
                try:
                    todos_payload = json.loads(todos_payload)
                except json.JSONDecodeError as exc:
                    return {"error": f"Invalid todo JSON: {exc}", "success": False}
            normalized_todos = self._normalize_todos(todos_payload)
            todo_state[:] = normalized_todos if todo_state is not None else normalized_todos
            return {
                "success": True,
                "result": {
                    "todos": list(todo_state or []),
                    "rendered": self._render_todos(todo_state or []),
                    "task_ledger": list(todo_state or []),
                },
            }
        if tool_name == "todo_read":
            return {
                "success": True,
                "result": {
                    "todos": list(todo_state or []),
                    "rendered": self._render_todos(todo_state or []),
                    "task_ledger": list(todo_state or []),
                },
            }
        if tool_name == "agent_spawn":
            background = arguments.get("background")
            if "run_in_background" in arguments:
                background = arguments.get("run_in_background")
            resident = bool(arguments.get("resident", False))
            mode = self._normalize_spawn_mode(arguments.get("mode", "default"))
            return await subagents.spawn(
                profile=str(arguments.get("profile") or arguments.get("subagent_type") or "general"),
                prompt=str(arguments.get("prompt", "") or ""),
                description=str(arguments.get("description", "") or ""),
                name=str(arguments.get("name", "") or ""),
                model=str(arguments.get("model", "") or ""),
                mode=mode,
                background=bool(background) if background is not None else None,
                isolation=str(arguments.get("isolation", "") or "").strip() or None,
                resident=resident,
            )
        if tool_name == "agent_wait":
            return await subagents.wait(
                str(arguments.get("agent_id", "") or ""),
                int(arguments.get("timeout_seconds", 300) or 300),
            )
        if tool_name == "agent_send":
            return await subagents.send(
                str(arguments.get("agent_id", "") or ""),
                str(arguments.get("message", "") or ""),
            )
        if tool_name == "agent_list":
            return subagents.list_agents()
        return {"error": f"Unknown runtime-managed tool: {tool_name}", "success": False}

    def _runtime_session_id(self, task: Task | None) -> str:
        if task:
            runtime_resume = self._runtime_resume_payload(task)
            runtime_session_id = str(
                runtime_resume.get("runtime_session_id") or ""
            ).strip()
            if runtime_session_id:
                return runtime_session_id
        return f"rt_{uuid.uuid4().hex}"

    @staticmethod
    def _runtime_resume_payload(task: Task | None) -> dict[str, Any]:
        if task is None:
            return {}
        context_snapshot = getattr(task, "context_snapshot", {}) or {}
        if not isinstance(context_snapshot, dict):
            return {}
        raw_resume = context_snapshot.get("runtime_resume", {})
        return dict(raw_resume) if isinstance(raw_resume, dict) else {}

    def _approved_resume_tool_calls(self, task: Task | None) -> dict[str, dict[str, Any]]:
        if task is None:
            return {}
        runtime_resume = self._runtime_resume_payload(task)
        raw_calls = runtime_resume.get("approved_tool_calls")
        if raw_calls is None:
            return {}
        if not isinstance(raw_calls, dict):
            raise RuntimeError("canonical approved_tool_calls is not a mapping")
        records = {
            str(fingerprint): dict(record)
            for fingerprint, record in dict(raw_calls or {}).items()
            if isinstance(record, dict)
        }
        if len(records) != len(raw_calls):
            raise RuntimeError("canonical approved_tool_calls contains an invalid permit")
        required = (
            "id",
            "function",
            "fingerprint",
            "runtime_session_id",
            "checkpoint_id",
            "task_id",
        )
        for fingerprint, record in records.items():
            if (
                not fingerprint
                or not all(
                    str(record.get(key, "") or "").strip() for key in required
                )
                or not isinstance(record.get("arguments"), dict)
            ):
                raise RuntimeError(
                    "canonical exact ToolCall permit has incomplete ownership"
                )
        return records

    def _approved_resume_tool_call(
        self,
        task: Task | None,
        *,
        tool_call_id: str = "",
        fingerprint: str = "",
        state: str = "",
    ) -> dict[str, Any]:
        records = self._approved_resume_tool_calls(task)
        if state:
            records = {
                key: record
                for key, record in records.items()
                if str(record.get("state", "") or "") == str(state)
            }
        if fingerprint:
            return dict(records.get(str(fingerprint), {}) or {})
        if tool_call_id:
            return next(
                (
                    dict(record)
                    for record in records.values()
                    if str(record.get("id", "") or "") == str(tool_call_id)
                ),
                {},
            )
        return dict(next(iter(records.values()), {}) or {})

    @staticmethod
    def _canonical_exact_tool_call_denial_result(
        *,
        approval: dict[str, Any] | None = None,
        permit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the one canonical ToolResult for a durable owner denial.

        A live callback has the richest structured approval payload, while a
        restart begins with only the persisted exact-call permit.  Merge the
        two representations without replacing live policy evidence, then
        force the two decision fields whose meaning is fixed by this result.
        """

        canonical_approval = dict(approval or {})
        durable_permit = dict(permit or {})
        permit_approval = {
            "approval_checkpoint_id": durable_permit.get("checkpoint_id"),
            "approved_tool_call_id": durable_permit.get("id"),
            "approved_tool_call_fingerprint": durable_permit.get("fingerprint"),
            "approval_claim_id": durable_permit.get("claim_id"),
            "approval_consumer_id": durable_permit.get("consumer_id"),
            "approval_checkpoint_type": durable_permit.get("checkpoint_type"),
            "approval_checkpoint_project_id": durable_permit.get(
                "checkpoint_project_id"
            ),
        }
        for key, value in permit_approval.items():
            if value not in (None, "") and not canonical_approval.get(key):
                canonical_approval[key] = value
        canonical_approval.setdefault(
            "policy_source",
            "durable_tool_permission",
        )
        canonical_approval.setdefault(
            "rationale",
            "The owner denied this exact ToolCall.",
        )
        canonical_approval.setdefault("risk_level", "high")
        canonical_approval.setdefault("confidence", 1.0)
        canonical_approval.update({"action": "reject", "human_reply": "deny"})
        return {
            "error": "The owner denied this exact ToolCall.",
            "success": False,
            "approval": canonical_approval,
        }

    def _assert_durable_tool_permits_resumable(self, task: Task | None) -> None:
        """Keep every LLM/effect boundary behind a ready-only permit queue.

        A retained ``executing`` permit may already have crossed an external
        effect, while ``result_persisted`` needs checkpoint reconciliation.
        Neither can be treated as an empty queue and followed by a new model
        turn.  Raising here routes the whole canonical queue through the joined
        abort settlement path, which terminalizes or releases each permit with
        its exact state semantics.
        """

        for permit in self._approved_resume_tool_calls(task).values():
            state = str(permit.get("state", "") or "").strip()
            if state == "ready":
                continue
            if state not in {"denied", "executing", "result_persisted"}:
                raise RuntimeError(
                    f"durable ToolCall permit has unknown state {state!r}"
                )
            raise RuntimeError(
                "durable ToolCall permit requires exact recovery before a new "
                f"runtime turn (state={state})"
            )

    async def _set_approved_resume_tool_call_state(
        self,
        task: Task | None,
        record: dict[str, Any] | None,
        *,
        remove_fingerprint: str = "",
    ) -> None:
        if task is None:
            return
        approved_calls = self._approved_resume_tool_calls(task)
        fingerprint = str(remove_fingerprint or "").strip()
        if record is None:
            if not fingerprint:
                if len(approved_calls) != 1:
                    raise ValueError(
                        "removing a durable ToolCall permit requires its fingerprint"
                    )
                fingerprint = next(iter(approved_calls))
            existing = dict(approved_calls.get(fingerprint, {}) or {})
            runtime_session_id = str(
                existing.get("runtime_session_id", "") or ""
            ).strip()
        else:
            fingerprint = str(record.get("fingerprint", "") or "").strip()
            if not fingerprint:
                raise ValueError("approved ToolCall record requires fingerprint")
            runtime_session_id = str(
                record.get("runtime_session_id", "") or ""
            ).strip()
        store = getattr(self.memory_manager, "store", None)
        update = getattr(store, "update_task_runtime_tool_permit", None)
        if not callable(update):
            raise RuntimeError(
                "durable ToolCall permit store is unavailable"
            )
        persisted = await update(
            task.id,
            runtime_session_id=runtime_session_id,
            fingerprint=fingerprint,
            permit=dict(record) if record is not None else None,
            expected_permit=(existing if record is None else None),
        )
        task.context_snapshot = dict(persisted.context_snapshot or {})
        task.metadata = dict(persisted.metadata or {})

    async def _refresh_durable_tool_permits(self, task: Task | None) -> None:
        """Refresh only the Store-owned exact-permit ledger on ``task``.

        Several checkpoint consumers may approve independent ToolCalls for the
        same persisted assistant turn.  Their atomic Store merges must be
        visible to the single elected runtime before it crosses the next LLM
        boundary; a controller-local Task object is not an ownership source.
        """

        if task is None:
            return
        store = getattr(self.memory_manager, "store", None)
        get_ledger = getattr(store, "get_task_runtime_tool_ledger", None)
        if not callable(get_ledger):
            return
        ledger = await get_ledger(
            task.id,
            project_id=str(getattr(task, "project_id", "") or "default"),
        )
        if ledger is None:
            raise RuntimeError(
                f"task {task.id} disappeared while draining durable ToolCalls"
            )
        apply_to_task = getattr(ledger, "apply_to_task", None)
        if not callable(apply_to_task):
            raise RuntimeError("canonical durable ToolCall ledger snapshot is invalid")
        apply_to_task(task)

    async def _durable_tool_permit_matches_context(
        self,
        *,
        permit: dict[str, Any],
        task: Task | None,
        runtime_session_id: str,
    ) -> bool:
        """Verify that a one-shot permit is still owned by its durable row."""

        if task is None:
            return False
        store = getattr(self.memory_manager, "store", None)
        getter = getattr(store, "get_execution_checkpoint", None)
        if not callable(getter):
            return False
        checkpoint_id = str(permit.get("checkpoint_id", "") or "").strip()
        checkpoint_type = str(
            permit.get("checkpoint_type", "") or "tool_permission"
        ).strip()
        project_id = str(
            permit.get("checkpoint_project_id")
            or getattr(task, "project_id", "")
            or "default"
        ).strip() or "default"
        checkpoint = await getter(
            checkpoint_id,
            project_id=project_id,
            checkpoint_type=checkpoint_type,
        )
        if checkpoint is None or str(checkpoint.status or "") != "consuming":
            return False
        payload = dict(checkpoint.payload or {})
        interaction = dict(payload.get("interaction", {}) or {})
        ownership = dict(interaction.get("ownership", {}) or {})
        tool_call = dict(payload.get("tool_call", {}) or {})
        claim = dict(interaction.get("claim", {}) or {})
        task_id = str(getattr(task, "id", "") or "").strip()
        return bool(
            checkpoint_type == "tool_permission"
            and str(checkpoint.task_id or ownership.get("waiting_task_id") or "").strip()
            == task_id
            and str(ownership.get("waiting_task_id", "") or "").strip() == task_id
            and str(tool_call.get("id", "") or "").strip()
            == str(permit.get("id", "") or "").strip()
            and str(tool_call.get("runtime_session_id", "") or "").strip()
            == str(runtime_session_id or "").strip()
            and str(tool_call.get("fingerprint", "") or "").strip()
            == str(permit.get("fingerprint", "") or "").strip()
            and str(claim.get("claim_id", "") or "").strip()
            == str(permit.get("claim_id", "") or "").strip()
            and str(claim.get("consumer_id", "") or "").strip()
            == str(permit.get("consumer_id", "") or "").strip()
        )

    def _conversation_turn_id(self, task: Task | None, runtime_session_id: str) -> str:
        if task:
            metadata = dict(getattr(task, "metadata", {}) or {})
            runtime_meta = dict(metadata.get("runtime_v2", {}) or {})
            for source in (metadata, runtime_meta):
                for key in (
                    "conversation_turn_id",
                    "current_turn_id",
                    "runtime_v2_current_turn_id",
                    "canonical_turn_id",
                    "turn_id",
                ):
                    value = str(source.get(key, "") or "").strip()
                    if value:
                        return value
        return f"{runtime_session_id}:turn:{uuid.uuid4().hex}"

    @staticmethod
    def _runtime_iteration_turn_id(conversation_turn_id: str, iteration: int) -> str:
        normalized_turn_id = str(conversation_turn_id or "").strip()
        if not normalized_turn_id:
            normalized_turn_id = f"turn:{uuid.uuid4().hex}"
        return f"{normalized_turn_id}:iter:{iteration + 1}"

    def _build_tool_hook_bus(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        permission_resolver: RuntimePermissionAdapter,
        on_progress: Any = None,
    ) -> RuntimeToolHookBus:
        hook_bus = RuntimeToolHookBus(
            emit_event=lambda event_type, payload: self._emit_runtime_event(
                runtime_session_id,
                task,
                event_type,
                payload,
            ),
        )
        if self.config.system.native_runtime.enable_tool_hooks:
            hook_bus.register_pre_hook(
                "permission_gate",
                lambda context: self._approval_pre_hook(
                    context,
                    permission_resolver=permission_resolver,
                    on_progress=on_progress,
                    runtime_session_id=runtime_session_id,
                ),
            )
            hook_bus.register_post_hook("approval_metadata", self._approval_post_hook)
            hook_bus.register_post_hook("command_exit", self._command_exit_post_hook)
            hook_bus.register_failure_hook("failure_cascade", self._failure_cascade_hook)
        for name, hook in self._pre_tool_hooks:
            hook_bus.register_pre_hook(name, hook)
        for name, hook in self._post_tool_hooks:
            hook_bus.register_post_hook(name, hook)
        for name, hook in self._failure_tool_hooks:
            hook_bus.register_failure_hook(name, hook)
        return hook_bus

    async def _approval_pre_hook(
        self,
        context: RuntimeToolHookContext,
        *,
        permission_resolver: RuntimePermissionAdapter,
        on_progress: Any = None,
        runtime_session_id: str = "",
    ) -> dict[str, Any] | None:
        predicted = context.predicted_permission
        # These fields are runtime credentials, not part of the provider's
        # ToolCall schema.  Always discard any incoming look-alike value
        # before attaching policy state computed in this process.
        context.call.pop("_native_permission_auto_sign", None)
        context.call.pop("_sandbox_override", None)
        context.call["_runtime_session_id"] = str(runtime_session_id or "")

        def _apply_sandbox_override(value: Any) -> None:
            if not isinstance(value, dict) or not value:
                return
            sandbox_override = dict(value)
            context.call["_sandbox_override"] = sandbox_override
            context.state.setdefault("metadata", {})[
                "native_execution_sandbox_override"
            ] = sandbox_override

        if (
            predicted is not None
            and getattr(predicted, "resolution", None) == PermissionResolution.ALLOW
        ):
            predicted_metadata = dict(getattr(predicted, "metadata", {}) or {})
            _apply_sandbox_override(predicted_metadata.get("sandbox_override"))
            level = str(predicted_metadata.get("native_approval_level", "") or "")
            # Company shell/Python effects retain the controller-owned exact
            # effect fence even when the native permission policy allows the
            # call without asking.  This marker is trusted runtime metadata,
            # never model-supplied, and the store revalidates the complete
            # frozen launch envelope before recording the effect.
            context.call["_native_permission_auto_sign"] = {
                "level": level,
                "policy_source": str(getattr(predicted, "source", "") or ""),
                "scope_id": str(
                    predicted_metadata.get(
                        "native_permission_scope_id", "",
                    )
                    or ""
                ),
            }
        if predicted is not None and getattr(predicted, "resolution", None) == PermissionResolution.DENY:
            return {
                "result": permission_resolver.build_blocked_result(
                    predicted,
                    tool_name=context.tool_name,
                    arguments=context.arguments,
                ),
                "stop_execution": True,
                "stop_batch_on_failure": True,
            }
        violation = ownership_guard_violation(
            task=context.task,
            tool_name=context.tool_name,
            arguments=context.arguments,
            org_engine=None,
        )
        if violation:
            return {
                "result": {
                    "error": f"Tool execution blocked by ownership contract: {violation}",
                    "approval": {
                        "action": "reject",
                        "risk_level": "high",
                        "confidence": 0.99,
                        "policy_source": "ownership_contract",
                        "rationale": violation,
                    },
                    "success": False,
                },
                "stop_execution": True,
                "stop_batch_on_failure": True,
            }
        permit = context.call.get("_approval_permit")
        if isinstance(permit, dict) and bool(permit.get("approved")):
            permit_call_id = str(permit.get("id", "") or "").strip()
            permit_runtime_id = str(permit.get("runtime_session_id", "") or "").strip()
            permit_task_id = str(permit.get("task_id", "") or "").strip()
            permit_fingerprint = str(permit.get("fingerprint", "") or "").strip()
            execution_envelope: dict[str, Any] = {}
            execution_identity: dict[str, Any] = {}
            sandbox_override = dict(
                dict(getattr(predicted, "metadata", {}) or {}).get(
                    "sandbox_override", {},
                )
                or {}
            )
            if (
                context.tool_name in {"shell_exec", "python_exec"}
                and is_company_runtime_task(context.task)
            ):
                execution_envelope = company_opaque_execution_plan_for_permit(
                    context.task,
                    context.tool_name,
                    context.arguments,
                    permit_envelope=dict(
                        permit.get("execution_envelope", {}) or {}
                    ),
                    sandbox_override=sandbox_override,
                ).envelope
                execution_identity = company_opaque_execution_identity(
                    context.task
                )
            actual_fingerprint = exact_tool_call_fingerprint(
                tool_call_id=str(context.call.get("id", "") or ""),
                tool_name=context.tool_name,
                arguments=dict(context.arguments or {}),
                runtime_session_id=permit_runtime_id,
                execution_envelope=execution_envelope,
                execution_identity=execution_identity,
            )
            durable_owner_matches = await self._durable_tool_permit_matches_context(
                permit=permit,
                task=context.task,
                runtime_session_id=runtime_session_id,
            )
            if (
                durable_owner_matches
                and
                permit_call_id
                and permit_call_id == str(context.call.get("id", "") or "")
                and permit_runtime_id == str(runtime_session_id or "")
                and permit_task_id
                and permit_task_id == str(getattr(context.task, "id", "") or "")
                and permit_fingerprint
                and permit_fingerprint == actual_fingerprint
                and dict(permit.get("execution_envelope", {}) or {})
                == execution_envelope
                and dict(permit.get("execution_identity", {}) or {})
                == execution_identity
            ):
                canonical_permit = self._approved_resume_tool_call(
                    context.task,
                    tool_call_id=permit_call_id,
                )
                if canonical_permit:
                    context.call["_approval_permit"] = canonical_permit
                _apply_sandbox_override(sandbox_override)
                return {
                    "approval": {
                        "action": "auto_approve",
                        "risk_level": "low",
                        "confidence": 1.0,
                        "policy_source": "durable_tool_permission",
                        "rationale": "Exact persisted ToolCall approved by the owner.",
                        "human_reply": str(permit.get("decision", "") or "approve_once"),
                        "approval_checkpoint_id": str(permit.get("checkpoint_id", "") or ""),
                        "approved_tool_call_id": permit_call_id,
                        "approved_tool_call_fingerprint": permit_fingerprint,
                    }
                }
        if self.approval_callback is None or context.tool is None or getattr(context.tool, "runtime_managed", False):
            return None
        requires_prompt = (
            predicted is not None
            and getattr(predicted, "resolution", None) == PermissionResolution.ASK
        ) if predicted is not None else bool(getattr(context.tool, "requires_confirmation", False))
        if not requires_prompt:
            return None
        call_context = {
            "id": str(context.call.get("id", "") or ""),
            "tool_call_id": str(context.call.get("id", "") or ""),
            "runtime_session_id": str(runtime_session_id or ""),
            "batch_id": str(context.call.get("batch_id", "") or ""),
            "arguments_raw": str(context.call.get("arguments_raw", "") or ""),
        }
        try:
            allowed, decision = await self.approval_callback(
                context.tool,
                context.arguments,
                context.task,
                on_progress,
                call_context=call_context,
            )
        except TypeError as exc:
            # Third-party callback adapters may still implement the original
            # four-argument protocol.  Only fall back for a signature mismatch;
            # a TypeError raised inside a modern callback must remain visible.
            if "call_context" not in str(exc):
                raise
            allowed, decision = await self.approval_callback(
                context.tool,
                context.arguments,
                context.task,
                on_progress,
            )
        approval_payload = {
            "action": getattr(getattr(decision, "action", None), "value", ""),
            "risk_level": getattr(getattr(decision, "risk_level", None), "value", ""),
            "confidence": float(getattr(decision, "confidence", 0.0) or 0.0),
            "policy_source": str(getattr(decision, "policy_source", "") or ""),
            "rationale": str(getattr(decision, "rationale", "") or ""),
            **dict(getattr(decision, "metadata", {}) or {}),
        }
        sandbox_override = approval_payload.get("sandbox_override")
        if allowed:
            _apply_sandbox_override(sandbox_override)
        approval_checkpoint_id = str(
            approval_payload.get("approval_checkpoint_id", "") or ""
        ).strip()
        durable_denial_permit: dict[str, Any] = {}
        if approval_checkpoint_id and context.task is not None:
            permit = self._approved_resume_tool_call(
                context.task,
                tool_call_id=str(context.call.get("id", "") or ""),
            )
            if (
                str(permit.get("checkpoint_id", "") or "") == approval_checkpoint_id
                and str(permit.get("id", "") or "")
                == str(context.call.get("id", "") or "")
                and str(permit.get("task_id", "") or "")
                == str(getattr(context.task, "id", "") or "")
                and str(permit.get("runtime_session_id", "") or "")
                == str(runtime_session_id or "")
            ):
                if allowed and not await self._begin_durable_tool_permission_effect(
                    context.task,
                    permit,
                ):
                    return {
                        "result": {
                            "error": (
                                "Tool execution blocked because its durable approval "
                                "lease is no longer owned by this runtime."
                            ),
                            "success": False,
                        },
                        "stop_execution": True,
                        "stop_batch_on_failure": True,
                    }
                if allowed:
                    # Exact begin already advanced both checkpoint and permit
                    # atomically. Pull the canonical state into this Task.
                    await self._refresh_durable_tool_permits(context.task)
                    executing_permit = self._approved_resume_tool_call(
                        context.task,
                        tool_call_id=str(context.call.get("id", "") or ""),
                    )
                    if executing_permit:
                        context.call["_approval_permit"] = executing_permit
                else:
                    permit_is_exact_denial = bool(
                        str(permit.get("checkpoint_type", "") or "")
                        == "tool_permission"
                        and permit.get("approved") is False
                        and str(permit.get("decision", "") or "") == "deny"
                        and str(permit.get("state", "") or "") == "ready"
                        and str(permit.get("function", "") or "")
                        == context.tool_name
                        and dict(permit.get("arguments", {}) or {})
                        == dict(context.arguments or {})
                        and str(permit.get("fingerprint", "") or "")
                        == str(
                            approval_payload.get(
                                "approved_tool_call_fingerprint",
                                "",
                            )
                            or ""
                        )
                        and str(permit.get("id", "") or "")
                        == str(
                            approval_payload.get("approved_tool_call_id", "")
                            or ""
                        )
                        and str(permit.get("claim_id", "") or "").strip()
                        and str(permit.get("consumer_id", "") or "").strip()
                        and await self._durable_tool_permit_matches_context(
                            permit=permit,
                            task=context.task,
                            runtime_session_id=runtime_session_id,
                        )
                    )
                    if permit_is_exact_denial:
                        durable_denial_permit = permit
                        await self._set_approved_resume_tool_call_state(
                            context.task,
                            {**permit, "state": "denied"},
                        )
        if not allowed:
            permission_resolver.record_denial(
                context.tool_name,
                context.arguments,
                task=context.task,
                denial_id=str(context.call.get("id", "") or ""),
            )
            durable_human_denial = bool(
                durable_denial_permit
                and approval_checkpoint_id
                and str(approval_payload.get("human_reply", "") or "")
                .strip()
                .lower()
                == "deny"
            )
            if durable_human_denial:
                denial_result = self._canonical_exact_tool_call_denial_result(
                    approval=approval_payload,
                    permit=durable_denial_permit,
                )
                return {
                    "approval": dict(denial_result["approval"]),
                    "result": denial_result,
                    "stop_execution": True,
                    "stop_batch_on_failure": True,
                }
            return {
                "approval": approval_payload,
                "result": {
                    "error": f"Tool execution blocked by autonomy policy: {getattr(decision, 'rationale', '')}",
                    "approval": approval_payload,
                    "success": False,
                },
                "stop_execution": True,
                "stop_batch_on_failure": True,
            }
        return {"approval": approval_payload}

    async def _approval_post_hook(self, context: RuntimeToolHookContext) -> dict[str, Any] | None:
        approval = dict(context.state.get("approval", {}) or {})
        if not approval or not isinstance(context.result, dict):
            return None
        merged = dict(context.result)
        current = dict(merged.get("approval", {}) or {})
        current.update(approval)
        merged["approval"] = current
        return {"result": merged}

    async def _command_exit_post_hook(self, context: RuntimeToolHookContext) -> dict[str, Any] | None:
        if not isinstance(context.result, dict):
            return None
        payload = context.result.get("result", {})
        if not isinstance(payload, dict):
            return None
        exit_code = payload.get("exit_code")
        if exit_code in (None, 0):
            return None
        merged = dict(context.result)
        merged["success"] = False
        if not merged.get("error"):
            stderr = str(payload.get("stderr", "") or "").strip()
            merged["error"] = stderr or f"{context.tool_name} exited with code {exit_code}"
        return {
            "result": merged,
            "stop_batch_on_failure": True,
        }

    async def _failure_cascade_hook(self, context: RuntimeToolHookContext) -> dict[str, Any] | None:
        if not isinstance(context.result, dict):
            return None
        if bool(context.result.get("success", True)):
            return None
        return {
            "stop_batch_on_failure": True,
            "metadata": {"failure_source": context.tool_name},
        }

    @staticmethod
    def _apply_response_language(system_prompt: str) -> str:
        """Append a hard reply-language directive to every system prompt.

        Upstream prompts are English, which biases models toward English replies.
        The SuperOnes distribution defaults to Simplified Chinese: an unset
        AMPULY_RESPONSE_LANGUAGE means "zh-CN"; set it to "en" (or any locale) to
        change the reply language, or to "off" to disable the directive.
        """
        lang = os.environ.get("AMPULY_RESPONSE_LANGUAGE", "").strip() or "zh-CN"
        if lang.lower() in {"off", "none", "disable"}:
            return system_prompt
        if lang.lower() in {"zh", "zh-cn", "zh_cn", "chinese"}:
            directive = (
                "\n\n[响应语言]\n无论以上指令使用什么语言，你的全部思考、沟通、汇报与"
                "用户可见产出一律使用简体中文；代码、命令与专有名词保持原文。"
            )
        else:
            directive = (
                f"\n\n[Response Language]\nRegardless of the language of these "
                f"instructions, think, communicate and produce all user-facing "
                f"output in {lang}. Keep code, commands and proper nouns as-is."
            )
        return system_prompt + directive

    async def _bootstrap_messages(
        self,
        *,
        system_prompt: str,
        user_content: Any,
        user_message: str,
        context_messages: list[dict[str, Any]] | None,
        task: Task | None,
        suppress_resume_user_append: bool = False,
        tool_pairing_log: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        system_prompt = self._apply_response_language(system_prompt)
        runtime_resume = self._runtime_resume_payload(task)
        ready_permit = self._approved_resume_tool_call(task, state="ready")
        if ready_permit and (
            task is None
            or not task.session_id
            or not getattr(self.memory_manager, "store", None)
        ):
            raise RuntimeError(
                "approved ToolCall cannot resume without its durable session store"
            )
        if runtime_resume and task and getattr(self.memory_manager, "store", None) and task.session_id:
            restored = await self._restore_transcript_messages(task)
            if ready_permit and not restored:
                raise RuntimeError(
                    "approved ToolCall has no durable transcript to resume"
                )
            if restored:
                sanitizer = getattr(self.llm, "sanitize_tool_call_history", None)
                if ready_permit:
                    restored = self._sanitize_restored_history_for_resume(
                        restored,
                        task=task,
                        sanitizer=(
                            sanitizer
                            if callable(sanitizer)
                            else lambda messages: list(messages)
                        ),
                    )
                else:
                    # A turn cut off by a restart leaves a tool call with no
                    # result. Answer it as "interrupted, outcome unknown" before
                    # the sanitizer would drop the whole call, so the model knows
                    # it was made and does not assume it succeeded. The stored
                    # transcript is not touched.
                    restored = self._pair_tool_calls_for_model(
                        restored,
                        task=task,
                        warn_state=tool_pairing_log,
                    )
                    if callable(sanitizer):
                        restored = sanitizer(restored)
                prefix_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
                if context_messages:
                    prefix_messages.extend(context_messages)
                messages = [*prefix_messages, *restored]
                # An approved restart resumes at the exact unresolved ToolCall
                # boundary.  Appending a new user turn here would place it
                # between the assistant tool_call and its tool result and would
                # force the model to regenerate the command.
                if (
                    not self._approved_resume_tool_call(task)
                    and not bool(runtime_resume.get("continue_after_tool_result"))
                    and not suppress_resume_user_append
                    and self._should_append_resume_user_turn(restored, user_message)
                ):
                    messages.append({"role": "user", "content": user_content})
                return messages, len(prefix_messages)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if context_messages:
            messages.extend(context_messages)
        messages.append({"role": "user", "content": user_content})
        return messages, len(messages)

    def _sanitize_restored_history_for_resume(
        self,
        restored: list[dict[str, Any]],
        *,
        task: Task,
        sanitizer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Sanitize history without deleting an approved pending ToolCall.

        The generic provider sanitizer intentionally drops an unterminated
        assistant tool-call block.  That is correct for ordinary recovery,
        but an approved exact ToolCall resumes precisely at that boundary: the
        runtime must execute it and append its ToolResult before another model
        turn.  Preserve the one durable pending block while still sanitizing
        all earlier history.  Any ambiguous or non-adjacent tail fails closed.
        """

        permits = self._approved_resume_tool_calls(task)
        ready_records = [
            dict(record)
            for record in permits.values()
            if str(record.get("state", "") or "").strip() == "ready"
        ]
        ready_ids = {
            str(record.get("id", "") or "").strip()
            for record in ready_records
        }
        ready_ids.discard("")
        if len(ready_ids) != len(ready_records):
            raise RuntimeError(
                "approved ToolCall permits have duplicate or empty call ids"
            )
        if not ready_ids:
            return list(sanitizer(restored))

        block_index = -1
        block_ids: set[str] = set()
        block_calls: dict[str, dict[str, Any]] = {}
        for index, message in enumerate(restored):
            if str(message.get("role", "") or "").strip() != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            candidate_calls = {
                str(call.get("id", "") or "").strip(): dict(call)
                for call in calls
                if isinstance(call, dict)
                and str(call.get("id", "") or "").strip()
            }
            if len(candidate_calls) != len(calls):
                raise RuntimeError(
                    "approved ToolCall transcript has duplicate or empty call ids"
                )
            candidate_ids = set(candidate_calls)
            if ready_ids & candidate_ids:
                if block_index >= 0:
                    raise RuntimeError(
                        "approved ToolCall permits span multiple transcript blocks"
                    )
                block_index = index
                block_ids = candidate_ids
                block_calls = candidate_calls
        if block_index < 0 or not ready_ids.issubset(block_ids):
            raise RuntimeError(
                "approved ToolCall has no exact durable assistant transcript block"
            )

        preserved_tail: list[dict[str, Any]] = []
        completed_ids: set[str] = set()
        for message in restored[block_index + 1 :]:
            role = str(message.get("role", "") or "").strip()
            tool_call_id = str(message.get("tool_call_id", "") or "").strip()
            if role != "tool" or tool_call_id not in block_ids:
                raise RuntimeError(
                    "approved ToolCall transcript has a non-adjacent recovery tail"
                )
            if tool_call_id in completed_ids:
                raise RuntimeError(
                    "approved ToolCall transcript has duplicate ToolResults"
                )
            completed_ids.add(tool_call_id)
            preserved_tail.append(dict(message))
        if completed_ids & ready_ids:
            raise RuntimeError(
                "approved ToolCall already has a durable transcript result"
            )
        if ready_ids != block_ids - completed_ids:
            raise RuntimeError(
                "approved ToolCall transcript contains an unowned pending call"
            )

        for permit in permits.values():
            call_id = str(permit.get("id", "") or "").strip()
            if call_id not in ready_ids:
                continue
            call = block_calls.get(call_id, {})
            function = dict(call.get("function", {}) or {})
            if str(call.get("type", "function") or "").strip() != "function":
                raise RuntimeError(
                    "approved ToolCall transcript has a non-function call type"
                )
            name = str(function.get("name", "") or "").strip()
            if name != str(permit.get("function", "") or "").strip():
                raise RuntimeError(
                    "approved ToolCall transcript function does not match permit"
                )
            raw_arguments = function.get("arguments", "{}")
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments or {})
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "approved ToolCall transcript arguments are not canonical JSON"
                ) from exc
            if arguments != dict(permit.get("arguments", {}) or {}):
                raise RuntimeError(
                    "approved ToolCall transcript arguments do not match permit"
                )

        prefix = list(sanitizer(restored[:block_index]))
        return [*prefix, dict(restored[block_index]), *preserved_tail]

    async def _restore_transcript_messages(self, task: Task) -> list[dict[str, Any]]:
        store = getattr(self.memory_manager, "store", None)
        if not store or not hasattr(store, "get_session_transcript") or not task.session_id:
            return []
        transcript = await store.get_session_transcript(task.session_id)
        if not transcript:
            return []
        runtime_messages: list[dict[str, Any]] = []
        seen_tool_call_ids: set[str] = set()
        pending_tool_calls: list[dict[str, Any]] = []
        for message_index, item in enumerate(transcript):
            message = item["message"]
            parts = item["parts"]
            if getattr(message, "summary_flag", False):
                continue
            role = str(getattr(message, "role", "") or "")
            if role == "user":
                content = self._normalize_content_for_resume(parts)
                if content:
                    runtime_messages.append({"role": "user", "content": content})
                continue
            if role == "assistant":
                assistant_texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []
                for part_index, part in enumerate(parts):
                    payload = dict(part.payload or {})
                    if part.part_type == "text":
                        text = str(payload.get("text", "") or "").strip()
                        if text:
                            assistant_texts.append(text)
                    elif part.part_type == "tool_call":
                        tool_name = str(payload.get("tool_name", "") or "").strip()
                        tool_call_id = str(payload.get("tool_call_id", "") or "").strip()
                        if not tool_call_id:
                            tool_call_id = self._synthesize_resume_tool_call_id(
                                message_index=message_index,
                                part_index=part_index,
                                tool_name=tool_name,
                            )
                        tool_calls.append({
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(payload.get("arguments", {}), ensure_ascii=False, default=str),
                            },
                        })
                        seen_tool_call_ids.add(tool_call_id)
                        pending_tool_calls.append({
                            "id": tool_call_id,
                            "tool_name": tool_name,
                            "consumed": False,
                        })
                    elif part.part_type in {"tool_output", "tool_result"}:
                        tool_results.append(payload)
                if assistant_texts or tool_calls:
                    assistant_message: dict[str, Any] = {
                        "role": "assistant",
                        "content": "\n\n".join(assistant_texts).strip(),
                    }
                    if tool_calls:
                        assistant_message["tool_calls"] = tool_calls
                    runtime_messages.append(assistant_message)
                for payload in tool_results:
                    output = payload.get("result", payload.get("output", ""))
                    tool_call_id = self._resolve_resume_tool_result_id(
                        payload,
                        seen_tool_call_ids=seen_tool_call_ids,
                        pending_tool_calls=pending_tool_calls,
                    )
                    rendered_output = json.dumps(output, ensure_ascii=False, default=str) if not isinstance(output, str) else output
                    if tool_call_id:
                        runtime_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": rendered_output,
                        })
                    else:
                        downgraded = self._render_orphan_tool_result_for_resume(payload, rendered_output)
                        if downgraded:
                            runtime_messages.append({"role": "assistant", "content": downgraded})
                continue
            rendered = self._normalize_content_for_resume(parts)
            if rendered:
                runtime_messages.append({"role": "assistant", "content": rendered})
        return self._dedupe_transcript_messages(runtime_messages)

    def _synthesize_resume_tool_call_id(
        self,
        *,
        message_index: int,
        part_index: int,
        tool_name: str,
    ) -> str:
        normalized_name = "".join(
            ch if ch.isalnum() else "_"
            for ch in str(tool_name or "tool").strip().lower()
        ).strip("_") or "tool"
        return f"resume_call_{message_index}_{part_index}_{normalized_name}"

    def _resolve_resume_tool_result_id(
        self,
        payload: dict[str, Any],
        *,
        seen_tool_call_ids: set[str],
        pending_tool_calls: list[dict[str, Any]],
    ) -> str:
        explicit_id = str(payload.get("tool_call_id", "") or "").strip()
        tool_name = str(payload.get("tool_name", "") or "").strip()
        if explicit_id and explicit_id in seen_tool_call_ids:
            for pending in pending_tool_calls:
                if pending.get("id") == explicit_id and not pending.get("consumed"):
                    pending["consumed"] = True
                    break
            return explicit_id

        for pending in pending_tool_calls:
            if pending.get("consumed"):
                continue
            if tool_name and str(pending.get("tool_name", "") or "").strip() != tool_name:
                continue
            pending["consumed"] = True
            return str(pending.get("id", "") or "")

        for pending in pending_tool_calls:
            if pending.get("consumed"):
                continue
            pending["consumed"] = True
            return str(pending.get("id", "") or "")
        return ""

    def _render_orphan_tool_result_for_resume(
        self,
        payload: dict[str, Any],
        rendered_output: str,
    ) -> str:
        content = str(rendered_output or "").strip()
        if not content:
            return ""
        tool_name = str(payload.get("tool_name", "") or "tool").strip() or "tool"
        return f"Tool result [{tool_name}]\n{content}"

    def _sanitize_tool_message_sequence(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        known_tool_calls: dict[str, str] = {}
        for message in messages:
            role = str(message.get("role", "") or "")
            if role == "assistant":
                sanitized_message = dict(message)
                tool_calls = list(message.get("tool_calls", []) or [])
                valid_tool_calls: list[dict[str, Any]] = []
                for call in tool_calls:
                    call_id = str(call.get("id", "") or "").strip()
                    if not call_id:
                        continue
                    valid_tool_calls.append(call)
                    known_tool_calls[call_id] = self._tool_call_name(call)
                if valid_tool_calls:
                    sanitized_message["tool_calls"] = valid_tool_calls
                else:
                    sanitized_message.pop("tool_calls", None)
                sanitized.append(sanitized_message)
                continue
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id", "") or "").strip()
                if tool_call_id and tool_call_id in known_tool_calls:
                    sanitized.append(message)
                    continue
                downgraded = self._render_orphan_runtime_tool_message(
                    message,
                    tool_name=known_tool_calls.get(tool_call_id, ""),
                )
                if downgraded:
                    sanitized.append({"role": "assistant", "content": downgraded})
                continue
            sanitized.append(message)
        return sanitized

    @staticmethod
    def _tool_call_name(call: dict[str, Any]) -> str:
        function = call.get("function", {})
        if isinstance(function, dict):
            return str(function.get("name", "") or "").strip()
        return str(call.get("function", "") or "").strip()

    def _render_orphan_runtime_tool_message(
        self,
        message: dict[str, Any],
        *,
        tool_name: str = "",
    ) -> str:
        content = str(message.get("content", "") or "").strip()
        if not content:
            return ""
        call_id = str(message.get("tool_call_id", "") or "").strip()
        label = tool_name or (f"orphan call {call_id}" if call_id else "orphan tool result")
        return f"Tool result [{label}]\n{content}"

    # What the model reads in place of a result that was never recorded.
    _INTERRUPTED_TOOL_CALL_NOTE = (
        "This tool call was interrupted (for example by a restart) before its result was "
        "recorded, so whether it ran and what it returned is unknown. Do not assume it "
        "succeeded. If you still need its effect or its output, check the current state "
        "first (read or query again), then decide whether to call it again."
    )

    @classmethod
    def _pair_tool_call_messages(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Answer every assistant tool call and leave out stray tool messages, on a copy.

        Providers (DeepSeek, OpenAI) refuse a request in which an assistant
        ``tool_calls`` message is not directly followed by one tool message per
        call id, or in which a tool message answers no call right before it.
        A turn cut off by a restart leaves exactly that behind. Each missing
        answer becomes a tool message saying the outcome is unknown; the call
        itself is never sent again. Returns ``(messages, filled_ids,
        dropped_ids)``. With nothing to repair the input list itself comes
        back; the input is never mutated, so the stored transcript and the
        runtime history keep what really happened.
        """
        paired: list[dict[str, Any]] = []
        filled: list[str] = []
        dropped: list[str] = []
        index = 0
        total = len(messages)
        while index < total:
            message = messages[index]
            role = str(message.get("role", "") or "").strip()
            index += 1
            if role == "tool":
                dropped.append(str(message.get("tool_call_id", "") or "").strip())
                continue
            paired.append(message)
            calls = message.get("tool_calls") if role == "assistant" else None
            if not isinstance(calls, list) or not calls:
                continue
            expected: dict[str, str] = {}
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id", "") or "").strip()
                if call_id and call_id not in expected:
                    expected[call_id] = cls._tool_call_name(call)
            answered: set[str] = set()
            while index < total and str(messages[index].get("role", "") or "").strip() == "tool":
                tool_message = messages[index]
                index += 1
                tool_call_id = str(tool_message.get("tool_call_id", "") or "").strip()
                if tool_call_id in expected and tool_call_id not in answered:
                    answered.add(tool_call_id)
                    paired.append(tool_message)
                else:
                    dropped.append(tool_call_id)
            for call_id, tool_name in expected.items():
                if call_id in answered:
                    continue
                filled.append(call_id)
                paired.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "interrupted": True,
                            "outcome": "unknown",
                            "tool_name": tool_name,
                            "note": cls._INTERRUPTED_TOOL_CALL_NOTE,
                        },
                        ensure_ascii=False,
                    ),
                })
        if not filled and not dropped:
            return messages, [], []
        return paired, filled, dropped

    def _pair_tool_calls_for_model(
        self,
        messages: list[dict[str, Any]],
        *,
        task: Task | None,
        warn_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """The copy of ``messages`` a model may be sent; one WARNING per turn when it had gaps."""
        paired, filled, dropped = self._pair_tool_call_messages(messages)
        if (filled or dropped) and not (warn_state is not None and warn_state.get("warned")):
            if warn_state is not None:
                warn_state["warned"] = True
            logger.warning(
                "Tool-call history for the model had gaps (task={}): {} unanswered call(s) sent as "
                "interrupted with unknown outcome {}, {} stray tool message(s) left out; the stored "
                "transcript is unchanged and no call is re-sent",
                str(getattr(task, "id", "") or "-"),
                len(filled),
                filled[:8],
                len(dropped),
            )
        return paired

    @staticmethod
    def _normalize_verification_contract_line(line: str) -> str:
        return re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", str(line or "")).strip()

    def _normalize_content_for_resume(self, parts: list[Any]) -> str:
        snippets: list[str] = []
        for part in parts:
            payload = dict(part.payload or {})
            if part.part_type == "text":
                text = str(payload.get("text", "") or "").strip()
                if text:
                    snippets.append(text)
            elif part.part_type == "tool_output":
                output = str(payload.get("output", "") or "").strip()
                if output:
                    snippets.append(f"Tool output [{payload.get('tool_name', 'tool')}]\n{output}")
            elif part.part_type == "subtask_result":
                summary = str(payload.get("summary", "") or "").strip()
                if summary:
                    snippets.append(summary)
        return "\n\n".join(snippets).strip()

    def _should_append_resume_user_turn(self, restored: list[dict[str, Any]], user_message: str) -> bool:
        normalized = " ".join(str(user_message or "").split())
        if not normalized:
            return False
        if not restored:
            return True
        latest = restored[-1]
        if latest.get("role") != "user":
            return True
        latest_normalized = " ".join(str(latest.get("content", "") or "").split())
        return latest_normalized != normalized

    def _dedupe_transcript_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        for message in messages:
            fingerprint = hashlib.sha1(
                json.dumps(message, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if deduped:
                previous = deduped[-1]
                previous_fp = hashlib.sha1(
                    json.dumps(previous, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                if previous_fp == fingerprint:
                    continue
            deduped.append(message)
        return deduped

    def _restore_task_ledger(self, task: Task | None) -> list[dict[str, Any]]:
        if not task:
            return []
        runtime_resume = self._runtime_resume_payload(task)
        ledger = runtime_resume.get("task_ledger")
        if not isinstance(ledger, list):
            ledger = dict(task.metadata.get("runtime_v2", {}) or {}).get("task_ledger", [])
        return self._normalize_todos(ledger)

    def _start_runtime_prefetch(
        self,
        task: Task | None,
        messages: list[dict[str, Any]],
    ) -> _RuntimePrefetchHandle | None:
        prefetch_cfg = self.config.system.native_runtime.prefetch
        if not prefetch_cfg.enabled or task is None or self.prefetch_provider is None:
            return None
        query = self._build_prefetch_query(messages)
        if not query:
            return None
        return _RuntimePrefetchHandle(
            task=asyncio.create_task(self.prefetch_provider(task, query, list(messages))),
            query=query,
        )

    async def _consume_ready_prefetch(
        self,
        handle: _RuntimePrefetchHandle | None,
        *,
        messages: list[dict[str, Any]],
        base_prefix_len: int,
        runtime_session_id: str,
        task: Task | None,
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        if handle is None or handle.consumed or not handle.task.done():
            return messages, base_prefix_len, []
        try:
            payload = handle.task.result() or {}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Runtime prefetch failed: {exc}")
            handle.consumed = True
            return messages, base_prefix_len, []
        prefetch_parts: list[str] = []
        hits: list[str] = []
        labels = {
            "session_memory": "Session Memory",
            "focused_memory": "Focused Memory",
            "project_memory_candidates": "Project Memory Candidates",
            "skills_summary": "Skills Summary",
        }
        for key, value in payload.items():
            text = str(value or "").strip()
            if not text:
                continue
            prefetch_parts.append(f"## {labels.get(key, key.replace('_', ' ').title())}\n{text}")
            hits.append(key)
        handle.consumed = True
        if not prefetch_parts:
            return messages, base_prefix_len, []
        content = "## Runtime Prefetch\n" + "\n\n".join(prefetch_parts)
        prefetch_message = {"role": "system", "content": content}
        insert_at = base_prefix_len - 1 if base_prefix_len > 1 else 1
        updated_messages = [
            *messages[:insert_at],
            prefetch_message,
            *messages[insert_at:],
        ]
        await self._emit_runtime_event(
            runtime_session_id,
            task,
            "prefetch_consumed",
            {"query": handle.query[:500], "hits": hits},
        )
        return updated_messages, base_prefix_len + 1, hits

    @staticmethod
    def _cancel_prefetch(handle: _RuntimePrefetchHandle | None) -> None:
        if handle is not None and not handle.task.done():
            handle.task.cancel()

    def _build_prefetch_query(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for message in reversed(messages):
            role = str(message.get("role", "") or "")
            if role not in {"user", "assistant"}:
                continue
            content = str(message.get("content", "") or "").strip()
            if not content:
                continue
            parts.append(content[:800])
            if len(parts) >= 2:
                break
        return "\n\n".join(reversed(parts)).strip()

    async def _maybe_start_streaming_tool_calls(
        self,
        *,
        upto_index: int,
        tool_call_chunks: dict[int, dict[str, Any]],
        early_tool_runs: dict[int, dict[str, Any]],
        executor: StreamingToolExecutor,
        planner: ToolPlanner,
        permission_resolver: RuntimePermissionAdapter,
        task: Task | None,
        on_progress: Any,
        runtime_session_id: str,
    ) -> None:
        config = self.config.system.native_runtime.streaming_tool_start
        if not config.enabled:
            return
        for index in sorted(tool_call_chunks):
            if index >= upto_index or index in early_tool_runs:
                continue
            call = self._finalize_tool_calls({index: tool_call_chunks[index]})[0]
            if call.get("arguments_parse_error"):
                continue
            if not self._can_stream_start_tool_call(
                planner=planner,
                permission_resolver=permission_resolver,
                call=call,
                task=task,
            ):
                continue
            early_tool_runs[index] = {
                "call": call,
                "task": self._early_tool_group().create_task(
                    executor.execute([call], task=task, on_progress=on_progress)
                ),
            }
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "streaming_tool_started",
                {"tool_call_id": call.get("id", ""), "tool_name": call.get("function", "")},
            )

    def _can_stream_start_tool_call(
        self,
        *,
        planner: ToolPlanner,
        permission_resolver: RuntimePermissionAdapter,
        call: dict[str, Any],
        task: Task | None,
    ) -> bool:
        config = self.config.system.native_runtime.streaming_tool_start
        tool = self.tools.get(str(call.get("function", "") or ""))
        if tool is None:
            return False
        if config.safe_read_only_only and not planner.is_concurrency_safe(tool):
            return False
        if config.safe_read_only_only and not planner.is_read_only(tool):
            return False
        if not config.safe_read_only_only and not planner.is_concurrency_safe(tool):
            return False
        if config.require_allow_prediction:
            decision = permission_resolver.predicted_decision(tool, dict(call.get("arguments", {}) or {}), task=task)
            if decision.resolution != PermissionResolution.ALLOW:
                return False
        return not bool(getattr(tool, "requires_confirmation", False))

    async def _collect_execution_results(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        early_tool_runs: dict[int, dict[str, Any]],
        executor: StreamingToolExecutor,
        task: Task | None,
        on_progress: Any,
    ) -> list[dict[str, Any]]:
        ordered_results: list[dict[str, Any]] = []
        remaining_calls: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for index, call in enumerate(tool_calls):
            early = early_tool_runs.get(index)
            if not early:
                remaining_calls.append(call)
                continue
            early_call = dict(early.get("call", {}) or {})
            if not self._same_tool_call_signature(call, early_call):
                if not early["task"].done():
                    early["task"].cancel()
                    await asyncio.gather(early["task"], return_exceptions=True)
                remaining_calls.append(call)
                continue
            result_items = await early["task"]
            if result_items:
                by_id[str(call.get("id", "") or "")] = result_items[0]
        if remaining_calls:
            for item in await executor.execute(remaining_calls, task=task, on_progress=on_progress):
                by_id[str(item.get("tool_call", {}).get("id", "") or "")] = item
        for call in tool_calls:
            item = by_id.get(str(call.get("id", "") or ""))
            if item is not None:
                ordered_results.append(item)
        return ordered_results

    def _same_tool_call_signature(self, current: dict[str, Any], started: dict[str, Any]) -> bool:
        if str(current.get("function", "") or "") != str(started.get("function", "") or ""):
            return False
        current_args = dict(current.get("arguments", {}) or {})
        started_args = dict(started.get("arguments", {}) or {})
        return json.dumps(current_args, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
            started_args,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _finalize_tool_calls(self, chunks: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        finalized: list[dict[str, Any]] = []
        for index in sorted(chunks.keys()):
            item = chunks[index]
            raw_arguments = "".join(item.get("arguments_chunks", []))
            parsed_arguments: Any = {}
            parse_error: str | None = None
            if raw_arguments.strip():
                try:
                    parsed_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    parsed_arguments = raw_arguments
                    parse_error = f"Invalid tool arguments JSON for `{item.get('function', '')}`: {exc}"
            # Valid JSON that is not an object (e.g. ``[...]`` or ``"x"``) must surface as a
            # parse error too. Otherwise downstream code sees an empty ``arguments`` dict and
            # a falsy ``arguments_parse_error``, executing the tool with no arguments (which
            # can silently wipe state, e.g. todo_write receiving an empty list). Keep the more
            # specific JSONDecodeError message when parsing itself failed.
            if (
                raw_arguments.strip()
                and parse_error is None
                and not isinstance(parsed_arguments, dict)
            ):
                parse_error = (
                    f"Tool arguments for `{item.get('function', '')}` must be a JSON object; "
                    f"got {type(parsed_arguments).__name__}."
                )
            finalized.append({
                "id": item.get("id") or f"tool_{index}",
                "function": item.get("function") or "",
                "arguments": parsed_arguments if isinstance(parsed_arguments, dict) else {},
                "arguments_raw": raw_arguments,
                "arguments_parse_error": parse_error,
            })
        return finalized

    def _early_tool_group(self) -> _TurnEarlyTools:
        group = getattr(self, "_turn_early_tools", None)
        if group is None:
            group = _TurnEarlyTools()
            self._turn_early_tools = group
        return group

    async def _join_early_tools(self) -> None:
        group = getattr(self, "_turn_early_tools", None)
        if group is not None:
            await group.cancel_join()

    @staticmethod
    async def _cancel_early_tool_runs(early_tool_runs: dict[int, dict[str, Any]]) -> None:
        tasks: list[asyncio.Task[Any]] = []
        for item in early_tool_runs.values():
            task = item.get("task")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _tool_call_chunks_from_response(self, tool_calls: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
        chunks: dict[int, dict[str, Any]] = {}
        for index, call in enumerate(tool_calls or []):
            raw_arguments = call.get("arguments_raw")
            if raw_arguments is None:
                arguments = call.get("arguments", {})
                if isinstance(arguments, str):
                    raw_arguments = arguments
                else:
                    raw_arguments = json.dumps(arguments, ensure_ascii=False, default=str)
            chunks[index] = {
                "id": str(call.get("id", "") or "").strip(),
                "function": str(call.get("function", "") or "").strip(),
                "arguments_chunks": [str(raw_arguments or "")],
            }
        return chunks

    def _clean_tool_protocol_retry_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        base_prefix_len: int,
        strategy: str,
    ) -> list[dict[str, Any]]:
        if strategy == "sanitize":
            return self.llm.sanitize_tool_call_history(messages)
        prefix = self.llm.sanitize_tool_call_history(messages[:base_prefix_len])
        note = {
            "role": "system",
            "content": (
                "Previous tool-call transcript was reset after a provider tool-calling protocol failure. "
                "Continue from the current task state and regenerate any necessary tool calls cleanly."
            ),
        }
        return [*prefix, note]

    async def _recover_tool_protocol_stream_error(
        self,
        *,
        exc: Exception,
        messages: list[dict[str, Any]],
        prepare_request_tools: Callable[[int, bool], Awaitable[list[dict[str, Any]]]],
        base_prefix_len: int,
        runtime_session_id: str,
        task: Task | None,
        iteration: int,
        early_tool_runs: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self.llm.is_tool_protocol_error(exc):
            return None
        await self._cancel_early_tool_runs(early_tool_runs)
        last_error: Exception = exc
        attempted_payloads: list[list[dict[str, Any]]] = []
        for strategy in ("sanitize", "clean"):
            retry_messages = self._clean_tool_protocol_retry_messages(
                messages,
                base_prefix_len=base_prefix_len,
                strategy=strategy,
            )
            if attempted_payloads and retry_messages == attempted_payloads[-1]:
                continue
            attempted_payloads.append(retry_messages)
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "tool_protocol_retry",
                {
                    "iteration": iteration + 1,
                    "strategy": strategy,
                    "message": str(last_error),
                },
            )
            # After identical-payload suppression, but BEFORE the recovery
            # try/except. A failed snapshot write must escape immediately,
            # not be classified as a protocol error and fall through to clean.
            request_tools = await prepare_request_tools(
                iteration + 1, True,
            )
            try:
                from contextlib import nullcontext
                from ampuly.computer_use.desktop_frames import desktop_frame_task_scope, has_desktop_frame_task
                frame_scope = desktop_frame_task_scope(task, self.llm) if has_desktop_frame_task(task) else nullcontext()
                with model_retry_scope(), frame_scope:
                    response = await self.llm.chat(retry_messages, tools=request_tools or None)
                return {
                    "messages": retry_messages,
                    "assistant_text": str(response.get("content", "") or ""),
                    "tool_call_chunks": self._tool_call_chunks_from_response(response.get("tool_calls", [])),
                    "usage": dict(response.get("usage", {}) or {}),
                    "model": str(response.get("model", "") or ""),
                    "strategy": strategy,
                }
            except Exception as retry_exc:
                if isinstance(retry_exc, ModelBudgetStop):
                    raise
                quota_error = self._provider_quota_error(retry_exc)
                if quota_error is not None:
                    await self._emit_provider_quota_exhausted(
                        runtime_session_id=runtime_session_id,
                        task=task,
                        iteration=iteration,
                        error=retry_exc,
                    )
                    if quota_error is retry_exc:
                        raise
                    raise quota_error from retry_exc
                last_error = retry_exc
                if received_rejection(retry_exc) is None and not self.llm.is_tool_protocol_error(retry_exc):
                    # The recovery request went out and no answer came back (review R12): it may have been served,
                    # so nothing asks again -- the turn fails, marked, and nobody re-runs it automatically.
                    return {"outcome_unknown": retry_exc}
                if not self.llm.is_tool_protocol_error(retry_exc):
                    break
        return None

    def _provider_quota_error(
        self,
        error: Exception,
    ) -> ProviderQuotaExhaustedError | None:
        chain: list[Exception] = []
        pending: list[BaseException] = [error]
        seen: set[int] = set()
        while pending:
            current = pending.pop(0)
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, Exception):
                chain.append(current)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)

        for current in chain:
            if isinstance(current, ProviderQuotaExhaustedError):
                if current is error:
                    return current
                # Do not re-raise the nested instance from its wrapper: that
                # would mutate nested.__cause__ back to the wrapper and form
                # an exception-chain cycle (wrapper -> nested -> wrapper).
                return ProviderQuotaExhaustedError(str(current))

        rate_limit_checker = getattr(self.llm, "is_rate_limit_error", None)
        if callable(rate_limit_checker):
            for current in chain:
                if rate_limit_checker(current):
                    return ProviderQuotaExhaustedError(str(current))
        return None

    async def _emit_provider_quota_exhausted(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        iteration: int,
        error: Exception,
    ) -> None:
        try:
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "provider_quota_exhausted",
                {
                    "iteration": iteration + 1,
                    "message": str(error)[:600],
                },
            )
        except Exception:
            # Telemetry is best-effort at this recovery boundary. The
            # dispatcher needs the typed exception to return the claimed
            # WorkItem to READY; an event sink outage must not replace it.
            logger.opt(exception=True).warning(
                "Failed to emit provider_quota_exhausted runtime event"
            )

    @staticmethod
    def _provider_error_feedback_message(
        exc: Exception,
        *,
        context_reset: bool = False,
    ) -> dict[str, str]:
        """Conversation message telling the model why the last request failed.

        Unclassified provider rejections (content filters, transient 4xx) never
        produce model output, so without this the model has no way to know the
        request failed or why. Feeding the provider's own error text back lets
        the model decide how to proceed (rephrase, drop a quote, change tack)
        instead of the runtime blindly replaying an identical payload.
        """
        error_text = " ".join(str(exc).split())[:600]
        if context_reset:
            detail = (
                "The previous LLM request kept failing at the model provider, so the "
                "intermediate steps of the current turn were dropped from the request."
            )
        else:
            detail = (
                "The previous LLM request failed at the model provider before any "
                "output was produced."
            )
        return {
            "role": "system",
            "content": (
                f"[runtime notice] {detail} Provider error: {error_text}. "
                "This was not a user action. Adjust your next step accordingly — for "
                "example rephrase sensitive wording, avoid quoting flagged content "
                "verbatim, or choose another way to make progress — then continue the task."
            ),
        }

    def _truncate_to_last_clean_user_turn(
        self,
        messages: list[dict[str, Any]],
        base_prefix_len: int,
    ) -> list[dict[str, Any]] | None:
        """Walk backwards to find the last role=user message and truncate there,
        then sanitize to ensure no unpaired tool_call remains."""
        for i in range(len(messages) - 1, base_prefix_len - 1, -1):
            if messages[i].get("role") == "user":
                candidate = messages[: i + 1]
                return self.llm.sanitize_tool_call_history(candidate)
        return None

    async def _apply_context_pipeline(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_schemas: list[dict[str, Any]] | None,
        task: Task | None,
        base_prefix_len: int,
        runtime_session_id: str,
        compaction_boundaries: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        active_subagents: list[dict[str, Any]],
        force_compact: bool = False,
        observed_tokens: int = 0,
    ) -> list[dict[str, Any]]:
        # History below the hard threshold is never rewritten: model quality
        # and prompt-cache prefixes both depend on old messages staying
        # byte-identical. The only routine mutation is the idempotent
        # per-message tool-result budget (same clip an entry already got).
        bounded = self._apply_tool_result_budget(messages)
        pipeline_steps = ["tool_result_budgeting"]
        compacted = bounded
        durable_applied = False
        wants_compaction = force_compact or self._should_apply_hard_compaction(
            bounded, tool_schemas, observed_tokens=observed_tokens, task=task
        )
        if wants_compaction:
            breaker_limit = max(
                1,
                int(self.config.system.native_runtime.reactive_compaction.circuit_breaker_failures or 2),
            )
            failures = int(runtime_notes.get("durable_compaction_failures", 0) or 0)
            if failures < breaker_limit:
                compacted, durable_applied = await self._apply_durable_compaction(
                    bounded,
                    task=task,
                    base_prefix_len=base_prefix_len,
                    runtime_session_id=runtime_session_id,
                )
                if durable_applied:
                    pipeline_steps.append("durable_compaction")
                    runtime_notes["durable_compaction_failures"] = 0
                else:
                    runtime_notes["durable_compaction_failures"] = failures + 1
            if not durable_applied and force_compact:
                # Emergency-only mechanical fallback: overflow pressure with
                # the summarizing compactor unavailable or circuit-broken.
                compacted = self._apply_tool_aware_microcompact(compacted, base_prefix_len)
                pipeline_steps.append("emergency_microcompact")
        if compacted != bounded:
            boundary_record = {
                "summary": "Runtime V2 context pipeline compacted persisted history.",
                "message_count": len(compacted),
                "pipeline": [*pipeline_steps, "session_memory_reinjection"],
            }
            compaction_boundaries.append(boundary_record)
            store = getattr(self.memory_manager, "store", None)
            if store and hasattr(store, "save_runtime_compaction_boundary"):
                await store.save_runtime_compaction_boundary(
                    boundary_id=f"cb_{uuid.uuid4().hex}",
                    runtime_session_id=runtime_session_id,
                    task_id=task.id if task else None,
                    summary=boundary_record["summary"],
                    metadata={"message_count": len(compacted)},
                )
            await self._persist_compaction_boundary(task, boundary_record, runtime_session_id)
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "compaction_applied",
                {"message_count": len(compacted)},
            )
        reinjected = await self._reinject_session_memory(compacted, task=task)
        artifact_reinjected = self._reinject_runtime_artifacts(
            reinjected,
            task=task,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        return self._sanitize_tool_message_sequence(self._dedupe_transcript_messages(artifact_reinjected))

    def _apply_tool_result_budget(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        guard_budget = int(self.config.system.native_runtime.context_guard.tool_output_char_budget or 12_000)
        runtime_budget = int(self.config.system.native_runtime.tool_result_budget_chars or 20_000)
        budget = min(runtime_budget, guard_budget)
        compacted: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "tool":
                content = str(message.get("content", "") or "")
                if len(content) > budget:
                    # Keep head and tail: openings carry the command/context,
                    # endings carry the verdict (exit codes, tracebacks).
                    head = max(1, budget // 2)
                    tail = max(0, budget - head)
                    omitted = len(content) - head - tail
                    compacted.append({
                        **message,
                        "content": (
                            content[:head]
                            + f"\n[tool result truncated by runtime_v2: {omitted} chars omitted]\n"
                            + (content[-tail:] if tail else "")
                        ),
                    })
                    continue
            compacted.append(message)
        return compacted

    def _apply_tool_aware_microcompact(self, messages: list[dict[str, Any]], base_prefix_len: int) -> list[dict[str, Any]]:
        config = self.config.system.native_runtime.tool_aware_microcompact
        if not config.enabled:
            return self._apply_microcompact(self._apply_history_snip(messages, base_prefix_len), base_prefix_len)
        preserve_recent = max(4, int(config.preserve_recent_messages or 8))
        if len(messages) <= base_prefix_len + preserve_recent:
            return messages
        preserved_tail_start = max(base_prefix_len, len(messages) - preserve_recent)
        compacted: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index < base_prefix_len or index >= preserved_tail_start:
                compacted.append(message)
                continue
            role = str(message.get("role", "") or "")
            if role == "tool":
                compacted.append({
                    **message,
                    "content": self._summarize_tool_message_content(
                        str(message.get("content", "") or ""),
                        budget=int(config.tool_result_char_budget or 4000),
                        preserve_failure_outputs=bool(config.preserve_failure_outputs),
                    ),
                })
                continue
            if role == "assistant":
                compacted.append({
                    **message,
                    "content": self._truncate_text_block(
                        str(message.get("content", "") or ""),
                        budget=int(config.assistant_char_budget or 3000),
                        marker="[assistant context microcompacted]",
                    ),
                })
                continue
            if role == "system":
                compacted.append({
                    **message,
                    "content": self._truncate_text_block(
                        str(message.get("content", "") or ""),
                        budget=int(config.assistant_char_budget or 3000),
                        marker="[system context microcompacted]",
                    ),
                })
                continue
            compacted.append(message)
        return self._apply_history_snip(compacted, base_prefix_len)

    def _summarize_tool_message_content(
        self,
        content: str,
        *,
        budget: int,
        preserve_failure_outputs: bool,
    ) -> str:
        if len(content) <= budget:
            return content
        try:
            parsed = json.loads(content)
        except Exception:
            return self._truncate_text_block(content, budget=budget, marker="[tool result microcompacted]")
        if not isinstance(parsed, dict):
            return self._truncate_text_block(content, budget=budget, marker="[tool result microcompacted]")
        is_failure = not bool(parsed.get("success", True)) or bool(parsed.get("error"))
        if is_failure and preserve_failure_outputs:
            return self._truncate_text_block(content, budget=max(budget, 6000), marker="[tool failure output truncated]")
        result_payload = parsed.get("result", {})
        preview_fields: dict[str, Any] = {
            "success": parsed.get("success", True),
        }
        if parsed.get("error"):
            preview_fields["error"] = str(parsed.get("error", ""))[:800]
        if isinstance(result_payload, dict):
            for key in ("rendered", "summary", "stdout", "stderr", "content", "todos", "task_ledger"):
                value = result_payload.get(key)
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, str):
                    preview_fields[key] = clip_text(
                        value,
                        limit=1200,
                        marker=f"{key} preview truncated",
                    ).text
                elif isinstance(value, list):
                    preview_fields[key] = value[:6]
                elif isinstance(value, dict):
                    preview_fields[key] = {
                        inner_key: inner_value
                        for inner_key, inner_value in list(value.items())[:6]
                    }
                else:
                    preview_fields[key] = value
        rendered = json.dumps(preview_fields, ensure_ascii=False, default=str)
        if len(rendered) <= budget:
            return rendered + "\n[tool result microcompacted]"
        return self._truncate_text_block(rendered, budget=budget, marker="[tool result microcompacted]")

    @staticmethod
    def _truncate_text_block(content: str, *, budget: int, marker: str) -> str:
        marker_text = marker.strip("[]")
        clip = clip_text(content, limit=budget, marker=marker_text)
        exact_marker = f"[{marker_text}]"
        detailed_marker = f"[{marker_text}:"
        if clip.truncated and exact_marker not in clip.text and detailed_marker in clip.text:
            return clip.text.replace(detailed_marker, f"{exact_marker}\n{detailed_marker}", 1)
        return clip.text

    def _apply_history_snip(self, messages: list[dict[str, Any]], base_prefix_len: int) -> list[dict[str, Any]]:
        trigger = int(self.config.system.native_runtime.history_snip_trigger_messages or 40)
        if len(messages) <= trigger or len(messages) <= base_prefix_len + 12:
            return messages
        preserved_tail = messages[-12:]
        hidden = len(messages) - base_prefix_len - len(preserved_tail)
        if hidden <= 0:
            return messages
        return [
            *messages[:base_prefix_len],
            {
                "role": "system",
                "content": f"[runtime_v2 snip] {hidden} earlier messages hidden after transcript persistence.",
            },
            *preserved_tail,
        ]

    def _apply_microcompact(self, messages: list[dict[str, Any]], base_prefix_len: int) -> list[dict[str, Any]]:
        limit = int(self.config.system.native_runtime.microcompact_chars or 8_000)
        if not limit or len(messages) <= base_prefix_len + 8:
            return messages
        preserved_tail_start = max(base_prefix_len, len(messages) - 8)
        compacted: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index < base_prefix_len or index >= preserved_tail_start:
                compacted.append(message)
                continue
            content = str(message.get("content", "") or "")
            if len(content) <= limit:
                compacted.append(message)
                continue
            compacted.append({
                **message,
                "content": content[:limit] + "\n[runtime_v2 microcompact]",
            })
        return compacted

    _DURABLE_COMPACTION_MARKER = "[runtime_v2 durable compaction]"

    async def _apply_durable_compaction(
        self,
        messages: list[dict[str, Any]],
        *,
        task: Task | None,
        base_prefix_len: int,
        runtime_session_id: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fold old messages into one LLM summary, keeping prefix and tail.

        Returns (messages, applied). On any summarizer failure the original
        list is returned unchanged so the caller can count failures and the
        model keeps seeing the full history for this round.
        """
        compactor = self.history_compactor
        summarize = getattr(compactor, "summarize_runtime_history", None) if compactor else None
        if not callable(summarize):
            return messages, False
        preserve_recent = max(
            4,
            int(self.config.system.native_runtime.tool_aware_microcompact.preserve_recent_messages or 8),
        )
        start = max(base_prefix_len, len(messages) - preserve_recent)
        # Never split an assistant tool_calls message from its tool results.
        while start > base_prefix_len and str(messages[start].get("role", "") or "") == "tool":
            start -= 1
        # base_prefix_len goes stale once session-memory/artifact messages are
        # injected into the prefix region, shifting real prefix messages past
        # the boundary. Never fold the system head, and keep the seed user
        # request verbatim on every round (Codex-style): a previous summary is
        # a user message too, but carries the marker and must stay foldable so
        # exactly one summary exists at a time.
        fold_start = base_prefix_len
        while fold_start < start and str(messages[fold_start].get("role", "") or "") == "system":
            fold_start += 1
        if (
            fold_start < start
            and str(messages[fold_start].get("role", "") or "") == "user"
            and self._DURABLE_COMPACTION_MARKER not in str(messages[fold_start].get("content", "") or "")
            and not any(str(item.get("role", "") or "") == "user" for item in messages[:fold_start])
        ):
            fold_start += 1
        folded = messages[fold_start:start]
        if len(folded) < 4:
            return messages, False
        try:
            summary = await summarize(
                project_id=str(getattr(task, "project_id", "") or ""),
                session_id=runtime_session_id,
                messages=self._render_messages_for_compaction(folded),
                # Folding a history is a paid call, made on this task's
                # behalf and on every long turn. Without this the turn's
                # recorded cost is the answer minus the summarising.
                meter=meter_for_task(self.cost_tracker, task),
                # ... and on this turn's own provider, which _run_native_agent already narrowed to the step's
                # model or tier. The compactor is one process-wide instance built on the engine's default, so
                # without this a strong step's history would be folded by whatever the default happens to be.
                llm=getattr(self, "llm", None),
            )
        except Exception as exc:
            logger.warning(f"Durable compaction failed; keeping full history this round: {exc}")
            if isinstance(exc, ModelBudgetStop) and not isinstance(exc, RouteRefused):
                raise
            # A summarising call refused before it was sent (its key, its model) costs nothing and stops nothing:
            # the turn goes on with its full history, as on any other failed summary.
            return messages, False
        summary_text = str(summary or "").strip()
        if not summary_text:
            return messages, False
        summary_message = {
            "role": "user",
            "content": (
                f"{self._DURABLE_COMPACTION_MARKER} Earlier conversation was compacted to stay "
                "within the context window. Continue seamlessly from this summary; the full "
                "transcript remains persisted and queryable.\n\n" + summary_text
            ),
        }
        candidate = [*messages[:fold_start], summary_message, *messages[start:]]
        # A summary is a candidate, not proof of successful compaction. Keep
        # the original view if it grows; the caller counts this as a failure
        # under the existing bounded breaker (no unbounded paid summarising).
        # Counters may be unavailable/estimated, so also require a smaller
        # deterministic serialized view. Neither measurement is billing.
        try:
            before_size = len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8"))
            after_size = len(json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8"))
        except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
            logger.warning("Durable compaction candidate could not be measured; preserving original history")
            return messages, False
        before_tokens = self._safe_count_input_tokens(messages, None)
        after_tokens = self._safe_count_input_tokens(candidate, None)
        if after_size >= before_size or (before_tokens > 0 and after_tokens > before_tokens):
            logger.warning("Durable compaction candidate did not reduce context; preserving original history")
            return messages, False
        return candidate, True

    def _render_messages_for_compaction(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        per_message_budget = 4_000
        rendered: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role", "") or "assistant")
            content = str(message.get("content", "") or "")
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                names: list[str] = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function", "")
                    name = function.get("name", "") if isinstance(function, dict) else str(function or "")
                    if name:
                        names.append(str(name))
                if names:
                    content = (content + "\n[called tools: " + ", ".join(names) + "]").strip()
            if role == "tool":
                content = f"[tool result {str(message.get('tool_call_id', '') or '')}] {content}".strip()
            if len(content) > per_message_budget:
                head = per_message_budget // 2
                tail = per_message_budget - head - 100
                content = (
                    content[:head]
                    + f"\n[{len(content) - head - tail} chars omitted]\n"
                    + content[-tail:]
                )
            if content:
                rendered.append({"role": role, "content": content})
        return rendered

    async def _reinject_session_memory(
        self,
        messages: list[dict[str, Any]],
        *,
        task: Task | None,
    ) -> list[dict[str, Any]]:
        if not task or not self.memory_manager or not task.session_id:
            return messages
        session_memory = await self.memory_manager.build_session_memory_context(task.session_id)
        if not session_memory.strip():
            return messages
        memory_message = {"role": "system", "content": session_memory}
        if len(messages) >= 2 and messages[1].get("role") == "system" and "## Session Memory" in str(messages[1].get("content", "")):
            return [messages[0], memory_message, *messages[2:]]
        return [messages[0], memory_message, *messages[1:]]

    def _boot_artifact_manifest(self, task: Task | None) -> list[dict[str, Any]]:
        if task is None:
            return []
        manifest = list(task.metadata.get("_prompt_harness_boot_artifacts", []) or [])
        return [dict(item) for item in manifest if isinstance(item, dict) and str(item.get("type", "") or "").strip()]

    def _boot_artifact_hashes(self, task: Task | None) -> dict[str, str]:
        if task is None:
            return {}
        harness_meta = dict(task.metadata.get("prompt_harness", {}) or {})
        hashes = dict(harness_meta.get("artifact_hashes", {}) or {})
        return {str(key): str(value) for key, value in hashes.items() if str(key).strip() and str(value).strip()}

    def _compact_artifact_text(self, content: str) -> str:
        budget = max(800, int(self.config.system.native_runtime.artifact_compaction.artifact_char_budget or 12_000))
        text = str(content or "").strip()
        if len(text) <= budget:
            return text
        return text[: max(120, budget - 40)].rstrip() + "\n[runtime artifact truncated]"

    def _task_ledger_artifact_record(self, todo_state: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not todo_state:
            return None
        lines = ["Current task ledger:"]
        for item in todo_state[:12]:
            status = str(item.get("status", "") or "pending").strip() or "pending"
            content = str(item.get("content", "") or item.get("title", "") or "").strip()
            active = str(item.get("active_form", "") or item.get("activeForm", "") or "").strip()
            if not content:
                continue
            line = f"- [{status}] {content}"
            if active and active != content:
                line += f" | active: {active}"
            lines.append(line)
        return build_runtime_artifact_record(
            "task_ledger",
            "Task Ledger",
            self._compact_artifact_text("\n".join(lines)),
            metadata={"item_count": len(todo_state)},
        )

    def _plan_artifact_record(self, task: Task | None) -> dict[str, Any] | None:
        if task is None:
            return None
        plan = task.metadata.get("work_item_runtime_plan")
        if not plan:
            return None
        return build_runtime_artifact_record(
            "plan_attachment",
            "Plan",
            self._compact_artifact_text(str(plan)),
            metadata={"source": "work_item_runtime_plan"},
        )

    def _skills_artifact_record(self, task: Task | None) -> dict[str, Any] | None:
        if task is None:
            return None
        manifest = self._boot_artifact_manifest(task)
        for item in manifest:
            if str(item.get("type", "") or "").strip() == "skills_delta":
                return dict(item)
        return None

    def _tool_surface_artifact_record(self, task: Task | None) -> dict[str, Any] | None:
        if task is None:
            return None
        manifest = self._boot_artifact_manifest(task)
        for item in manifest:
            if str(item.get("type", "") or "").strip() == "tool_surface_delta":
                return dict(item)
        return None

    def _active_subagents_artifact_record(self, active_subagents: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not active_subagents or not self.config.system.native_runtime.artifact_compaction.reinject_active_subagents:
            return None
        lines = ["Active subagents:"]
        for item in active_subagents[:8]:
            name = str(item.get("name", "") or item.get("agent_id", "") or "subagent").strip()
            status = str(item.get("status", "") or "unknown").strip()
            description = str(item.get("description", "") or "").strip()
            worktree_path = str(item.get("worktree_path", "") or "").strip()
            line = f"- {name}: {status}"
            if description:
                line += f" | {description}"
            if worktree_path:
                line += f" | worktree={worktree_path}"
            lines.append(line)
        return build_runtime_artifact_record(
            "active_subagents",
            "Active Subagents",
            self._compact_artifact_text("\n".join(lines)),
            metadata={"item_count": len(active_subagents)},
        )

    def _verification_artifact_record(self, runtime_notes: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.system.native_runtime.artifact_compaction.reinject_verification_state:
            return None
        verification = dict(runtime_notes.get("verification", {}) or {})
        if not verification:
            return None
        lines = [
            f"- Completed: {bool(verification.get('completed', False))}",
            f"- Passed: {bool(verification.get('passed', False))}",
            f"- Profile: {str(verification.get('profile', '') or '').strip()}",
        ]
        verdict = str(verification.get("verdict", "") or "").strip()
        if verdict:
            lines.append(f"- Verdict: {verdict}")
        status_line = str(verification.get("status_line", "") or "").strip()
        if status_line:
            lines.append(f"- Status line: {status_line}")
        return build_runtime_artifact_record(
            "verification_state",
            "Verification",
            self._compact_artifact_text("Verification state:\n" + "\n".join(lines)),
            metadata={"completed": bool(verification.get("completed", False))},
        )

    def _permission_artifact_record(self, runtime_notes: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.system.native_runtime.artifact_compaction.reinject_permission_state:
            return None
        details = list(runtime_notes.get("permission_details", []) or [])
        if not details:
            return None
        lines = ["Recent permission decisions:"]
        for item in details[-8:]:
            tool_name = str(item.get("tool_name", "") or "tool").strip()
            resolution = str(item.get("resolution", "") or "allow").strip()
            risk_level = str(item.get("risk_level", "") or "low").strip()
            source = str(item.get("source", "") or "").strip()
            rationale = str(item.get("rationale", "") or "").strip()
            line = f"- {tool_name}: {resolution} | risk={risk_level}"
            if source:
                line += f" | source={source}"
            if rationale:
                line += f" | {rationale}"
            lines.append(line)
        return build_runtime_artifact_record(
            "permission_state",
            "Permission State",
            self._compact_artifact_text("\n".join(lines)),
            metadata={"item_count": len(details)},
        )

    def _worktree_artifact_record(
        self,
        task: Task | None,
        active_subagents: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        path = self._primary_worktree_path(active_subagents)
        if not path and task is not None:
            path = str(task.metadata.get("target_output_dir", "") or "").strip()
        if not path:
            return None
        return build_runtime_artifact_record(
            "worktree_state",
            "Worktree",
            f"Active worktree path:\n- {path}",
            metadata={"path": path},
        )

    def _resume_artifact_record(
        self,
        *,
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        resume_state = self._build_resume_state(
            messages=messages,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        lines = [
            f"- Message count: {resume_state.get('message_count', 0)}",
            f"- Task ledger items: {resume_state.get('task_ledger_items', 0)}",
            f"- Compaction records: {len(resume_state.get('compaction_records', []) or [])}",
            f"- Active subagents: {resume_state.get('active_subagents', 0)}",
        ]
        verification_status = str(resume_state.get("verification_status", "") or "").strip()
        if verification_status:
            lines.append(f"- Verification: {verification_status}")
        return build_runtime_artifact_record(
            "resume_state",
            "Resume State",
            self._compact_artifact_text("Runtime resume envelope:\n" + "\n".join(lines)),
            metadata=resume_state,
        )

    @staticmethod
    def _merge_artifact_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            artifact_type = str(record.get("type", "") or "").strip()
            if not artifact_type:
                continue
            if artifact_type not in order:
                order.append(artifact_type)
            merged[artifact_type] = dict(record)
        return [merged[item] for item in order if item in merged]

    def _compose_runtime_artifact_manifest(
        self,
        *,
        task: Task | None,
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.config.system.native_runtime.artifact_compaction.enabled:
            return list(runtime_notes.get("artifact_manifest", []) or [])
        records: list[dict[str, Any]] = [
            *list(runtime_notes.get("artifact_manifest", []) or []),
            *list(self._boot_artifact_manifest(task)),
        ]
        for candidate in (
            self._task_ledger_artifact_record(todo_state),
            self._plan_artifact_record(task),
            self._tool_surface_artifact_record(task) if self.config.system.native_runtime.artifact_compaction.reinject_tool_surface_delta else None,
            self._skills_artifact_record(task) if self.config.system.native_runtime.artifact_compaction.reinject_skills_delta else None,
            self._active_subagents_artifact_record(active_subagents),
            self._verification_artifact_record(runtime_notes),
            self._permission_artifact_record(runtime_notes),
            self._worktree_artifact_record(task, active_subagents),
            self._resume_artifact_record(
                messages=messages,
                todo_state=todo_state,
                runtime_notes=runtime_notes,
                compaction_boundaries=compaction_boundaries,
                active_subagents=active_subagents,
            ),
        ):
            if candidate is not None:
                records.append(candidate)
        merged = self._merge_artifact_records(records)
        runtime_notes["artifact_manifest"] = list(merged)
        runtime_notes["artifact_hashes"] = {
            str(item.get("type", "") or ""): str(item.get("content_hash", "") or "")
            for item in merged
            if str(item.get("type", "") or "").strip()
        }
        return merged

    def _record_to_artifact(self, record: dict[str, Any]) -> RuntimeArtifact | None:
        artifact_type = str(record.get("type", "") or "").strip()
        title = str(record.get("title", "") or "").strip()
        content = self._compact_artifact_text(str(record.get("content", "") or "").strip())
        if not artifact_type or not title or not content:
            return None
        metadata = dict(record.get("metadata", {}) or {})
        if record.get("content_hash"):
            metadata["content_hash"] = str(record.get("content_hash", "") or "")
        return RuntimeArtifact(
            artifact_type=artifact_type,
            title=title,
            content=content,
            scope=str(record.get("scope", "runtime") or "runtime"),
            metadata=metadata,
        )

    def _reinject_runtime_artifacts(
        self,
        messages: list[dict[str, Any]],
        *,
        task: Task | None,
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cfg = self.config.system.native_runtime.prompt_harness
        if not cfg.enabled or not cfg.artifact_messages_enabled or not cfg.reinject_after_compaction:
            return messages
        manifest = self._compose_runtime_artifact_manifest(
            task=task,
            messages=messages,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        artifacts = [artifact for item in manifest if (artifact := self._record_to_artifact(item)) is not None]
        artifact_messages = render_runtime_artifact_messages(
            artifacts,
            previous_hashes=self._boot_artifact_hashes(task),
            emit_delta_messages=cfg.emit_delta_messages,
        )
        if not artifact_messages:
            return strip_runtime_artifact_messages(messages)
        base_messages = strip_runtime_artifact_messages(messages)
        insert_at = 1
        while insert_at < len(base_messages) and str(base_messages[insert_at].get("role", "") or "") == "system":
            insert_at += 1
        return [
            *base_messages[:insert_at],
            *artifact_messages,
            *base_messages[insert_at:],
        ]

    def _handle_pause_or_peer_wait(
        self,
        execution_results: list[dict[str, Any]],
        aggregated_artifacts: dict[str, Any],
        runtime_session_id: str,
        *,
        task: Task | None,
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
    ) -> TaskResult | None:
        permission_requests = self._permission_requests_from_results(execution_results)
        worktree_path = self._primary_worktree_path(active_subagents)
        resume_state = self._build_resume_state(
            messages=messages,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        artifact_manifest = self._compose_runtime_artifact_manifest(
            task=task,
            messages=messages,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        verification_verdict = str(dict(runtime_notes.get("verification", {}) or {}).get("status_line", "") or "")
        for item in execution_results:
            call = item["tool_call"]
            result = item["result"]
            tool_payload = result.get("result", result if isinstance(result, dict) else {})
            if not isinstance(tool_payload, dict):
                tool_payload = {}
            if call.get("function") == "request_user_input" or tool_payload.get("requires_user_input"):
                reason = tool_payload.get("reason") or f"Need user input before continuing tool `{call.get('function', '')}`."
                return TaskResult(
                    status=TaskStatus.AWAITING_HUMAN,
                    content=reason,
                    artifacts={
                        **aggregated_artifacts,
                        "runtime_session_id": runtime_session_id,
                        "tool_name": call.get("function", ""),
                        "tool_args": call.get("arguments", {}),
                        "pause_request": tool_payload,
                        "permission_requests": permission_requests,
                        "active_subagents": active_subagents,
                        "compaction_boundaries": list(compaction_boundaries),
                        "compaction_records": list(compaction_boundaries),
                        "worktree_path": worktree_path,
                        "task_ledger": list(todo_state),
                        "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
                        "verification_verdict": verification_verdict,
                        "artifact_manifest": list(artifact_manifest),
                        "resume_state": resume_state,
                    },
                )
            if tool_payload.get("requires_peer_wait"):
                reason = tool_payload.get("reason") or f"Waiting for peer coordination for `{call.get('function', '')}`."
                return TaskResult(
                    status=TaskStatus.AWAITING_PEER,
                    content=reason,
                    artifacts={
                        **aggregated_artifacts,
                        "runtime_session_id": runtime_session_id,
                        "tool_name": call.get("function", ""),
                        "tool_args": call.get("arguments", {}),
                        "pause_request": tool_payload,
                        "permission_requests": permission_requests,
                        "active_subagents": active_subagents,
                        "compaction_boundaries": list(compaction_boundaries),
                        "compaction_records": list(compaction_boundaries),
                        "worktree_path": worktree_path,
                        "task_ledger": list(todo_state),
                        "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
                        "verification_verdict": verification_verdict,
                        "artifact_manifest": list(artifact_manifest),
                        "resume_state": resume_state,
                    },
                )
            approval = dict(result.get("approval", {}) or {})
            if approval.get("action") in {"require_input", "escalate"}:
                return TaskResult(
                    status=TaskStatus.AWAITING_HUMAN,
                    content=str(result.get("error", "") or "Awaiting approval."),
                    artifacts={
                        **aggregated_artifacts,
                        "runtime_session_id": runtime_session_id,
                        "tool_name": call.get("function", ""),
                        "tool_args": call.get("arguments", {}),
                        "approval": approval,
                        "permission_requests": permission_requests,
                        "active_subagents": active_subagents,
                        "compaction_boundaries": list(compaction_boundaries),
                        "compaction_records": list(compaction_boundaries),
                        "worktree_path": worktree_path,
                        "task_ledger": list(todo_state),
                        "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
                        "verification_verdict": verification_verdict,
                        "artifact_manifest": list(artifact_manifest),
                        "resume_state": resume_state,
                    },
                )
        return None

    async def _persist_task_ledger(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        messages: list[dict[str, Any]],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> None:
        ledger_cfg = self.config.system.native_runtime.task_ledger
        if not ledger_cfg.enabled:
            return
        normalized = self._normalize_todos(todo_state)[: max(1, int(ledger_cfg.max_items or 24))]
        runtime_notes["task_ledger"] = list(normalized)
        if task is not None and ledger_cfg.persist_to_task_metadata:
            task.metadata = dict(task.metadata)
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            runtime_meta["task_ledger"] = list(normalized)
            task.metadata["runtime_v2"] = runtime_meta
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["runtime_v2"] = runtime_meta
        if ledger_cfg.persist_to_runtime_session:
            await self._save_runtime_session(
                runtime_session_id,
                task,
                "running",
                self._build_runtime_state_metadata(
                    task=task,
                    messages=messages,
                    todo_state=normalized,
                    runtime_notes=runtime_notes,
                    compaction_boundaries=compaction_boundaries,
                    active_subagents=active_subagents,
                ),
            )
        if ledger_cfg.emit_runtime_events:
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "task_ledger_updated",
                {"task_ledger": list(normalized), "item_count": len(normalized)},
            )

    def _build_runtime_state_metadata(
        self,
        *,
        task: Task | None,
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        artifact_manifest = self._compose_runtime_artifact_manifest(
            task=task,
            messages=messages,
            todo_state=todo_state,
            runtime_notes=runtime_notes,
            compaction_boundaries=compaction_boundaries,
            active_subagents=active_subagents,
        )
        return {
            "task_ledger": list(todo_state),
            "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
            "verification": dict(runtime_notes.get("verification", {}) or {}),
            "verification_evidence": dict((runtime_notes.get("verification", {}) or {}).get("evidence", {}) or {}),
            "verification_verdict": str(dict(runtime_notes.get("verification", {}) or {}).get("status_line", "") or ""),
            "compaction_records": list(compaction_boundaries),
            "artifact_manifest": list(artifact_manifest),
            "resume_state": self._build_resume_state(
                messages=messages,
                todo_state=todo_state,
                runtime_notes=runtime_notes,
                compaction_boundaries=compaction_boundaries,
                active_subagents=active_subagents,
            ),
        }

    def _build_resume_state(
        self,
        *,
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
        compaction_boundaries: list[dict[str, Any]],
        active_subagents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "message_count": len(messages),
            "task_ledger_items": len(todo_state),
            "prefetch_hits": list(runtime_notes.get("prefetch_hits", []) or []),
            "compaction_records": list(compaction_boundaries),
            "active_subagents": len(active_subagents),
            "verification_evidence": dict((runtime_notes.get("verification", {}) or {}).get("evidence", {}) or {}),
            "verification_status": str(dict(runtime_notes.get("verification", {}) or {}).get("status_line", "") or ""),
            "artifact_count": len(list(runtime_notes.get("artifact_manifest", []) or [])),
            "artifact_types": [
                str(item.get("type", "") or "").strip()
                for item in list(runtime_notes.get("artifact_manifest", []) or [])
                if isinstance(item, dict) and str(item.get("type", "") or "").strip()
            ],
        }

    def _build_verification_prompt(
        self,
        task: Task | None,
        *,
        work_item_name: str = "",
        turn_type: str = "",
    ) -> str:
        scope = "the parent agent's work"
        if work_item_name:
            scope = f"the parent agent's company work item `{work_item_name}` ({turn_type or 'execute'})"
        contract = [
            f"Validate {scope}.",
            "You must produce executable verification evidence, not just an opinion.",
            "Required format:",
            "Check: <what you verified>",
            "Command: <exact command you ran>",
            "Observed Output: <what you saw>",
            "Result: PASS | FAIL",
            "Repeat for each check you actually ran.",
            "End with exactly one line: VERDICT: PASS or VERDICT: FAIL or VERDICT: PARTIAL.",
            "If the work is acceptable overall, you may begin the final summary with `VERIFIED:`.",
            "If blocking issues remain, begin the final summary with `ISSUES:`.",
            "Do not omit command/output evidence.",
        ]
        if task is not None and str(task.metadata.get("execution_mode", "") or "").strip() == "company_mode":
            contract.append(
                "Focus on regressions, missing evidence, handoff quality, ownership-contract violations, and risky filesystem or shell changes."
            )
        else:
            contract.append(
                "Focus on regressions, missed validation, and risky filesystem or shell changes."
            )
        return " ".join(contract)

    def _parse_verification_evidence(self, text: str) -> VerificationEvidence:
        raw = str(text or "").strip()
        if not raw:
            return VerificationEvidence(status="missing", raw_output=raw)
        lines = [line.rstrip() for line in raw.splitlines()]
        checks: list[dict[str, Any]] = []
        current: dict[str, str] = {}
        verdict = ""
        summary_lines: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            normalized_line = self._normalize_verification_contract_line(line)
            lowered = normalized_line.lower()
            if lowered.startswith("verdict:"):
                verdict_value = normalized_line.split(":", 1)[1].strip().lower()
                if verdict_value.startswith("pass"):
                    verdict = "pass"
                elif verdict_value.startswith("fail"):
                    verdict = "fail"
                elif verdict_value.startswith("partial"):
                    verdict = "partial"
                continue
            if lowered.startswith("check:"):
                if current:
                    checks.append(dict(current))
                    current = {}
                current["check"] = normalized_line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("command:"):
                current["command"] = normalized_line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("observed output:"):
                current["observed_output"] = normalized_line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("result:"):
                current["result"] = normalized_line.split(":", 1)[1].strip().upper()
                continue
            if normalized_line.startswith("VERIFIED:") or normalized_line.startswith("ISSUES:"):
                summary_lines.append(normalized_line)
                continue
            if current and "observed_output" in current and "result" not in current:
                current["observed_output"] = f"{current.get('observed_output', '')}\n{line}".strip()
            else:
                summary_lines.append(normalized_line)
        if current:
            checks.append(dict(current))
        status = "provided" if checks and verdict else "missing"
        summary = "\n".join(summary_lines).strip()
        return VerificationEvidence(
            status=status,
            verdict=verdict,
            summary=summary,
            checks=checks,
            raw_output=raw,
        )

    @staticmethod
    def _verification_unavailable_evidence(*, summary: str, raw_output: str = "") -> VerificationEvidence:
        text = str(summary or "").strip() or "Verification unavailable."
        return VerificationEvidence(
            status="unavailable",
            verdict="partial",
            summary=text,
            checks=[],
            raw_output=str(raw_output or text).strip(),
        )

    def _build_verification_retry_prompt(
        self,
        task: Task | None,
        *,
        previous_output: str,
    ) -> str:
        summary = self._build_verification_prompt(task)
        if task is not None and str(task.metadata.get("execution_mode", "") or "").strip() == "company_mode":
            work_item_name = str(task.title or "").strip()
            turn_type = turn_type_for_task(task, fallback="execute")
            summary = self._build_verification_prompt(
                task,
                work_item_name=work_item_name,
                turn_type=turn_type,
            )
        previous = str(previous_output or "").strip()
        return (
            "Your previous verification response did not satisfy the structured evidence contract. "
            "Rewrite the verification result using the required format below. "
            "If your earlier pass lacked executable checks, rerun the minimum necessary checks now and report them explicitly.\n\n"
            f"{summary}\n\n"
            "Previous output:\n"
            f"{previous or '[empty]'}"
        )

    def _apply_verification_contract(
        self,
        assistant_text: str,
        *,
        task: Task | None,
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
    ) -> tuple[str, str]:
        config = self.config.system.native_runtime.verification_contract
        if not config.enabled:
            return assistant_text, ""
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        employee = current_employee_turn(task) if task is not None else None
        if self._is_task_mode_runtime_task(task):
            verification_state = dict(runtime_notes.get("verification", {}) or {})
            if verification_state.get("completed"):
                verdict = (
                    f"Verification: verified by {verification_state.get('profile', 'verify')}."
                    if verification_state.get("passed")
                    else f"Verification: not verified. {str(verification_state.get('verdict', '')).strip() or 'Issues remain.'}"
                )
                runtime_notes["verification"] = {
                    **verification_state,
                    "status_line": verdict,
                    "advisory": True,
                }
                return assistant_text, verdict
            return assistant_text, ""
        verification_state = dict(runtime_notes.get("verification", {}) or {})
        if verification_state.get("completed"):
            if verification_state.get("passed"):
                verdict = f"Verification: verified by {verification_state.get('profile', 'verify')}."
            else:
                verdict = f"Verification: not verified. {str(verification_state.get('verdict', '')).strip() or 'Blocking issues remain.'}"
        elif self._verification_required(task=task, todo_state=todo_state, runtime_notes=runtime_notes):
            verdict = "Verification: not run because the verifier did not complete successfully."
        elif employee is None and task is not None and task.metadata.get(self.config.system.native_runtime.verification_policy.skip_metadata_key):
            verdict = "Verification: not run because this task explicitly skipped verification."
        else:
            verdict = "Verification: not required because no code edits or risky runtime actions were detected."
        runtime_notes["verification"] = {
            **verification_state,
            "status_line": verdict,
        }
        if employee is not None:
            # A trusted pinned employee output is machine-validated separately.
            # Keep its exact bytes; audit verdicts stay in the normal records.
            return assistant_text, verdict
        if not config.append_status_to_final:
            return assistant_text, verdict
        if not config.require_explicit_status or "verification:" in assistant_text.lower():
            return assistant_text, verdict
        final_text = assistant_text.strip()
        if final_text:
            final_text = f"{final_text}\n\n{verdict}"
        else:
            final_text = verdict
        return final_text, verdict

    async def _ensure_attempt_user_turn_seed(
        self,
        task: Task,
        user_message: str,
        *,
        runtime_session_id: str,
        conversation_turn_id: str,
    ) -> dict[str, Any]:
        """Persist exactly one explicit user correction per WorkItem attempt."""

        revision = str(
            (task.metadata or {}).get(
                "_runtime_v2_attempt_user_seed_revision", ""
            )
            or ""
        ).strip()
        work_item_id = linked_work_item_id_for_task(task)
        try:
            attempt_seq = int(
                (task.metadata or {}).get(
                    "claimed_work_item_attempt_seq", 0
                )
                or 0
            )
        except (TypeError, ValueError):
            attempt_seq = 0
        if not revision or not work_item_id or attempt_seq <= 0:
            raise RuntimeError(
                "attempt user-turn seed lacks a durable WorkItem revision"
            )
        seed_payload = {
            "task_id": str(task.id or "").strip(),
            "work_item_id": work_item_id,
            "attempt_seq": attempt_seq,
        }
        seed_key = "runtime-v2-attempt-user:" + hashlib.sha256(
            json.dumps(
                seed_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        store = getattr(self.memory_manager, "store", None)
        ensure_seed = getattr(store, "ensure_runtime_user_turn_seed", None)
        if not callable(ensure_seed):
            raise RuntimeError(
                "durable runtime user-turn seed store is unavailable"
            )
        receipt = dict(
            await ensure_seed(
            task=task,
            runtime_session_id=runtime_session_id,
            seed_key=seed_key,
            prompt_revision=revision,
            content=user_message,
            metadata={
                "conversation_turn_id": str(conversation_turn_id or "").strip(),
                "canonical_turn_id": str(conversation_turn_id or "").strip(),
                "turn_id": str(conversation_turn_id or "").strip(),
                "work_item_id": work_item_id,
                "claimed_work_item_attempt_seq": attempt_seq,
                "runtime_user_turn_kind": "work_item_attempt_correction",
            },
            )
            or {}
        )
        canonical_revision = str(
            receipt.get("prompt_revision", "") or revision
        ).strip()
        canonical_content = str(receipt.get("content", "") or user_message)
        task.metadata["_runtime_v2_attempt_user_seed_key"] = seed_key
        task.metadata["_runtime_v2_user_seeded"] = True
        task.metadata["_runtime_v2_attempt_user_seed_revision"] = canonical_revision
        try:
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "attempt_user_turn_seeded",
                {
                    "seed_key": seed_key,
                    "created": bool(dict(receipt or {}).get("created", False)),
                    "work_item_id": work_item_id,
                    "claimed_work_item_attempt_seq": attempt_seq,
                    "prompt_revision": canonical_revision,
                    "canonical_reused": bool(
                        receipt.get("canonical_reused", False)
                    ),
                    "inferred_turn_mode": "rework",
                },
            )
        except CompanyRunControllerLeaseLost:
            raise
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to publish attempt user-turn seed telemetry"
            )
        return {
            **receipt,
            "content": canonical_content,
            "prompt_revision": canonical_revision,
        }

    async def _seed_user_turn(
        self,
        task: Task | None,
        user_message: str,
        *,
        runtime_session_id: str,
        conversation_turn_id: str,
    ) -> None:
        if not task or not self.memory_manager or not task.session_id:
            return
        if task.metadata.get("_runtime_v2_user_seeded"):
            return
        task.metadata["_runtime_v2_user_seeded"] = True
        canonical_turn_id = str(conversation_turn_id or "").strip()
        metadata = {
            "kind": "runtime_v2_user_turn",
            "runtime_session_id": runtime_session_id,
        }
        if canonical_turn_id:
            metadata.update({
                "conversation_turn_id": canonical_turn_id,
                "canonical_turn_id": canonical_turn_id,
                "turn_id": canonical_turn_id,
                "ui_message_id": f"runtime-v2-user:{canonical_turn_id}",
            })
        message = await self.memory_manager.record_user_turn(
            session_id=task.session_id,
            content=user_message,
            project_id=task.project_id,
            metadata=metadata,
        )
        store = getattr(self.memory_manager, "store", None)
        if store and hasattr(store, "save_runtime_transcript_entry"):
            await store.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=getattr(message, "message_id", "") if message else "",
                role="user",
                entry_type="message",
                content=user_message,
                metadata=metadata,
            )

    async def _persist_assistant_turn(
        self,
        task: Task | None,
        assistant_text: str,
        tool_calls: list[dict[str, Any]],
        *,
        runtime_session_id: str,
        turn_id: str = "",
        conversation_turn_id: str = "",
        iteration: int | None = None,
        thinking_text: str = "",
    ) -> None:
        if not task or not self.memory_manager or not task.session_id:
            return
        message_turn_id = str(turn_id or f"{runtime_session_id}:{iteration or ''}").strip().rstrip(":")
        canonical_turn_id = str(conversation_turn_id or message_turn_id).strip().rstrip(":")
        is_task_mode = self._is_task_mode_runtime_task(task)
        is_company_mode = (not is_task_mode) and self._is_company_mode_runtime_task(task)
        is_intermediate_tool_turn = bool(tool_calls) and is_task_mode
        if is_intermediate_tool_turn:
            source_kind = "runtime_v2_intermediate_assistant"
        elif is_company_mode:
            source_kind = "runtime_v2_company_assistant"
        else:
            source_kind = "runtime_v2_assistant"
        metadata = {
            "kind": source_kind,
            "runtime_session_id": runtime_session_id,
            "source_kind": source_kind,
        }
        if not tool_calls:
            metadata.update(
                result_delivery_identity_payload_for_task(
                    task,
                    canonical_turn_id=canonical_turn_id,
                )
            )
            metadata.update(work_item_identity_payload_for_task(task))
            if task.session_id:
                metadata["child_session_id"] = str(task.session_id)
        if is_company_mode:
            metadata["execution_mode"] = "company_mode"
            metadata["company_runtime_raw_turn"] = True
            if not tool_calls:
                # Terminal iteration of the company turn — this is the role's
                # final reply. Marked so the UI can show it at summary detail
                # even though the kind is otherwise full-detail-only.
                metadata["company_final_turn"] = True
            if task.assigned_to:
                metadata["role_id"] = str(task.assigned_to)
        else:
            metadata["visible_speaker"] = "SuperOnes"
        if canonical_turn_id:
            metadata["conversation_turn_id"] = canonical_turn_id
            metadata["canonical_turn_id"] = canonical_turn_id
            metadata["turn_id"] = canonical_turn_id
            if message_turn_id and message_turn_id != canonical_turn_id:
                metadata["execution_turn_id"] = message_turn_id
            if is_company_mode:
                # Tool-calling iterations of a company conversation turn share
                # one UI row; the terminal reply gets its own id. The id-keyed
                # backfill merge keeps the first-inserted content for same-kind
                # candidates, so reusing the shared id freezes the row at
                # iteration 1 and swallows the final reply entirely.
                if metadata.get("company_final_turn"):
                    metadata["ui_message_id"] = f"runtime-v2-company-assistant-final:{canonical_turn_id}"
                else:
                    metadata["ui_message_id"] = f"runtime-v2-company-assistant:{canonical_turn_id}"
            elif is_intermediate_tool_turn:
                metadata["ui_message_id"] = f"runtime-v2-intermediate-assistant:{message_turn_id or canonical_turn_id}"
            else:
                metadata["ui_message_id"] = f"runtime-v2-assistant:{canonical_turn_id}"
        if iteration is not None:
            metadata["iteration"] = iteration
        normalized_thinking = str(thinking_text or "").strip()
        if normalized_thinking and is_task_mode:
            metadata["runtime_thinking"] = normalized_thinking
        message = await self.memory_manager.append_session_message(
            session_id=task.session_id,
            role="assistant",
            text=assistant_text,
            project_id=task.project_id,
            agent_id=task.assigned_to or None,
            task_id=task.id,
            metadata=metadata,
        )
        if not message:
            return
        if normalized_thinking and is_task_mode:
            await self.memory_manager.append_session_part(
                task.session_id,
                message.message_id,
                "thinking",
                {
                    "text": normalized_thinking,
                    "turn_id": canonical_turn_id,
                    "runtime_session_id": runtime_session_id,
                    "kind": "runtime_v2_thinking",
                },
            )
        store = getattr(self.memory_manager, "store", None)
        if store and hasattr(store, "save_runtime_transcript_entry"):
            await store.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=message.message_id,
                role="assistant",
                entry_type="message",
                content=assistant_text,
                metadata=metadata,
            )
        for tool_call in tool_calls:
            await self.memory_manager.append_session_part(
                task.session_id,
                message.message_id,
                "tool_call",
                {
                    "tool_call_id": tool_call.get("id", ""),
                    "tool_name": tool_call.get("function", ""),
                    "arguments": tool_call.get("arguments", {}),
                },
            )
            if store and hasattr(store, "save_runtime_tool_call"):
                tool_name = str(tool_call.get("function", "") or "")
                tool_arguments = dict(tool_call.get("arguments", {}) or {})
                tool_call_metadata = {
                    "arguments_raw": str(tool_call.get("arguments_raw", "") or ""),
                    "arguments_parse_error": str(tool_call.get("arguments_parse_error", "") or ""),
                    "project_id": str(task.project_id or "default").strip()
                    or "default",
                    "delegation_run_id": str(
                        task.metadata.get("delegation_run_id", "") or ""
                    ).strip(),
                    "work_item_id": (
                        linked_work_item_id_for_task(task)
                        or str(
                            task.metadata.get(
                                "_company_parent_work_item_id", ""
                            )
                            or ""
                        ).strip()
                    ),
                    "runtime_task_id": str(task.id or "").strip(),
                    "claimed_work_item_attempt_seq": task.metadata.get(
                        "claimed_work_item_attempt_seq", 0
                    )
                    or 0,
                }
                if (
                    is_company_runtime_task(task)
                    and tool_name in {"shell_exec", "python_exec"}
                ):
                    try:
                        plan = build_company_opaque_execution_plan(
                            task,
                            tool_name,
                            tool_arguments,
                        )
                        execution_identity = company_opaque_execution_identity(
                            task
                        )
                        fingerprint = exact_tool_call_fingerprint(
                            tool_call_id=str(tool_call.get("id", "") or ""),
                            tool_name=tool_name,
                            arguments=tool_arguments,
                            runtime_session_id=runtime_session_id,
                            execution_envelope=plan.envelope,
                            execution_identity=execution_identity,
                        )
                        tool_call_metadata.update(
                            {
                                "company_opaque_fingerprint": fingerprint,
                                "company_opaque_execution_envelope_digest": (
                                    opaque_execution_envelope_digest(
                                        plan.envelope
                                    )
                                ),
                            }
                        )
                    except Exception:
                        # Approval will fail closed on the same canonical
                        # builder.  Keep the malformed call for diagnostics,
                        # but it can never qualify as verification evidence.
                        pass
                await store.save_runtime_tool_call(
                    runtime_session_id=runtime_session_id,
                    task_id=task.id,
                    session_id=task.session_id,
                    message_id=message.message_id,
                    tool_call_id=str(tool_call.get("id", "") or ""),
                    tool_name=tool_name,
                    arguments=tool_arguments,
                    metadata=tool_call_metadata,
                )

    async def _persist_tool_result(
        self,
        task: Task | None,
        call: dict[str, Any],
        result: dict[str, Any],
        decision: Any,
        *,
        runtime_session_id: str,
        hook_metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not task or not self.memory_manager or not task.session_id:
            return False
        message = await self.memory_manager.append_session_message(
            session_id=task.session_id,
            role="assistant",
            text=json.dumps(result, ensure_ascii=False, default=str),
            part_type="tool_output",
            project_id=task.project_id,
            agent_id=task.assigned_to or None,
            task_id=task.id,
            metadata={
                "kind": "runtime_v2_tool_output",
                "tool_name": str(call.get("function", "") or ""),
                "runtime_session_id": runtime_session_id,
                "hook_metadata": dict(hook_metadata or {}),
            },
        )
        if not message:
            return False
        store = getattr(self.memory_manager, "store", None)
        await self.memory_manager.append_session_part(
            task.session_id,
            message.message_id,
            "tool_result",
            {
                "tool_call_id": str(call.get("id", "") or ""),
                "tool_name": str(call.get("function", "") or ""),
                "result": result.get("result", result),
                "permission_decision": {
                    "resolution": getattr(getattr(decision, "resolution", None), "value", ""),
                    "scope": getattr(getattr(decision, "scope", None), "value", ""),
                    "rationale": str(getattr(decision, "rationale", "") or ""),
                    "source": str(getattr(decision, "source", "") or ""),
                },
            },
        )
        if store and hasattr(store, "save_runtime_transcript_entry"):
            await store.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=message.message_id,
                role="assistant",
                entry_type="tool_result",
                content=json.dumps(result, ensure_ascii=False, default=str),
                metadata={"tool_name": str(call.get("function", "") or "")},
            )
        result_metadata = {
            "hook_metadata": dict(hook_metadata or {}),
            "project_id": str(task.project_id or "default").strip()
            or "default",
            "delegation_run_id": str(
                task.metadata.get("delegation_run_id", "") or ""
            ).strip(),
            "work_item_id": (
                linked_work_item_id_for_task(task)
                or str(
                    task.metadata.get("_company_parent_work_item_id", "")
                    or ""
                ).strip()
            ),
            "runtime_task_id": str(task.id or "").strip(),
            "claimed_work_item_attempt_seq": task.metadata.get(
                "claimed_work_item_attempt_seq", 0
            )
            or 0,
            "permission_decision": {
                "resolution": getattr(getattr(decision, "resolution", None), "value", ""),
                "scope": getattr(getattr(decision, "scope", None), "value", ""),
                "risk_level": getattr(getattr(decision, "risk_level", None), "value", ""),
                "rationale": str(getattr(decision, "rationale", "") or ""),
                "source": str(getattr(decision, "source", "") or ""),
            },
        }
        tool_name = str(call.get("function", "") or "")
        permit = self._approved_resume_tool_call(
            task,
            tool_call_id=str(call.get("id", "") or ""),
        )
        execution_envelope = dict(
            permit.get("execution_envelope", {}) or {}
        )
        execution_fingerprint = str(
            permit.get("fingerprint", "") or ""
        ).strip()
        if (
            not execution_envelope
            and is_company_runtime_task(task)
            and tool_name in {"shell_exec", "python_exec"}
        ):
            try:
                plan = build_company_opaque_execution_plan(
                    task,
                    tool_name,
                    dict(call.get("arguments", {}) or {}),
                    sandbox_override=dict(
                        dict(hook_metadata or {}).get(
                            "native_execution_sandbox_override", {}
                        )
                        or {}
                    )
                    or None,
                )
                execution_envelope = plan.envelope
                execution_fingerprint = exact_tool_call_fingerprint(
                    tool_call_id=str(call.get("id", "") or ""),
                    tool_name=tool_name,
                    arguments=dict(call.get("arguments", {}) or {}),
                    runtime_session_id=runtime_session_id,
                    execution_envelope=execution_envelope,
                    execution_identity=company_opaque_execution_identity(task),
                )
            except (OpaqueExecutionEnvelopeError, TypeError, ValueError):
                pass
        if execution_envelope and execution_fingerprint:
            result_metadata.update(
                {
                    "company_opaque_fingerprint": execution_fingerprint,
                    "company_opaque_execution_envelope_digest": (
                        opaque_execution_envelope_digest(
                            execution_envelope
                        )
                    ),
                    "company_opaque_execution_sandbox": dict(
                        dict(
                            execution_envelope.get("execution_context", {}) or {}
                        ).get("sandbox", {})
                        or {}
                    ),
                }
            )
        if permit:
            coordinator = self.interaction_coordinator
            if coordinator is None:
                raise RuntimeError(
                    "durable ToolResult completion requires InteractionCoordinator"
                )
            receipt = await coordinator.persist_exact_tool_result(
                permit,
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=message.message_id,
                tool_call_id=str(call.get("id", "") or ""),
                tool_name=str(call.get("function", "") or ""),
                payload=dict(result),
                metadata=result_metadata,
                checkpoint_payload_patch={
                    "approval_result": {
                        "reply": str(permit.get("decision", "") or ""),
                        "approved": bool(permit.get("approved")),
                        "scope": (
                            "once"
                            if str(permit.get("decision", "") or "")
                            == "approve_once"
                            else str(permit.get("decision", "") or "")
                        ),
                        "tool_call_id": str(permit.get("id", "") or ""),
                        "tool_call_fingerprint": str(
                            permit.get("fingerprint", "") or ""
                        ),
                        "tool_result_persisted": True,
                    }
                },
            )
            if not receipt.applied:
                raise RuntimeError(
                    "durable ToolResult completion lost its interaction claim: "
                    f"{receipt.outcome}"
                )
            await self._refresh_durable_tool_permits(task)
            return True
        if store and hasattr(store, "save_runtime_tool_result"):
            await store.save_runtime_tool_result(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=message.message_id,
                tool_call_id=str(call.get("id", "") or ""),
                tool_name=str(call.get("function", "") or ""),
                payload=dict(result),
                metadata=result_metadata,
            )
        return False

    async def _begin_durable_tool_permission_effect(
        self,
        task: Task | None,
        permit: dict[str, Any],
    ) -> bool:
        """Fence an exact ToolCall immediately before invoking its executor."""

        if task is None:
            return False
        coordinator = self.interaction_coordinator
        if coordinator is None:
            return False
        checkpoint_id = str(permit.get("checkpoint_id", "") or "").strip()
        claim_id = str(permit.get("claim_id", "") or "").strip()
        consumer_id = str(permit.get("consumer_id", "") or "").strip()
        if not checkpoint_id or not claim_id or not consumer_id:
            return False
        receipt = await coordinator.begin_exact_tool_effect(permit)
        return bool(receipt.acquired)

    async def _settle_aborted_runtime(
        self,
        *,
        task: Task,
        error: BaseException,
    ) -> None:
        """Join durable abort cleanup before the runtime owner exits.

        Tool-permission settlement and runtime-session convergence are
        independent durable facts.  Attempt both and propagate the first
        cleanup failure so the Engine's ordered shutdown remains fail-closed
        with its Store open.
        """

        cleanup_errors: list[BaseException] = []
        try:
            # Early tools are part of this turn. Controller close and stop both
            # come through here; cancel them before the session is terminalized.
            await self._join_early_tools()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            # AE04a: the turn's subagents go first, inside this shielded cleanup, so a second cancel arriving at
            # the owner cannot leave a background helper running on.
            await self._cancel_live_subagents(task)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            await self._settle_interrupted_durable_tool_permissions(
                task=task,
                error=error,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            await self._terminalize_aborted_runtime_session(
                task,
                error=error,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _terminalize_aborted_runtime_session(
        self,
        task: Task,
        *,
        error: BaseException,
    ) -> None:
        """Terminalize every aborted native runtime without data loss.

        Controller shutdown normally changes the row to ``suspended`` inside
        the atomic WorkItem/checkpoint transaction before cancelling this
        coroutine.  This handler is the complementary boundary for an
        independent runtime abort.  It never creates a row for a turn which
        did not reach its initial ``running`` write and it merges the
        persisted resume/artifact envelope rather than replacing it.  A
        cancellation is resumable (``suspended``); every other escaping error
        is terminal (``failed``).
        """

        runtime_state = dict((task.metadata or {}).get("runtime_v2", {}) or {})
        runtime_session_id = str(
            runtime_state.get("runtime_session_id")
            or self._runtime_resume_payload(task).get("runtime_session_id")
            or ""
        ).strip()
        if not runtime_session_id:
            return
        store = getattr(self.memory_manager, "store", None)
        get_runtime_session = getattr(store, "get_runtime_session", None)
        if not callable(get_runtime_session):
            return
        persisted = await get_runtime_session(runtime_session_id)
        if persisted is None:
            return
        live_statuses = {"running", "starting", "working"}
        persisted_status = str(persisted.get("status", "") or "").strip().lower()
        if persisted_status not in live_statuses:
            runtime_state["status"] = persisted_status
            task.metadata = dict(task.metadata or {})
            task.metadata["runtime_v2"] = runtime_state
            return

        cancelled = isinstance(error, asyncio.CancelledError)
        terminal_status = "suspended" if cancelled else "failed"
        interrupted_at = datetime.now().isoformat()
        terminal_metadata = {
            **dict(persisted.get("metadata", {}) or {}),
            "status": terminal_status,
            "runtime_interrupted_at": interrupted_at,
            "runtime_interruption_kind": type(error).__name__,
            "runtime_interruption_error": str(error)[:1000],
            "runtime_interrupt_reason": (
                "runtime_owner_cancelled" if cancelled else "runtime_owner_failed"
            ),
        }
        if cancelled:
            terminal_metadata["runtime_suspend_reason"] = "runtime_owner_cancelled"
        try:
            await self._save_runtime_session(
                runtime_session_id,
                task,
                terminal_status,
                terminal_metadata,
            )
        except CompanyRunControllerLeaseLost:
            # The atomic controller terminal write can win after our initial read.
            # Its dispatch hold correctly rejects this tail write; accept the
            # race only when the authoritative transaction already made the
            # runtime row non-live.
            current = await get_runtime_session(runtime_session_id)
            current_status = str(
                dict(current or {}).get("status", "") or ""
            ).strip().lower()
            if current is None or current_status in live_statuses:
                raise
            task_runtime_state = dict(
                (task.metadata or {}).get("runtime_v2", {}) or {}
            )
            task_runtime_state["status"] = current_status
            task.metadata = dict(task.metadata or {})
            task.metadata["runtime_v2"] = task_runtime_state

    async def _settle_interrupted_durable_tool_permissions(
        self,
        *,
        task: Task,
        error: BaseException,
    ) -> None:
        """Close or release every claimed permit when its runtime aborts.

        Once a call reached ``executing`` its external outcome is uncertain and
        must never be replayed.  A ``ready`` call has not crossed that boundary,
        so its claim is released to ``answered`` for exact recovery.
        """

        coordinator = self.interaction_coordinator
        if coordinator is None:
            return
        await self._refresh_durable_tool_permits(task)
        permits = list(self._approved_resume_tool_calls(task).values())
        first_cleanup_error: BaseException | None = None
        for permit in permits:
            state = str(permit.get("state", "") or "").strip()
            if state not in {"ready", "denied", "executing", "result_persisted"}:
                continue
            checkpoint_id = str(permit.get("checkpoint_id", "") or "").strip()
            claim_id = str(permit.get("claim_id", "") or "").strip()
            consumer_id = str(permit.get("consumer_id", "") or "").strip()
            if not checkpoint_id or not claim_id or not consumer_id:
                continue
            error_patch = {
                "approval_result": {
                    "tool_call_id": str(permit.get("id", "") or ""),
                    "tool_call_fingerprint": str(
                        permit.get("fingerprint", "") or ""
                    ),
                    "error_kind": type(error).__name__,
                    "error": str(error)[:1000],
                    "tool_result_persisted": state == "result_persisted",
                    "outcome_unknown": state == "executing",
                }
            }
            try:
                receipt = await coordinator.settle_interrupted_exact_tool(
                    permit,
                    state=state,
                    error_kind=type(error).__name__,
                    payload_patch=error_patch,
                )
                checkpoint_status = str(
                    getattr(receipt.checkpoint, "status", "") or ""
                ).strip()
                if checkpoint_status == "outcome_unknown":
                    await self._report_external_outcome_unknown(
                        permit, receipt.checkpoint, error
                    )
                if receipt.applied or checkpoint_status not in {
                    "pending",
                    "answered",
                    "consuming",
                    "resuming",
                }:
                    await self._set_approved_resume_tool_call_state(
                        task,
                        None,
                        remove_fingerprint=str(
                            permit.get("fingerprint", "") or ""
                        ),
                    )
            except BaseException as exc:
                logger.opt(exception=True).warning(
                    "Could not settle interrupted durable permission {}",
                    checkpoint_id,
                )
                if (
                    not isinstance(exc, asyncio.CancelledError)
                    and first_cleanup_error is None
                ):
                    first_cleanup_error = exc
        if first_cleanup_error is not None:
            raise first_cleanup_error

    async def _report_external_outcome_unknown(
        self, permit: dict[str, Any], checkpoint: Any, error: BaseException
    ) -> None:
        """An external write whose result never came back holds the run's budget.

        The effect gateway already does this for effects it proposes; a
        tool-permission call on the native path had the same uncertainty
        and nothing linked it. Only tools that declare an external side
        effect count: a workspace write that was interrupted is recoverable
        from disk, a message that may have been sent is not.
        """
        hook = self.external_outcome_unknown
        if not callable(hook):
            return
        name = str(permit.get("function", "") or "").strip()
        tool = self.tools.get(name) if name else None
        effects = set(getattr(tool, "permission_effects", None) or ())
        if "external_side_effect" not in effects:
            return
        from types import SimpleNamespace

        execution = SimpleNamespace(
            detail=f"tool {name} interrupted while executing ({type(error).__name__}); result never persisted",
            record=SimpleNamespace(effect_id=f"tool_call:{permit.get('id', '')}"),
        )
        try:
            await hook(checkpoint, execution)
        except Exception as exc:  # noqa: BLE001 - the settlement stands; the hold failure is written down
            logger.warning(
                "budget hold for unknown external outcome of {} failed: {}: {}",
                name, type(exc).__name__, exc,
            )

    async def _complete_durable_tool_permission(
        self,
        *,
        task: Task | None,
        item: dict[str, Any],
        runtime_session_id: str,
    ) -> None:
        """Resolve a tool-permission lease only after its result is durable."""

        if task is None:
            return
        call = dict(item.get("tool_call", {}) or {})
        permit = self._approved_resume_tool_call(
            task,
            tool_call_id=str(call.get("id", "") or ""),
        )
        if not permit or str(permit.get("id", "") or "") != str(call.get("id", "") or ""):
            return
        checkpoint_id = str(permit.get("checkpoint_id", "") or "").strip()
        claim_id = str(permit.get("claim_id", "") or "").strip()
        consumer_id = str(permit.get("consumer_id", "") or "").strip()
        if not checkpoint_id or not claim_id or not consumer_id:
            return
        coordinator = self.interaction_coordinator
        if coordinator is None:
            return
        receipt = await coordinator.finish_exact_tool_permission(
            permit,
            payload_patch={
                "approval_result": {
                    "reply": str(permit.get("decision", "") or ""),
                    "approved": bool(permit.get("approved")),
                    "scope": (
                        "once"
                        if str(permit.get("decision", "") or "") == "approve_once"
                        else str(permit.get("decision", "") or "")
                    ),
                    "tool_call_id": str(permit.get("id", "") or ""),
                    "tool_call_fingerprint": str(permit.get("fingerprint", "") or ""),
                    "tool_result_persisted": True,
                }
            },
        )
        if not receipt.applied:
            logger.warning(
                "Could not finish durable tool permission {} after result persistence: {}",
                checkpoint_id,
                receipt.outcome,
            )
            checkpoint_status = str(
                getattr(receipt.checkpoint, "status", "") or ""
            ).strip()
            if checkpoint_status not in {
                "pending",
                "answered",
                "consuming",
                "resuming",
            }:
                await self._set_approved_resume_tool_call_state(
                    task,
                    None,
                    remove_fingerprint=str(
                        permit.get("fingerprint", "") or ""
                    ),
                )
            return
        await self._set_approved_resume_tool_call_state(
            task,
            None,
            remove_fingerprint=str(permit.get("fingerprint", "") or ""),
        )

    async def _persist_compaction_boundary(
        self,
        task: Task | None,
        boundary_record: dict[str, Any],
        runtime_session_id: str,
    ) -> None:
        if not task or not self.memory_manager or not task.session_id:
            return
        message = await self.memory_manager.append_session_message(
            session_id=task.session_id,
            role="assistant",
            text=boundary_record["summary"],
            project_id=task.project_id,
            agent_id=task.assigned_to or None,
            task_id=task.id,
            summary_flag=True,
            metadata={
                "kind": "runtime_v2_compaction_boundary",
                "runtime_session_id": runtime_session_id,
                **boundary_record,
            },
        )
        store = getattr(self.memory_manager, "store", None)
        if store and hasattr(store, "save_runtime_transcript_entry"):
            await store.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=getattr(message, "message_id", "") if message else "",
                role="assistant",
                entry_type="compaction_boundary",
                content=boundary_record["summary"],
                metadata=dict(boundary_record),
            )

    @staticmethod
    def _is_task_mode_runtime_task(task: Task | None) -> bool:
        if task is None:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        if execution_mode == "company_mode":
            return False
        if execution_mode in {"task_mode", "task", "project_mode", "project"}:
            return True
        mode = str(metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(metadata.get("task_mode_contract", "") or "").strip()
        runtime_kind = str(metadata.get("runtime_kind", "") or "").strip()
        projection_id = str(metadata.get("work_item_projection_id", "") or "").strip()
        if projection_id and projection_id != "task_mode_execution":
            return False
        return (
            mode == "task"
            or task_mode_contract == "single_full_capability_main_agent"
            or runtime_kind == "task_mode_agent_turn"
            or projection_id == "task_mode_execution"
        )

    @staticmethod
    def _is_company_mode_runtime_task(task: Task | None) -> bool:
        if task is None:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        mode = str(metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(metadata.get("task_mode_contract", "") or "").strip()
        runtime_kind = str(metadata.get("runtime_kind", "") or "").strip()
        projection_id = str(metadata.get("work_item_projection_id", "") or "").strip()
        if (
            execution_mode in {"task_mode", "task", "project_mode", "project"}
            or task_mode_contract == "single_full_capability_main_agent"
            or runtime_kind == "task_mode_agent_turn"
            or projection_id == "task_mode_execution"
            or (mode == "task" and not projection_id)
        ):
            return False
        if execution_mode == "company_mode":
            return True
        if str(metadata.get("execution_model", "") or "").strip() == "multi_team_org":
            return True
        if projection_id:
            return True
        if str(metadata.get("company_profile", "") or "").strip():
            return True
        return bool(metadata.get("work_item_runtime"))

    @staticmethod
    def _metadata_flag_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    @staticmethod
    def _runtime_event_identity_payload(task: Task | None) -> dict[str, str]:
        if NativeRuntimeV2._is_task_mode_runtime_task(task):
            return {}
        return work_item_identity_payload_for_task(task)

    @staticmethod
    def _permission_group_key(tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_tool = str(tool_name or "").strip().casefold()
        if normalized_tool != "shell_exec":
            return ""
        command = str(
            arguments.get("command")
            or arguments.get("cmd")
            or ""
        ).strip()
        command_family = ""
        if re.match(r"^(?:python|python3)\b", command, re.IGNORECASE):
            command_family = "python"
        elif re.match(r"^node\b", command, re.IGNORECASE):
            command_family = "node"
        if not command_family:
            return ""
        domains = sorted({
            match.group(1).casefold()
            for match in re.finditer(r"https?://([^/\s'\"<>]+)", command)
        })
        domain_key = ",".join(domains) if domains else "no-domain"
        return f"tool:shell_exec/{command_family}:domain:{domain_key}"

    async def _emit_runtime_event(
        self,
        runtime_session_id: str,
        task: Task | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        task_metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        execution_mode = str(task_metadata.get("execution_mode", "") or "").strip()
        if not execution_mode and self._is_task_mode_runtime_task(task):
            execution_mode = "task_mode"
        event_payload = {
            "type": event_type,
            "runtime_session_id": runtime_session_id,
            "task_id": task.id if task else None,
            "session_id": task.session_id if task else None,
            "agent_id": task.assigned_to if task else None,
            "role_id": task.assigned_to if task else None,
            "execution_mode": execution_mode,
            **self._runtime_event_identity_payload(task),
            "timestamp_ms": int(time.time() * 1000),
            **payload,
        }
        store = getattr(self.memory_manager, "store", None)
        if store and hasattr(store, "save_runtime_event"):
            await store.save_runtime_event(runtime_session_id, event_type, event_payload)
        if self.event_bus:
            await self.event_bus.publish(AmpulyEvent(event_type="runtime_event", payload=event_payload))

    def _primary_worktree_path(self, active_subagents: list[dict[str, Any]]) -> str:
        for item in active_subagents:
            path = str(item.get("worktree_path", "") or "").strip()
            if path:
                return path
        return ""

    async def _save_runtime_session(
        self,
        runtime_session_id: str,
        task: Task | None,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        if task is not None:
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            runtime_meta.update({
                "runtime_session_id": runtime_session_id,
                "status": status,
                **dict(metadata or {}),
            })
            task.metadata["runtime_v2"] = runtime_meta
        store = getattr(self.memory_manager, "store", None)
        if store and hasattr(store, "save_runtime_session"):
            await store.save_runtime_session(
                runtime_session_id=runtime_session_id,
                task_id=task.id if task else None,
                session_id=task.session_id if task else None,
                project_id=task.project_id if task else "default",
                status=status,
                metadata=metadata,
                controller_run_id=(
                    str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
                    if task
                    else ""
                ),
                controller_owner_token=(
                    str(
                        (task.metadata or {}).get(
                            "company_run_controller_owner_token",
                            "",
                        )
                        or ""
                    ).strip()
                    if task
                    else ""
                ),
                controller_lease_generation=(
                    int(
                        (task.metadata or {}).get(
                            "company_run_controller_lease_generation",
                            0,
                        )
                        or 0
                    )
                    if task
                    else 0
                ),
            )

    async def _emit_prompt_prefix_state(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        base_prefix_len: int,
    ) -> None:
        config = self.config.system.native_runtime.prompt_prefix_stability
        if not config.enabled or not config.emit_cache_fingerprint_events:
            return
        prefix_messages = messages[:base_prefix_len]
        if hasattr(self.llm, "build_cache_fingerprint"):
            fingerprint = self.llm.build_cache_fingerprint(messages=prefix_messages, tools=tool_schemas)
        else:
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"messages": prefix_messages, "tools": tool_schemas or []},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        token_count = self._safe_count_input_tokens(prefix_messages, tool_schemas)
        if task is not None:
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            runtime_meta["prompt_prefix_fingerprint"] = fingerprint
            runtime_meta["prompt_prefix_tokens"] = token_count
            task.metadata["runtime_v2"] = runtime_meta
        await self._emit_runtime_event(
            runtime_session_id,
            task,
            "prompt_prefix_state",
            {
                "fingerprint": fingerprint,
                "token_count": token_count,
                "base_prefix_len": base_prefix_len,
                "message_count": len(prefix_messages),
            },
        )

    async def _emit_context_usage(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        phase: str,
    ) -> dict[str, Any]:
        config = self.config.system.native_runtime.context_usage_reporting
        payload = self._context_usage_payload(messages, tool_schemas)
        if not config.enabled or not config.emit_runtime_events:
            return payload
        await self._emit_runtime_event(
            runtime_session_id,
            task,
            "context_usage",
            {
                "phase": phase,
                **payload,
                "message_count": len(messages),
            },
        )
        warn_remaining_pct = int(self.config.system.native_runtime.context_guard.warn_remaining_pct or 0)
        if (
            payload.get("context_window", 0)
            and warn_remaining_pct > 0
            and int(payload.get("context_remaining_pct", 100) or 100) <= warn_remaining_pct
        ):
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "context_warning",
                {
                    "phase": phase,
                    **payload,
                    "threshold_pct": warn_remaining_pct,
                },
            )
        return payload

    def _update_runtime_notes(
        self,
        runtime_notes: dict[str, Any],
        execution_results: list[dict[str, Any]],
    ) -> None:
        mutating_tools = set(runtime_notes.get("mutating_tools", []) or [])
        risky_tools = set(runtime_notes.get("observed_risky_tools", []) or [])
        permission_details = list(runtime_notes.get("permission_details", []) or [])
        for item in execution_results:
            call = dict(item.get("tool_call", {}) or {})
            decision = item.get("permission_decision")
            tool_name = str(call.get("function", "") or "")
            if tool_name in {
                "file_write",
                "file_edit",
                "apply_patch",
                "shell_exec",
                "python_exec",
                "git_commit",
            }:
                mutating_tools.add(tool_name)
            if decision is not None:
                risk_level = getattr(getattr(decision, "risk_level", None), "value", str(getattr(decision, "risk_level", "")))
                resolution = getattr(getattr(decision, "resolution", None), "value", str(getattr(decision, "resolution", "")))
                detail = {
                    "tool_name": tool_name,
                    "resolution": resolution,
                    "risk_level": risk_level,
                    "source": str(getattr(decision, "source", "") or ""),
                    "rationale": str(getattr(decision, "rationale", "") or ""),
                }
                permission_details.append(detail)
                if risk_level in {"high", "critical"} or resolution in {"ask", "deny"}:
                    risky_tools.add(tool_name)
        runtime_notes["mutating_tools"] = sorted(mutating_tools)
        runtime_notes["observed_risky_tools"] = sorted(risky_tools)
        runtime_notes["permission_details"] = permission_details[-20:]

    def _verification_required(
        self,
        *,
        task: Task | None,
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
    ) -> bool:
        policy = self.config.system.native_runtime.verification_policy
        if not policy.enabled or task is None:
            return False
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        employee = current_employee_turn(task)
        if employee is None and task.metadata.get(policy.skip_metadata_key):
            return False
        verification_state = dict(runtime_notes.get("verification", {}) or {})
        if verification_state.get("completed"):
            return False
        if self._is_task_mode_runtime_task(task):
            return self._metadata_flag_enabled(task.metadata.get("explicit_verification_requested"))
        explicit_requirement = task.metadata.get("work_item_verification_required")
        if isinstance(explicit_requirement, bool) and (employee is None or explicit_requirement):
            return explicit_requirement
        if len(todo_state or []) >= max(1, policy.min_todos_for_verification):
            return True
        if policy.require_on_code_edits and runtime_notes.get("mutating_tools"):
            return True
        if policy.require_on_risky_tools and runtime_notes.get("observed_risky_tools"):
            return True
        return False

    async def _run_verification_audit(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        subagents: SubagentManager,
        messages: list[dict[str, Any]],
        todo_state: list[dict[str, Any]],
        runtime_notes: dict[str, Any],
    ) -> None:
        """Record verifier evidence without owning workflow disposition.

        Verification is an advisory runtime audit. Company-mode quality
        failures are handled by the company contract, manager-review, rework,
        and final-delivery state machines; task-mode callers receive the same
        evidence in their result artifacts. Only an explicit interaction such
        as ``request_user_input`` may create an owner wait.
        """
        if not self._verification_required(task=task, todo_state=todo_state, runtime_notes=runtime_notes):
            return
        policy = self.config.system.native_runtime.verification_policy
        summary = self._build_verification_prompt(task)
        if task is not None and str(task.metadata.get("execution_mode", "") or "").strip() == "company_mode":
            work_item_name = str(task.title or "").strip()
            turn_type = turn_type_for_task(task, fallback="execute")
            summary = self._build_verification_prompt(
                task,
                work_item_name=work_item_name,
                turn_type=turn_type,
            )
        await self._emit_runtime_event(
            runtime_session_id,
            task,
            "verification_started",
            {
                "profile": policy.verifier_profile,
                "mutating_tools": list(runtime_notes.get("mutating_tools", []) or []),
                "risky_tools": list(runtime_notes.get("observed_risky_tools", []) or []),
                "todo_count": len(todo_state or []),
            },
        )
        verification_result = await subagents.spawn(
            profile=policy.verifier_profile,
            prompt=summary,
            description="Runtime verification pass",
            name="verifier",
            background=False,
            isolation="worktree",
            fork_context_messages=list(messages),
            fork_system_prompt=str(messages[0].get("content", "")) if messages else "",
            fork_mode=True,
        )
        verdict_text = str(
            verification_result.get("result", "")
            or verification_result.get("error", "")
            or ""
        ).strip()
        verification_evidence = self._parse_verification_evidence(verdict_text)
        spawn_success = bool(verification_result.get("success", False))
        repair_attempted = False
        if spawn_success and verdict_text and verification_evidence.status != "provided":
            repair_attempted = True
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "verification_repair_requested",
                {
                    "profile": policy.verifier_profile,
                    "reason": "missing_structured_evidence",
                },
            )
            repair_result = await subagents.spawn(
                profile=policy.verifier_profile,
                prompt=self._build_verification_retry_prompt(task, previous_output=verdict_text),
                description="Runtime verification evidence repair",
                name="verifier_repair",
                background=False,
                isolation="worktree",
                fork_context_messages=list(messages),
                fork_system_prompt=str(messages[0].get("content", "")) if messages else "",
                fork_mode=True,
            )
            repaired_text = str(
                repair_result.get("result", "")
                or repair_result.get("error", "")
                or ""
            ).strip()
            if repaired_text:
                verdict_text = repaired_text
            verification_evidence = self._parse_verification_evidence(verdict_text)
            spawn_success = bool(repair_result.get("success", False))
        if verification_evidence.status != "provided":
            unavailable_reason = verdict_text or "Verifier did not provide structured evidence."
            if not spawn_success:
                unavailable_reason = (
                    "Verification unavailable: verifier did not complete successfully. "
                    + unavailable_reason
                ).strip()
            elif repair_attempted:
                unavailable_reason = (
                    "Verification unavailable: verifier still did not provide structured evidence after repair. "
                    + unavailable_reason
                ).strip()
            else:
                unavailable_reason = (
                    "Verification unavailable: verifier did not provide structured evidence. "
                    + unavailable_reason
                ).strip()
            verification_evidence = self._verification_unavailable_evidence(
                summary=unavailable_reason,
                raw_output=verdict_text,
            )
            verdict_text = unavailable_reason
        passed = (
            spawn_success
            and verdict_text
            and verification_evidence.status == "provided"
            and verification_evidence.verdict == "pass"
        )
        verification_state = {
            "completed": True,
            "passed": passed,
            "profile": policy.verifier_profile,
            "verdict": verdict_text,
            "spawn_success": spawn_success,
            "evidence": verification_evidence.__dict__,
            "repair_attempted": repair_attempted,
        }
        runtime_notes["verification"] = verification_state
        if task is not None:
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            runtime_meta["verification"] = verification_state
            task.metadata["runtime_v2"] = runtime_meta
        await self._emit_runtime_event(
            runtime_session_id,
            task,
            "verification_completed",
            verification_state,
        )

    async def _maybe_update_background_session_memory(
        self,
        *,
        runtime_session_id: str,
        task: Task | None,
        messages: list[dict[str, Any]],
    ) -> None:
        config = self.config.system.native_runtime.background_session_memory
        if not config.enabled or not task or not self.memory_manager or not task.session_id:
            return
        updater = getattr(self.memory_manager, "update_runtime_session_memory", None)
        if not callable(updater):
            return
        try:
            result = await updater(
                session_id=task.session_id,
                project_id=task.project_id,
                llm=self.llm,
                messages=messages,
                update_interval_messages=config.update_interval_messages,
                max_input_chars=config.max_input_chars,
            )
        except Exception as exc:  # pragma: no cover - defensive
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "session_memory_update_failed",
                {"message": str(exc)},
            )
            return
        if result:
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "session_memory_updated",
                dict(result),
            )

    def _safe_count_input_tokens(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
    ) -> int:
        counter = getattr(self.llm, "count_input_tokens", None)
        if not callable(counter):
            return 0
        try:
            return int(counter(messages, tools=tool_schemas) or 0)
        except TypeError:
            try:
                return int(counter(messages) or 0)
            except Exception:
                return 0
        except Exception:
            return 0

    def _context_window_limit(self) -> int:
        getter = getattr(self.llm, "get_context_window", None)
        if not callable(getter):
            return 0
        try:
            return int(getter() or 0)
        except TypeError:
            try:
                return int(getter(task_type=None) or 0)
            except Exception:
                return 0
        except Exception:
            return 0

    def _context_usage_payload(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        *,
        observed_tokens: int = 0,
    ) -> dict[str, Any]:
        # Anchor on the provider-reported prompt size of the latest request
        # when it exceeds the local estimate: the context only grows within a
        # turn, so max() protects against estimator undercounting.
        token_count = max(
            self._safe_count_input_tokens(messages, tool_schemas),
            int(observed_tokens or 0),
        )
        context_window = self._context_window_limit()
        remaining_tokens = max(0, context_window - token_count) if context_window > 0 else 0
        remaining_pct = int((remaining_tokens / context_window) * 100) if context_window > 0 else 0
        usage_ratio = (token_count / context_window) if context_window > 0 else 0.0
        return {
            "token_count": token_count,
            "context_tokens": token_count,
            "context_window": context_window,
            "context_remaining_tokens": remaining_tokens,
            "context_remaining_pct": remaining_pct,
            "usage_ratio": round(usage_ratio, 4),
            "hard_threshold": float(self.config.system.native_runtime.context_guard.hard_threshold or 0.90),
        }

    def _hard_threshold_for_task(self, task: Any | None) -> float:
        """The compaction threshold for this turn: per-turn-type override, else global."""
        config = self.config.system.native_runtime.context_guard
        default = float(config.hard_threshold or 0.90)
        overrides = dict(getattr(config, "turn_thresholds", {}) or {})
        if not overrides or task is None:
            return default
        metadata = dict(getattr(task, "metadata", {}) or {})
        for key in ("work_item_turn_type", "work_kind", "delegation_turn_kind"):
            turn = str(metadata.get(key, "") or "").strip().lower()
            if turn and turn in overrides:
                try:
                    return max(0.05, min(0.99, float(overrides[turn])))
                except (TypeError, ValueError):
                    return default
        return default

    def _should_apply_hard_compaction(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        *,
        observed_tokens: int = 0,
        task: Any | None = None,
    ) -> bool:
        config = self.config.system.native_runtime.context_guard
        if not config.enabled:
            return False
        payload = self._context_usage_payload(messages, tool_schemas, observed_tokens=observed_tokens)
        if payload["context_window"] <= 0:
            return False
        return float(payload["usage_ratio"]) >= self._hard_threshold_for_task(task)

    def _clip_tool_result_for_history(
        self,
        tool_name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        clipped = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        payload = clipped.get("result")
        config = self.config.system.native_runtime.context_guard
        generic_budget = int(config.tool_output_char_budget or 12_000)
        shell_stdout_budget = int(config.shell_stdout_char_budget or 12_000)
        shell_stderr_budget = int(config.shell_stderr_char_budget or 6_000)
        if not isinstance(payload, dict):
            return clipped
        if tool_name in {"shell_exec", "python_exec"}:
            if payload.get("stdout"):
                clipped_stdout = clip_text(
                    str(payload.get("stdout", "") or ""),
                    limit=shell_stdout_budget,
                    marker="stdout truncated by context_guard",
                )
                payload["stdout"] = clipped_stdout.text
                if clipped_stdout.truncated:
                    payload["stdout_truncated"] = True
                    payload["stdout_omitted_chars"] = clipped_stdout.omitted_chars
            if payload.get("stderr"):
                clipped_stderr = clip_text(
                    str(payload.get("stderr", "") or ""),
                    limit=shell_stderr_budget,
                    marker="stderr truncated by context_guard",
                )
                payload["stderr"] = clipped_stderr.text
                if clipped_stderr.truncated:
                    payload["stderr_truncated"] = True
                    payload["stderr_omitted_chars"] = clipped_stderr.omitted_chars
        for key in ("content", "rendered", "summary", "diff_preview"):
            if payload.get(key):
                clipped_value = clip_text(
                    str(payload.get(key, "") or ""),
                    limit=generic_budget,
                    marker="tool output truncated by context_guard",
                )
                payload[key] = clipped_value.text
                if clipped_value.truncated:
                    payload[f"{key}_truncated"] = True
                    payload[f"{key}_omitted_chars"] = clipped_value.omitted_chars
        clipped["result"] = payload
        return clipped

    async def _stamp_llm_in_flight(self, task: Task | None, active: bool) -> None:
        """Record on the task, durably, that a model call is open (or not).

        From outside, a run waiting on a long generation is indistinguishable
        from a run that died: no tool calls, no checkpoints, no events. The
        watchdog needs one durable fact to tell them apart, and only the
        runtime knows it. The stamp is naive local time, like every other
        timestamp the store writes; a stale stamp (a crash mid-call) ages out
        on the reader's side rather than pinning an order as "thinking"
        forever. Persistence is best-effort: the call proceeds either way.
        """
        if task is None:
            return
        runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
        if active:
            runtime_meta["llm_in_flight_since"] = datetime.now().isoformat()
        else:
            runtime_meta.pop("llm_in_flight_since", None)
        task.metadata["runtime_v2"] = runtime_meta
        store = getattr(self.memory_manager, "store", None)
        save_task = getattr(store, "save_task", None)
        if not callable(save_task):
            return
        try:
            await save_task(task)
        except Exception as exc:  # the stamp is for the watchdog, not the run
            logger.debug(f"llm in-flight stamp not persisted: {exc}")

    async def _emit_status_snapshot(
        self,
        runtime_session_id: str,
        task: Task | None,
        snapshot: dict[str, Any],
    ) -> None:
        await self._emit_runtime_event(runtime_session_id, task, "status_snapshot", dict(snapshot))

    async def _extract_durable_memory(
        self,
        *,
        task: Task | None,
        user_message: str,
        assistant_text: str,
        runtime_session_id: str,
    ) -> dict[str, Any]:
        if not task or not self.memory_manager or not task.session_id:
            return {}
        if not self.config.system.native_runtime.auto_extract_durable_memory:
            return {}
        extractor = getattr(self.memory_manager, "extract_durable_memories", None)
        if not callable(extractor):
            return {}
        try:
            result = await extractor(
                session_id=task.session_id,
                project_id=task.project_id,
                query=user_message,
                assistant_response=assistant_text,
                llm=self.llm,
                min_messages=self.config.system.native_runtime.durable_memory_extract_min_messages,
                max_input_chars=self.config.system.native_runtime.durable_memory_max_input_chars,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Durable memory extraction failed: {exc}")
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "durable_memory_extraction_failed",
                {"message": str(exc)},
            )
            return {}
        if result:
            await self._emit_runtime_event(
                runtime_session_id,
                task,
                "durable_memory_extracted",
                dict(result),
            )
        return {"durable_memory_extraction": result} if result else {}

    def _merge_artifacts(self, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in incoming.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = [*merged[key], *value]
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    def _format_interrupt_notice(self, inbox_messages: list[dict[str, Any]]) -> str:
        lines = ["New runtime inbox messages arrived while you were working:"]
        for item in inbox_messages[:6]:
            message_id = str(item.get("msg_id", "") or "").strip()
            from_agent = str(item.get("from_agent", "") or item.get("from", "") or "").strip()
            subject = str(item.get("subject", "") or "").strip()
            message_class = str(item.get("message_class", "") or dict(item.get("metadata", {}) or {}).get("message_class", "")).strip()
            protocol_type = str(item.get("protocol_type", "") or dict(item.get("metadata", {}) or {}).get("protocol_type", "")).strip()
            summary = " | ".join(
                [
                    part
                    for part in [
                        f"message_id={message_id}" if message_id else "",
                        f"from={from_agent}" if from_agent else "",
                        f"subject={subject}" if subject else "",
                        f"class={message_class}" if message_class else "",
                        f"protocol={protocol_type}" if protocol_type else "",
                    ]
                    if part
                ]
            )
            body = str(item.get("body", "") or "").strip()
            lines.append(f"- {summary or str(item)}")
            if body:
                lines.append(f"  body={body}")
        return "\n".join(lines)

    def _render_todos(self, todo_state: list[dict[str, Any]]) -> str:
        if not todo_state:
            return "(no TODO items)"
        status_icons = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "done": "[x]",
        }
        return "\n".join(
            f"{status_icons.get(str(item.get('status', 'pending')), '[ ]')} "
            f"{str(item.get('title') or item.get('content') or item.get('id', 'todo'))}"
            for item in todo_state
        )

    def _normalize_todos(self, todos_payload: Any) -> list[dict[str, Any]]:
        if not isinstance(todos_payload, list):
            return []
        normalized: list[dict[str, Any]] = []
        has_active = False
        for index, raw in enumerate(todos_payload, start=1):
            if not isinstance(raw, dict):
                continue
            status = self._normalize_todo_status(raw.get("status"))
            if status == "in_progress":
                if has_active:
                    status = "pending"
                else:
                    has_active = True
            title = (
                str(raw.get("title") or "").strip()
                or str(raw.get("content") or "").strip()
                or str(raw.get("active_form") or raw.get("activeForm") or "").strip()
                or f"todo-{index}"
            )
            active_form = str(raw.get("active_form") or raw.get("activeForm") or title).strip() or title
            content = str(raw.get("content") or title).strip() or title
            normalized.append(
                {
                    "id": str(raw.get("id") or index),
                    "title": title,
                    "content": content,
                    "active_form": active_form,
                    "status": status,
                }
            )
        return normalized

    @staticmethod
    def _normalize_spawn_mode(value: Any) -> str:
        mode = str(value or "default").strip() or "default"
        legacy_key = mode.replace("_", "").lower()
        return {
            "acceptedits": "accept_edits",
            "bypasspermissions": "bypass_permissions",
            "dontask": "dont_ask",
        }.get(legacy_key, mode)

    @staticmethod
    def _normalize_todo_status(value: Any) -> str:
        status = str(value or "pending").strip().lower()
        if status in {"done", "completed", "complete", "finished"}:
            return "completed"
        if status in {"in_progress", "in-progress", "active", "current"}:
            return "in_progress"
        return "pending"

    async def _emit_progress(self, callback: Any, text: str, task: Task | None) -> None:
        try:
            await callback(text, task_id=task.id if task else None)
        except TypeError:
            await callback(text)

    def _permission_requests_from_results(self, execution_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for item in execution_results:
            decision = item.get("permission_decision")
            call = item.get("tool_call", {})
            if decision is None:
                continue
            resolution = getattr(decision, "resolution", None)
            resolution_value = getattr(resolution, "value", str(resolution))
            if resolution_value not in {"ask", "deny"}:
                continue
            raw_arguments = call.get("arguments")
            requests.append({
                "tool_name": str(call.get("function", "") or ""),
                # The blocked call's arguments must survive into the park
                # checkpoint: a late approval reply rebuilds the allowlist
                # context from them, and without the command text no grant can
                # be recorded — the task resumes, retries, re-blocks, and
                # re-parks on an identical card forever.
                "tool_args": dict(raw_arguments) if isinstance(raw_arguments, dict) else {},
                "resolution": resolution_value,
                "scope": getattr(getattr(decision, "scope", None), "value", str(getattr(decision, "scope", ""))),
                "risk_level": getattr(getattr(decision, "risk_level", None), "value", str(getattr(decision, "risk_level", ""))),
                "rationale": str(getattr(decision, "rationale", "") or ""),
                "source": str(getattr(decision, "source", "") or ""),
            })
        return requests
