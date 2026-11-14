"""Runtime-managed native subagents."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from loguru import logger

from ampuly.core.config import AmpulyConfig, NativeSubagentProfileConfig
from ampuly.core.models import AmpulyEvent, Task, TaskResult, TaskStatus
from ampuly.layer2_organization.work_item_identity import projection_id_for_task, work_item_identity_payload_for_task
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
from ampuly.layer3_agent.runtime_v2.worktree import cleanup_worktree, create_worktree


ChildAgentFactory = Callable[..., Any]


_NON_INHERITED_CHILD_IDENTITY_KEYS = frozenset(
    {
        "shared_role_session",
        "shared_role_id",
        "work_item_projection_id",
        "work_item_turn_type",
        "derived_work_item_projection",
        "authoritative_output",
        "feedback_scope",
        "requires_user_feedback",
        "review_owner_kind",
        "_runtime_v2_user_seeded",
        "_runtime_v2_attempt_user_seed_key",
        "_runtime_v2_attempt_user_seed_required",
        "_runtime_v2_attempt_user_seed_revision",
    }
)


@dataclass
class SubagentState:
    agent_id: str
    profile: str
    name: str = ""
    description: str = ""
    model: str = ""
    mode: str = "default"
    isolation: str = "shared"
    max_iterations: int = 24
    status: str = "running"
    background: bool = False
    resident: bool = False
    fork_mode: bool = False
    created_at: float = field(default_factory=time.time)
    latest_result: str = ""
    pending_messages_count: int = 0
    last_notification_kind: str = ""
    worktree: dict[str, Any] | None = None
    fork_system_prompt: str = ""
    fork_context_messages: list[dict[str, Any]] = field(default_factory=list)
    fork_allowed_tools: list[str] = field(default_factory=list)
    runtime_task: asyncio.Task[Any] | None = None
    inbox: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    update_event: asyncio.Event = field(default_factory=asyncio.Event)
    task_result: TaskResult | None = None


class SubagentManager:
    def __init__(
        self,
        *,
        parent_task: Task | None,
        config: AmpulyConfig | None,
        child_agent_factory: ChildAgentFactory | None,
        event_bus: Any = None,
        store: Any = None,
        memory_manager: Any = None,
        runtime_session_id: str = "",
        model_check: Callable[[str], str] | None = None,
        parent_remaining_iterations: Callable[[], int] | None = None,
        declared_max_iterations: int | None = None,
    ) -> None:
        self.parent_task = parent_task
        self.config = config or AmpulyConfig()
        self.child_agent_factory = child_agent_factory
        # Asked before a subagent with a model of its own is set up: "" to go ahead, else the refusal's code.
        self.model_check = model_check
        # AE04a: how many steps the parent turn still has, read at spawn time. A subagent never gets more than that
        # (a parent on its last step cannot hand a helper a fresh budget). None -- a manager outside a runtime --
        # leaves the profile tier alone.
        self.parent_remaining_iterations = parent_remaining_iterations
        # An employee package's or instance's own step cap. Accepted here so the seam exists; AE04b folds it into the
        # effective limit once packages carry one. Not applied yet.
        self.declared_max_iterations = declared_max_iterations
        self.event_bus = event_bus
        self.store = store
        self.memory_manager = memory_manager
        self.runtime_session_id = runtime_session_id
        self.states: dict[str, SubagentState] = {}
        self.agent_names: dict[str, str] = {}

    async def _cross_entry_child_admission(self) -> int | dict[str, Any] | None:
        """None for other parents. An int is the admitted child depth.

        A dict is a refusal and is returned to the model unchanged in shape
        from the other spawn refusals: success false, and the workflow code.
        """
        parent = self.parent_task
        metadata = dict(getattr(parent, "metadata", {}) or {})
        source = str(metadata.get("execution_source") or "")
        from ampuly.core.cross_entry_guards import (
            CROSS_ENTRY_SOURCES,
            CrossEntryRefused,
            admit_cross_entry_child,
        )

        if source not in CROSS_ENTRY_SOURCES:
            return None
        if self.store is None or parent is None or not str(getattr(parent, "id", "") or ""):
            return {
                "success": False,
                "error": "workflow_child_capacity_unreadable",
                "code": "workflow_child_capacity_unreadable",
            }
        extra = [
            "running" if not state.completion.is_set() else "done"
            for state in self.states.values()
        ]
        try:
            return await admit_cross_entry_child(
                self.store,
                parent.id,
                entry=source,
                project_id=str(getattr(parent, "project_id", "") or ""),
                extra_statuses=extra,
                parent=parent,
            )
        except CrossEntryRefused as exc:
            return {"success": False, "error": exc.code, "code": exc.code}

    async def spawn(
        self,
        *,
        profile: str,
        prompt: str,
        background: bool | None = None,
        isolation: str | None = None,
        description: str = "",
        name: str = "",
        model: str = "",
        mode: str = "default",
        fork_context_messages: list[dict[str, Any]] | None = None,
        fork_system_prompt: str = "",
        fork_allowed_tools: list[str] | None = None,
        fork_mode: bool = False,
        resident: bool = False,
    ) -> dict[str, Any]:
        if self.child_agent_factory is None:
            return {"error": "Native subagent factory is not configured", "success": False}
        cross_entry_depth = await self._cross_entry_child_admission()
        if isinstance(cross_entry_depth, dict):
            return cross_entry_depth
        # Direct and employee parents use the workflow child caps. Other
        # parents keep the native depth and task_mode.max_sub_agents checks.
        if cross_entry_depth is None:
            parent_depth = int(getattr(self.parent_task, "metadata", {}).get("_native_runtime_depth", 0) or 0)
            max_depth = int(self.config.system.native_runtime.subagent_max_depth or 3)
            if parent_depth >= max_depth:
                return {
                    "error": f"Maximum native subagent depth ({max_depth}) reached",
                    "success": False,
                }
            # AE04a: one run (one runtime turn) gets at most ``max_sub_agents`` subagents in total, and at most that many
            # alive at once. The model is told, in words it can act on, instead of the spawn blowing up the turn.
            limit = self._subagent_limit()
            if limit <= 0:
                return {
                    "error": "Subagents are switched off for this run (task_mode.max_sub_agents is 0). Do the work yourself.",
                    "success": False,
                    "limit": 0,
                }
            running = sum(1 for state in self.states.values() if not state.completion.is_set())
            if running >= limit:
                return {
                    "error": (
                        f"Subagent concurrency limit ({limit}) reached: {running} subagent(s) of this run are still "
                        "running. Wait for one with agent_wait before spawning another, or do the work yourself."
                    ),
                    "success": False,
                    "limit": limit,
                    "running": running,
                }
            if len(self.states) >= limit:
                return {
                    "error": (
                        f"Subagent limit ({limit}) reached: this run already spawned {len(self.states)} subagent(s). "
                        "Finish the work with the ones you have, or do it yourself."
                    ),
                    "success": False,
                    "limit": limit,
                    "spawned": len(self.states),
                }

        profile_cfg = self._profile_config(profile)
        effective_mode = str(mode or "default").strip() or "default"
        default_isolation = profile_cfg.default_isolation
        if not default_isolation:
            default_isolation = "shared" if profile in {"explore", "plan"} else "worktree"
        effective_isolation = str(isolation or default_isolation or "shared").strip().lower() or "shared"
        if effective_mode == "plan":
            effective_isolation = "shared"
        if effective_isolation not in {"shared", "worktree"}:
            effective_isolation = str(profile_cfg.default_isolation or "shared").strip().lower() or "shared"
        effective_background = profile_cfg.background if background is None else bool(background)
        effective_resident = bool(resident) and bool(effective_background)
        effective_model = str(model or profile_cfg.model or "").strip()
        if effective_model and self.model_check is not None:
            refusal = str(self.model_check(effective_model) or "")
            if refusal:
                # Before any worktree, saved state or event: nothing to clean up and nothing reported as started.
                return {
                    "error": f"Model {effective_model!r} cannot run a subagent ({refusal}). Use a model from the model "
                             "catalogue, or leave the model out to use this step's own model.",
                    "success": False,
                    "refused": refusal,
                }
        effective_description = str(description or prompt or "").strip()
        effective_name = str(name or "").strip()
        if effective_name and effective_name in self.agent_names:
            existing_state = self.states.get(self.agent_names[effective_name])
            if existing_state is not None and not existing_state.completion.is_set():
                return {"error": f"Subagent name `{effective_name}` is already in use", "success": False}

        # Read the parent's remaining steps exactly once, before any worktree, saved state or background task.
        # Carry this admission snapshot into the child: a second callback read after an await could disagree.
        try:
            effective_max_iterations = self._effective_max_iterations(profile_cfg)
        except Exception:  # A missing/invalid budget is not permission to give the child a fresh tier.
            return {
                "error": "The parent's remaining step budget is unavailable; no subagent was started.",
                "success": False,
                "refused": "parent_step_budget_unavailable",
            }
        if effective_max_iterations <= 0:
            return {
                "error": "The parent has no remaining steps; no subagent was started.",
                "success": False,
                "refused": "parent_step_budget_exhausted",
                "max_iterations": 0,
            }

        agent_id = f"na_{uuid.uuid4().hex[:10]}"
        worktree = None
        if effective_isolation == "worktree":
            base_path = str(getattr(self.parent_task, "metadata", {}).get("target_output_dir", "") or "").strip()
            worktree = await create_worktree(base_path or None, config=self.config)
        state = SubagentState(
            agent_id=agent_id,
            profile=profile,
            name=effective_name,
            description=effective_description,
            model=effective_model,
            mode=effective_mode,
            isolation=effective_isolation,
            max_iterations=effective_max_iterations,
            background=effective_background,
            resident=effective_resident,
            fork_mode=bool(fork_mode),
            worktree=worktree,
            fork_system_prompt=str(fork_system_prompt or ""),
            fork_context_messages=list(fork_context_messages or []),
            fork_allowed_tools=list(fork_allowed_tools or []),
        )
        self.states[agent_id] = state
        if effective_name:
            self.agent_names[effective_name] = agent_id
        self._ensure_comms_endpoint(state)
        await self._save_state(state, "running")
        if state.worktree and self.store and hasattr(self.store, "save_runtime_worktree_session"):
            await self.store.save_runtime_worktree_session(
                worktree_session_id=f"wt_{agent_id}",
                runtime_session_id=self.runtime_session_id,
                task_id=self.parent_task.id if self.parent_task else None,
                path=str(state.worktree.get("path", "") or ""),
                status="active",
                metadata=dict(state.worktree or {}),
            )
        await self._emit(
            "subagent_started",
            state,
            {
                "prompt": prompt,
                "description": effective_description,
                "name": effective_name,
                "isolation": effective_isolation,
                "mode": effective_mode,
                "model": effective_model,
                "fork_mode": state.fork_mode,
                "resident": state.resident,
            },
        )

        async def _execute_turn(turn_prompt: str) -> None:
            child: Task | None = None
            company_managed_child = False
            child_persisted = False
            save_child = getattr(self.store, "save_task", None)
            try:
                state.status = "running"
                state.update_event.set()
                await self._save_state(state, state.status)
                child = self._build_child_task(state, turn_prompt)
                child.metadata["_permission_bridge_runtime_session_id"] = self.runtime_session_id
                setattr(child, "_runtime_permission_bridge", self._build_permission_bridge(state, child))
                company_managed_child = bool(
                    str(child.metadata.get("delegation_run_id", "") or "").strip()
                    or str(child.metadata.get("execution_mode", "") or "").strip()
                    == "company_mode"
                )
                if company_managed_child and callable(save_child):
                    # The child Task is the durable execution envelope used by
                    # both structured artifact ownership and exact opaque-tool
                    # permits.  Persist every resident turn's fresh child ID
                    # before any handler can perform an effect.
                    await save_child(child)
                    child_persisted = True
                elif company_managed_child:
                    raise RuntimeError(
                        "company native subagent has no durable Task Store"
                    )
                child_agent = self._build_child_agent(
                    profile=profile,
                    allowed_tools=list(state.fork_allowed_tools) or self._resolve_allowed_tools(profile, mode=effective_mode),
                    prompt_addendum=self._profile_prompt(profile, mode=effective_mode),
                    state=state,
                )
                await self._ensure_child_session(child)
                setattr(child, "_runtime_inbox_queue", state.inbox)
                result = await child_agent.execute(child)
                if child_persisted and callable(save_child):
                    self._apply_result_to_child_task(child, result)
                    # Keep the subagent run in `running` until its Task result
                    # is durable; the company Store validates that exact
                    # parent chain on every child write.
                    await save_child(child)
                state.task_result = result
                state.latest_result = result.content
                terminal_status = result.status.value
                state.last_notification_kind = self._resident_notification_kind(result)
                state.status = "idle" if state.resident else terminal_status
                await self._save_state(state, state.status, {
                    "turn_status": terminal_status,
                    "notification_kind": state.last_notification_kind,
                })
                await self._emit(
                    "subagent_completed",
                    state,
                    {
                        "status": terminal_status,
                        "resident": state.resident,
                        "resident_status": state.status,
                        "accepts_followups": bool(state.resident or not state.completion.is_set()),
                        "pending_messages_count": state.pending_messages_count,
                        "content_preview": result.content[:500],
                    },
                )
                if state.resident:
                    await self._emit_worker_notification(
                        state,
                        notification_kind=state.last_notification_kind or "idle",
                        summary=result.content or f"{state.name or state.agent_id} is idle",
                    )
            except Exception as exc:  # pragma: no cover - defensive
                failure_content = str(exc)
                failed_result = TaskResult(
                    status=TaskStatus.FAILED,
                    content=failure_content,
                )
                if child_persisted and child is not None and callable(save_child):
                    self._apply_result_to_child_task(child, failed_result)
                    try:
                        await save_child(child)
                    except Exception as persist_exc:
                        failure_content = (
                            f"{failure_content}; failed to persist native subagent result: "
                            f"{persist_exc}"
                        )
                        failed_result = TaskResult(
                            status=TaskStatus.FAILED,
                            content=failure_content,
                        )
                state.latest_result = failure_content
                state.last_notification_kind = "error"
                state.task_result = failed_result
                state.status = "idle" if state.resident else TaskStatus.FAILED.value
                await self._save_state(state, state.status, {"error": failure_content, "notification_kind": state.last_notification_kind})
                await self._emit(
                    "subagent_completed",
                    state,
                    {
                        "status": TaskStatus.FAILED.value,
                        "resident": state.resident,
                        "resident_status": state.status,
                        "accepts_followups": bool(state.resident),
                        "pending_messages_count": state.pending_messages_count,
                        "content_preview": failure_content[:500],
                    },
                )
                if state.resident:
                    await self._emit_worker_notification(
                        state,
                        notification_kind="error",
                        summary=failure_content,
                    )
            finally:
                state.update_event.set()

        async def _runner() -> None:
            current_prompt = prompt
            try:
                while True:
                    await _execute_turn(current_prompt)
                    if not state.resident:
                        return
                    state.status = "idle"
                    state.update_event.set()
                    await self._save_state(state, state.status, {"notification_kind": state.last_notification_kind or "idle"})
                    next_message = await state.inbox.get()
                    state.pending_messages_count = max(0, state.pending_messages_count - 1)
                    current_prompt = str(next_message.get("body", "") or "").strip()
                    if not current_prompt:
                        current_prompt = str(next_message.get("message", "") or "").strip()
                    if not current_prompt:
                        current_prompt = str(next_message)
            finally:
                state.completion.set()
                state.update_event.set()
                if state.worktree and self.store and hasattr(self.store, "save_runtime_worktree_session"):
                    await self.store.save_runtime_worktree_session(
                        worktree_session_id=f"wt_{agent_id}",
                        runtime_session_id=self.runtime_session_id,
                        task_id=self.parent_task.id if self.parent_task else None,
                        path=str(state.worktree.get("path", "") or ""),
                        status="closed",
                        metadata=dict(state.worktree or {}),
                    )
                await cleanup_worktree(state.worktree)

        if effective_background:
            state.runtime_task = asyncio.create_task(_runner())
            return {
                "success": True,
                "agent_id": agent_id,
                "name": effective_name,
                "status": "running",
                "background": True,
                "resident": state.resident,
                "resident_status": state.status,
                "accepts_followups": bool(state.resident),
                "worktree_path": (state.worktree or {}).get("path", ""),
            }

        await _runner()
        return self._result_payload(state)

    async def wait(self, agent_id: str, timeout_seconds: int = 300) -> dict[str, Any]:
        resolved_id = self._resolve_agent_id(agent_id)
        state = self.states.get(resolved_id)
        if state is None:
            return {"error": f"Unknown subagent: {agent_id}", "success": False}
        if state.resident and state.status != "running":
            return self._result_payload(state)
        deadline = time.time() + max(1, int(timeout_seconds or 1))
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return self._result_payload(state)
            if state.completion.is_set():
                return self._result_payload(state)
            if state.resident and state.status != "running":
                return self._result_payload(state)
            try:
                await asyncio.wait_for(state.update_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return self._result_payload(state)
            state.update_event.clear()
        return self._result_payload(state)

    async def send(self, agent_id: str, message: str) -> dict[str, Any]:
        resolved_id = self._resolve_agent_id(agent_id)
        state = self.states.get(resolved_id)
        if state is None:
            return {"error": f"Unknown subagent: {agent_id}", "success": False}
        if state.completion.is_set():
            return {"error": f"Subagent {agent_id} has already completed", "success": False}
        rendered = str(message or "").strip()
        self._persist_follow_up_message(state, rendered)
        await state.inbox.put(
            {
                "body": rendered,
                "message_class": "chat",
                "actionable": True,
                "worker_id": state.agent_id,
                "origin_task_id": str(getattr(self.parent_task, "id", "") or "").strip(),
                "origin_session_id": str(getattr(self.parent_task, "session_id", "") or "").strip(),
            }
        )
        state.pending_messages_count += 1
        state.update_event.set()
        await self._save_state(state, state.status, {"queued_message": rendered[:500]})
        await self._emit(
            "subagent_updated",
            state,
            {
                "message": rendered[:500],
                "resident": state.resident,
                "resident_status": state.status,
                "pending_messages_count": state.pending_messages_count,
            },
        )
        return {"success": True, "agent_id": state.agent_id, "name": state.name, "status": state.status}

    def list_agents(self) -> dict[str, Any]:
        return {
            "success": True,
            "agents": [self._result_payload(state) for state in self.states.values()],
        }

    async def cancel_all(self, reason: str = "parent_turn_ended") -> list[str]:
        """Cancel every background and resident subagent of this run and wait for each to wind down (AE04a).

        A background subagent runs on its own ``asyncio.Task`` and a resident one waits on its inbox for ever, so
        neither ends with the parent turn by itself. Cancelling runs each task's own ``finally``, which closes its
        worktree session and removes the isolated copy; here the run is then recorded as ``cancelled`` for the store
        and the event stream. A subagent that finished on its own in the meantime keeps its own result. Calling this
        again finds nothing to do. Returns the ids that were cancelled.
        """
        pending: list[tuple[SubagentState, asyncio.Task[Any]]] = []
        for state in self.states.values():
            runtime_task = state.runtime_task
            if runtime_task is None or runtime_task.done():
                continue
            runtime_task.cancel()
            pending.append((state, runtime_task))
        if not pending:
            return []
        await asyncio.wait([runtime_task for _, runtime_task in pending])
        cancelled_ids: list[str] = []
        for state, runtime_task in pending:
            error = None if runtime_task.cancelled() else runtime_task.exception()
            if not runtime_task.cancelled() and error is None:
                continue  # finished by itself before the cancel landed: its own result stands
            state.status = "cancelled"
            state.update_event.set()
            cancelled_ids.append(state.agent_id)
            detail: dict[str, Any] = {"reason": reason}
            if error is not None:
                detail["error"] = str(error)
            try:
                await self._save_state(state, state.status, detail)
                await self._emit(
                    "subagent_completed",
                    state,
                    {
                        "status": state.status,
                        "reason": reason,
                        "resident": state.resident,
                        "resident_status": state.status,
                        "accepts_followups": False,
                        "pending_messages_count": state.pending_messages_count,
                        "content_preview": (state.latest_result or "")[:500],
                    },
                )
            except Exception as exc:  # noqa: BLE001 - the subagent is stopped either way; the record is best effort
                logger.warning("Could not record cancelled subagent {}: {}", state.agent_id, exc)
        return cancelled_ids

    def _build_child_task(self, state: SubagentState, prompt: str) -> Task:
        parent = self.parent_task or Task()
        metadata = dict(parent.metadata or {})
        for key in _NON_INHERITED_CHILD_IDENTITY_KEYS:
            metadata.pop(key, None)
        metadata.pop("_fork_allowed_tools", None)
        metadata["runtime_kind"] = "native_subagent"
        metadata["runtime_auxiliary_task"] = True
        metadata["runtime_auxiliary_kind"] = "native_subagent"
        metadata["user_visible"] = False
        metadata["shared_role_session"] = False
        if parent.session_id:
            metadata["parent_session_id"] = parent.session_id
        metadata["_native_runtime_depth"] = int(metadata.get("_native_runtime_depth", 0) or 0) + 1
        if str(metadata.get("execution_source") or "") in {"direct_task", "employee_delegate"}:
            try:
                parent_entry_depth = int((parent.metadata or {}).get("entry_child_depth") or 0)
            except (TypeError, ValueError):
                parent_entry_depth = 0
            metadata["entry_child_depth"] = parent_entry_depth + 1
        metadata["subagent_profile"] = state.profile
        metadata["_subagent_name"] = state.name
        metadata["_subagent_description"] = state.description
        metadata["_subagent_model"] = state.model
        metadata["_subagent_mode"] = state.mode
        metadata["_subagent_max_iterations"] = state.max_iterations
        metadata["_fork_mode"] = state.fork_mode
        if state.profile == "verify":
            metadata[self.config.system.native_runtime.verification_policy.skip_metadata_key] = True
            metadata["work_item_verification_required"] = False
        if state.worktree and state.worktree.get("path"):
            metadata["target_output_dir"] = state.worktree["path"]
        execution_context = dict((state.worktree or {}).get("execution_context", {}) or {})
        if execution_context:
            metadata["_execution_context"] = execution_context
        if state.fork_system_prompt:
            metadata["_runtime_system_prompt_override"] = state.fork_system_prompt
        if state.fork_context_messages:
            metadata["_fork_context_messages"] = list(state.fork_context_messages)
        if state.fork_allowed_tools:
            metadata["_fork_allowed_tools"] = list(state.fork_allowed_tools)
        metadata["_comms_endpoint_id"] = state.agent_id
        metadata["_comms_parent_endpoint_id"] = self._parent_endpoint_id()
        parent_work_item_id = linked_work_item_id_for_task(parent)
        if parent_work_item_id:
            # Store validates this copied hint against the durable parent link
            # before it can participate in an exact opaque-effect identity.
            metadata["_company_parent_work_item_id"] = parent_work_item_id
        metadata["_subagent_resident"] = state.resident
        return Task(
            title=state.name or state.description or f"{state.profile} subagent",
            description=prompt,
            assigned_to=parent.assigned_to,
            project_id=parent.project_id,
            session_id=f"{parent.session_id or 'session'}:{state.agent_id}",
            parent_session_id=parent.session_id,
            parent_id=parent.id,
            tags=list(parent.tags),
            metadata=metadata,
        )

    async def _ensure_child_session(self, child: Task) -> None:
        ensure_session = getattr(self.memory_manager, "ensure_session", None)
        if not callable(ensure_session) or not child.session_id:
            return
        await ensure_session(
            child.session_id,
            project_id=child.project_id,
            title=child.title,
            mode="child",
            parent_session_id=child.parent_session_id,
            metadata={
                "runtime_kind": "native_subagent",
                "user_visible": False,
                "shared_role_session": False,
                "subagent_profile": str(
                    child.metadata.get("subagent_profile", "") or ""
                ).strip(),
                "subagent_name": str(
                    child.metadata.get("_subagent_name", "") or ""
                ).strip(),
                "source_task_id": str(child.parent_id or "").strip(),
            },
        )

    @staticmethod
    def _apply_result_to_child_task(child: Task, result: TaskResult) -> None:
        child.status = result.status
        child.result = {
            "content": result.content,
            "artifacts": dict(result.artifacts or {}),
        }

    def _resolve_allowed_tools(self, profile: str, mode: str = "default") -> list[str]:
        profiles = self.config.agents.native_subagents or {}
        profile_cfg: NativeSubagentProfileConfig = profiles.get(profile) or profiles.get("general") or NativeSubagentProfileConfig()
        if profile_cfg.allowed_tools:
            return self._apply_mode_tool_filter(list(profile_cfg.allowed_tools), mode)
        read_only = [
            "file_read",
            "file_search",
            "list_dir",
            "web_search",
            "web_fetch",
            "todo_read",
            "todo_write",
            "request_user_input",
            "agent_spawn",
            "agent_wait",
            "agent_send",
            "agent_list",
        ]
        implement = [
            "shell_exec",
            "file_read",
            "file_write",
            "file_edit",
            "file_search",
            "list_dir",
            "web_search",
            "web_fetch",
            "python_exec",
            "todo_read",
            "todo_write",
            "request_user_input",
            "agent_spawn",
            "agent_wait",
            "agent_send",
            "agent_list",
        ]
        verify = [
            "shell_exec",
            "file_read",
            "file_search",
            "list_dir",
            "web_search",
            "web_fetch",
            "python_exec",
            "browser_navigate",
            "browser_snapshot",
            "browser_observe",
            "browser_click",
            "browser_type",
            "browser_submit_enter",
            "browser_wait_for",
            "browser_scroll",
            "browser_take_screenshot",
            "browser_close",
            "todo_read",
            "todo_write",
            "request_user_input",
            "agent_spawn",
            "agent_wait",
            "agent_send",
            "agent_list",
        ]
        mapping = {
            "general": implement,
            "explore": read_only,
            "plan": read_only,
            "implement": implement,
            "verify": verify,
        }
        return self._apply_mode_tool_filter(mapping.get(profile, implement), mode)

    def _profile_prompt(self, profile: str, mode: str = "default") -> str:
        prompts = {
            "general": "Complete the task directly and report only the essential outcome.",
            "explore": "Read-only exploration only. Do not modify files or system state.",
            "plan": "Read-only planning only. Produce a concise implementation plan with critical files.",
            "implement": "Implement directly in the assigned workspace, then verify your changes with commands.",
            "verify": (
                "Try to break the implementation. Prefer executable checks over code reading. "
                "For every check you actually ran, emit `Check:`, `Command:`, `Observed Output:`, and `Result:` lines. "
                "End with exactly one line `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`. "
                "You may start the final summary with `VERIFIED:` if the work is acceptable, or `ISSUES:` if blocking problems remain."
            ),
        }
        base = prompts.get(profile, prompts["general"])
        normalized_mode = str(mode or "default").strip().lower()
        if normalized_mode == "plan":
            return base + " Operate in plan mode: do not make filesystem or shell changes."
        if normalized_mode in {"accept_edits", "bypass_permissions", "dont_ask", "acceptedits", "bypasspermissions", "dontask"}:
            return base + f" Runtime spawn mode hint: {mode}."
        return base

    def _profile_config(self, profile: str) -> NativeSubagentProfileConfig:
        profiles = self.config.agents.native_subagents or {}
        return profiles.get(profile) or profiles.get("general") or NativeSubagentProfileConfig()

    def _subagent_limit(self) -> int:
        """``task_mode.max_sub_agents``: the run's total and its concurrency, one value (AE04a). 0 or less: none."""
        return max(0, int(self.config.system.task_mode.max_sub_agents))

    def _effective_max_iterations(self, profile_cfg: NativeSubagentProfileConfig) -> int:
        """The profile tier capped at the parent's one admission snapshot (AE04a).

        Zero or less stops spawn before any effect: never send zero to a child that reads it as "not set".
        A provided callback must return an integer; malformed/unavailable budgets fail closed in spawn.
        ``declared_max_iterations`` is not folded in here yet (AE04b).
        """
        tier = max(1, int(profile_cfg.max_iterations or 24))
        if self.parent_remaining_iterations is None:
            return tier
        remaining = self.parent_remaining_iterations()
        if type(remaining) is not int:
            if inspect.iscoroutine(remaining):
                remaining.close()  # The callback is synchronous; do not run an accidentally async budget provider.
            raise ValueError("parent remaining iterations must be an integer")
        return max(0, min(tier, remaining))

    def _apply_mode_tool_filter(self, tools: list[str], mode: str) -> list[str]:
        if str(mode or "default").strip().lower() != "plan":
            return list(tools)
        plan_safe = {
            "file_read",
            "file_search",
            "list_dir",
            "web_search",
            "web_fetch",
            "todo_read",
            "todo_write",
            "request_user_input",
            "agent_spawn",
            "agent_wait",
            "agent_send",
            "agent_list",
        }
        return [tool for tool in tools if tool in plan_safe]

    def _build_child_agent(
        self,
        *,
        profile: str,
        allowed_tools: list[str],
        prompt_addendum: str,
        state: SubagentState,
    ) -> Any:
        overrides = {
            "name": state.name,
            "description": state.description,
            "model": state.model,
            "mode": state.mode,
            "max_iterations": state.max_iterations,
        }
        signature = inspect.signature(self.child_agent_factory)
        accepts_overrides = len(signature.parameters) >= 4 or any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if accepts_overrides:
            return self.child_agent_factory(profile, allowed_tools, prompt_addendum, overrides)
        if state.model:
            # The model would be dropped silently while subagent_started reports it: refuse instead.
            from ampuly.llm.budget import RouteRefused

            raise RouteRefused(f"this runtime cannot run a subagent on {state.model!r}",
                               code="subagent_override_unsupported", model=state.model)
        return self.child_agent_factory(profile, allowed_tools, prompt_addendum)

    def _build_permission_bridge(
        self,
        state: SubagentState,
        child_task: Task | None = None,
    ) -> Callable[..., Awaitable[tuple[bool, Any]]]:
        async def _bridge(
            *,
            tool: Any,
            arguments: dict[str, Any],
            approval_engine: Any,
            on_progress: Any = None,
            call_context: dict[str, Any] | None = None,
        ) -> tuple[bool, Any]:
            execution_task = child_task or self.parent_task or Task(project_id="default")
            metadata = {
                "category": getattr(tool, "category", "general"),
                "requires_confirmation": getattr(tool, "requires_confirmation", False),
                "description": getattr(tool, "description", ""),
                "permission_effects": list(getattr(tool, "permission_effects", None) or []),
                "subagent_id": state.agent_id,
                "subagent_profile": state.profile,
                "subagent_name": state.name,
                "subagent_mode": state.mode,
                "parent_runtime_session_id": self.runtime_session_id,
            }
            return await approval_engine.authorize_tool_call(
                # ToolCall/ToolResult ledger identity belongs to the child
                # runtime.  Owner visibility is resolved separately from the
                # child's durable parent chain when the checkpoint is built.
                task=execution_task,
                tool_name=getattr(tool, "name", ""),
                arguments=arguments,
                metadata=metadata,
                on_progress=on_progress,
                call_context=dict(call_context or {}),
                tool=tool,
            )

        return _bridge

    def _resolve_agent_id(self, agent_id: str) -> str:
        raw = str(agent_id or "").strip()
        if raw in self.states:
            return raw
        return self.agent_names.get(raw, raw)

    def _parent_endpoint_id(self) -> str:
        if self.parent_task is None:
            return "runtime-parent"
        session_id = str(getattr(self.parent_task, "session_id", "") or "").strip()
        task_id = str(getattr(self.parent_task, "id", "") or "").strip()
        return f"task::{session_id or task_id or 'runtime-parent'}"

    def _comms_layout(self):
        if self.parent_task is None:
            return None
        workspace_root = (
            str(getattr(self.parent_task, "metadata", {}).get("comms_workspace_root", "") or "").strip()
            or str(getattr(self.parent_task, "metadata", {}).get("workspace_root", "") or "").strip()
            or str(getattr(self.parent_task, "metadata", {}).get("target_output_dir", "") or "").strip()
        )
        if not workspace_root:
            return None
        try:
            from ampuly.layer2_organization import comms as _comms

            return _comms.resolve_layout(
                workspace_root,
                str(getattr(self.parent_task, "project_id", "") or "default").strip() or "default",
                str(getattr(self.parent_task, "parent_session_id", "") or getattr(self.parent_task, "session_id", "") or "default").strip() or "default",
            )
        except Exception:
            return None

    def _ensure_comms_endpoint(self, state: SubagentState) -> None:
        layout = self._comms_layout()
        if layout is None:
            return
        try:
            from ampuly.layer2_organization import comms as _comms

            _comms.ensure_layout(layout, [self._parent_endpoint_id(), state.agent_id])
        except Exception:
            return

    def _persist_follow_up_message(self, state: SubagentState, message: str) -> None:
        if not message:
            return
        layout = self._comms_layout()
        if layout is None:
            return
        try:
            from ampuly.layer2_organization import comms as _comms

            _comms.send_message(
                layout,
                from_role=self._parent_endpoint_id(),
                to_role=state.agent_id,
                subject=f"Follow-up for {state.name or state.agent_id}",
                body=message,
                priority="normal",
                extra_frontmatter={
                    "transport_kind": "system",
                    "semantic_type": "work_update",
                    "message_class": "chat",
                    "actionable": True,
                    "worker_id": state.agent_id,
                    "origin_task_id": str(getattr(self.parent_task, "id", "") or "").strip(),
                    "origin_session_id": str(getattr(self.parent_task, "session_id", "") or "").strip(),
                    "comms_state": "open",
                    "from_endpoint_type": "native_subagent",
                    "to_endpoint_type": "native_subagent",
                    "refs": {
                        "task_id": str(getattr(self.parent_task, "id", "") or "").strip(),
                        "runtime_session_id": self.runtime_session_id,
                    },
                },
            )
        except Exception:
            return

    async def _emit(self, event_type: str, state: SubagentState, payload: dict[str, Any]) -> None:
        if not self.event_bus:
            return
        await self.event_bus.publish(AmpulyEvent(
            event_type="runtime_event",
            payload={
                "type": event_type,
                "agent_id": state.agent_id,
                "profile": state.profile,
                "task_id": str(getattr(self.parent_task, "id", "") or "").strip(),
                "session_id": str(getattr(self.parent_task, "session_id", "") or "").strip(),
                "resident": state.resident,
                "resident_status": state.status,
                "pending_messages_count": state.pending_messages_count,
                **payload,
            },
        ))

    async def _save_state(self, state: SubagentState, status: str, metadata: dict[str, Any] | None = None) -> None:
        if not self.store or not hasattr(self.store, "save_runtime_subagent_run"):
            return
        merged_metadata = {
            "name": state.name,
            "description": state.description,
            "model": state.model,
            "mode": state.mode,
            "isolation": state.isolation,
            "max_iterations": state.max_iterations,
            "fork_mode": state.fork_mode,
            "fork_context_messages": len(state.fork_context_messages),
            "resident": state.resident,
            "resident_status": state.status,
            "accepts_followups": bool(state.resident and not state.completion.is_set()),
            "pending_messages_count": state.pending_messages_count,
            "last_notification_kind": state.last_notification_kind,
        }
        merged_metadata.update(metadata or {})
        await self.store.save_runtime_subagent_run(
            subagent_run_id=state.agent_id,
            runtime_session_id=self.runtime_session_id,
            task_id=self.parent_task.id if self.parent_task else None,
            agent_id=state.agent_id,
            profile=state.profile,
            status=status,
            worktree_path=str((state.worktree or {}).get("path", "") or ""),
            metadata=merged_metadata,
        )

    async def _emit_worker_notification(
        self,
        state: SubagentState,
        *,
        notification_kind: str,
        summary: str,
        actionable: bool = False,
    ) -> None:
        payload = {
            "worker_id": state.agent_id,
            "worker_type": "native_subagent",
            "notification_kind": str(notification_kind or "idle").strip() or "idle",
            "summary": str(summary or "").strip(),
            "task_id": str(getattr(self.parent_task, "id", "") or "").strip(),
            "session_id": str(getattr(self.parent_task, "session_id", "") or "").strip(),
            **work_item_identity_payload_for_task(self.parent_task),
            "projection_id": projection_id_for_task(self.parent_task) if self.parent_task is not None else "",
            "details_ref": state.agent_id,
            "actionable": bool(actionable),
            "resident_status": state.status,
            "pending_messages_count": state.pending_messages_count,
            "name": state.name,
        }
        await self._emit("worker_notification", state, payload)

    @staticmethod
    def _resident_notification_kind(result: TaskResult) -> str:
        if result.status == TaskStatus.AWAITING_HUMAN:
            return "permission_needed"
        if result.status in {
            TaskStatus.AWAITING_PEER,
            TaskStatus.AWAITING_MANAGER_REVIEW,
            TaskStatus.AWAITING_REVIEW,
        }:
            return "blocked"
        if result.status == TaskStatus.FAILED:
            return "error"
        if result.status == TaskStatus.DONE:
            return "task_complete"
        return "idle"

    def _result_payload(self, state: SubagentState) -> dict[str, Any]:
        payload = {
            "success": state.task_result is not None and state.task_result.status == TaskStatus.DONE,
            "agent_id": state.agent_id,
            "profile": state.profile,
            "name": state.name,
            "description": state.description,
            "model": state.model,
            "mode": state.mode,
            "isolation": state.isolation,
            "max_iterations": state.max_iterations,
            "status": state.status,
            "background": state.background,
            "fork_mode": state.fork_mode,
            "resident": state.resident,
            "resident_status": state.status,
            "accepts_followups": bool(state.resident and not state.completion.is_set()),
            "pending_messages_count": state.pending_messages_count,
            "last_notification_kind": state.last_notification_kind,
            "result": state.latest_result,
            "worktree_path": (state.worktree or {}).get("path", ""),
            "venv_path": (state.worktree or {}).get("venv_path", ""),
            "python_executable": (state.worktree or {}).get("python_executable", ""),
        }
        if state.task_result and state.task_result.status == TaskStatus.AWAITING_HUMAN:
            artifacts = dict(state.task_result.artifacts or {})
            payload.update(
                {
                    "requires_user_input": True,
                    "reason": state.task_result.content,
                    "approval": dict(artifacts.get("approval", {}) or {}),
                    "permission_requests": list(artifacts.get("permission_requests", []) or []),
                }
            )
        if state.task_result and state.task_result.status == TaskStatus.AWAITING_PEER:
            artifacts = dict(state.task_result.artifacts or {})
            payload.update(
                {
                    "requires_peer_wait": True,
                    "reason": state.task_result.content,
                    "permission_requests": list(artifacts.get("permission_requests", []) or []),
                }
            )
        return payload
