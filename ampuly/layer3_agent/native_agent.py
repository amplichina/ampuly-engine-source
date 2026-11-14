"""AMPULY Native Agent — the primary agent implementation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Coroutine

from loguru import logger

from ampuly.core.company_tools import (
    COMPANY_ALL_COLLABORATION_TOOL_NAMES,
    MULTI_TEAM_COORDINATION_TURN_MODES,
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from ampuly.core.config import AmpulyConfig
from ampuly.core.models import AgentInfo, AgentStatus, ExecutionMode, Task, TaskResult, TaskStatus
from ampuly.core.events import EventBus
from ampuly.core.models import AmpulyEvent
from ampuly.core.worker_envelope import classify_worker_message
from ampuly.i18n_runtime import is_zh
from ampuly.llm.budget import BUDGET_STOP_ARTIFACT, ModelBudgetStop, RouteRefused, budget_stop_kind
from ampuly.llm.provider import LLMProvider, ProviderQuotaExhaustedError, current_credential_scope
from ampuly.layer1_perception.context_assembler import ContextAssembler
from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
from ampuly.layer0_interaction.coordinator import InteractionCoordinator
from ampuly.layer3_agent.company_runtime_contract import build_company_work_item_contract
from ampuly.layer3_agent.runtime_v2 import NativeRuntimeV2
from ampuly.layer3_agent.runtime_v2.runtime import native_steps_used
from ampuly.layer3_agent.prompt_harness import PromptHarnessBuilder
from ampuly.layer3_agent.prompt_harness.builder import _final_decider_role_id, _memory_skill_user_facing
from ampuly.layer4_tools.output_budget import clip_text
from ampuly.layer4_tools.registry import ToolRegistry
from ampuly.layer5_memory.memory_manager import MemoryManager
from ampuly.layer5_memory.preference import PreferenceManager
from ampuly.layer5_memory.skill_library import SkillLibrary, frozen_skill_kwargs
from ampuly.layer6_observability.cost_tracker import CostTracker
from ampuly.layer3_agent.prompt_harness.sections import (
    HONEST_REPORTING_CONTRACT,
    LONG_RUNNING_SESSION_CONTRACT,
    MEMORY_TRUST_CONTRACT,
    SAFE_ACTIONS_CONTRACT,
    SUBAGENT_HARNESS_CONTRACT,
)

# ---------------------------------------------------------------------------
# Role-aware system prompt components
# ---------------------------------------------------------------------------

_CORE_HEADER = (
    "You are {role_name}, an AI agent in the AMPULY (One-Person Company) system.\n"
    "Role: {responsibility}\n\n"
    "You accomplish tasks by using the tools available to your role."
)

_TASK_MODE_CORE_HEADER = (
    "You are {role_name}, an Ampuly task execution agent.\n"
    "Role: {responsibility}\n\n"
    "You accomplish standalone user tasks by using the tools available to your role."
)

_CORE_OPERATING_PRINCIPLES = """
## Core Operating Principles
- Use available context and tools before asking the user for missing information.
- Own the user's goal within the explicit scope and keep moving with the best
  evidence available.
- Be honest about uncertainty, failed attempts, unavailable tools, and
  unverified results.
- Follow the runtime safety, reporting, memory, and subagent contracts when
  actions become risky or stateful.
- Use the current tool strategy and tool schemas as the source of truth for
  choosing tools and exact arguments.
"""


_NATIVE_WORKING_CONTRACT = """
## Native Working Contract
- Use the task brief, runtime context, available tools, and explicit runtime
  addenda to choose the right working posture for this turn.
- Treat planning, execution, review, verification, and synthesis as flexible
  working postures, not as prompt profiles selected by metadata.
- Prefer concrete, evidence-backed progress over describing hypothetical work.
- Keep implementation changes scoped to the request and consistent with the
  project.

## Planning And Review Practice
- For planning, produce decision-complete steps with clear inputs, outputs,
  handoffs, risks, and validation targets.
- For review, inspect the current workspace and evidence directly. Do not
  approve, reject, or repeat old findings without checking the current state.
- When a runtime addendum requires a structured verdict, dispatch, report, or
  handoff shape, follow that addendum exactly.

## Native Self-Verification Contract
- Before final delivery, check the user's goal against the actual changes,
  artifacts, and paths you touched.
- When you change code, files, UI behavior, commands, or generated artifacts,
  prefer executable evidence: targeted tests, lint/type checks, smoke commands,
  browser checks, or direct artifact inspection.
- If you cannot run a relevant verification step, say so plainly in one
  sentence and explain the constraint.
- Include a short verification status in the final reply when you changed
  something or when the runtime asks for one.
- If verification reveals a blocking issue, fix it before finishing when
  possible. If it cannot be fixed in this turn, report the blocker honestly
  instead of presenting the work as complete.
"""

_USER_INPUT_GUIDELINES = """
## User Input Recovery
- If the latest user reply resolves the blocker, continue instead of asking again.
- If it is incomplete or ambiguous, ask only for the exact remaining gap.
- Never repeat the same broad question or ask for what the user already provided.
"""

_TASK_MODE_ORCHESTRATION = """
## Task-Mode Orchestration
- You are the user's primary task-mode execution agent for this session.
- Execute as a single full-capability agent; do not model task mode as a
  company organization, recruiting flow, employee persona, or staff assignment.
- Treat the `task_generalist` role id as routing and logging metadata only, not
  as a persona source.
- Carry out the action assigned by the current turn instead of merely narrating what you would do.
- Use `agent_spawn`, `agent_wait`, and `agent_send` only for bounded parallel
  work or context isolation when that improves the result.
"""

# file_write / file_edit are deliberately NOT blocked: coordination turns
# produce in-context content (briefs, matrices, review notes) that must be
# persistable to the workspace, or it gets trapped in blocking DM hand-offs.
_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST = {
    "shell_exec",
    "apply_patch",
    "python_exec",
    "web_search",
    "web_fetch",
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_snapshot",
    "browser_observe",
    "browser_decide_act",
    "browser_type",
    "browser_submit_enter",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
    "browser_close",
    "git_status",
    "git_commit",
    "git_diff",
    "agent_spawn",
    "agent_wait",
    "agent_send",
    "agent_list",
}

# The explicit way for a role to ask for every registered general tool.  An
# empty ``role.tools`` used to mean this implicitly (company mode: the whole
# registry; task mode: no gating at all), so a role saved without tools --
# a new console role, an inspector with every box unticked, a market preset
# whose tool set name did not resolve -- quietly got shell and the browser.
# an empty list is an empty allow-list and grants nothing;
# a role that wants the whole registry says so with ``"*"``.
ALL_GENERAL_TOOLS_WILDCARD = "*"

_PROMPT_PROFILE_COMMUNICATION = """
## Communication Contract
- Before the first meaningful tool action, briefly state the immediate plan.
- During longer work, give short progress updates when you find a root cause,
  change direction, or complete a meaningful milestone.
- Final delivery must be outcome-first and include an explicit verification
  status when the runtime asks for one.
"""

_PROMPT_PROFILE_HARNESS = """
## Runtime Harness Reminder
- The runtime may compact history, summarize older turns, and re-inject structured runtime artifacts.
- Preserve important state in task tools and artifacts rather than only in free-form prose.
- When resuming work, trust the reinjected runtime state before re-solving old steps.
"""


@dataclass
class NativePromptBundle:
    """Layered prompt payload for the native runtime."""

    profile_name: str
    stable_system_prompt: str
    runtime_policy_messages: list[dict[str, Any]] = field(default_factory=list)


class PromptProfileManager:
    """Build unified native prompts with stable static sections."""

    UNIFIED_PROFILE = "unified"

    def __init__(self, role: AgentInfo, config: AmpulyConfig) -> None:
        self.role = role
        self.config = config

    def resolve_profile(self, task: Task) -> str:
        _ = task
        # Compatibility/observability label only. Prompt profiles are no longer
        # selected from YAML; the native prompt is intentionally unified.
        return self.UNIFIED_PROFILE

    def build_stable_system_prompt(self, task: Task) -> tuple[str, str]:
        profile = self.resolve_profile(task)
        header = _TASK_MODE_CORE_HEADER if self._is_task_mode_task(task) else _CORE_HEADER
        parts: list[str] = [
            header.format(
                role_name=self.role.name,
                responsibility=self.role.responsibility,
            ),
            _CORE_OPERATING_PRINCIPLES,
            SAFE_ACTIONS_CONTRACT,
            HONEST_REPORTING_CONTRACT,
            MEMORY_TRUST_CONTRACT,
            SUBAGENT_HARNESS_CONTRACT,
            _NATIVE_WORKING_CONTRACT,
            _USER_INPUT_GUIDELINES,
            _PROMPT_PROFILE_COMMUNICATION,
            _PROMPT_PROFILE_HARNESS,
            LONG_RUNNING_SESSION_CONTRACT,
        ]
        return profile, "\n\n".join(part for part in parts if part)

    def build_runtime_policy_messages(self, task: Task) -> list[dict[str, Any]]:
        parts: list[str] = []

        if self._is_company_mode_task(task):
            parts.append(self._build_company_work_item_contract(task))

        if self._is_task_mode_task(task):
            parts.append(_TASK_MODE_ORCHESTRATION)
        if self.role.prompt_refs and not self._is_task_generalist_role(task):
            parts.append("## Role Operating Instructions\n" + "\n\n".join(self.role.prompt_refs))
        runtime_prompt_addendum = str(task.metadata.get("_subagent_profile_prompt", "") or "").strip()
        if runtime_prompt_addendum:
            parts.append(f"## Runtime Profile Override\n{runtime_prompt_addendum}")
        return [
            {"role": "system", "content": part}
            for part in parts
            if str(part or "").strip()
        ]

    def build_prompt_bundle(self, task: Task) -> NativePromptBundle:
        profile, stable_prompt = self.build_stable_system_prompt(task)
        return NativePromptBundle(
            profile_name=profile,
            stable_system_prompt=stable_prompt,
            runtime_policy_messages=self.build_runtime_policy_messages(task),
        )

    def build_prompt(self, task: Task) -> tuple[str, str]:
        bundle = self.build_prompt_bundle(task)
        parts = [
            bundle.stable_system_prompt,
            *[
                str(message.get("content", "") or "").strip()
                for message in bundle.runtime_policy_messages
            ],
        ]
        return bundle.profile_name, "\n\n".join(part for part in parts if part)

    @staticmethod
    def _is_task_mode_task(task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return mode in {"project", "task"} or execution_mode == ExecutionMode.TASK_MODE.value

    @staticmethod
    def _is_company_mode_task(task: Task) -> bool:
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return execution_mode == ExecutionMode.COMPANY_MODE.value

    def _is_task_generalist_role(self, task: Task) -> bool:
        role_id = str(getattr(self.role, "role_id", "") or "").strip()
        return role_id == "task_generalist" and self._is_task_mode_task(task)

    def _build_company_work_item_contract(self, task: Task) -> str:
        return build_company_work_item_contract(task)


def child_llm_for(llm: Any, model_override: str) -> Any:
    """The provider a subagent runs on: the parent's own, or -- for another model -- that vendor's sibling.

    A model named by the parent (or a subagent profile) runs only if the catalogue lists it, and then through
    ``for_model`` -- never a copy of the parent's config carrying the parent's key, base and effort to another vendor
    (model-routing phase 0). Customer keys are refused inside ``for_model``; a scripted parent gets a scripted child.
    """
    chosen = str(model_override or "").strip()
    if not chosen or chosen == str(getattr(getattr(llm, "config", None), "default_model", "") or ""):
        return llm
    listed = getattr(llm, "model_listed", None)
    for_model = getattr(llm, "for_model", None)
    if not callable(for_model) or not callable(listed) or listed(chosen) is not True:
        raise RouteRefused(f"subagent model {chosen!r} is not in the model catalogue",
                           code="subagent_model_not_listed", model=chosen)
    customer_id, environment_id = current_credential_scope()
    return for_model(chosen, customer_id=customer_id, environment_id=environment_id)


# ---------------------------------------------------------------------------
# AE04a: a work item's steps are counted over all its turns; at the role's limit it stops for a person
# ---------------------------------------------------------------------------

# The budget-stop kind when an item reached its role's step limit, before or during a turn. It rides the
# allowance's rail (``budget_stop_wait``: the same card, checkpoint, answers and no retry) but is not the allowance's
# doing, and every word the owner reads about it says so.
STEP_LIMIT_STOP = "step_limit"
# How many more rounds of the limit the owner has released on that card (「继续这一项」), kept on the task beside the
# count of steps used (``runtime.NATIVE_STEPS_USED_KEY``). Missing means none.
NATIVE_STEPS_RELEASED_KEY = "native_steps_released"


def native_step_limit(config: AmpulyConfig | None) -> int:
    """The role's step limit per work item: ``system.max_agent_iterations`` (50 by default).

    A package's or an instance's own cap is AE04b's to fold in (the strictest of platform, package, instance, order);
    this is the one place that would read them.
    """
    limit = getattr(getattr(config, "system", None), "max_agent_iterations", None)
    if limit is None:
        limit = AmpulyConfig().system.max_agent_iterations
    return max(0, int(limit))


def native_steps_released(task: Task | None) -> int:
    """The rounds of the limit the owner has released for this item; 0 when missing or unreadable."""
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    try:
        return max(0, int(metadata.get(NATIVE_STEPS_RELEASED_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


def native_steps_remaining(task: Task | None, limit: int) -> int:
    """What a turn of this item may still walk: the limit, plus the rounds the owner released, less the steps used."""
    allowance = max(0, int(limit)) * (1 + native_steps_released(task))
    return max(0, allowance - native_steps_used(task))


def release_native_steps(task: Task) -> int:
    """The owner's 「继续这一项」 on the step-limit card: one more round of the limit for this item. Returns the rounds
    released so far. Nothing is reset: the steps already walked stay on the record."""
    task.metadata = dict(task.metadata or {})
    released = native_steps_released(task) + 1
    task.metadata[NATIVE_STEPS_RELEASED_KEY] = released
    return released


def _step_limit_words(*, used: int, limit: int, one_answer: bool, turn_started: bool = False) -> dict[str, str]:
    if is_zh():
        turn = ("这一回合已执行，已使用的模型调用和费用保留在本次记录中。" if turn_started
                else "这一回合没有开始，没有发出模型调用，也没有花费。")
        allowance = "订单预算没有拦这一项。" if turn_started else "订单预算没有拦这一项，也没有被动。"
        reason = (f"这一项累计已走 {used} 步，到了岗位上限（{limit} 步）：{turn}"
                  f"这是岗位的步数上限，不是订单额度：{allowance}这一项已停下来等您，没有判失败，也不会自动重试。")
        if one_answer:
            reason += f"这张卡只有「继续这一项」一个回答：选它会再放一轮 {limit} 步让它接着做；不想继续，请取消订单。"
        else:
            reason += f"选「继续这一项」会再放一轮 {limit} 步让它接着做；也可以判这一项失败。"
        return {
            "header": "岗位上限",
            "headline": f"{{title}}到了岗位上限（累计 {used} 步，上限 {limit} 步），停下等您",
            "reason": reason,
            "detail": f"到了岗位上限：累计已走 {used} 步，岗位上限 {limit} 步，" + ("这一回合已执行。" if turn_started else "这一回合没有开始。"),
            "failed": f"这一项到了岗位上限（累计 {used} 步，上限 {limit} 步），owner 判定这一项失败（不是订单额度的问题）。",
        }
    turn = ("this turn ran; its model calls and costs remain in the turn record. " if turn_started
            else "this turn was not started, no model call was sent and nothing was spent. ")
    allowance = "the allowance did not stop this item. " if turn_started else "the allowance did not stop this item and was not touched. "
    reason = (f"This item has used {used} steps over its turns and reached its role's step limit ({limit}): {turn}"
              f"This is the role's step limit, not the order's allowance -- {allowance}The item has stopped "
              "and is waiting for you; it has not failed and will not be retried on its own. ")
    if one_answer:
        reason += (f"This card has one answer, “Continue this item”: it releases one more round of {limit} steps. To "
                   "stop instead, cancel the order.")
    else:
        reason += f"“Continue this item” releases one more round of {limit} steps -- or fail it."
    return {
        "header": "Role step limit",
        "headline": f"{{title}} reached its role's step limit ({used} of {limit} steps) and is waiting for you",
        "reason": reason,
        "detail": f"Role step limit reached: {used} steps used over the item's turns, limit {limit}; " + ("this turn ran." if turn_started else "this turn was not started."),
        "failed": f"This item reached its role's step limit ({used} of {limit} steps); the owner failed it (not the order's allowance).",
    }


def step_limit_stopped_result(task: Task, *, limit: int, used: int, turn_started: bool = False) -> TaskResult:
    """The result when an item reaches its role's step limit, before or during a turn (AE04a).

    A work item of a company run waits for a person on the budget-stop rail -- the card, the durable checkpoint, the
    two answers and their parsing are ``budget_stop_wait``'s, built by its own request builder so the wait record,
    the event numbering and the one-answer rule for review and report cards stay its -- with this stop's own words
    in place of the allowance's: 「到了岗位上限」, not 「订单额度用完」. Anything else (a trial, a plain task, a hidden
    helper card) stops as a failed result carrying the mark, which the engine never retries.
    """
    from ampuly.layer2_organization import budget_stop_wait

    words = _step_limit_words(used=int(used), limit=int(limit), one_answer=False, turn_started=turn_started)
    if not budget_stop_wait.waits_for_a_person(task):
        return TaskResult(status=TaskStatus.FAILED, content=words["detail"], artifacts={BUDGET_STOP_ARTIFACT: STEP_LIMIT_STOP})
    from ampuly.layer2_organization.company_mode import owner_facing_turn_title

    request = budget_stop_wait._pause_request(task, STEP_LIMIT_STOP, detail=words["detail"])
    questions = [item for item in list(request.get("input_questions", []) or []) if isinstance(item, dict)]
    one_answer = bool(questions) and len(list(questions[0].get("options", []) or [])) == 1
    words = _step_limit_words(used=int(used), limit=int(limit), one_answer=one_answer, turn_started=turn_started)
    subject = owner_facing_turn_title(str(task.title or task.id), dict(task.metadata or {}))
    headline = words["headline"].format(title=subject)
    request.update({"reason": words["reason"], "headline": headline[:1].upper() + headline[1:]})
    for question in questions:
        question["header"] = words["header"]
    return TaskResult(status=TaskStatus.AWAITING_HUMAN, content=request["headline"],
                      artifacts={BUDGET_STOP_ARTIFACT: STEP_LIMIT_STOP, "pause_request": request})


def step_limit_failed_by_owner(task: Task, *, limit: int) -> TaskResult:
    """The owner's 「判这一项失败」 on the step-limit card: the allowance's settlement, with the cause in its own words."""
    from ampuly.layer2_organization import budget_stop_wait

    result = budget_stop_wait.failed_by_owner(task, STEP_LIMIT_STOP)
    words = _step_limit_words(used=native_steps_used(task), limit=int(limit), one_answer=False)
    return replace(result, content=words["failed"])


class NativeAgent:
    """AMPULY Native Agent — wraps NativeRuntimeV2 with memory, skills, and preferences."""

    def __init__(
        self,
        role: AgentInfo,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        context_assembler: ContextAssembler,
        memory: MemoryManager,
        preferences: PreferenceManager,
        skills: SkillLibrary,
        event_bus: EventBus,
        cost_tracker: CostTracker | None = None,
        config: AmpulyConfig | None = None,
        communication: Any | None = None,
        approval_callback: Any = None,
        permission_policy: Any = None,
        interaction_coordinator: InteractionCoordinator | None = None,
        external_outcome_unknown: Any | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.role = role
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_assembler = context_assembler
        self.memory = memory
        self.preferences = preferences
        self.skills = skills
        self.event_bus = event_bus
        self.cost_tracker = cost_tracker
        self.config = config or AmpulyConfig()
        self.communication = communication
        self.approval_callback = approval_callback
        self.permission_policy = permission_policy
        self.interaction_coordinator = interaction_coordinator
        self.external_outcome_unknown = external_outcome_unknown
        self.prompt_profiles = PromptProfileManager(role, self.config)
        # AE04a: the engine passes what the work item has left of its role's limit; left out, the turn gets the
        # configured limit as before (a child agent's own cap arrives through its config copy).
        max_iter = self.config.system.max_agent_iterations if max_iterations is None else max(1, int(max_iterations))
        comp_threshold = self.config.system.context_compression_threshold
        self.loop = NativeRuntimeV2(
            llm=llm,
            tool_registry=tool_registry,
            event_bus=event_bus,
            cost_tracker=cost_tracker,
            memory_manager=memory,
            history_compactor=getattr(memory, "history_compactor", None),
            max_iterations=max_iter,
            compression_threshold=comp_threshold,
            config=self.config,
            child_agent_factory=self._create_child_agent,
            approval_callback=approval_callback,
            permission_policy=permission_policy,
            prefetch_provider=self._build_runtime_prefetch_payload,
            interaction_coordinator=interaction_coordinator,
            external_outcome_unknown=external_outcome_unknown,
        )

    def _is_task_mode_task(self, task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        if mode in {"project", "task"}:
            return True
        return execution_mode == ExecutionMode.TASK_MODE.value

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> TaskResult:
        """Execute a task end-to-end."""
        self.role.status = AgentStatus.RUNNING
        self.role.current_task_id = task.id

        await self.event_bus.publish(AmpulyEvent(
            event_type="agent_status_changed",
            payload={"role_id": self.role.role_id, "status": "running", "task_id": task.id},
        ))

        allowed = self._resolve_allowed_tools(task)
        inbox_interrupt_provider = None
        if (
            self.communication
            and task.metadata.get("execution_mode") == ExecutionMode.COMPANY_MODE.value
            and not bool(task.metadata.get("_disable_live_inbox_interrupts", False))
        ):
            inbox_interrupt_provider = self._create_inbox_interrupt_provider()
        runtime_inbox_queue = getattr(task, "_runtime_inbox_queue", None)
        if runtime_inbox_queue is not None:
            inbox_interrupt_provider = self._create_runtime_inbox_provider(runtime_inbox_queue, task)

        provider_quota_in_flight = False
        try:
            system_prompt = await self._build_system_prompt(task)
            user_message = await self._build_user_message(task)
            context_messages = await self._build_context_messages(task)

            result = await self.loop.run(
                system_prompt=system_prompt,
                user_message=user_message,
                context_messages=context_messages,
                attachment_refs=list(task.metadata.get("attachment_refs", []) or []),
                task=task,
                allowed_tools=allowed,
                on_progress=on_progress,
                inbox_interrupt_provider=inbox_interrupt_provider,
            )
            artifacts = dict(result.artifacts or {})
            if result.status == TaskStatus.FAILED and artifacts.get("model_outcome_unknown") and not artifacts.get(BUDGET_STOP_ARTIFACT):
                # A model request went out and no answer came back (model-routing phase 0, C10): nothing re-sends it.
                # Marked as a stop, a company work item waits for a person instead of failing, and nothing retries it.
                result = replace(result, artifacts={**artifacts, BUDGET_STOP_ARTIFACT: "outcome_unknown"})
            elif (result.status == TaskStatus.FAILED
                  and artifacts.get("native_iteration_limit_reached") is True
                  and artifacts.get(BUDGET_STOP_ARTIFACT) == STEP_LIMIT_STOP
                  and not artifacts.get("model_outcome_unknown")):
                # Runtime.run has already added this turn's steps in its finally. Park immediately, even with
                # max_retries=0, and preserve the turn's real session, spend and usage instead of retrying it.
                stopped = step_limit_stopped_result(
                    task, limit=native_step_limit(self.config), used=native_steps_used(task), turn_started=True,
                )
                result = replace(result, status=stopped.status, content=stopped.content,
                                 artifacts={**artifacts, **stopped.artifacts})

            from ampuly.layer3_agent.employee_runtime import current_employee_turn
            employee = current_employee_turn(task)
            if employee is not None:
                await employee.finalize_result(result)
            return result

        except ProviderQuotaExhaustedError:
            # Quota exhaustion is an environment-level outage with a
            # dedicated company-dispatcher recovery path.  Preserve the
            # typed signal so a claimed WorkItem is returned to READY with
            # backoff instead of being committed as a terminal FAILED
            # TaskResult.  RuntimeV2 has already joined its tool/permission
            # abort cleanup before this exception reaches us.
            provider_quota_in_flight = True
            raise
        except ModelBudgetStop as e:
            # The task's budget refused the call. Still a failed result (every caller is written for one), but it
            # says so: a nested assessment that ends like this must not be read as "no assessment".
            logger.warning(f"Agent {self.role.role_id} was stopped by the budget on task {task.id}: {e}")
            return TaskResult(status=TaskStatus.FAILED, content=str(e), artifacts={BUDGET_STOP_ARTIFACT: budget_stop_kind(e)})
        except Exception as e:
            logger.error(f"Agent {self.role.role_id} failed on task {task.id}: {e}")
            return TaskResult(status=TaskStatus.FAILED, content=str(e))

        finally:
            self.role.status = AgentStatus.IDLE
            self.role.current_task_id = None
            try:
                await self.event_bus.publish(AmpulyEvent(
                    event_type="agent_status_changed",
                    payload={"role_id": self.role.role_id, "status": "idle"},
                ))
            except Exception:
                if not provider_quota_in_flight:
                    raise
                # Telemetry cleanup must not replace the typed recovery
                # signal that the company dispatcher needs in order to park
                # the claimed WorkItem.  Preserve the old propagation
                # semantics for every non-quota path.
                logger.opt(exception=True).warning(
                    "Failed to publish idle agent status while provider quota "
                    "exhaustion was propagating"
                )

    async def _build_native_prompt_bundle(self, task: Task) -> NativePromptBundle:
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        employee = current_employee_turn(task)
        if employee is not None:
            return NativePromptBundle(
                profile_name="frozen_employee", stable_system_prompt=employee.system_prompt,
                runtime_policy_messages=[],
            )
        override = str(task.metadata.get("_runtime_system_prompt_override", "") or "").strip()
        if override:
            task.metadata["runtime_prompt_profile"] = "override"
            return NativePromptBundle(
                profile_name="override",
                stable_system_prompt=override,
                runtime_policy_messages=[],
            )
        bundle = self.prompt_profiles.build_prompt_bundle(task)
        task.metadata["runtime_prompt_profile"] = bundle.profile_name
        return bundle

    async def _build_system_prompt(self, task: Task) -> str:
        bundle = await self._build_native_prompt_bundle(task)
        return bundle.stable_system_prompt

    async def _build_user_message(self, task: Task) -> str:
        task_brief = self.context_assembler.build_task_brief(task)
        rework_feedback = await self.context_assembler.build_rework_feedback_context(
            task,
            include_previous_submission=False,
        )
        if not rework_feedback:
            task.metadata.pop("_runtime_v2_attempt_user_seed_required", None)
            task.metadata.pop("_runtime_v2_attempt_user_seed_revision", None)
            return task_brief

        # A WorkItem re-dispatch is a new business attempt even when it resumes
        # the same Native runtime session.  Put the authoritative correction in
        # the user turn, not only in a replaceable system-prefix message.  The
        # runtime uses the stable revision below to persist exactly one such
        # turn per attempt across provider retries, permission continuations,
        # and process restarts.
        turn_mode = await self.context_assembler.build_turn_mode_context(task)
        work_item_id = linked_work_item_id_for_task(task)
        try:
            attempt_seq = int(
                (task.metadata or {}).get("claimed_work_item_attempt_seq", 0)
                or 0
            )
        except (TypeError, ValueError):
            attempt_seq = 0
        try:
            rework_round = int(
                (task.metadata or {}).get("gate_harness_rework_count", 0)
                or 0
            )
        except (TypeError, ValueError):
            rework_round = 0
        attempt_identity = (
            "### Durable Attempt Identity\n"
            f"- WorkItem: `{work_item_id}`\n"
            f"- Attempt sequence: `{attempt_seq}`\n"
            f"- Deterministic rework round: `{rework_round}`"
        )
        correction = "\n\n".join(
            part
            for part in (
                "## Current WorkItem Attempt — Correction Required",
                attempt_identity,
                turn_mode,
                rework_feedback,
                (
                    "Apply the correction now using the available tools. Do not "
                    "resubmit an unchanged artifact."
                ),
            )
            if str(part or "").strip()
        ).strip()
        revision_payload = {
            "task_id": str(task.id or ""),
            "work_item_id": work_item_id,
            "claimed_work_item_attempt_seq": attempt_seq,
            "gate_harness_rework_count": rework_round,
            "gate_harness_rework_request": (
                task.metadata or {}
            ).get("gate_harness_rework_request", {}),
            "correction": correction,
        }
        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        task.metadata["_runtime_v2_attempt_user_seed_required"] = True
        task.metadata["_runtime_v2_attempt_user_seed_revision"] = revision
        # The original assignment is already durable in the restored
        # transcript and remains available in the dynamic system context.
        # Seed only the authoritative correction delta.  In particular, this
        # path excludes the best-effort live Task.result excerpt: Engine writes
        # a provider failure into that field before a same-attempt retry, which
        # must not mutate the durable business-attempt prompt.
        return correction

    async def _build_context_messages(self, task: Task) -> list[dict[str, Any]]:
        fork_messages = list(task.metadata.get("_fork_context_messages", []) or [])
        if fork_messages:
            return fork_messages

        # The full correction delta is carried by the per-attempt user turn.
        # Keep the generic Turn Mode and all other runtime context in the
        # system prefix, but avoid duplicating the same blocker block there.
        suppress_rework = bool(
            (task.metadata or {}).get(
                "_runtime_v2_attempt_user_seed_required", False
            )
        )
        marker = object()
        previous_suppression = task.metadata.get(
            "suppress_company_rework_feedback_context", marker
        )
        if suppress_rework:
            task.metadata["suppress_company_rework_feedback_context"] = True
        try:
            harness_output = await self._build_prompt_harness(task)
        finally:
            if suppress_rework:
                if previous_suppression is marker:
                    task.metadata.pop(
                        "suppress_company_rework_feedback_context", None
                    )
                else:
                    task.metadata[
                        "suppress_company_rework_feedback_context"
                    ] = previous_suppression
        dynamic_messages = [
            *harness_output.runtime_policy_messages,
            *harness_output.workspace_context_messages,
            *harness_output.artifact_messages,
        ]
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        employee = current_employee_turn(task)
        if employee is not None:
            dynamic_messages.extend(dict(item) for item in employee.context_messages)
        session_id = getattr(task, "session_id", None)
        if not session_id:
            return dynamic_messages
        context_snapshot = task.context_snapshot if isinstance(task.context_snapshot, dict) else {}
        raw_runtime_resume = context_snapshot.get("runtime_resume")
        has_runtime_resume = isinstance(raw_runtime_resume, dict) and bool(raw_runtime_resume)
        legacy_skip_session_history = raw_runtime_resume is True
        if bool(context_snapshot.get("skip_session_history", False)) or has_runtime_resume or legacy_skip_session_history:
            return dynamic_messages
        return [
            *dynamic_messages,
            *(
                await self.memory.build_session_history_tail_messages(
                    session_id,
                    include_latest_user_turn=False,
                )
            ),
        ]

    async def _build_prompt_harness(self, task: Task) -> Any:
        allowed_tools = self._resolve_allowed_tools(task)
        prompt_bundle = await self._build_native_prompt_bundle(task)
        harness = PromptHarnessBuilder(
            task=task,
            role_id=self.role.role_id,
            config=self.config,
            context_assembler=self.context_assembler,
            preferences=self.preferences,
            skills=self.skills,
        )
        output = await harness.build(
            system_prompt=prompt_bundle.stable_system_prompt,
            allowed_tools=allowed_tools,
            runtime_policy_messages=prompt_bundle.runtime_policy_messages,
        )
        task.metadata["prompt_harness"] = {
            "static_section_ids": list(output.static_section_ids),
            "dynamic_section_ids": list(output.dynamic_section_ids),
            "artifact_manifest": list(output.artifact_manifest),
            "artifact_hashes": dict(output.artifact_hashes),
        }
        task.metadata["_prompt_harness_boot_artifacts"] = list(output.artifact_manifest)
        return output

    def _registered_general_tool_names(self) -> set[str]:
        return {
            str(tool.name or "").strip()
            for tool in self.tool_registry.list_tools()
            if str(tool.name or "").strip()
            and str(tool.name or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    @staticmethod
    def _configured_general_tool_names(tools: list[str] | tuple[str, ...]) -> set[str]:
        # ``"*"`` (ALL_GENERAL_TOOLS_WILDCARD) survives this filter on purpose:
        # it is not a tool name, and ``_expand_general_tool_wildcard`` turns it
        # into the registry's general tools.  Dropping it here would make a
        # wildcard role indistinguishable from a role with no tools.
        return {
            str(tool or "").strip()
            for tool in list(tools or [])
            if str(tool or "").strip()
            and str(tool or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    def _expand_general_tool_wildcard(self, configured: set[str]) -> set[str]:
        """Replace ``"*"`` with every registered general tool; otherwise unchanged."""
        if ALL_GENERAL_TOOLS_WILDCARD not in configured:
            return set(configured)
        expanded = set(configured)
        expanded.discard(ALL_GENERAL_TOOLS_WILDCARD)
        expanded.update(self._registered_general_tool_names())
        return expanded

    def _resolve_allowed_tools(self, task: Task) -> list[str]:
        from ampuly.layer3_agent.employee_runtime import current_employee_turn
        employee = current_employee_turn(task)
        if employee is not None:
            # The final registry checkpoint also intersects current grants.
            return employee.allowed_tools
        turn_mode = resolve_company_turn_mode(task, runtime_state={})
        inherited = list(task.metadata.get("_fork_allowed_tools", []) or [])
        if inherited:
            if not company_collaboration_enabled_for_task(task):
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
                ]
            else:
                _, allowed_collab = resolve_task_collaboration_tools(
                    task,
                    role=self.role.role_id,
                    seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                    runtime_state={},
                    role_cfg=self.role,
                )
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES or tool in allowed_collab
                ]
            if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
                inherited = [
                    tool for tool in inherited
                    if tool not in _MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST
                ]
            return inherited

        # The role's own list is the allow-list, empty or not.  Until
        # 2026-10-07 an empty list fell back -- to the whole registry in
        # company mode, to ``None`` (no gating) otherwise -- so "no tools" and
        # "every tool" were the same configuration.  The registry already
        # reads ``[]`` as zero tools (registry.py list_tools); this hands it
        # exactly that.  A role that wants everything writes ``"*"``.
        allowed = self._expand_general_tool_wildcard(
            self._configured_general_tool_names(list(self.role.tools or []))
        )
        if company_collaboration_enabled_for_task(task):
            _, allowed_collab = resolve_task_collaboration_tools(
                task,
                role=self.role.role_id,
                seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                runtime_state={},
                role_cfg=self.role,
            )
            allowed.update(allowed_collab)

        if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
            allowed.difference_update(_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST)
        return sorted(allowed)

    async def _build_runtime_prefetch_payload(
        self,
        task: Task,
        query: str,
        _messages: list[dict[str, Any]],
    ) -> dict[str, str]:
        prefetch_cfg = self.config.system.native_runtime.prefetch
        if not prefetch_cfg.enabled:
            return {}
        payload: dict[str, str] = {}
        max_chars = max(400, int(prefetch_cfg.max_chars or 4000))
        session_id = getattr(task, "session_id", None)
        include_project_knowledge = bool(task.metadata.get("include_project_knowledge", False))
        if prefetch_cfg.session_memory and session_id:
            session_memory = (await self.memory.build_session_memory_context(session_id)).strip()
            if session_memory:
                payload["session_memory"] = clip_text(
                    session_memory,
                    limit=max_chars,
                    marker="session memory prefetch truncated",
                ).text
        if prefetch_cfg.focused_memory:
            focused = (
                await self.memory.build_focused_memory_context(
                    query=query,
                    project_id=task.project_id,
                    session_id=session_id,
                    include_project_knowledge=include_project_knowledge,
                    max_chars=max_chars,
                )
            ).strip()
            if focused:
                payload["focused_memory"] = clip_text(
                    focused,
                    limit=max_chars,
                    marker="focused memory prefetch truncated",
                ).text
        if prefetch_cfg.project_memory_candidates:
            project_memory = (
                await self.memory.build_project_memory_context(
                    project_id=task.project_id,
                    include_project_knowledge=include_project_knowledge,
                )
            ).strip()
            if project_memory:
                payload["project_memory_candidates"] = clip_text(
                    project_memory,
                    limit=max_chars,
                    marker="project memory prefetch truncated",
                ).text
        harness_cfg = self.config.system.native_runtime.prompt_harness
        skills_in_prompt_harness = bool(harness_cfg.enabled and harness_cfg.artifact_messages_enabled)
        if prefetch_cfg.skills_summary and not skills_in_prompt_harness:
            execution_mode = str(task.metadata.get("execution_mode", "") or "").strip() or None
            skills_summary = str(
                self.skills.build_skills_summary(
                    task.project_id,
                    execution_mode=execution_mode,
                    role_id=self.role.role_id,
                    user_facing=_memory_skill_user_facing(task, self.role.role_id),
                    final_decider_role_id=_final_decider_role_id(task),
                    # BA: an order in flight is offered the skill bodies its launch froze (runtime contract).
                    **frozen_skill_kwargs(task.metadata),
                )
                or ""
            ).strip()
            if skills_summary:
                payload["skills_summary"] = clip_text(
                    skills_summary,
                    limit=max_chars,
                    marker="skills summary prefetch truncated",
                ).text
        return payload

    def _create_inbox_interrupt_provider(self) -> Any:
        communication = self.communication
        agent_role_id = self.role.role_id

        async def _provide(task: Task) -> list[dict[str, Any]]:
            if communication is None:
                return []
            return await communication.consume_live_inbox_messages(task, agent_id=agent_role_id)

        return _provide

    def _create_runtime_inbox_provider(self, inbox_queue: Any, task: Task) -> Any:
        async def _provide(_task: Task) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            while True:
                try:
                    message = inbox_queue.get_nowait()
                except Exception:
                    break
                if not message:
                    continue
                if isinstance(message, dict):
                    normalized = dict(message)
                    normalized.setdefault("from", str(normalized.get("from_agent", "runtime_subagent_parent") or "runtime_subagent_parent"))
                    normalized.setdefault("body", str(normalized.get("body", normalized.get("message", "")) or ""))
                    items.append(normalized)
                    continue
                items.append({"from": "runtime_subagent_parent", "body": str(message)})
            endpoint_id = str(task.metadata.get("_comms_endpoint_id", "") or "").strip()
            if endpoint_id:
                workspace_root = (
                    str(task.metadata.get("comms_workspace_root", "") or "").strip()
                    or str(task.metadata.get("workspace_root", "") or "").strip()
                    or str(task.metadata.get("target_output_dir", "") or "").strip()
                )
                if workspace_root:
                    try:
                        from ampuly.layer2_organization import comms as _comms

                        layout = _comms.resolve_layout(
                            workspace_root,
                            str(task.project_id or "default").strip() or "default",
                            str(task.parent_session_id or task.session_id or "default").strip() or "default",
                        )
                        unread = _comms.list_unread(layout, endpoint_id, limit=6)
                        injected_ids = {
                            str(item).strip()
                            for item in list(task.context_snapshot.get("runtime_inbox_injected_message_ids", []) or [])
                            if str(item).strip()
                        }
                        for header in unread:
                            msg_id = str(header.message_id or "").strip()
                            if msg_id and msg_id in injected_ids:
                                continue
                            _, body = _comms.read_message(header.path)
                            if body.strip():
                                items.append(classify_worker_message(
                                    {
                                        "from": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "from_agent": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "subject": str(header.subject or "").strip(),
                                        "message_id": str(header.message_id or "").strip(),
                                        "msg_id": str(header.message_id or "").strip(),
                                        "body": body.strip(),
                                        "reply_needed": bool(header.blocking),
                                        "urgency": str(header.priority or "").strip() or "normal",
                                        "transport_kind": str(header.raw_frontmatter.get("transport_kind", "") or "").strip(),
                                        "semantic_type": str(header.raw_frontmatter.get("semantic_type") or header.raw_frontmatter.get("kind") or "").strip(),
                                        "metadata": dict(header.raw_frontmatter or {}),
                                    }
                                ))
                                if msg_id:
                                    injected_ids.add(msg_id)
                        if injected_ids:
                            task.context_snapshot = dict(task.context_snapshot)
                            task.context_snapshot["runtime_inbox_injected_message_ids"] = sorted(injected_ids)[-50:]
                    except Exception:
                        pass
            return items

        return _provide

    def _create_child_agent(
        self,
        profile: str,
        allowed_tools: list[str],
        prompt_addendum: str,
        overrides: dict[str, Any] | None = None,
    ) -> "NativeAgent":
        overrides = dict(overrides or {})
        role_name = str(overrides.get("name") or f"{self.role.name} [{profile}]").strip() or f"{self.role.name} [{profile}]"
        role = AgentInfo(
            role_id=f"{self.role.role_id}:{profile}",
            name=role_name,
            responsibility=self.role.responsibility,
            status=AgentStatus.IDLE,
            current_task_id=None,
            reports_to=self.role.reports_to,
            icon=self.role.icon,
            can_spawn=list(self.role.can_spawn),
            tools=list(allowed_tools),
            preferred_external_agent=self.role.preferred_external_agent,
            prompt_refs=[*self.role.prompt_refs],
            skill_refs=[*self.role.skill_refs],
            handoff_template_ref=self.role.handoff_template_ref,
            memory_policy_ref=self.role.memory_policy_ref,
            artifact_contract_ref=self.role.artifact_contract_ref,
            runtime_policy=dict(self.role.runtime_policy),
            org_id=self.role.org_id,
            budget_monthly_cents=self.role.budget_monthly_cents,
            spent_monthly_cents=self.role.spent_monthly_cents,
            heartbeat_enabled=self.role.heartbeat_enabled,
            heartbeat_interval_sec=self.role.heartbeat_interval_sec,
            last_heartbeat_at=self.role.last_heartbeat_at,
            capabilities=self.role.capabilities,
        )
        if prompt_addendum:
            role.prompt_refs.append(prompt_addendum)
        if overrides.get("description"):
            role.prompt_refs.append(f"Subagent task summary: {str(overrides['description']).strip()}")
        if overrides.get("mode"):
            role.prompt_refs.append(f"Runtime spawn mode: {str(overrides['mode']).strip()}")

        child_llm = child_llm_for(self.llm, str(overrides.get("model") or ""))

        child_config = self.config
        max_iterations = overrides.get("max_iterations")
        if self.config is not None and max_iterations:
            child_config = self.config.model_copy(deep=True)
            child_config.system.max_agent_iterations = max(1, int(max_iterations))

        return NativeAgent(
            role=role,
            llm=child_llm,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            event_bus=self.event_bus,
            cost_tracker=self.cost_tracker,
            config=child_config,
            communication=self.communication,
            approval_callback=self.approval_callback,
            permission_policy=self.permission_policy,
            interaction_coordinator=self.interaction_coordinator,
            external_outcome_unknown=self.external_outcome_unknown,
        )
