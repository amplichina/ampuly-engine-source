"""Server-owned Ego leaf for the one local sandbox contract.

This is neither an authorization issuer nor an Effect journal. Resources are
volatile; a restarted host cannot recover a write merely from its payload.
Only the original approval service and Effect gateway can dispatch a submit.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from ampuly.computer_use.contracts import (
    ACTION_KINDS, ActionReceipt, ComputerAction, Observation, Outcome, VerificationSpec,
)
from ampuly.computer_use.ego_native_binding import (
    EgoBindingRefused, EgoNativeBinding, EgoNativeBindingRegistry, EgoNativeScope,
)
from ampuly.computer_use.gateway import ComputerUseGateway, ControlLease, LeaseState
from ampuly.effects.models import (
    EffectRecord, EffectState, ProviderOutcome, ProviderResult, SimulationStatus, VerificationStatus,
)
from ampuly.layer4_tools.browser_backend import Action
from ampuly.layer4_tools.ego_sandbox_page import (
    FIELD_TO_PRODUCT_KEY, PAGE_SCHEMA, guard_from_inspection,
)

ORIGIN = "http://127.0.0.1:8899"
TOOLS = frozenset({"browser_navigate", "browser_observe", "browser_type",
                   "browser_select_option", "browser_scroll", "browser_click"})
_STATUS = re.compile(r"toggleStatus\(([1-9][0-9]{0,14}),'(上架|下架)'\)\Z")
_OPEN = re.compile(r"openProduct\(([1-9][0-9]{0,14})\)\Z")
_NUMBER = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")


class EgoHostRefused(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _page_hash(inspection: dict[str, Any]) -> str:
    """Bind approval to page semantics, never CLI capture/clock metadata."""
    target = inspection.get("target")
    return _digest({"url": inspection.get("url"), "title": inspection.get("title"),
                    "edit_id": inspection.get("edit_id"), "fields": inspection.get("fields"),
                    "target": {key: target.get(key) for key in (
                        "count", "selector", "label", "tag", "onclick", "disabled")}
                    if type(target) is dict else None})


def _text(value: Any, maximum: int = 2048, *, empty: bool = True) -> str:
    if (type(value) is not str or len(value) > maximum or not empty and not value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise EgoHostRefused("ego_scalar_invalid")
    return value


def _product(value: Any, product_id: int) -> dict[str, Any]:
    keys = {"id", *FIELD_TO_PRODUCT_KEY.values()}
    if type(value) is not dict or set(value) != keys or type(value.get("id")) is not int or value["id"] != product_id:
        raise EgoHostRefused("ego_product_unavailable")
    result = copy.deepcopy(value)
    for key in ("title", "subtitle", "category", "emoji", "status"):
        _text(result[key])
    if result["status"] not in {"上架", "下架"}:
        raise EgoHostRefused("ego_product_invalid")
    for key in ("price", "originalPrice"):
        number = result[key]
        if type(number) not in {int, float} or not math.isfinite(number) or not 0 <= number <= 1e12:
            raise EgoHostRefused("ego_product_invalid")
    if type(result["stock"]) is not int or not 0 <= result["stock"] <= 9007199254740991:
        raise EgoHostRefused("ego_product_invalid")
    return result


def _expected_product(guard: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(before)
    if guard["kind"] == "status":
        match = _STATUS.fullmatch(guard["target"]["onclick"])
        if match is None or int(match[1]) != before["id"]:
            raise EgoHostRefused("ego_product_identity_changed")
        result["status"] = match[2]
    elif guard["kind"] == "save_existing":
        if guard["edit_id"] != before["id"]:
            raise EgoHostRefused("ego_product_identity_changed")
        for field_id, key in FIELD_TO_PRODUCT_KEY.items():
            value = _text(guard["fields"][field_id])
            if key in {"price", "originalPrice", "stock"}:
                clean = value.strip()
                # Fixed decimal subset; no JS expression/hex/Infinity coercion.
                if not _NUMBER.fullmatch(clean):
                    raise EgoHostRefused("ego_numeric_field_invalid")
                number = float(clean)
                if not math.isfinite(number) or not 0 <= number <= 1e12:
                    raise EgoHostRefused("ego_numeric_field_invalid")
                if key == "stock" and not number.is_integer():
                    raise EgoHostRefused("ego_numeric_field_invalid")
                result[key] = int(number) if key == "stock" else number
            elif key in {"title", "subtitle"}:
                result[key] = value.strip()
            elif key == "emoji":
                result[key] = value or "📦"
            else:
                result[key] = value
    else:
        raise EgoHostRefused("ego_submit_not_supported")
    return _product(result, before["id"])


@dataclass
class _Resource:
    binding: EgoNativeBinding
    session_id: str
    lease_id: str
    scope_digest: str
    adapter: Any
    sequence: int = 0


class EgoNativeHost:
    """A bounded, existing-task dispatcher; no browser/space is created here."""

    def __init__(self, *, engine: Any, registry: EgoNativeBindingRegistry,
                 registration_key: object, gateway: ComputerUseGateway,
                 adapter_for: Callable[[EgoNativeBinding], Any], product_reader: Callable,
                 max_parallel: int = 2, clock: Callable[[], float] = time.time):
        if (type(registry) is not EgoNativeBindingRegistry or type(gateway) is not ComputerUseGateway
                or type(registration_key) is not object or type(max_parallel) is not int
                or not 1 <= max_parallel <= 2 or not callable(adapter_for) or not callable(product_reader)):
            raise ValueError("ego_server_dependencies_required")
        self.engine, self.registry, self.gateway = engine, registry, gateway
        self._key, self._adapter_for, self._reader, self._clock = registration_key, adapter_for, product_reader, clock
        self._slots = asyncio.Semaphore(max_parallel)
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[str, _Resource] = {}
        self._spaces: dict[int, _Resource] = {}

    @staticmethod
    def _adapter_identity(adapter, binding, *, readback=False):
        backend = getattr(adapter, "backend", None)
        native_space = getattr(backend, "_ego_space_id", None)
        if (getattr(adapter, "origin", None) != ORIGIN
                or getattr(adapter, "batch_id", None) != binding.scope.batch_id
                or type(native_space) is not int or native_space != binding.scope.space_id
                or getattr(backend, "ego_profile_id", None) != binding.scope.profile_id
                or getattr(backend, "lifecycle_state", None) not in ({"active", "unknown"} if readback else {"active"})):
            raise EgoHostRefused("ego_adapter_resource_mismatch")

    def bind_task(self, task: Any, scope: EgoNativeScope) -> EgoNativeBinding:
        if str(getattr(self.engine, "project_id", "")) != scope.project_id or not scope.allowed_tools <= TOOLS:
            raise EgoHostRefused("ego_server_scope_mismatch")
        binding = self.registry.bind(task, scope, registration_key=self._key)
        if scope.task_id in self._tasks:
            if self._tasks[scope.task_id].binding is not binding:
                raise EgoHostRefused("ego_binding_conflict")
            return binding
        if scope.space_id in self._spaces:
            resource = self._spaces[scope.space_id]
            adapter = resource.adapter
            session_id, lease_id, scope_digest = resource.session_id, resource.lease_id, resource.scope_digest
        else:
            adapter = self._adapter_for(binding)
            if (getattr(adapter, "origin", None) != ORIGIN or getattr(adapter, "batch_id", None) != scope.batch_id
                    or not callable(getattr(adapter, "inspect", None))
                    or not callable(getattr(adapter, "guarded_click", None))):
                self.registry.revoke(task, registration_key=self._key)
                raise EgoHostRefused("ego_sandbox_adapter_required")
            self._adapter_identity(adapter, binding)
            scope_digest = _digest({k: v for k, v in self._scope(binding).items() if k != "task_id"})
            session_id, lease_id = "ego-" + scope_digest, "ego-lease-" + uuid4().hex
            self.gateway.open_session(ControlLease(lease_id, session_id, scope_digest,
                LeaseState.AGENT_OPERATING, scope.expires_at, ("127.0.0.1",)))
            self._locks[scope.space_id] = asyncio.Lock()
        self._adapter_identity(adapter, binding)
        resource = _Resource(binding, session_id, lease_id, scope_digest, adapter)
        self._tasks[scope.task_id] = resource
        self._spaces.setdefault(scope.space_id, resource)
        return binding

    @staticmethod
    def _scope(binding: EgoNativeBinding) -> dict[str, Any]:
        scope = binding.scope
        return {name: getattr(scope, name) for name in (
            "task_id", "project_id", "run_id", "order_id", "batch_id", "budget_grant_id", "profile_id", "space_id")}

    def select_binding(self, task: Any) -> EgoNativeBinding | None:
        try:
            return self.registry.select_binding(task)
        except EgoBindingRefused as exc:
            if exc.reason != "ego_binding_unknown":
                raise
            return self.registry.readback_binding(task)

    def _resource(self, binding: EgoNativeBinding, *, readback: bool = False) -> _Resource:
        if type(binding) is not EgoNativeBinding:
            raise EgoHostRefused("ego_binding_required")
        current = (self.registry.readback_binding(binding.task) if readback
                   else self.registry.select_binding(binding.task))
        resource = self._tasks.get(binding.scope.task_id)
        if current is not binding or resource is None or resource.binding is not binding:
            raise EgoHostRefused("ego_binding_mismatch")
        self._adapter_identity(resource.adapter, binding, readback=readback)
        return resource

    def select_effect(self, effect: EffectRecord, *, readback: bool = False) -> EgoNativeBinding | None:
        if type(readback) is not bool or not isinstance(effect, EffectRecord) or type(effect.payload) is not dict:
            raise EgoHostRefused("ego_effect_invalid")
        payload = effect.payload
        scope = payload.get("ego_scope")
        if payload.get("backend") != "ego":
            if scope is not None:
                raise EgoHostRefused("ego_backend_mismatch")
            return None
        if (type(scope) is not dict or effect.provider != "browser:submit" or effect.kind != "browser.submit"):
            raise EgoHostRefused("ego_effect_invalid")
        resource = self._tasks.get(scope.get("task_id"))
        if resource is None:
            raise EgoHostRefused("ego_effect_binding_unavailable")
        binding = resource.binding
        self._resource(binding, readback=readback)
        if (scope != self._scope(binding) or effect.project_id != binding.scope.project_id
                or effect.run_id != binding.scope.run_id or payload.get("binding_key") != resource.session_id):
            raise EgoHostRefused("ego_effect_scope_mismatch")
        return binding

    @asynccontextmanager
    async def _serialized(self, binding: EgoNativeBinding):
        async with self._slots:
            async with self._locks[binding.scope.space_id]:
                yield

    def _unknown(self, binding: EgoNativeBinding):
        try:
            self.registry.mark_unknown(binding.task, registration_key=self._key)
        except EgoBindingRefused as exc:
            if exc.reason not in {"ego_binding_revoked", "ego_binding_closed"}:
                raise

    async def _sync(self, binding: EgoNativeBinding, operation: Callable, *args,
                    _before_dispatch: Callable | None = None, **kwargs):
        # Keep the slot/space lock until an already-running CLI has exited.
        # Cancelling asyncio cannot establish that its synchronous child stopped.
        def run():
            if _before_dispatch is not None:
                _before_dispatch()
            return operation(*args, **kwargs)
        worker = asyncio.create_task(asyncio.to_thread(run))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
                self._unknown(binding)
                if worker.done():
                    break
            except Exception:
                self._unknown(binding)
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            # Retrieve any exception without interpreting it as a successful act.
            if worker.done() and not worker.cancelled():
                worker.exception()
            raise asyncio.CancelledError()
        return result

    def _source(self, url: Any, binding: EgoNativeBinding):
        try:
            parsed = urlsplit(_text(url, 4096, empty=False))
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (f"{parsed.scheme}://{parsed.netloc}" != ORIGIN or parsed.path != "/admin"
                    or parsed.fragment or parsed.username or parsed.password
                    or query != {"batch": [binding.scope.batch_id]}):
                raise ValueError()
        except (TypeError, ValueError):
            raise EgoHostRefused("ego_origin_or_batch_refused") from None

    async def _inspect(self, binding: EgoNativeBinding, selector: str = "") -> dict[str, Any]:
        resource = self._resource(binding, readback=True)
        result = await self._sync(binding, resource.adapter.inspect, selector)
        if type(result) is not dict or result.get("ok") is not True or result.get("unknown") is True:
            self._unknown(binding)
            raise EgoHostRefused("ego_observation_unavailable")
        if result.get("schema") != PAGE_SCHEMA or result.get("resource_state") not in {"active", "unknown"}:
            raise EgoHostRefused("ego_observation_invalid")
        self._source(result.get("url"), binding)
        return copy.deepcopy(result)

    def _observe(self, binding: EgoNativeBinding, inspection: dict[str, Any]) -> str:
        resource = self._resource(binding, readback=True)
        shared = self._spaces[binding.scope.space_id]
        shared.sequence += 1
        return self.gateway.observe(Observation(resource.session_id, binding.scope.task_id,
            resource.scope_digest, shared.sequence, inspection["url"], str(inspection.get("title", ""))[:200],
            True, _digest(inspection), "agent", resource.lease_id,
            "unknown" if inspection.get("resource_state") == "unknown" else "observed", self._clock()))

    def _admit(self, binding: EgoNativeBinding, inspection: dict[str, Any], kind: str,
               target: dict[str, Any], *, effect: EffectRecord | None = None):
        resource = self._resource(binding)
        observed = self._observe(binding, inspection)
        action = ComputerAction(effect.effect_id if effect else "ego-action-" + uuid4().hex,
            "ego-attempt-" + uuid4().hex, kind, resource.scope_digest, resource.lease_id, observed,
            target, "current sandbox product state" if effect else "sandbox navigation/draft",
            ACTION_KINDS[kind], 30.0, False, False, ("127.0.0.1",),
            VerificationSpec(("api", "element")) if effect else None)
        return self.gateway.submit(resource.session_id, action, effect_claim=effect.effect_id if effect else "")

    def _can_send(self, binding: EgoNativeBinding, admission, tool: str, *, effect: EffectRecord | None = None):
        resource = self._resource(binding)
        self.registry.require_action(binding.task, binding, tool)
        if not self.gateway.dispatchable(resource.session_id, admission.action.attempt_id):
            raise EgoHostRefused("ego_control_revoked")
        if effect is not None:
            from ampuly.computer_use.local_browser_consent import REGISTRY
            if not REGISTRY.effect_is_local(effect.effect_id) or not REGISTRY.effect_allowed(effect.effect_id):
                raise EgoHostRefused("ego_local_consent_unavailable")

    def _receipt(self, binding: EgoNativeBinding, admission, outcome: Outcome, *, reason="", evidence=None):
        resource = self._tasks[binding.scope.task_id]
        return self.gateway.record(resource.session_id, ActionReceipt(admission.action.action_id,
            admission.action.attempt_id, outcome, reason, evidence or {}))

    def _public(self, binding: EgoNativeBinding, inspection: dict[str, Any], *, status="observed"):
        unknown = str(getattr(self.registry.state_of(binding.task), "value", "")) == "unknown"
        return {"backend": "ego", "status": status, "url": inspection["url"],
                "title": inspection.get("title", ""), "edit_id": inspection.get("edit_id"),
                "fields": inspection.get("fields", {}), "interactive_elements": inspection.get("elements", []),
                "rows": inspection.get("rows", []), "resource_state": "unknown" if unknown else inspection.get("resource_state"),
                "read_only_recovery": unknown or inspection.get("read_only_recovery", False),
                "snapshot_digest": _digest(inspection), "task_id": binding.scope.task_id}

    async def _read_product(self, binding: EgoNativeBinding, product_id: int):
        if type(product_id) is not int or not 1 <= product_id <= 9007199254740991:
            raise EgoHostRefused("ego_product_identity_invalid")
        result = self._reader(binding, product_id)
        if not inspect.isawaitable(result):
            raise EgoHostRefused("ego_async_product_reader_required")
        return _product(await result, product_id)

    @staticmethod
    def _guard_kind(inspection):
        target = inspection.get("target") or {}
        onclick = target.get("onclick", "")
        if onclick == "saveProduct()": return "save_existing"
        if _STATUS.fullmatch(onclick): return "status"
        if _OPEN.fullmatch(onclick): return "open_existing"
        if onclick == "go('products')": return "menu_products"
        if onclick == "closeModal()": return "close_modal"
        raise EgoHostRefused("ego_control_not_supported")

    def _guard(self, binding, inspection, selector, kind):
        return guard_from_inspection(inspection, selector=selector, kind=kind,
                                     origin=ORIGIN, batch_id=binding.scope.batch_id)

    async def dispatch(self, tool_name: str, arguments: dict[str, Any], *, task: Any,
                       binding: EgoNativeBinding, on_progress=None) -> dict[str, Any]:
        if tool_name not in TOOLS or type(arguments) is not dict or binding.task is not task:
            raise EgoHostRefused("ego_tool_or_task_invalid")
        if tool_name == "browser_observe":
            self.registry.readback_binding(task)
        else:
            self.registry.require_action(task, binding, tool_name)
        proposal = None
        async with self._serialized(binding):
            if tool_name == "browser_observe":
                if set(arguments) - {"max_chars"}:
                    raise EgoHostRefused("ego_tool_arguments_invalid")
                observed = await self._inspect(binding)
                self._observe(binding, observed)
                return self._public(binding, observed)
            if tool_name == "browser_click":
                if set(arguments) - {"selector", "expect"}:
                    raise EgoHostRefused("ego_tool_arguments_invalid")
                selector = _text(arguments.get("selector"), 1024, empty=False)
                if selector.startswith("css:"): selector = selector[4:]
                observed = await self._inspect(binding, selector)
                kind = self._guard_kind(observed)
                guard = self._guard(binding, observed, selector, kind)
                if kind in {"save_existing", "status"}:
                    product_id = guard["edit_id"] if kind == "save_existing" else int(_STATUS.fullmatch(guard["target"]["onclick"])[1])
                    before = await self._read_product(binding, product_id)
                    expected = _expected_product(guard, before)
                    if expected == before:
                        return {"backend": "ego", "status": "already_in_expected_state", "product_id": product_id,
                                "state_verified": True, "submission_performed": False}
                    resource = self._resource(binding)
                    payload = {"backend": "ego", "ego_scope": self._scope(binding), "binding_key": resource.session_id,
                        "operation": "click", "selector": selector, "source_url": observed["url"],
                        "source_url_hash": hashlib.sha256(observed["url"].encode()).hexdigest(),
                        "page": observed["url"], "element": {"text": guard["target"]["label"], "selector": selector},
                        "observation_hash": _page_hash(observed), "guard": guard, "product_id": product_id,
                        "before_product": before, "expected_product": expected}
                    proposal = copy.deepcopy(payload)
                else:
                    admission = self._admit(binding, observed, "select_candidate", {"selector": selector})
                    self._can_send(binding, admission, tool_name)
                    return await self._send_draft(binding, admission, tool_name,
                        self._resource(binding).adapter.guarded_click, selector, guard)
            else:
                return await self._draft(tool_name, arguments, binding)
        # Critical: no space lock or global slot is held while proposal may
        # synchronously drain/apply the approved Effect back into this Host.
        assert proposal is not None
        publication = await self.engine.propose_effect_for_approval(
            provider="browser:submit", kind="browser.submit", payload=proposal,
            idempotency_key=_digest({"scope": proposal["ego_scope"], "guard": proposal["guard"],
                                   "before": proposal["before_product"], "expected": proposal["expected_product"]}),
            prompt="确认本单沙盒商品保存或上下架；提交后按商品ID独立读回。",
            preview={"backend": "ego", "product_id": proposal["product_id"], "element": proposal["element"],
                     "before": proposal["before_product"], "expected": proposal["expected_product"]},
            task_id=binding.scope.task_id, run_id=binding.scope.run_id,
            customer_id=str(getattr(task, "metadata", {}).get("customer_id", "")),
            environment_id=str(getattr(task, "metadata", {}).get("environment_id", "")),
        )
        effect = publication.effect
        state = str(getattr(effect.state, "value", effect.state))
        return {"backend": "ego", "status": state, "effect_id": effect.effect_id,
                "product_id": proposal["product_id"], "submission_performed": state == "applied",
                "note": "当前状态可核验；无revision，不证明精确提交次数。"}

    async def _draft(self, tool, args, binding):
        selector = args.get("selector", "")
        observed = await self._inspect(binding, selector[4:] if type(selector) is str and selector.startswith("css:") else selector)
        draft = getattr(self._resource(binding).adapter, "guarded_draft", None)
        if not callable(draft):
            raise EgoHostRefused("ego_atomic_draft_not_supported")
        if tool == "browser_navigate":
            if set(args) - {"url", "wait_until"}: raise EgoHostRefused("ego_tool_arguments_invalid")
            self._source(args.get("url"), binding)
            action, kind = Action("navigate", args["url"]), "select_candidate"
        elif tool in {"browser_type", "browser_select_option"}:
            keys = {"selector", "text", "press_enter", "clear_existing"} if tool == "browser_type" else {"selector", "value", "label", "index"}
            if set(args) - keys: raise EgoHostRefused("ego_tool_arguments_invalid")
            selector = _text(selector, 128, empty=False)
            if selector.startswith("css:"): selector = selector[4:]
            field = selector[1:] if selector.startswith("#") else ""
            if field not in FIELD_TO_PRODUCT_KEY or observed.get("edit_id") is None or observed.get("fields", {}).get(field) is None:
                raise EgoHostRefused("ego_known_edit_field_required")
            if tool == "browser_type":
                if args.get("press_enter", False) is not False or args.get("clear_existing", True) is not True:
                    raise EgoHostRefused("ego_enter_or_append_not_supported")
                value = _text(args.get("text"))
                if field in {"f_cat", "f_status"}: raise EgoHostRefused("ego_select_field_required")
                action = Action("fill", selector, value)
            else:
                if field not in {"f_cat", "f_status"} or args.get("label") is not None or args.get("index") is not None:
                    raise EgoHostRefused("ego_exact_select_value_required")
                value = _text(args.get("value"))
                if field == "f_status" and value not in {"上架", "下架"}: raise EgoHostRefused("ego_status_invalid")
                action = Action("select", selector, value)
            kind = "type_draft"
        elif tool == "browser_scroll":
            if set(args) - {"amount", "direction", "to_bottom"}: raise EgoHostRefused("ego_tool_arguments_invalid")
            amount, direction = args.get("amount", 800), args.get("direction", "down")
            if (type(amount) is not int or not 1 <= amount <= 2000 or direction not in {"up", "down"}
                    or args.get("to_bottom", False) is not False): raise EgoHostRefused("ego_scroll_invalid")
            action, kind = Action("scroll", meta={"dy": amount if direction == "down" else -amount}), "scroll"
        else:
            raise EgoHostRefused("ego_tool_unsupported")
        admission = self._admit(binding, observed, kind, {"selector": selector} if selector else {})
        self._can_send(binding, admission, tool)
        return await self._send_draft(binding, admission, tool, draft, action, observed)

    async def _send_draft(self, binding, admission, tool, operation, *args):
        try:
            result = await self._sync(binding, operation, *args,
                action_id=admission.action.action_id, attempt_id=admission.action.attempt_id,
                _before_dispatch=lambda: self._can_send(binding, admission, tool))
            return await self._finish_draft(binding, admission, result)
        except asyncio.CancelledError:
            self._unknown(binding)
            self._receipt(binding, admission, Outcome.CANCELLED_UNKNOWN, reason="ego_draft_cancelled_unknown")
            raise
        except Exception:
            self._unknown(binding)
            self._receipt(binding, admission, Outcome.OUTCOME_UNKNOWN, reason="ego_draft_unknown")
            return {"backend": "ego", "status": "outcome_unknown", "automatic_retry": False}

    async def _finish_draft(self, binding, admission, result):
        if type(result) is not dict or result.get("ok") is not True or result.get("unknown") is True:
            self._unknown(binding)
            self._receipt(binding, admission, Outcome.OUTCOME_UNKNOWN, reason="ego_transport_unknown")
            return {"backend": "ego", "status": "outcome_unknown", "automatic_retry": False}
        after = await self._inspect(binding)
        self._receipt(binding, admission, Outcome.OUTCOME_UNKNOWN, reason="input_accepted_unverified")
        self._observe(binding, after)
        return self._public(binding, after, status="draft_input_sent")

    async def simulate(self, effect: EffectRecord, *, binding):
        if self.select_effect(effect) is not binding:
            return SimulationStatus.INCOMPLETE, {"reason": "ego_binding_mismatch"}
        return SimulationStatus.PREVIEW_ONLY, {"backend": "ego", "product_id": effect.payload.get("product_id"),
            "network_call_performed": False, "not_checked": ["fresh target", "current product", "business result"]}

    async def _effect_guard(self, effect, binding):
        from ampuly.application.effect_execution_scope import check_engine_effect_execution_scope, capture_server_execution_scope
        if (self.select_effect(effect) is not binding or effect.state is not EffectState.CLAIMED
                or not effect.authorization_id or not effect.claim_token):
            raise EgoHostRefused("ego_effect_not_claimed")
        refused = await check_engine_effect_execution_scope(self.engine, effect)
        if refused is not None:
            raise EgoHostRefused("ego_execution_scope_unavailable")
        scope = await capture_server_execution_scope(self.engine, task_id=binding.scope.task_id)
        if (scope.get("project_id") != binding.scope.project_id or scope.get("task_id") != binding.scope.task_id
                or scope.get("budget_id") != binding.scope.budget_grant_id
                or scope.get("delegation_run_id") != binding.scope.run_id):
            raise EgoHostRefused("ego_execution_scope_changed")

    async def apply(self, effect: EffectRecord, *, binding, idempotency_key: str):
        admission = None
        sent = False
        try:
            async with self._serialized(binding):
                await self._effect_guard(effect, binding)
                payload = effect.payload
                if idempotency_key != effect.idempotency_key or payload.get("operation") != "click":
                    raise EgoHostRefused("ego_effect_identity_invalid")
                selector = _text(payload.get("selector"), 1024, empty=False)
                before = await self._inspect(binding, selector)
                guard = self._guard(binding, before, selector, self._guard_kind(before))
                if guard != payload.get("guard") or _page_hash(before) != payload.get("observation_hash"):
                    raise EgoHostRefused("ego_approved_page_changed")
                product_id = payload.get("product_id")
                current = await self._read_product(binding, product_id)
                expected = _expected_product(guard, current)
                if current != payload.get("before_product") or expected != payload.get("expected_product"):
                    raise EgoHostRefused("ego_approved_product_changed")
                admission = self._admit(binding, before, "submit", {"selector": selector}, effect=effect)
                await self._effect_guard(effect, binding)
                self._can_send(binding, admission, "browser_click", effect=effect)
                sent = True
                result = await self._sync(binding, self._resource(binding).adapter.guarded_click,
                    selector, guard, action_id=admission.action.action_id, attempt_id=admission.action.attempt_id,
                    _before_dispatch=lambda: self._can_send(binding, admission, "browser_click", effect=effect))
                if (type(result) is dict and result.get("ok") is False and result.get("unknown") is False
                        and result.get("phase") == "before_effect" and result.get("error") == "sandbox_guard_changed"):
                    sent = False
                    self._receipt(binding, admission, Outcome.FAILED_BEFORE_EFFECT, reason="ego_live_guard_changed")
                    return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="ego_live_guard_changed; nothing clicked")
                if type(result) is not dict or result.get("ok") is not True or result.get("unknown") is True:
                    raise EgoHostRefused("ego_submit_transport_unknown")
                verification, evidence = await self._verify_state(effect, binding)
                if verification is not VerificationStatus.COMPLETE:
                    self._unknown(binding)
                    self._receipt(binding, admission, Outcome.OUTCOME_UNKNOWN, reason="ego_product_readback_incomplete", evidence=evidence)
                    return ProviderResult(ProviderOutcome.UNKNOWN, evidence, "submit sent; current result not verified")
                self._receipt(binding, admission, Outcome.SUCCEEDED, evidence={"verification": evidence})
                return ProviderResult(ProviderOutcome.APPLIED, {"verification": evidence}, "current product state verified; exact submission count unsupported")
        except asyncio.CancelledError:
            self._unknown(binding)
            if admission is not None:
                self._receipt(binding, admission, Outcome.CANCELLED_UNKNOWN if sent else Outcome.CANCELLED_BEFORE_EFFECT)
            raise
        except Exception:
            if sent:
                self._unknown(binding)
                if admission is not None: self._receipt(binding, admission, Outcome.OUTCOME_UNKNOWN, reason="ego_submit_unknown")
                return ProviderResult(ProviderOutcome.UNKNOWN, detail="ego_submit_unknown; read back, do not retry")
            if admission is not None: self._receipt(binding, admission, Outcome.FAILED_BEFORE_EFFECT, reason="ego_pre_dispatch_refused")
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail="ego_pre_dispatch_refused")

    async def _verify_state(self, effect, binding):
        product_id = effect.payload.get("product_id")
        current = await self._read_product(binding, product_id)
        expected = _product(effect.payload.get("expected_product"), product_id)
        after = await self._inspect(binding)
        rows = after.get("rows")
        matching = [row for row in rows if type(row) is dict and row.get("product_id") == product_id] if type(rows) is list else []
        # An exact product row must be independently visible in Ego, not a toast.
        page_held = (len(matching) == 1 and type(matching[0].get("columns")) is list
                     and any(type(column) is str and expected["title"] in column for column in matching[0]["columns"])
                     and any(type(column) is str and expected["status"] in column for column in matching[0]["columns"]))
        holds = current == expected and page_held
        evidence = {"sources": ["api", "element"], "checked": ["unique product ID GET fields", "Ego product row"],
            "holds": holds, "failed": [] if holds else ["current_product_or_row_mismatch"],
            "product_id": product_id, "current_product": current, "exact_submission_count_verified": False,
            "page_row": matching[0] if len(matching) == 1 else None}
        self._observe(binding, after)
        return (VerificationStatus.COMPLETE if holds else VerificationStatus.INCOMPLETE), evidence

    async def verify(self, effect: EffectRecord, *, binding):
        try:
            if self.select_effect(effect, readback=True) is not binding:
                raise EgoHostRefused("ego_binding_mismatch")
            async with self._serialized(binding):
                return await self._verify_state(effect, binding)
        except Exception:
            return VerificationStatus.INCOMPLETE, {"reason": "ego_readback_unavailable", "exact_submission_count_verified": False}

    def revoke_task(self, task: Any, *, reason: str = "owner_revoked") -> None:
        # Withdrawal is a control operation, not a request to read/use a live
        # lease. It must work after expiry and after a write became unknown.
        task_id = getattr(task, "id", None)
        resource = self._tasks.get(task_id) if type(task_id) is str else None
        if resource is None or resource.binding.task is not task:
            raise EgoHostRefused("ego_task_not_bound")
        binding = resource.binding
        self.registry.revoke(task, registration_key=self._key)
        self.gateway.set_lease_state(resource.session_id, LeaseState.REVOKED, note="ego_owner_revoked")
        self.gateway.cancel_in_flight(resource.session_id, reason="ego_owner_revoked")
