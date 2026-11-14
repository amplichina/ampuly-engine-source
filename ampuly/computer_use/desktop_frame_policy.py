"""Screenshot disclosure uses the original durable owner Effect approval lane.

The new provider grants one bounded, ephemeral capture policy; it never sends
an image, starts a helper or calls a model. Only a real claimed owner effect
can install its reference, and every image delivery rereads that exact journal
record plus the current explicit desktop switch revision. No new store/schema.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import inspect
import json
import math
import re
import time

from ampuly.computer_use.desktop_frames import FrameGrant, FrameRefused, clean_masks, scope_for
from ampuly.effects import (EffectRecord, EffectState, ProviderOutcome, ProviderResult, SimulationStatus,
                        VerificationStatus, EffectGateway)

KIND = 'desktop.frame-disclosure-policy.v1'


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class DesktopFramePolicyAuthority:
    supports_idempotency = True
    def __init__(self, manager, *, clock=time.time):
        engine = manager.engine
        gateway, service = getattr(engine, 'effect_gateway', None), getattr(engine, 'effect_approval_service', None)
        if (type(gateway) is not EffectGateway or service is None or getattr(service, 'gateway', None) is not gateway
                or not callable(getattr(engine, 'propose_effect_for_approval', None))
                or getattr(engine, '_initialized', None) is not True):
            raise FrameRefused('desktop_frame_owner_approval_unavailable')
        self.manager, self.gateway, self.clock = manager, gateway, clock
        holder = manager.frames
        vault = getattr(holder, 'vault', None)
        nonce = getattr(vault, 'session_nonce', None) if vault is not None else getattr(holder, 'session_nonce', None)
        if type(nonce) is not str or not nonce:
            raise FrameRefused('desktop_frame_owner_approval_unavailable')
        self.nonce = nonce
        self.name = 'desktop:frame-policy:' + self.nonce
        self._proposals, self._applied = {}, {}

    def _switch_revision(self):
        value = self.manager.read_switch('desktop_channel')
        if (type(value) is not dict or value.get('explicit') is not True or value.get('enabled') is not True
                or value.get('connector') != 'desktop_channel' or type(value.get('actor')) is not str
                or not value['actor'] or type(value.get('at')) is not str or not value['at']):
            raise FrameRefused('desktop_channel_closed_or_unanchored')
        return hashlib.sha256(_bytes({key: value[key] for key in ('connector', 'explicit', 'enabled', 'actor', 'at')})).hexdigest()

    @staticmethod
    def _key(scope, window, route, switch):
        return hashlib.sha256(_bytes([scope, window, route, switch])).hexdigest()

    def _payload(self, effect):
        if type(effect) is not EffectRecord:
            raise FrameRefused('desktop_frame_policy_missing')
        payload = effect.payload
        expected = {'scope', 'window_id', 'width', 'height', 'route', 'masks', 'full_window_approved',
            'session_nonce', 'expires_at', 'max_frames', 'switch_revision'}
        if (type(effect) is not EffectRecord or effect.provider != self.name or effect.kind != KIND
                or effect.project_id != self.manager.project_id or type(payload) is not dict or set(payload) != expected
                or type(payload.get('scope')) is not dict or set(payload['scope']) != {'project_id', 'order_id', 'task_id', 'node_id'}
                or payload['scope'].get('project_id') != self.manager.project_id
                or any(type(v) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', v) is None for v in payload['scope'].values())
                or type(payload.get('window_id')) is not str
                or re.fullmatch(r'hwnd:[1-9][0-9]*:pid:[1-9][0-9]*:started:[1-9][0-9]*', payload['window_id']) is None
                or type(payload.get('route')) is not dict or set(payload['route']) != {'model', 'provider', 'endpoint_fingerprint'}
                or any(type(v) is not str or not 0 < len(v) <= 200 for v in payload['route'].values())
                or '/' not in payload['route']['model']
                or payload['route']['provider'] != payload['route']['model'].split('/', 1)[0].lower()
                or re.fullmatch(r'[a-f0-9]{64}', payload['route']['endpoint_fingerprint']) is None
                or payload['session_nonce'] != self.nonce or payload['switch_revision'] != self._switch_revision()
                or type(payload['expires_at']) not in (int, float) or not math.isfinite(payload['expires_at'])
                or not self.clock() < payload['expires_at'] <= self.clock() + 900
                or type(payload['width']) is not int or type(payload['height']) is not int
                or payload['width'] <= 0 or payload['height'] <= 0 or payload['width'] * payload['height'] > 4194304
                or type(payload['full_window_approved']) is not bool or type(payload['max_frames']) is not int
                or not 1 <= payload['max_frames'] <= 32):
            raise FrameRefused('desktop_frame_policy_invalid_or_stale')
        from ampuly.effects.journal import _payload_hash
        if _payload_hash(payload) != effect.payload_hash:
            raise FrameRefused('desktop_frame_policy_hash_invalid')
        from ampuly.computer_use.contracts import Geometry
        geometry = Geometry(payload['window_id'], 'policy-mask-validation', 1.0, payload['width'], payload['height'], payload['expires_at'])
        masks = clean_masks(payload['masks'], geometry)
        if not masks and payload['full_window_approved'] is not True:
            raise FrameRefused('desktop_frame_unmasked_not_approved')
        return deepcopy(payload)

    async def simulate(self, effect):
        payload = self._payload(effect)
        return SimulationStatus.PREVIEW_ONLY, dict(scope=payload['scope'], window_id=payload['window_id'],
            model=payload['route']['model'], provider=payload['route']['provider'],
            endpoint_fingerprint=payload['route']['endpoint_fingerprint'],
            masked_regions=payload['masks'], full_window_disclosure=payload['full_window_approved'],
            max_frames=payload['max_frames'], expires_at=payload['expires_at'],
            checked=['strict scope and switch revision', 'finite image budget and lifetime'],
            not_checked=['live screen content', 'model output quality'], images_sent=0)

    async def apply(self, effect, *, idempotency_key):
        try:
            payload = self._payload(effect)
            current = self.gateway.journal.get(effect.effect_id)
            if (type(current) is not EffectRecord or current.state is not EffectState.CLAIMED
                    or not current.authorization_id or not current.authorized_by or not current.claim_token
                    or current.claim_token != effect.claim_token or current.payload_hash != effect.payload_hash
                    or current.payload != payload or effect.state is not EffectState.CLAIMED):
                raise FrameRefused('desktop_frame_policy_not_owner_claimed')
            key = self._key(payload['scope'], payload['window_id'], payload['route'], payload['switch_revision'])
            if key not in self._applied and len(self._applied) >= 32:
                raise FrameRefused('desktop_frame_policy_retention_limit')
            self._applied[key] = current.effect_id
            return ProviderResult(ProviderOutcome.APPLIED, receipt={'policy_hash': current.payload_hash,
                'session_nonce': self.nonce, 'authorization_id': current.authorization_id, 'images_sent': 0})
        except FrameRefused as exc:
            return ProviderResult(ProviderOutcome.KNOWN_NO_EFFECT, detail=exc.reason)

    async def verify(self, effect):
        try:
            payload = self._payload(effect)
            key = self._key(payload['scope'], payload['window_id'], payload['route'], payload['switch_revision'])
            active = self._applied.get(key) == effect.effect_id and effect.state is EffectState.APPLIED
            return (VerificationStatus.COMPLETE if active else VerificationStatus.INCOMPLETE), {
                'policy_installed': active, 'images_sent_by_policy_provider': 0,
                'not_proven': 'image delivery or desktop task success'}
        except FrameRefused:
            return VerificationStatus.INCOMPLETE, {'policy_installed': False, 'images_sent_by_policy_provider': 0}

    def __call__(self, scope, window, route, nonce):
        if nonce != self.nonce: raise FrameRefused('desktop_frame_policy_session_changed')
        switch = self._switch_revision()
        key = self._key(scope, window, route, switch)
        effect_id = self._applied.get(key)
        if not effect_id: raise FrameRefused('desktop_frame_owner_approval_required')
        current = self.gateway.journal.get(effect_id)
        payload = self._payload(current)
        if (current.state is not EffectState.APPLIED or not current.authorization_id or not current.authorized_by
                or payload['scope'] != scope or payload['window_id'] != window or payload['route'] != route
                or current.provider_receipt.get('policy_hash') != current.payload_hash
                or current.provider_receipt.get('session_nonce') != self.nonce
                or current.provider_receipt.get('authorization_id') != current.authorization_id):
            raise FrameRefused('desktop_frame_owner_approval_invalid')
        return FrameGrant(current.effect_id, current.payload_hash, payload['scope'], window, route,
            tuple(tuple(row) for row in payload['masks']), payload['full_window_approved'], nonce,
            payload['expires_at'], payload['max_frames'])

    async def _execution_binding(self, scope):
        # Order/direct correlation identifies the business scope, not an
        # execution Run. Read execution identity from the original stored Task.
        # The gateway still performs its fresh scope/budget/lease checks before
        # dispatch; these values never grant or reserve authority themselves.
        from ampuly.application.effect_execution_scope import capture_server_execution_scope
        engine = self.manager.engine
        stored = await engine.store.get_task(scope['task_id'])
        if stored is None or scope_for(stored) != scope:
            raise FrameRefused('desktop_frame_execution_scope_changed')
        execution = await capture_server_execution_scope(engine, task_id=scope['task_id'])
        binding = dict(run_id=execution['delegation_run_id'], customer_id='', environment_id='')
        reader = getattr(engine, 'effect_budget_reader', None)
        if callable(reader) and execution['budget_id']:
            budget = reader(execution['budget_id'])
            if inspect.isawaitable(budget):
                budget = await budget
            if not isinstance(budget, dict) or budget.get('project_id') != scope['project_id']:
                raise FrameRefused('desktop_frame_budget_scope_invalid')
            for field in ('customer_id', 'environment_id'):
                value = budget.get(field) or ''
                if type(value) is not str:
                    raise FrameRefused('desktop_frame_budget_scope_invalid')
                binding[field] = value
        return binding

    async def propose(self, task, geometry, route):
        scope, switch = scope_for(task), self._switch_revision()
        if scope['project_id'] != self.manager.project_id:
            raise FrameRefused('desktop_frame_scope_invalid')
        key = self._key(scope, geometry.window_id, route, switch)
        if key in self._proposals:
            # Repeated model/tool calls never create more approval cards. An
            # expired/rejected/unknown decision does not silently become new.
            current = self.gateway.journal.get(self._proposals[key]['effect_id'])
            if current.state not in {EffectState.PROPOSED, EffectState.AWAITING_APPROVAL}:
                raise FrameRefused('desktop_frame_policy_already_decided_or_expired')
            self._payload(current)
            return deepcopy(self._proposals[key])
        if len(self._proposals) >= 32: raise FrameRefused('desktop_frame_policy_retention_limit')
        execution_binding = await self._execution_binding(scope)
        payload = dict(scope=scope, window_id=geometry.window_id, width=geometry.width, height=geometry.height,
            route=dict(route), masks=[], full_window_approved=True, session_nonce=self.nonce,
            expires_at=self.clock() + 600.0, max_frames=16, switch_revision=switch)
        publication = await self.manager.engine.propose_effect_for_approval(provider=self.name, kind=KIND, payload=payload,
            idempotency_key='desktop-frame-policy:' + self.nonce + ':' + key,
            prompt=('允许这张工单的当前步骤，将指定应用的完整窗口截图发送给所列模型吗？'
                '最多16张、10分钟内，仅此窗口/项目/工单/步骤与模型路由；不读取其它窗口或磁盘文件。'
                '截图可能含业务信息，模型调用可能产生费用。关闭或重新配置桌面通道立即使此许可失效。'
                '拒绝不会发送截图，也不会执行桌面输入。'),
            preview={'scope': scope, 'window_id': geometry.window_id, 'model': route['model'], 'provider': route['provider'],
                'endpoint_fingerprint': route['endpoint_fingerprint'], 'full_window_disclosure': True,
                'max_frames': 16, 'expires_at': payload['expires_at'], 'images_sent': 0},
            task_id=scope['task_id'], **execution_binding, approval_ttl_seconds=600.0)
        effect_id = getattr(getattr(publication, 'effect', None), 'effect_id', '')
        checkpoint_id = getattr(getattr(publication, 'checkpoint', None), 'checkpoint_id', '')
        if not effect_id or not checkpoint_id: raise FrameRefused('desktop_frame_approval_receipt_unavailable')
        result = dict(status='awaiting_approval', action='desktop_frame_disclosure', effect_id=effect_id,
            checkpoint_id=checkpoint_id, note='已交负责人确认截图共享范围；尚未发送图像，也不会重复申请或操作。')
        self._proposals[key] = result
        return deepcopy(result)


def ensure_policy_authority(manager):
    existing = getattr(manager.engine, 'desktop_frame_authority', None)
    if existing is not None:
        if type(existing) is not DesktopFramePolicyAuthority or existing.manager is not manager:
            raise FrameRefused('desktop_frame_authority_conflict')
        return existing
    authority = DesktopFramePolicyAuthority(manager)
    authority.gateway.register(authority)
    manager.engine.desktop_frame_authority = authority
    return authority
