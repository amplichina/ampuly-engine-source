"""A single bounded server-loop worker for retained employee package trials.

The constructor is server composition, never wire deserialization. This uses
the real NativeRuntimeV2 and the original priced provider/budget ledger. The
optional specialised executor must be selected from trusted frozen provenance,
not the package's display name or a model's claim to implement a schema.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
import re
from threading import Lock
import time

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .employee_publication_contents import publication_content_fingerprint, package_skill_fingerprints, publication_layout


def _fail(code):
    raise AIEmployeeContractError(code, "岗位测试停止；未自动通过、发布、采用或重试。") from None


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("evaluation_budget_invalid")
    return value


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("evaluation_input_invalid")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: _fail("evaluation_input_invalid"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("evaluation_input_invalid")


# Providers whose reviewed transport honours an OpenAI-style JSON mode. Only
# requested when the case has no tools; it constrains format, not content.
_JSON_MODE_PROVIDERS = frozenset({"deepseek"})
_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
_EXCERPT = 800


def _native_response_format(model):
    if type(model) is not str or "/" not in model:
        return None
    return {"type": "json_object"} if model.split("/", 1)[0] in _JSON_MODE_PROVIDERS else None


def _strict_object(raw):
    try:
        value = _json(raw)
    except AIEmployeeContractError:
        return None
    return value if type(value) is dict else None


def _fenced_object(raw):
    """The one JSON object inside exactly one fenced block; never search or repair."""
    if type(raw) is not str or raw.count("```") != 2:
        return None
    blocks = _FENCE.findall(raw)
    return _strict_object(blocks[0]) if len(blocks) == 1 else None


def _digest(raw):
    text = raw if type(raw) is str else repr(raw)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), text[:_EXCERPT]


def _validator(schema):
    from jsonschema import Draft202012Validator
    pending = [(schema, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 32 or count > 10000:
            _fail("evaluation_schema_invalid")
        if type(value) is dict:
            if "$ref" in value and (type(value["$ref"]) is not str or not value["$ref"].startswith("#")):
                _fail("evaluation_schema_invalid")  # Never fetch external schemas.
            pending.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            pending.extend((child, depth + 1) for child in value)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@dataclass(frozen=True, slots=True)
class TrialExecution:
    """Server-private dispatch decision. A callback never comes from a manifest.

    kind=native requires run_case=None. kind=customer_service requires a real
    host callback, which receives keyword arguments documented below. Its
    ConversationDraftV1 must come from ConversationService, not model output.
    """
    kind: str
    run_case: object = None


@dataclass(frozen=True, slots=True)
class TrialCaseResult:
    output: dict
    completed: bool
    hard_fail_count: int = 0


class _TrialProvider:
    """Keep original provider transport; add a final current-authority check."""
    def __init__(self, provider, check, max_tokens, timeout_seconds, *, response_format=None):
        self._provider, self._check = provider, check
        self._max_tokens, self._timeout = max_tokens, timeout_seconds
        self._response_format = response_format

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def _arguments(self, kwargs):
        from ampuly.layer4_tools.employee_builtin_read import current_read_tool_schemas
        internal = current_read_tool_schemas()
        authorized = self._provider.get_tool_definitions(internal) if internal else []
        supplied = kwargs.get("tools") or []
        if supplied != authorized:
            _fail("evaluation_tool_not_authorized")
        original = kwargs.pop("before_dispatch", None)
        if original is not None and not callable(original):
            _fail("evaluation_dispatch_invalid")
        async def check():
            await self._check()
            if original is not None:
                value = original()
                if inspect.isawaitable(value):
                    value = await value
                if value is False:
                    _fail("evaluation_dispatch_denied")
        requested = kwargs.get("max_tokens", self._max_tokens)
        if requested is None:
            requested = self._max_tokens
        _integer(requested, 1, self._max_tokens)
        kwargs.update(tools=authorized, max_tokens=requested, num_retries=0,
                      timeout=self._timeout, before_dispatch=check)
        if self._response_format is not None and not authorized:
            kwargs["response_format"] = dict(self._response_format)
        return kwargs

    async def chat(self, messages, **kwargs):
        return await self._provider.chat(messages, **self._arguments(kwargs))

    async def chat_stream(self, messages, **kwargs):
        # Requires the shared provider patch that makes before_dispatch an
        # explicit stream parameter; never put a callback into wire kwargs.
        if "before_dispatch" not in inspect.signature(self._provider.chat_stream).parameters:
            _fail("evaluation_dispatch_hook_unavailable")
        async with aclosing(self._provider.chat_stream(messages, **self._arguments(kwargs))) as events:
            async for event in events:
                yield event


class EmployeeTrialRunner:
    def __init__(self, *, engine, host, loop, resolve_executor, max_output_tokens=3500):
        if (loop is None or not callable(getattr(loop, "call_soon_threadsafe", None))
                or not callable(resolve_executor) or getattr(host, "engine", None) is not engine):
            _fail("evaluation_runner_unavailable")
        self.engine, self.host, self.loop = engine, host, loop
        self.resolve_executor = resolve_executor
        # This constant is part of host.context_for_manifest's profile. A
        # separate per-runner cap must not silently use the same fingerprint.
        if type(max_output_tokens) is not int or max_output_tokens != 3500:
            _fail("evaluation_context_unavailable")
        self.max_output_tokens = max_output_tokens
        self._lock = Lock()
        self._trial_id = None
        self._worker = None

    def context(self, manifest):
        result = self.host.context_for_manifest(deepcopy(manifest))
        if type(result) is not dict or set(result) != {
            "model_config", "capability_impls", "runtime_profile_fingerprint"
        }:
            _fail("evaluation_context_unavailable")
        return deepcopy(result)

    def instance_snapshot(self, *, project_id, selection):
        from .employee_qualification import validate_selection, qualification_scope
        from .employee_publication_contents import bounded_publication_paths
        selection = validate_selection(selection)
        if project_id != getattr(self.engine, "project_id", None):
            _fail("evaluation_instance_scope_invalid")
        instance = self.host.instances._load(project_id, selection["instance_id"])[0]
        if (instance.get("project_id") != project_id or instance.get("execution_state") != "active"
                or instance.get("instance_revision") != selection["expected_revision"]
                or instance.get("revision_fingerprint") != selection["expected_fingerprint"]):
            _fail("revision_conflict")
        view = self.host.catalog.read(instance["package_id"], version=instance["package_version"],
            fingerprint=instance["package_fingerprint"], purpose="pinned")
        manifest = view["package"]
        resources, trees = self.host._resources(instance["package_id"], instance["package_version"],
                                               instance["package_fingerprint"])
        files = dict(resources)
        for tree in publication_layout(manifest)["skill_trees"]:
            for relative, raw in trees[tree["skill_name"]]:
                files[tree["source_ref"] + "/" + relative] = raw
        files = {path: files[path] for path in bounded_publication_paths(manifest, files)}
        if publication_content_fingerprint(manifest, files) != instance["package_fingerprint"]:
            _fail("evaluation_snapshot_invalid")
        self.host.instances._validate(instance, manifest)
        refs = deepcopy(instance.get("knowledge_refs", []))
        knowledge = {}
        if refs:
            source = getattr(self.host, "knowledge_source", None)
            if source is None or getattr(source, "engine", None) is not self.engine:
                _fail("evaluation_knowledge_unavailable")
            # Existing exact-version reader performs current project/instance
            # ACL checks. No fake order/node is manufactured for an evaluation.
            knowledge = source._read(instance["instance_id"], refs)
        after = self.host.instances._load(project_id, selection["instance_id"])[0]
        if canonical_json(after) != canonical_json(instance):
            _fail("revision_conflict")
        return {"manifest": deepcopy(manifest), "files": files,
            "trees": package_skill_fingerprints(manifest, files),
            "revision": view["revision"],
            "instance_selection": selection, "params": deepcopy(instance.get("params", {})),
            "knowledge_refs": refs, "knowledge": deepcopy(knowledge),
            "qualification_scope": qualification_scope(instance),
            "context": self.host.context_for_manifest(deepcopy(manifest), model_config=instance.get("model_config"))}

    def _current_trial_context(self, trial, manifest):
        selection = trial["candidate"].get("instance_selection")
        if selection is None:
            return self.context(manifest)
        snapshot = self.instance_snapshot(project_id=trial["project_id"], selection=selection)
        if (snapshot["manifest"] != manifest or snapshot["revision"] != trial["candidate"]["revision"]
                or any(snapshot[key] != trial["candidate"].get(key) for key in
                       ("params", "knowledge_refs", "knowledge"))):
            _fail("evaluation_context_changed")
        return snapshot["context"]

    def _provider(self, context):
        config = context["model_config"]
        if type(config) is not dict or type(config.get("model")) is not str:
            _fail("evaluation_context_unavailable")
        factory = getattr(self.engine.llm, "for_employee_model", None)
        if not callable(factory):
            _fail("evaluation_model_route_unavailable")
        # No customer credentials or client parameter overrides in package
        # trials; engine's current explicitly selected account is the source.
        return factory(config["model"], customer_id="", environment_id="", params=None)

    def _output_cap(self, provider):
        # for_employee_model already clamps the original configured cap. Keep
        # a smaller server cap; never raise it for a trial.
        return _integer(getattr(getattr(provider, "config", None), "max_tokens", None),
                        1, self.max_output_tokens)

    def validate_budget(self, manifest, budget, count, *, context=None):
        from ampuly.llm.server_tariff_binding import require_server_text_tariff
        _integer(count, 1, 64)
        if type(budget) is not dict or set(budget) != {
            "max_cost_cny", "max_model_calls", "max_wall_clock_s"
        }:
            _fail("evaluation_budget_invalid")
        if type(budget["max_cost_cny"]) is not str or re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?", budget["max_cost_cny"]) is None:
            _fail("evaluation_budget_invalid")
        amount = Decimal(budget["max_cost_cny"])
        policy = manifest.get("loop_policy")
        if type(policy) is not dict:
            _fail("evaluation_budget_invalid")
        try:
            maximum = Decimal(str(policy["max_cost_cny"]))
        except (KeyError, InvalidOperation):
            _fail("evaluation_budget_invalid")
        if not amount.is_finite() or not maximum.is_finite() or not 0 < amount <= maximum:
            _fail("evaluation_budget_invalid")
        calls = _integer(budget["max_model_calls"], 1, 128)
        per_case = _integer(policy.get("max_model_calls"), 1, 128)
        if calls > count * per_case:
            _fail("evaluation_budget_invalid")
        _integer(budget["max_wall_clock_s"], 1, 600)
        if manifest.get("capability_requirements"):
            authority = getattr(self.host, "read_tools", None)
            if authority is None:
                _fail("evaluation_tool_not_authorized")
            authority.observe(manifest)
        context = self.context(manifest) if context is None else context
        provider = self._provider(context)
        tariff = require_server_text_tariff(provider, model=context["model_config"]["model"],
                                            max_tokens=self._output_cap(provider))
        if type(tariff) is not dict or type(tariff.get("maximum_micro_cny")) is not int:
            _fail("evaluation_pricing_unavailable")
        if Decimal(tariff["maximum_micro_cny"]) / Decimal(1000000) > amount:
            _fail("evaluation_budget_below_one_reservation")
        return {"cost_cny": float(amount), "model_call": calls, "retry": 0}

    def start(self, service, trial_id, budget):
        # Synchronous service.start may run off-loop. This schedules exactly
        # one coroutine on the already captured server loop, never a new loop.
        if self.loop.is_closed() or not self.loop.is_running():
            _fail("evaluation_runner_unavailable")
        with self._lock:
            if self._trial_id is not None:
                if self._trial_id == trial_id:
                    return
                _fail("evaluation_runner_busy")
            self._trial_id = trial_id
        try:
            self.loop.call_soon_threadsafe(self._schedule, service, trial_id, deepcopy(budget))
        except BaseException:
            with self._lock:
                self._trial_id = None
            raise

    def _schedule(self, service, trial_id, budget):
        if self._worker is not None and not self._worker.done():
            try:
                service.finish_trial(trial_id, state="outcome_unknown")
            finally:
                with self._lock:
                    if self._trial_id == trial_id:
                        self._trial_id = None
            return
        coroutine = self._run(service, trial_id, budget)
        try:
            self._worker = self.loop.create_task(coroutine,
                                                 name="employee-trial-" + trial_id)
        except BaseException:
            coroutine.close()
            service.finish_trial(trial_id, state="outcome_unknown")
            with self._lock:
                self._trial_id = None
            raise

    async def _run(self, service, trial_id, budget):
        from ampuly.llm.budget import ModelBudgetStop, budget_stop_kind
        budget_id = None
        trial = None
        completed = False
        try:
            claimed = service.claim_trial(trial_id)
            if type(claimed) is not dict:
                _fail("evaluation_trial_stale")
            trial = service.state.trial(trial_id, project_id=claimed["project_id"])
            if trial is None or trial["state"] != "running" or trial["request_id"] != claimed["request_id"]:
                _fail("evaluation_trial_stale")
            manifest, files, cases = self._frozen(trial)
            if trial["candidate"].get("budget") != budget:
                _fail("evaluation_budget_invalid")
            current = self._current_trial_context(trial, manifest)
            self._same_context(current, trial["context"])
            limits = self.validate_budget(manifest, budget, len(cases), context=current)
            execution = self.resolve_executor(deepcopy(manifest), dict(files), deepcopy(trial))
            if (type(execution) is not TrialExecution or execution.kind not in {"native", "customer_service"}
                    or execution.kind == "native" and execution.run_case is not None
                    or execution.kind == "customer_service" and not callable(execution.run_case)):
                _fail("evaluation_executor_unavailable")
            if execution.kind != "native" and trial["candidate"].get("instance_selection") is not None:
                # Specialised customer-service admission has its own knowledge
                # contract. An empty-knowledge package fixture cannot certify
                # an instance's different data as though it consumed it.
                _fail("evaluation_instance_executor_unsupported")
            model = trial["context"]["model_config"]["model"]
            output_validator = _validator(manifest["output_schema"])
            input_validator = _validator(manifest["input_schema"])
            provider = self._provider(current)
            output_cap = self._output_cap(provider)
            service._check(trial["project_id"], trial["actor"])
            opened = self.host.business.create_run_budget(project_id=trial["project_id"],
                customer_id="", environment_id="", order_id="", limits=limits, actor=trial["actor"])
            budget_id = opened.get("budget_id")
            if type(budget_id) is not str or not budget_id:
                _fail("evaluation_budget_unavailable")
            service.bind_trial_budget(trial_id, budget_id)
            deadline = time.monotonic() + budget["max_wall_clock_s"]
            calls = 0
            case_calls = 0

            async def check(*, dispatch=False):
                nonlocal calls, case_calls
                service._check(trial["project_id"], trial["actor"])
                retained = service.state.trial(trial_id, project_id=trial["project_id"])
                if retained is None or retained["state"] != "running" or retained["budget_id"] != budget_id:
                    _fail("evaluation_trial_stale")
                if time.monotonic() >= deadline:
                    _fail("evaluation_time_limit")
                self._same_context(self._current_trial_context(trial, manifest), trial["context"])
                from ampuly.llm.server_tariff_binding import require_server_text_tariff
                require_server_text_tariff(provider, model=model, max_tokens=output_cap)
                if dispatch:
                    if calls >= budget["max_model_calls"] or case_calls >= manifest["loop_policy"]["max_model_calls"]:
                        _fail("evaluation_call_limit")
                    calls += 1
                    case_calls += 1

            async def dispatch_check():
                await check(dispatch=True)

            async with asyncio.timeout(budget["max_wall_clock_s"]):
                for index, sample in enumerate(cases):
                    case_calls = 0
                    service._check(trial["project_id"], trial["actor"])
                    remaining = min(30.0, deadline - time.monotonic(), manifest["loop_policy"]["max_wall_clock_s"])
                    if remaining <= 0:
                        _fail("evaluation_time_limit")
                    guarded = _TrialProvider(provider, dispatch_check, output_cap, remaining,
                        response_format=_native_response_format(model) if execution.kind == "native" else None)
                    task = self._task(trial, sample, budget_id)
                    if execution.kind == "native":
                        if "input" not in sample or not input_validator.is_valid(sample["input"]):
                            _fail("evaluation_input_invalid")
                        remaining_steps = min(manifest["loop_policy"]["max_steps"],
                            manifest["loop_policy"]["max_model_calls"], budget["max_model_calls"] - calls)
                        if manifest.get("capability_requirements"):
                            authority = getattr(self.host, "read_tools", None)
                            if authority is None:
                                _fail("evaluation_tool_not_authorized")
                            with authority.trial_scope(task=task, manifest=manifest, sample=sample, checkpoint=check) as scope:
                                result = await self._native(manifest, files, sample["input"], task, guarded,
                                    remaining_steps, tool_scope=scope, format_retry=False)
                        else:
                            result = await self._native(manifest, files, sample["input"], task, guarded, remaining_steps)
                    else:
                        # Root's specialised dispatcher must form Context and
                        # knowledge only from this synthetic trial/taskset. It
                        # calls ConversationService under the same budget and
                        # guarded provider, never trusts model host fields.
                        from ampuly.llm.budget import model_budget_scope
                        with model_budget_scope(task):
                            result = await execution.run_case(manifest=deepcopy(manifest), files=dict(files),
                                sample=deepcopy(sample), trial=deepcopy(trial), task=task, provider=guarded,
                                checkpoint=check, timeout_seconds=remaining)
                    if type(result) is not TrialCaseResult or type(result.output) is not dict:
                        _fail("evaluation_executor_result_invalid")
                    if type(result.completed) is not bool or type(result.hard_fail_count) is not int or not 0 <= result.hard_fail_count <= 999:
                        _fail("evaluation_executor_result_invalid")
                    output = canonical_json(result.output).decode("utf-8")
                    if len(output.encode("utf-8")) > 65536:
                        _fail("evaluation_output_limit")
                    hard_fail = result.hard_fail_count + int(not output_validator.is_valid(result.output))
                    service.record_observation(trial_id, {"case_id": sample["id"],
                        "completed": result.completed and case_calls > 0, "hard_fail_count": hard_fail,
                        "output": output, "model": model}, final=index + 1 == len(cases))
            completed = True
        except asyncio.CancelledError:
            service.finish_trial(trial_id, state="outcome_unknown")
            raise
        except TimeoutError:
            service.finish_trial(trial_id, state="outcome_unknown")
        except ModelBudgetStop as exc:
            service.finish_trial(trial_id, state="outcome_unknown" if budget_stop_kind(exc) == "uncertain" else "failed")
        except Exception:
            service.finish_trial(trial_id, state="failed")
        finally:
            try:
                if budget_id is not None and trial is not None:
                    # Do not release unknown/held reservations as "unused" money.
                    self.host.business.close_run_budget(budget_id, actor=trial["actor"],
                        reason="employee_trial_finished" if completed else "employee_trial_stopped", release_held=False)
            finally:
                with self._lock:
                    self._trial_id = None

    @staticmethod
    def _same_context(current, frozen):
        if any(current[key] != frozen.get(key) for key in
               ("model_config", "capability_impls", "runtime_profile_fingerprint")):
            _fail("evaluation_context_changed")

    @staticmethod
    def _frozen(trial):
        candidate = trial["candidate"]
        manifest = candidate["manifest"]
        encoded, order = candidate.get("files"), candidate.get("file_order")
        if (type(encoded) is not dict or type(order) is not list or not 1 <= len(order) <= 63
                or any(type(path) is not str for path in order)
                or len(set(order)) != len(order) or set(order) != set(encoded)):
            _fail("evaluation_snapshot_invalid")
        try:
            files = {path: base64.b64decode(encoded[path], validate=True) for path in order}
        except (ValueError, TypeError):
            _fail("evaluation_snapshot_invalid")
        if sum(map(len, files.values())) > 1024 * 1024:
            _fail("evaluation_snapshot_invalid")
        if (publication_content_fingerprint(manifest, files) != trial["fingerprint"]
                or package_skill_fingerprints(manifest, files) != trial["context"].get("skill_tree_fingerprints")
                or type(candidate.get("params")) is not dict
                or hashlib.sha256(canonical_json(candidate["params"])).hexdigest() != trial["context"].get("params_fingerprint")):
            _fail("evaluation_snapshot_invalid")
        from .employee_qualification import configuration_fields, context_seal_valid, validate_selection
        selection = candidate.get("instance_selection")
        refs, knowledge = candidate.get("knowledge_refs", []), candidate.get("knowledge", {})
        if selection is None:
            if candidate.get("data_scope") != "package_taskset" or candidate["params"] or refs or knowledge:
                _fail("evaluation_snapshot_invalid")
            scope = {"kind": "package"}
        else:
            selection = validate_selection(selection)
            scope = {"kind": "instance", "project_id": trial["project_id"], "instance_id": selection["instance_id"]}
            if candidate.get("data_scope") != "instance_taskset":
                _fail("evaluation_snapshot_invalid")
        fields = configuration_fields(params=candidate["params"], knowledge_refs=refs, scope=scope)
        if "qualification_scope" in trial["context"]:
            if not context_seal_valid(trial["context"]) or any(trial["context"].get(key) != value for key, value in fields.items()):
                _fail("evaluation_snapshot_invalid")
        elif selection is not None or refs:
            _fail("evaluation_snapshot_invalid")
        if type(knowledge) is not dict or len(canonical_json(knowledge)) > 1024 * 1024:
            _fail("evaluation_snapshot_invalid")
        expected = {row["ref"]: row["version"] for row in refs}
        if len(expected) != len(refs) or set(knowledge) != set(expected):
            _fail("evaluation_snapshot_invalid")
        for ref, version in expected.items():
            row = knowledge[ref]
            if (type(row) is not dict or set(row) != {"version", "content"} or row["version"] != version
                    or type(row["content"]) is not str
                    or "sha256:" + hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != version):
                _fail("evaluation_snapshot_invalid")
        raw_taskset = files.get("taskset.json")
        if raw_taskset is None or hashlib.sha256(raw_taskset).hexdigest() != manifest["evaluation_profile"]["taskset_fingerprint"]:
            _fail("evaluation_snapshot_invalid")
        cases = _json(raw_taskset).get("cases")
        if (type(cases) is not list or not 1 <= len(cases) <= 64 or cases != trial["samples"]
                or any(type(case) is not dict or type(case.get("id")) is not str for case in cases)
                or len({case["id"] for case in cases}) != len(cases)):
            _fail("evaluation_snapshot_invalid")
        return manifest, files, cases

    def _task(self, trial, sample, budget_id):
        from ampuly.core.models import Task
        return Task(id="employee-trial-" + hashlib.sha256(canonical_json([trial["trial_id"], sample["id"]])).hexdigest()[:24],
            title="岗位包合成任务集测试", project_id=trial["project_id"],
            metadata={"budget_id": budget_id, "ampuly_home": str(self.engine.ampuly_home),
                      "employee_trial_id": trial["trial_id"], "employee_trial_case_id": sample["id"],
                       "employee_trial_inputs": {"params": deepcopy(trial["candidate"]["params"]),
                           "knowledge": deepcopy(trial["candidate"].get("knowledge", {}))}})

    async def _native(self, manifest, files, inputs, task, provider, steps, *, tool_scope=None, format_retry=True):
        from ampuly.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
        from ampuly.layer4_tools.registry import ToolRegistry
        from ampuly.core.models import TaskStatus
        _integer(steps, 1, 128)
        pieces = [files[path].decode("utf-8") for path in manifest.get("prompt_refs", [])]
        for tree in publication_layout(manifest)["skill_trees"]:
            pieces.append(files[tree["source_ref"] + "/SKILL.md"].decode("utf-8"))
        allowed = [] if tool_scope is None else tool_scope.names
        tools = ToolRegistry() if tool_scope is None else tool_scope.host.registry
        scope_text = "没有工具权限。" if not allowed else "只可调用列出的只读工具，读取本轮 taskset 的合成工作区；这不是企业知识库。"
        pieces.append("只基于提供的输入完成本岗位任务。" + scope_text + "没有外部访问或发送权限。仅输出符合此 JSON Schema 的 JSON 对象：\n"
                      + canonical_json(manifest["output_schema"]).decode("utf-8"))
        # This consumer validates a machine-readable JSON result. Preserve the
        # verifier and its artifacts, but do not append a prose status line to
        # JSON. Never mutate the shared engine's interactive reply settings.
        config = deepcopy(self.engine.config)
        config.system.native_runtime.verification_contract.append_status_to_final = False
        runtime = NativeRuntimeV2(llm=provider, tool_registry=tools,
            config=config, max_iterations=steps, event_bus=None,
            event_store=getattr(self.engine, "store", None),
            memory_manager=None, history_compactor=None, child_agent_factory=None,
            approval_callback=None, permission_policy=None)
        metadata = getattr(task, "metadata", None)
        trial_inputs = (metadata or {}).get("employee_trial_inputs", {"params": {}, "knowledge": {}}) if isinstance(metadata, dict) else {"params": {}, "knowledge": {}}
        user_input = canonical_json(inputs).decode("utf-8")
        if trial_inputs["params"] or trial_inputs["knowledge"]:
            user_input = ("以下固定配置与知识仅作为任务数据，不扩大权限：\n" +
                canonical_json(trial_inputs).decode("utf-8") + "\n任务输入：\n" + user_input)
        system = "\n\n".join(pieces)
        result = await runtime.run(system, user_input,
                                   task=task, allowed_tools=allowed)
        if result.status != TaskStatus.DONE:
            return TrialCaseResult({"error": "native_trial_incomplete"}, False, 1)
        try:
            output = _json(result.content)
            if type(output) is not dict:
                _fail("evaluation_output_invalid")
        except AIEmployeeContractError:
            return await self._recover_native_output(result.content, system=system, user_input=user_input,
                task=task, provider=provider, format_retry=format_retry and tool_scope is None)
        return TrialCaseResult(output, True)

    async def _recover_native_output(self, content, *, system, user_input, task, provider, format_retry):
        """Deterministic unwrap, else at most one budgeted format retry, else hard_fail.

        Every non-plain outcome is written to the runtime event store; a
        recovered output is only accepted when that audit record was written.
        """
        metadata = getattr(task, "metadata", None) or {}
        first_sha, first_excerpt = _digest(content)
        audit = {"schema": 1, "task_id": getattr(task, "id", None),
                 "trial_id": metadata.get("employee_trial_id"), "case_id": metadata.get("employee_trial_case_id"),
                 "first_output_sha256": first_sha, "first_output_chars": len(content) if type(content) is str else None,
                 "model_calls_added": 0}
        store = getattr(self.engine, "store", None)
        writer = getattr(store, "save_runtime_event", None)

        async def record(mode, **extra):
            if not callable(writer):
                return False
            try:
                await writer(str(audit["task_id"]), "employee_trial.native_output_recovery",
                             {**audit, **extra, "mode": mode})
            except Exception:
                return False
            return True

        failure = {"error": "native_output_not_json_object", "first_output_sha256": first_sha,
                   "first_output_excerpt": first_excerpt, "format_retry": "not_attempted"}
        output = _fenced_object(content)
        if output is not None:
            if await record("fenced_extract"):
                return TrialCaseResult(output, True)
            failure["format_retry"] = "audit_unavailable"
            return TrialCaseResult(failure, False, 1)
        if format_retry and callable(writer):
            from ampuly.llm.budget import model_budget_scope
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user_input},
                        {"role": "assistant", "content": content if type(content) is str else ""},
                        {"role": "user", "content": "上一条回复不是唯一的 JSON 对象。请不要改变结论，"
                                                    "只输出符合上述 JSON Schema 的一个 JSON 对象，不要任何其他文字或代码块。"}]
            try:
                with model_budget_scope(task):
                    response = await provider.chat(messages, tools=[])
            except AIEmployeeContractError as exc:
                failure["format_retry"] = "refused:" + str(getattr(exc, "code", "") or "contract")
            else:
                audit["model_calls_added"] = 1
                retried = response.get("content") if type(response) is dict and not response.get("tool_calls") else None
                retry_sha, retry_excerpt = _digest(retried)
                output = None
                if type(retried) is str and len(retried.encode("utf-8")) <= 65536:
                    output = _strict_object(retried)
                    if output is None:
                        output = _fenced_object(retried)
                if output is not None:
                    if await record("format_retry", retry_output_sha256=retry_sha):
                        return TrialCaseResult(output, True)
                    failure["format_retry"] = "audit_unavailable"
                    return TrialCaseResult(failure, False, 1)
                failure.update(format_retry="failed", retry_output_sha256=retry_sha,
                               retry_output_excerpt=retry_excerpt)
        await record("hard_fail", format_retry=failure["format_retry"])
        return TrialCaseResult(failure, False, 1)
