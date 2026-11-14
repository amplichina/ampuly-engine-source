"""Managed console drafting on the original employee/native Harness path.

Only an already-authorized EmployeeNativeTurn can use this adapter. It is not
a channel connector: the host supplies one unverified customer message from
the original launch claim, and constructs the anonymous empty-knowledge scope.
No customer identity, trusted signal, knowledge or send action is accepted.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import inspect

from ampuly.core.models import TaskResult, TaskStatus
from ampuly.llm.budget import BUDGET_STOP_ARTIFACT, model_budget_scope
from ampuly.layer3_agent.employee_runtime import current_employee_turn, employee_dispatch_checkpoint
from .core.contracts import Context
from .empty_knowledge import EmptyKnowledgeBase
from .employee_executor import _canonical, _fail, _snapshot, _has_understanding, executor_profile
from .service import CustomerServiceDraftSession


@dataclass(frozen=True, slots=True)
class ConsoleDraftInput:
    """Host observation, not permission; only private turn wiring consumes it."""
    project_id: str
    order_id: str
    order_task_id: str
    step_task_id: str
    node_id: str
    run_id: str
    attempt: int
    launch_payload_sha256: str
    message: str
    revision_note: str = ""


def _scope(input, identity):
    if (type(input) is not ConsoleDraftInput
            or any(getattr(input, key) != getattr(identity, key) for key in (
                "project_id", "order_id", "order_task_id", "step_task_id", "node_id", "run_id", "attempt"))
            or type(input.message) is not str or not input.message.strip() or len(input.message) > 2000
            or type(input.revision_note) is not str or len(input.revision_note) > 4000):
        _fail("customer_service_console_scope_invalid")
    return input


def managed_native_agent(turn, provider):
    """Select from the verified package implementation, never its UI name."""
    profile = executor_profile(turn.basis.package_manifest())
    if profile["kind"] == "native":
        return None
    return CustomerServiceConsoleAgent(turn, provider)


class CustomerServiceConsoleAgent:
    """One fixed draft call; original NativeHarnessProvider owns its session."""
    def __init__(self, turn, provider):
        self.turn, self.provider = turn, provider
        self._used = False
        self._session = None

    async def execute(self, task, on_progress=None):
        from ampuly.llm.provider import model_credential_scope
        from ampuly.plugins.office_ui.order_model_plan import plan_scope_for
        from ampuly.layer3_agent.runtime_v2.runtime import native_steps_used

        turn = self.turn
        if self._used or current_employee_turn(task) is not turn or turn.task is not task:
            _fail("customer_service_console_scope_invalid")
        self._used = True
        manifest = turn.basis.package_manifest()
        _, prompt = _snapshot(manifest, turn.frozen_package_files())
        source = _scope(turn.host_input, turn.basis.identity)
        instance = turn.basis.instance_snapshot()
        node = task.metadata.get("workflow_node", {})
        if (turn.allowed_tools or instance.get("knowledge_refs") or instance.get("params")
                or not turn.basis.pin().get("reviewer_slot") or node.get("gate") != "human_required"):
            _fail("customer_service_console_configuration_unsupported")
        cap = getattr(getattr(self.provider, "config", None), "max_tokens", None)
        if type(cap) is not int or not 1 <= cap <= 3500:
            _fail("customer_service_output_cap_invalid")
        if turn.max_steps < 1:
            _fail("employee_step_limit")

        # These names identify an internal anonymous scope, NOT an authenticated
        # buyer/shop/account and never credentials for an external channel.
        project = hashlib.sha256(_canonical([source.project_id])).hexdigest()[:24]
        key = hashlib.sha256(_canonical([source.project_id, source.order_id,
            source.step_task_id, source.run_id, source.attempt])).hexdigest()[:24]
        context = Context(tenant_id="console-project-" + project, shop_id="empty-knowledge-scope",
            seller_id="console-operator", buyer_id="unverified-" + key,
            conversation_id="console-draft-" + key, channel="console", account_id="no-external-account")
        turns = [{"id": "intake-" + source.launch_payload_sha256[:24],
                  "role": "customer", "text": source.message}]

        async def checkpoint():
            await employee_dispatch_checkpoint("recheck", task=task)

        await checkpoint()
        # Count once before the call and persist through the normal fenced save.
        task.metadata["native_steps_used"] = native_steps_used(task) + 1
        await turn._save()
        await checkpoint()
        self._session = CustomerServiceDraftSession(context=context, knowledge=EmptyKnowledgeBase(context),
            provider=self.provider, before_dispatch=checkpoint, task_type="customer_service_employee_console",
            system_prompt=prompt, max_output_tokens=cap, revision_note=source.revision_note,
            structured_understanding=_has_understanding(manifest))
        md = task.metadata
        with model_credential_scope(str(md.get("customer_id", "") or ""),
                                    str(md.get("environment_id", "") or "")), \
                plan_scope_for(task), model_budget_scope(task):
            output = await self._session.prepare(turns, signals={},
                timeout_seconds=min(30.0, turn.remaining_seconds))
        await checkpoint()
        observation = self._session.model_observation or {}
        # The priced ledger is the monetary authority. TaskResult.cost remains
        # its legacy numeric slot, never promoted to a zero-cost fact.
        artifacts = {"customer_service_draft": deepcopy(output),
            "customer_service_executor": executor_profile(manifest),
            "cost_known": False, "estimated_cost_usd": None,
            "model_observation": observation, "review_required": True,
            "external_effects_produced": []}
        uncertain = set(output.get("validation_errors", [])) & {
            "model_timeout", "model_cancelled", "model_busy", "model_unavailable"}
        if uncertain:
            artifacts.update(model_outcome_unknown=True)
            artifacts[BUDGET_STOP_ARTIFACT] = "outcome_unknown"
            return TaskResult(status=TaskStatus.FAILED,
                content="客服模型本轮结果待核实；不会自动重发，现有安全草稿仅供人工处置。", artifacts=artifacts)
        if output.get("result_status") != "draft" or output.get("validation_errors"):
            return TaskResult(status=TaskStatus.AWAITING_HUMAN,
                content="客服草稿未通过结构检查，安全回退内容需人工处理；没有发送。", artifacts=artifacts)
        # DONE means the drafting action ended. The original Company node's
        # mandatory human gate owns approval; review_required is never cleared.
        if on_progress is not None:
            event = on_progress("客服回复草稿已生成，等待人工审核；尚未发送。")
            if inspect.isawaitable(event):
                await event
        return TaskResult(status=TaskStatus.DONE, content=_canonical(output).decode("utf-8"), artifacts=artifacts)
