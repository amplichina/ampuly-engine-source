"""One product-owned SDK attempt; explicit opt-in, never an engine singleton.

This assembles existing components, not another Agent loop. Real startup is
only reached by run(); importing/constructing the object has no side effects.
No automatic retries, personal credential discovery or production admission.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ampuly.layer3_agent.adapters.base import ExternalAgentAdapter
from ampuly.layer3_agent.harness import HarnessScope, _invoke_callback
from ampuly.layer3_agent.harness_codex_sdk import CodexSdkHarnessProvider
from ampuly.layer3_agent.managed_codex import MODEL_KEY, runtime_config, validate_product_home
from ampuly.llm.model_route_http import ManagedModelGateway


class ManagedAttemptRefused(RuntimeError):
    """Stable local failure, without credentials or upstream exception text."""


class ManagedSdkAttempt:
    def __init__(self, *, registry: Any, business: Any, task: Any, scope: HarnessScope,
                 bridge: Any, env: dict[str, str], interactions: Any = None, business_tools: Any = None):
        self.registry, self.business = registry, business
        self.task, self.scope = copy.deepcopy(task), scope
        self.bridge, self.env = bridge, dict(env)
        self.interactions = interactions
        self.business_tools = business_tools
        self.gateway: ManagedModelGateway | None = None
        self.provider: CodexSdkHarnessProvider | None = None
        self.session_id = ''
        self.cleanup_errors: list[str] = []
        self._used = False

    def _validate(self) -> None:
        self.scope.validate()
        meta = self.task.metadata
        if (self.task.project_id != self.scope.project_id
                or any(str(meta.get(k, '') or '') != getattr(self.scope, k)
                       for k in ('customer_id', 'environment_id', 'run_id'))
                or not self.task.id or not meta.get('budget_id')
                or not Path(self.scope.workspace_path).is_absolute()
                or not Path(str(meta.get('ampuly_home', ''))).is_absolute()
                or Path(meta['ampuly_home']).resolve() != self.business.path.parent.resolve()):
            raise ManagedAttemptRefused('attempt_scope_or_budget_root_mismatch')
        # Reuse the established launch-essential allowlist, but allow no real
        # model credential in the child. Only our ephemeral route key is added.
        allowed = ExternalAgentAdapter._INHERITED_LAUNCH_ENV | {'CODEX_HOME'}
        if (any(k.upper() not in allowed for k in self.env)
                or len({k.upper() for k in self.env}) != len(self.env)):
            raise ManagedAttemptRefused('attempt_environment_not_allowed')
        home = Path(self.env.get('CODEX_HOME', ''))
        validate_product_home(home)
        budget = self.business.run_budget(meta['budget_id'])
        from ampuly.layer4_tools.spend_guard import budget_order_id

        # one rule for "which order does this task's budget answer to", shared with the spend guard and the model gate
        if (not budget or budget.get('state') != 'open'
                or any(str(budget.get(k, '') or '') != str(meta.get(k, '') or '')
                              for k in ('customer_id', 'environment_id'))
                or str(budget.get('order_id', '') or '') != budget_order_id(meta)
                or budget.get('project_id') != self.scope.project_id
                or 'cost_cny' in dict(budget.get('limits', {}) or {})):
            raise ManagedAttemptRefused('attempt_budget_not_supported')

    async def _cleanup(self) -> None:
        if self.gateway is not None:
            self.gateway.route.close()  # revoke before waiting on the SDK
        if self.provider is not None:
            # This provider belongs exclusively to this object. Include a
            # partially created session that failed before returning its id.
            for sid in tuple(self.provider._threads):
                try:
                    await self.provider.detach_session_runtime(sid, 'owned attempt ended; no automatic retry')
                except Exception:
                    self.cleanup_errors.append('session_detach_failed')
            try:
                if not await asyncio.wait_for(self.provider.shutdown(), timeout=8):
                    self.cleanup_errors.append('runtime_still_owned')
            except Exception:
                self.cleanup_errors.append('runtime_shutdown_failed')
        if self.gateway is not None:
            try:
                await self.gateway.close()
            except Exception:
                self.cleanup_errors.append('gateway_close_failed')

    async def _finish(self) -> None:
        worker = asyncio.create_task(asyncio.wait_for(self._cleanup(), timeout=20))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            worker.result()
        except (Exception, asyncio.CancelledError):
            self.cleanup_errors.append('attempt_cleanup_incomplete')
        if cancelled:
            raise asyncio.CancelledError

    async def run(self, instruction: str, *, callbacks: Any = None) -> Any:
        if self._used:
            raise ManagedAttemptRefused('attempt_already_used')
        self._used = True
        await asyncio.to_thread(self._validate)
        failed = False
        try:
            self.gateway = ManagedModelGateway(self.registry)
            await self.gateway.start()
            token = self.gateway.route.prepare(ttl_seconds=300)
            # A console attempt (business tools bound by the entry) gets no
            # built-in shell; a workspace attempt keeps it. Decided here, from
            # the attempt's purpose, never from the conversation.
            tool_policy = 'business_tools_only' if self.business_tools is not None else 'workspace_shell'
            config = runtime_config(SimpleNamespace(model=self.bridge.model, model_wire_api='responses',
                model_api_base=self.gateway.base_url, model_api_key_env=MODEL_KEY),
                cwd=self.scope.workspace_path, env={**self.env, MODEL_KEY: token}, tool_policy=tool_policy)
            self.provider = CodexSdkHarnessProvider(self.registry, business=self.business,
                interactions=self.interactions, client_config=config, model_gateway=self.gateway,
                business_tools=self.business_tools)
            meta = self.task.metadata
            self.session_id = await self.provider.create_session(self.scope, context={
                'task_id': self.task.id, 'order_id': meta.get('order_id', ''),
                'budget_id': meta['budget_id'], 'model': self.bridge.model})

            async def on_event(event):
                if event.event_type == 'turn_started':
                    await self.gateway.route.bind(session_id=self.session_id, turn_id=event.turn_id,
                        task=self.task, bridge=self.bridge, prepared_token=token)
                await _invoke_callback(getattr(callbacks, 'on_event', None), event)

            wrapped = SimpleNamespace(on_event=on_event,
                on_progress=getattr(callbacks, 'on_progress', None))
            result = await self.provider.start_turn(self.session_id, instruction, callbacks=wrapped)
            items = await self.provider.collect_artifacts(self.session_id)
            result.artifacts = {**dict(result.artifacts or {}), 'harness_session_id': self.session_id,
                'provider_items': items, 'artifact_collection_complete': True,
                'artifact_validation': 'not_performed', 'tool_policy': tool_policy}
            return result
        except BaseException:
            failed = True
            raise
        finally:
            await self._finish()
            if self.cleanup_errors and not failed:
                raise ManagedAttemptRefused('attempt_cleanup_incomplete')
