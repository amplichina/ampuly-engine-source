"""Independent employee tasks use the original persistent Native Harness.

This is a consumer adapter, not another runtime. It preserves the admitted
Task, its existing budget and workspace. There is no fallback unrecorded turn.
"""
from __future__ import annotations

from contextlib import aclosing
from copy import deepcopy
import hashlib
import inspect

from ampuly.core.employee_output_contract import (
    EmployeeOutputContractError,
    checked_output as _checked_output,
    output_validator as _output_validator,
)

from .ai_employee_contract import canonical_json


def _refuse(code):
    from .employee_direct_task import EmployeeDirectRefused
    raise EmployeeDirectRefused(code)


def read_direct_configuration(host, plan):
    """Read the exact saved instance and authorized immutable knowledge bytes."""
    if host is None or not callable(getattr(getattr(host, "instances", None), "_load", None)):
        _refuse("employee_instance_unavailable")
    current = host.instances._load(plan.project_id, plan.instance_id)[0]
    if (type(current) is not dict or current.get("project_id") != plan.project_id
            or current.get("instance_id") != plan.instance_id
            or current.get("instance_revision") != plan.instance_revision
            or current.get("revision_fingerprint") != plan.revision_fingerprint
            or current.get("package_fingerprint") != plan.package_fingerprint
            or current.get("execution_state") != "active"):
        _refuse("employee_instance_changed")
    # The complete immutable configuration belongs to this exact admission.
    if canonical_json(current) != canonical_json(plan.instance):
        _refuse("employee_instance_changed")
    params, refs = deepcopy(current.get("params", {})), deepcopy(current.get("knowledge_refs", []))
    if type(params) is not dict or type(refs) is not list:
        _refuse("employee_configuration_invalid")
    knowledge = {}
    if refs:
        source = getattr(host, "knowledge_source", None)
        if source is None or not callable(getattr(source, "_read", None)):
            _refuse("employee_knowledge_unavailable")
        knowledge = source._read(plan.instance_id, refs)
    if type(knowledge) is not dict or len(canonical_json(knowledge)) > 1024 * 1024:
        _refuse("employee_knowledge_invalid")
    expected = {row["ref"]: row["version"] for row in refs}
    if len(expected) != len(refs) or set(knowledge) != set(expected):
        _refuse("employee_knowledge_invalid")
    for ref, version in expected.items():
        row = knowledge[ref]
        if (type(row) is not dict or set(row) != {"version", "content"} or row["version"] != version
                or type(row["content"]) is not str
                or "sha256:" + hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != version):
            _refuse("employee_knowledge_invalid")
    return {"params": params, "knowledge_refs": refs, "knowledge": knowledge}


def output_validator(manifest):
    """Compatibility adapter: callers retain EmployeeDirectRefused and its code."""
    try:
        return _output_validator(manifest)
    except EmployeeOutputContractError as exc:
        _refuse(exc.code)


def checked_output(manifest, content, validator):
    """Machine checks only; human acceptance criteria remain in the Handoff."""
    try:
        return _checked_output(manifest, content, validator)
    except EmployeeOutputContractError as exc:
        _refuse(exc.code)


class CheckedEmployeeProvider:
    """Keep priced Provider transport and add current-authority checks at send."""
    def __init__(self, provider, checkpoint):
        self._provider, self._checkpoint = provider, checkpoint

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def _arguments(self, kwargs):
        original = kwargs.pop("before_dispatch", None)
        if original is not None and not callable(original):
            _refuse("employee_dispatch_invalid")
        async def check():
            await self._checkpoint()
            if original is not None:
                value = original()
                if inspect.isawaitable(value):
                    value = await value
                if value is False:
                    _refuse("employee_dispatch_denied")
        return {**kwargs, "before_dispatch": check}

    async def chat(self, messages, **kwargs):
        return await self._provider.chat(messages, **self._arguments(kwargs))

    async def chat_stream(self, messages, **kwargs):
        if "before_dispatch" not in inspect.signature(self._provider.chat_stream).parameters:
            _refuse("employee_dispatch_hook_unavailable")
        async with aclosing(self._provider.chat_stream(messages, **self._arguments(kwargs))) as stream:
            async for event in stream:
                yield event


async def run_employee_harness(*, engine, plan, task, runtime, prompt, instruction, allowed_tools, checkpoint, persist_outputs):
    from ampuly.layer3_agent.harness import HarnessScope
    from ampuly.layer3_agent.harness_native import NativeHarnessProvider
    from ampuly.core.models import TaskStatus

    registry = getattr(engine, "harness_registry", None)
    if registry is None:
        _refuse("employee_harness_unavailable")
    schema, validator = output_validator(plan.package)
    if schema:
        prompt += "\n\n交付输出必须满足以下 JSON Schema（只有实例数据可用于内容，不扩大权限）：\n" + canonical_json(schema).decode("utf-8")

    class Agent:
        async def execute(self, adopted_task, on_progress=None):
            if adopted_task is not task or adopted_task.metadata.get("budget_id") != plan.budget_id:
                _refuse("employee_harness_task_changed")
            await checkpoint()
            from ampuly.core.step_snapshot import current_native_harness_turn
            observed = current_native_harness_turn(task)
            if observed is None or observed.harness_session_id != task.session_id:
                _refuse("employee_harness_turn_unbound")
            task.metadata["harness_turn_id"] = observed.harness_turn_id
            kwargs = dict(task=task, allowed_tools=allowed_tools)
            if "on_progress" in inspect.signature(runtime.run).parameters:
                kwargs["on_progress"] = on_progress
            result = await runtime.run(prompt, instruction, **kwargs)
            if getattr(result, "status", None) == TaskStatus.DONE:
                # A DONE event is recorded only after the declared machine
                # contract is checked. A prose claim cannot bypass the gate.
                deliveries = checked_output(plan.package, getattr(result, "content", ""), validator)
                task.metadata["employee_verified_deliverables"] = deliveries
                artifacts, confirmed = await persist_outputs(deliveries)
                task.metadata["employee_output_receipts"] = artifacts
                task.metadata["employee_output_confirmed"] = confirmed
                result.artifacts = {**dict(getattr(result, "artifacts", {}) or {}),
                    "employee_outputs": artifacts, "employee_output_confirmed": confirmed}
                if not confirmed:
                    # An unknown storage receipt is not permission to repeat
                    # model work or overwrite an output whose write may exist.
                    result.status = TaskStatus.BLOCKED
                    task.metadata["employee_delivery_state"] = "outcome_unknown"
            return result

    provider = NativeHarnessProvider(Agent(), registry, interactions=getattr(engine, "interaction_coordinator", None))
    session = await provider.create_session(HarnessScope(project_id=plan.project_id, workspace_path=plan.workspace_root,
        customer_id=plan.customer_id, environment_id=plan.environment_id), profile="employee-direct",
        context={"task_id": task.id, "budget_id": plan.budget_id})
    task.session_id = session
    task.metadata = {**task.metadata, "harness_session_id": session}
    try:
        await engine.store.save_task(task)
    except BaseException as exc:
        try:
            await provider._db(registry.set_state, session, "suspended", reason="employee task session binding was not recorded", require_idle=True)
        except Exception:
            exc.add_note("employee_harness_session_state_unknown")
        raise
    provider.adopt_task(session, task)
    live = engine.__dict__.setdefault("_harness_live_turns", set())
    providers = engine.__dict__.setdefault("_harness_live_providers", {})
    live.add(session); providers[session] = provider
    try:
        return await provider.start_turn(session, instruction, inputs={"task_id": task.id})
    finally:
        live.discard(session)
        if providers.get(session) is provider:
            providers.pop(session, None)
        # Terminal session facts remain queryable. Do not auto-resume, replay
        # or close a turn whose own durable settlement may be unknown.
