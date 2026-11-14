"""Inert Engine composition for an explicitly selected local Ego order.

The Owner entry supplies original consent and frozen order objects. Neither
model metadata nor this resource locator issues permission, budget or Effects.
Construction/authorization never opens a browser. Resource initialization is
an explicit server dependency and must return a real fixed-page inspection.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
import time
from types import MappingProxyType
from typing import Any

from .ego_native_binding import EgoNativeBindingRegistry, EgoNativeScope
from .ego_native_host import EgoNativeHost, TOOLS, ORIGIN
from .ego_sandbox_product_reader import EgoSandboxProductReader
from .local_browser_consent import LocalConsent, REGISTRY
from ..layer4_tools.ego_sandbox_page import PAGE_SCHEMA


_OWNER_ADMISSION_WAIT_SECONDS = 60.0
_MAX_ADMISSION_ORDERS = 8
_MAX_ADMISSION_WAITERS = 128


class EgoEngineRefused(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _id(value):
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip() and not any(ord(c) < 32 for c in value)


def _state(value):
    return str(getattr(value, 'value', value) or '')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass
class _Order:
    order_id: str
    anchor: str
    project: str
    run_id: str
    budget_id: str
    batch: str
    customer_id: str
    environment_id: str
    manifest_hash: str
    contract_hash: str
    grant: Any = field(repr=False)
    grant_fingerprint: str
    expires_at: float
    revoked: bool = False


@dataclass
class _Attempt:
    task: Any = field(repr=False)
    order: _Order
    failure: str = 'ego_task_preparing'
    scope: dict | None = field(default=None, repr=False)
    allowed_tools: frozenset = frozenset()
    binding: Any = None


@dataclass
class _AdmissionWait:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    waiters: int = 0


class EgoEngineBinding:
    def __init__(self, engine, *, resource_factory=None):
        if resource_factory is not None and not callable(resource_factory):
            raise TypeError('explicit_server_resource_factory_required')
        self.engine = engine
        self._factory = resource_factory
        self._key = object()
        self._registry = EgoNativeBindingRegistry(registration_key=self._key, clock=lambda: time.time())
        self._orders: dict[str, _Order] = {}
        self._anchors: dict[str, _Order] = {}
        self._attempts: dict[int, _Attempt] = {}
        self._task_ids: dict[str, Any] = {}
        self._rejected_orders: set[str] = set()
        self._rejected_anchors: set[str] = set()
        self._failed_tasks: dict[int, Any] = {}
        self._admission_closed = False
        self._adapters: dict[int, Any] = {}
        self._host = None
        self._prepare_lock = asyncio.Lock()
        # Volatile rendezvous only: an event never carries authorization.
        self._admission_waits: dict[str, _AdmissionWait] = {}
        self._admission_waiters = 0

    @property
    def enabled(self):
        return self._factory is not None

    @staticmethod
    def _grant_fingerprint(grant):
        return _digest({key: getattr(grant, key) for key in (
            'project', 'anchor_task', 'order_id', 'batch', 'actor', 'paths', 'expires',
            'maximum', 'request_id', 'duration', 'consent_id')})

    def _live(self, order):
        grant = order.grant
        if (order.revoked or grant.stopped or REGISTRY.inspect(order.project, order.anchor) is not grant
                or REGISTRY.clock() >= grant.expires or time.time() >= order.expires_at
                or self._grant_fingerprint(grant) != order.grant_fingerprint):
            raise EgoEngineRefused('ego_order_consent_inactive')
        return order

    def authorize_order(self, *, grant, frozen_order, run_id, budget_id, frozen_manifest, frozen_contract):
        try:
            result = self._authorize_order(grant=grant, frozen_order=frozen_order, run_id=run_id,
                budget_id=budget_id, frozen_manifest=frozen_manifest, frozen_contract=frozen_contract)
            self._wake_order_admission(frozen_order.order_id)
            return result
        except BaseException:
            # The caller explicitly selected Ego. A failed composition must
            # not leave its freshly authorized generic browser grant active.
            if type(grant) is LocalConsent and REGISTRY.inspect(grant.project, grant.anchor_task) is grant:
                if len(self._rejected_orders) >= 128:
                    self._admission_closed = True
                else:
                    self._rejected_orders.add(grant.order_id)
                    self._rejected_anchors.add(grant.anchor_task)
                REGISTRY.revoke(grant.project, grant.anchor_task)
            # Failed admission wakes only its own waiters; they still require
            # the original registration/rejection checks and may not fall back.
            failed_order_id = grant.order_id if type(grant) is LocalConsent else getattr(frozen_order, 'order_id', None)
            if _id(failed_order_id):
                self._wake_order_admission(failed_order_id)
            if self._admission_closed:
                self._wake_order_admission()
            raise

    def _authorize_order(self, *, grant, frozen_order, run_id, budget_id, frozen_manifest, frozen_contract):
        if not self.enabled:
            raise EgoEngineRefused('ego_server_resource_factory_unavailable')
        from ..plugins.office_ui.local_browser_consent_ws import frozen_local_paths
        if type(grant) is not LocalConsent or REGISTRY.inspect(grant.project, grant.anchor_task) is not grant:
            raise EgoEngineRefused('ego_original_owner_consent_required')
        project = str(getattr(self.engine, 'project_id', '') or '')
        fields = getattr(frozen_order, 'fields', None)
        order_id = getattr(frozen_order, 'order_id', None)
        anchor = getattr(frozen_order, 'launched_task_id', None)
        if (not all(_id(value) for value in (project, order_id, anchor, run_id, budget_id))
                or getattr(frozen_order, 'project_id', None) != project or _state(getattr(frozen_order, 'status', '')) != 'launched'
                or type(fields) is not dict or fields.get('batch') != grant.batch
                or (grant.project, grant.anchor_task, grant.order_id) != (project, anchor, order_id)
                or type(frozen_manifest) is not dict or type(frozen_contract) is not dict
                or frozen_contract.get('project_id') != project):
            raise EgoEngineRefused('ego_frozen_order_scope_invalid')
        paths = frozen_local_paths(frozen_manifest)
        if paths != grant.paths or set(paths) != {ORIGIN} or set(paths[ORIGIN]) != {'/admin', '/api/products'}:
            raise EgoEngineRefused('ego_frozen_sandbox_paths_required')
        remaining = grant.expires - REGISTRY.clock()
        if grant.stopped or not 0 < remaining <= 7200:
            raise EgoEngineRefused('ego_order_consent_inactive')
        # Build and validate everything before changing any registration map.
        admission = _Order(order_id, anchor, project, run_id, budget_id, grant.batch,
            str(getattr(frozen_order, 'customer_id', '') or ''), str(getattr(frozen_order, 'environment_id', '') or ''),
            _digest(frozen_manifest), _digest(frozen_contract), grant, self._grant_fingerprint(grant), time.time() + remaining)
        previous = self._orders.get(order_id)
        if previous is not None:
            if (previous.grant is grant and not previous.revoked
                    and (previous.run_id, previous.budget_id, previous.manifest_hash, previous.contract_hash) ==
                        (run_id, budget_id, admission.manifest_hash, admission.contract_hash)):
                self._live(previous); return
            raise EgoEngineRefused('ego_order_already_selected')
        if anchor in self._anchors or len(self._orders) >= 8:
            raise EgoEngineRefused('ego_order_registration_conflict')
        self._orders[order_id] = admission
        self._anchors[anchor] = admission

    def _allowed(self, original):
        # NativeAgent returns a definite list; an absent intersection is not
        # permission to expand the entire registry. Managed turns may use tuple.
        if type(original) not in {list, tuple} or any(type(name) is not str for name in original):
            raise EgoEngineRefused('ego_original_tool_scope_invalid')
        return frozenset(original) & TOOLS

    def _rejection(self, task):
        hinted = dict(getattr(task, 'metadata', {}) or {}).get('order_id')
        if (self._admission_closed or id(task) in self._failed_tasks or hinted in self._rejected_orders
                or getattr(task, 'id', None) in self._rejected_anchors):
            if len(self._failed_tasks) < 128:
                self._failed_tasks[id(task)] = task
            else:
                self._admission_closed = True
            raise EgoEngineRefused('ego_selected_order_unavailable')

    async def _verify(self, task, order):
        from ..application.effect_execution_scope import capture_server_execution_scope
        self._live(order)
        if getattr(self.engine, '_shutting_down', False):
            raise EgoEngineRefused('ego_engine_stopping')
        durable = await self.engine.store.get_task(task.id)
        metadata = dict(getattr(durable, 'metadata', {}) or {})
        if (durable is None or durable.project_id != order.project or durable.id != task.id
                or getattr(task, 'project_id', None) != order.project
                or metadata.get('order_id') != order.order_id
                or _state(durable.status) in {'done', 'completed', 'failed', 'cancelled', 'canceled', 'closed'}):
            raise EgoEngineRefused('ego_durable_task_scope_invalid')
        scope = await capture_server_execution_scope(self.engine, task_id=task.id)
        current = dict(getattr(task, 'metadata', {}) or {})
        if (scope.get('project_id') != order.project or scope.get('budget_id') != order.budget_id
                or scope.get('delegation_run_id') != order.run_id
                or any(current.get(key) != metadata.get(key) for key in (
                    'order_id', 'budget_id', 'delegation_run_id', 'company_run_controller_owner_token',
                    'company_run_controller_lease_generation'))
                or any(str(source.get(key) or '') != value for source in (current, metadata)
                    for key, value in (('customer_id', order.customer_id), ('environment_id', order.environment_id)))
                or not _id(scope.get('controller_owner_token'))
                or type(scope.get('controller_generation')) is not int or scope['controller_generation'] <= 0):
            raise EgoEngineRefused('ego_server_execution_scope_invalid')
        run = await self.engine.store.get_delegation_run(order.run_id)
        if (run is None or run.project_id != order.project or not _state(run.status)
                or _state(run.status) in {'done', 'completed', 'failed', 'cancelled', 'canceled', 'closed', 'unknown', 'pending_verification'}
                or not await self.engine.store.delegation_run_controller_lease_is_current(order.run_id,
                    project_id=order.project, owner_token=scope['controller_owner_token'], generation=scope['controller_generation'])):
            raise EgoEngineRefused('ego_run_controller_inactive')
        reader = getattr(self.engine, 'effect_budget_reader', None)
        if not callable(reader): raise EgoEngineRefused('ego_original_budget_unavailable')
        budget = reader(order.budget_id)
        if inspect.isawaitable(budget): budget = await budget
        if (type(budget) is not dict or budget.get('state') != 'open' or budget.get('health', 'ok') != 'ok'
                or budget.get('budget_id') != order.budget_id or budget.get('order_id') != order.order_id
                or any(str(budget.get(key) or '') != value for key, value in (
                    ('project_id', order.project), ('customer_id', order.customer_id), ('environment_id', order.environment_id)))):
            raise EgoEngineRefused('ego_original_budget_inactive')
        available = dict(budget.get('available') or {}).get('cost_cny')
        if available is not None and (type(available) not in {int, float} or not math.isfinite(available) or available <= 0):
            raise EgoEngineRefused('ego_original_budget_exhausted')
        return scope

    def _get_host(self):
        if self._host is None:
            from .browser_bridge import gateway
            self._host = EgoNativeHost(engine=self.engine, registry=self._registry, registration_key=self._key,
                gateway=gateway(), adapter_for=lambda binding: self._adapters[binding.scope.space_id],
                product_reader=EgoSandboxProductReader(), clock=lambda: time.time())
        return self._host

    def _wake_order_admission(self, order_id=None):
        if order_id is None:
            for slot in self._admission_waits.values():
                slot.event.set()
        elif _id(order_id):
            slot = self._admission_waits.get(order_id)
            if slot is not None:
                slot.event.set()

    def _fail_admission_task(self, task):
        if len(self._failed_tasks) < 128:
            self._failed_tasks[id(task)] = task
        else:
            self._admission_closed = True
            self._wake_order_admission()

    async def _await_owner_admission(self, task):
        # A hint only chooses the rendezvous. It grants no permission and is
        # checked against authoritative ownership/_verify inside prepare_task.
        deadline = asyncio.get_running_loop().time() + _OWNER_ADMISSION_WAIT_SECONDS
        hinted = dict(getattr(task, 'metadata', {}) or {}).get('order_id')
        if not _id(hinted):
            from ..layer2_organization.company_runtime_identity import resolve_company_interaction_ownership
            try:
                async with asyncio.timeout_at(deadline):
                    ownership = await resolve_company_interaction_ownership(
                        self.engine.store, self.engine.project_id, waiting_task_id=task.id)
            except TimeoutError:
                self._fail_admission_task(task)
                raise EgoEngineRefused('ego_owner_admission_timed_out') from None
            except asyncio.CancelledError:
                self._fail_admission_task(task)
                raise
            existing = self._anchors.get(ownership.get('ui_anchor_task_id'))
            if existing is None:
                raise EgoEngineRefused('ego_owner_order_identity_required')
            hinted = existing.order_id
        self._rejection(task)
        order = self._orders.get(hinted)
        if order is not None:
            self._live(order)
            return
        slot = self._admission_waits.get(hinted)
        if (self._admission_waiters >= _MAX_ADMISSION_WAITERS
                or slot is None and len(self._admission_waits) >= _MAX_ADMISSION_ORDERS):
            raise EgoEngineRefused('ego_owner_admission_capacity')
        if slot is None:
            slot = _AdmissionWait()
            self._admission_waits[hinted] = slot
        slot.waiters += 1
        self._admission_waiters += 1
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    await slot.event.wait()
            except TimeoutError:
                self._fail_admission_task(task)
                raise EgoEngineRefused('ego_owner_admission_timed_out') from None
            except asyncio.CancelledError:
                self._fail_admission_task(task)
                raise
            self._rejection(task)
            order = self._orders.get(hinted)
            if order is None:
                self._fail_admission_task(task)
                raise EgoEngineRefused('ego_owner_admission_unavailable')
            self._live(order)
        finally:
            slot.waiters -= 1
            self._admission_waiters -= 1
            if slot.waiters == 0 and self._admission_waits.get(hinted) is slot:
                self._admission_waits.pop(hinted)

    async def prepare_task(self, actual_task, original_allowed_tools):
        if actual_task is None:
            return None
        self._rejection(actual_task)
        ego_browser_required = False
        if self.enabled:
            allowed = self._allowed(original_allowed_tools)
            ego_browser_required = any(name.startswith('browser_') for name in original_allowed_tools)
            if ego_browser_required:
                if not allowed:
                    raise EgoEngineRefused('ego_browser_tools_not_allowed')
                # Wait outside the shared prepare lock. No model, resource or
                # browser is started until genuine Owner admission is verified.
                await self._await_owner_admission(actual_task)
        if not self._orders and not self._rejected_anchors:
            return None
        async with self._prepare_lock:
            self._rejection(actual_task)
            previous = self._attempts.get(id(actual_task))
            if previous is not None:
                if previous.failure == 'ego_browser_tools_not_allowed':
                    if not self._allowed(original_allowed_tools):
                        return None
                    raise EgoEngineRefused('ego_original_tool_scope_changed')
                if previous.failure: raise EgoEngineRefused(previous.failure)
                self._live(previous.order)
                if self._allowed(original_allowed_tools) != previous.allowed_tools:
                    self.revoke_order(order_id=previous.order.order_id)
                    raise EgoEngineRefused('ego_original_tool_scope_changed')
                try:
                    if await self._verify(actual_task, previous.order) != previous.scope:
                        raise EgoEngineRefused('ego_server_execution_scope_changed')
                except BaseException:
                    previous.failure = 'ego_task_execution_scope_changed'
                    raise
                return self.select_binding(actual_task)
            from ..layer2_organization.company_runtime_identity import resolve_company_interaction_ownership
            ownership = await resolve_company_interaction_ownership(self.engine.store, self.engine.project_id,
                                                                    waiting_task_id=actual_task.id)
            if ownership.get('ui_anchor_task_id') in self._rejected_anchors:
                if len(self._failed_tasks) < 128:
                    self._failed_tasks[id(actual_task)] = actual_task
                else:
                    self._admission_closed = True
                raise EgoEngineRefused('ego_selected_order_unavailable')
            order = self._anchors.get(ownership.get('ui_anchor_task_id'))
            hinted_order = dict(getattr(actual_task, 'metadata', {}) or {}).get('order_id')
            if order is None:
                if hinted_order in self._orders:
                    order = self._orders[hinted_order]
                else:
                    if ego_browser_required:
                        raise EgoEngineRefused('ego_owner_admission_unavailable')
                    return None
            if len(self._attempts) >= 128:
                raise EgoEngineRefused('ego_task_registration_capacity')
            record = _Attempt(actual_task, order)
            self._attempts[id(actual_task)] = record
            try:
                allowed = self._allowed(original_allowed_tools)
                if not allowed:
                    # This is an analysis/file-only seat. Do not interrupt its
                    # ordinary Native turn, create resources or widen tools;
                    # any later browser call on this object still fails closed.
                    record.allowed_tools = frozenset()
                    record.failure = 'ego_browser_tools_not_allowed'
                    return None
                if self._task_ids.setdefault(actual_task.id, actual_task) is not actual_task:
                    raise EgoEngineRefused('ego_task_object_mismatch')
                if ownership.get('ui_anchor_task_id') != order.anchor:
                    raise EgoEngineRefused('ego_order_anchor_mismatch')
                record.scope = await self._verify(actual_task, order)
                initial_url = ORIGIN + '/admin?batch=' + order.batch
                frozen = MappingProxyType(dict(task_id=actual_task.id, project_id=order.project, run_id=order.run_id,
                    order_id=order.order_id, budget_grant_id=order.budget_id, batch_id=order.batch,
                    expires_at=order.expires_at, allowed_tools=allowed, consent_id=order.grant.consent_id))
                pending = self._factory(task=actual_task, scope=frozen, initial_url=initial_url)
                if not inspect.isawaitable(pending): raise EgoEngineRefused('ego_async_resource_factory_required')
                resource = await pending
                if type(resource) is not dict or set(resource) != {'adapter', 'profile_id', 'space_id', 'inspection'}:
                    raise EgoEngineRefused('ego_bootstrap_resource_invalid')
                adapter, profile, space, observed = (resource[key] for key in ('adapter', 'profile_id', 'space_id', 'inspection'))
                backend = getattr(adapter, 'backend', None)
                if (not _id(profile) or profile.lower() == 'default' or type(space) is not int or not 0 <= space <= 9007199254740991
                        or getattr(adapter, 'origin', None) != ORIGIN or getattr(adapter, 'batch_id', None) != order.batch
                        or getattr(backend, 'ego_profile_id', None) != profile or getattr(backend, '_ego_space_id', None) != space
                        or getattr(backend, 'lifecycle_state', None) != 'active' or type(observed) is not dict
                        or observed.get('schema') != PAGE_SCHEMA or observed.get('ok') is not True or observed.get('unknown') is not False
                        or observed.get('url') != initial_url or observed.get('resource_state') != 'active'
                        or observed.get('read_only_recovery') is not False):
                    raise EgoEngineRefused('ego_bootstrap_identity_unverified')
                if space in self._adapters and self._adapters[space] is not adapter:
                    raise EgoEngineRefused('ego_bootstrap_space_conflict')
                if await self._verify(actual_task, order) != record.scope:
                    raise EgoEngineRefused('ego_execution_changed_during_bootstrap')
                self._adapters[space] = adapter
                scope = EgoNativeScope(actual_task.id, order.project, order.run_id, order.order_id, order.batch,
                    order.budget_id, profile, space, order.expires_at, allowed)
                record.binding = self._get_host().bind_task(actual_task, scope)
                record.allowed_tools, record.failure = allowed, ''
                return record.binding
            except BaseException as exc:
                record.failure = 'ego_task_preparation_failed'
                if isinstance(exc, Exception) and not isinstance(exc, EgoEngineRefused):
                    raise EgoEngineRefused(record.failure) from None
                raise

    def select_binding(self, task):
        if task is None: return None
        self._rejection(task)
        record = self._attempts.get(id(task))
        if record is None:
            if getattr(task, 'id', None) in self._task_ids:
                raise EgoEngineRefused('ego_task_object_mismatch')
            if dict(getattr(task, 'metadata', {}) or {}).get('order_id') in self._orders:
                raise EgoEngineRefused('ego_task_not_prepared')
            return None
        if record.task is not task or record.failure: raise EgoEngineRefused(record.failure or 'ego_task_object_mismatch')
        self._live(record.order)
        return self._host.select_binding(task)

    async def dispatch(self, tool_name, arguments, *, task, binding, on_progress=None):
        if self.select_binding(task) is not binding: raise EgoEngineRefused('ego_binding_mismatch')
        record = self._attempts[id(task)]
        if await self._verify(task, record.order) != record.scope: raise EgoEngineRefused('ego_server_execution_scope_changed')
        return await self._host.dispatch(tool_name, arguments, task=task, binding=binding, on_progress=on_progress)

    def select_effect(self, effect, *, readback=False):
        if self._host is None:
            if effect.payload.get('backend') == 'ego' or 'ego_scope' in effect.payload:
                raise EgoEngineRefused('ego_effect_host_unavailable')
            return None
        binding = self._host.select_effect(effect, readback=readback)
        if binding is not None: self._live(self._orders[binding.scope.order_id])
        return binding

    async def simulate(self, effect, *, binding):
        self._live(self._orders[binding.scope.order_id])
        return await self._host.simulate(effect, binding=binding)

    async def apply(self, effect, *, binding, idempotency_key):
        self._live(self._orders[binding.scope.order_id])
        return await self._host.apply(effect, binding=binding, idempotency_key=idempotency_key)

    async def verify(self, effect, *, binding):
        self._live(self._orders[binding.scope.order_id])
        return await self._host.verify(effect, binding=binding)

    def revoke_order(self, *, order_id):
        order = self._orders.get(order_id)
        if order is None:
            if order_id in self._admission_waits:
                if len(self._rejected_orders) < 128:
                    self._rejected_orders.add(order_id)
                else:
                    self._admission_closed = True
                self._wake_order_admission(order_id)
                if self._admission_closed:
                    self._wake_order_admission()
            return
        order.revoked = True
        self._wake_order_admission(order_id)
        if REGISTRY.inspect(order.project, order.anchor) is order.grant:
            REGISTRY.revoke(order.project, order.anchor)
        incomplete = False
        for record in self._attempts.values():
            if record.order is order:
                record.failure = 'ego_order_revoked'
                if self._host is not None and record.binding is not None:
                    try:
                        self._host.revoke_task(record.task)
                    except Exception:
                        incomplete = True
        if incomplete:
            raise EgoEngineRefused('ego_resource_revocation_incomplete')
