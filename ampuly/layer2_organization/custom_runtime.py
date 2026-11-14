"""Custom/org runtime entrypoint.

Custom mode runs with company-mode semantics, but with a user-selected
organization config.  The runner owns the custom isolation boundary:

- company mode's engine/config objects are not mutated;
- the custom runtime uses its own AmpulyEngine instance and company executor;
- the runtime is rebound to the caller's store so UI session/task/transcript
  state remains in the same project context as the chat that started it.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, TYPE_CHECKING

from ampuly.core.config import AmpulyConfig
from ampuly.core.models import UserMessage
from ampuly.layer0_interaction.coordinator import InteractionDecisionLease
from ampuly.layer0_interaction.message_bus import _validate_creation_source_task_id
from ampuly.core.org_config import (
    apply_org_config_payload_to_config,
    load_org_config_payload,
    validate_runnable_org_config,
)

if TYPE_CHECKING:
    from ampuly.engine import AmpulyEngine


class CustomRuntimeRunner:
    """Run custom/org turns through an isolated company-runtime engine."""

    def __init__(self, parent: AmpulyEngine) -> None:
        self.parent = parent
        self._runtime_ingress_tasks: dict[int, Any] = {}

    def _build_org_config(self, organization_id: str | None) -> tuple[AmpulyConfig, str | None]:
        config_dir = self.parent.ampuly_home / "config"
        payload, source_path = load_org_config_payload(config_dir, organization_id)
        try:
            base_config = AmpulyConfig.load(config_dir) if config_dir.exists() else copy.deepcopy(self.parent.config)
        except Exception:
            base_config = copy.deepcopy(self.parent.config)
        loaded_config = apply_org_config_payload_to_config(
            base_config,
            payload,
            source_path=source_path,
        )
        resolved_org_id = str(getattr(loaded_config.org, "organization_id", "") or organization_id or "").strip() or None
        validate_runnable_org_config(loaded_config, organization_id=resolved_org_id or "")
        return loaded_config, resolved_org_id

    async def _inputs_for_task(self, task_id, *, project_id, org_id):
        """Resolve only original stored ancestry; flags/refs in messages are ignored."""
        from ampuly.core.runtime_inputs import RuntimeInputs, RuntimeInputsError
        if not task_id:
            return None
        getter = getattr(getattr(self.parent, 'store', None), 'get_task', None)
        if not callable(getter):
            return None
        seen = set()
        for _ in range(16):
            if type(task_id) is not str or task_id in seen:
                raise RuntimeInputsError('runtime_inputs_ancestry_invalid')
            seen.add(task_id)
            task = await getter(task_id)
            if task is None or task.project_id != project_id:
                raise RuntimeInputsError('runtime_inputs_scope_mismatch')
            metadata = task.metadata or {}
            contract = metadata.get('workflow_contract')
            if isinstance(contract, dict) and 'runtime_projection' in contract:
                loader = getattr(self.parent, '_runtime_input_loader', None)
                if not callable(loader):
                    raise RuntimeInputsError()
                inputs = loader(task, project_id=project_id, organization_id=org_id)
                if (type(inputs) is not RuntimeInputs or inputs.project_id != project_id
                        or inputs.organization_id != org_id):
                    raise RuntimeInputsError('runtime_inputs_scope_mismatch')
                return inputs
            if isinstance(contract, dict) and contract.get('workflow_id') and callable(getattr(self.parent, '_runtime_input_loader', None)):
                # Do not backfill a legacy workflow's missing generation from
                # today's shared config. An explicit new invocation must pin it.
                raise RuntimeInputsError('runtime_inputs_legacy_generation_missing')
            parent_id = metadata.get('origin_task_id')
            if not parent_id:
                return None
            task_id = parent_id
        raise RuntimeInputsError('runtime_inputs_ancestry_invalid')

    async def _create_runtime(
        self,
        *,
        project_id: str | None,
        org_id: str | None,
        runtime_inputs: Any = None,
    ) -> tuple[AmpulyEngine, str]:
        """Build the one isolated Engine used by every custom-org ingress."""

        from ampuly.engine import AmpulyEngine
        from ampuly.core.service_models import ServiceError

        normalized_org_id = str(org_id or "").strip()
        if not normalized_org_id:
            raise ServiceError(
                "org_id_required",
                "org_id_required",
                {
                    "project_id": project_id or self.parent.project_id or "default",
                    "reason": "custom_company_run_requires_durable_org_id",
                },
            )
        if runtime_inputs is None:
            org_config, resolved_org_id = self._build_org_config(normalized_org_id)
        else:
            from ampuly.core.runtime_inputs import RuntimeInputs, RuntimeInputsError
            if (type(runtime_inputs) is not RuntimeInputs or runtime_inputs.organization_id != normalized_org_id
                    or runtime_inputs.project_id != (project_id or self.parent.project_id or 'default')):
                raise RuntimeInputsError('runtime_inputs_scope_mismatch')
            org_config = runtime_inputs.config_for(self.parent.config)
            resolved_org_id = runtime_inputs.organization_id
        if not resolved_org_id:
            raise ServiceError(
                "org_id_required",
                "org_id_required",
                {
                    "project_id": project_id or self.parent.project_id or "default",
                    "reason": "custom_company_run_requires_durable_org_id",
                },
            )
        normalized_project_id = str(
            project_id or self.parent.project_id or "default"
        ).strip() or "default"
        shared_store = getattr(self.parent, "store", None)
        runtime = AmpulyEngine(
            config=org_config,
            ampuly_home=self.parent.ampuly_home,
            project_id=normalized_project_id,
            store=shared_store,
            owns_store=shared_store is None,
            run_startup_reconcile=shared_store is None,
            active_task_run_registry=getattr(
                self.parent,
                "_active_task_run_registry",
                None,
            ),
            owns_active_task_run_registry=False,
            on_progress=self.parent.on_progress,
            on_runtime_event=self.parent.on_runtime_event,
            on_escalation=None,
            interaction_coordinator=getattr(
                self.parent,
                "interaction_coordinator",
                None,
            ),
            pre_delivery_validator=getattr(
                self.parent,
                "pre_delivery_validator",
                None,
            ),
            **({"runtime_inputs": runtime_inputs} if runtime_inputs is not None else {}),
            # runtime contract: the order's runtime asks the
            # same order port as the engine that owns it.
            order_liveness=getattr(self.parent, "order_liveness", None),
        )
        # Registration is synchronous and precedes initialization's first
        # await.  The Store-owning parent can therefore always find and cancel
        # this ingress if shutdown crosses custom runtime construction.
        runtime._runtime_input_loader = getattr(self.parent, '_runtime_input_loader', None)
        ingress_task = self.parent._register_runtime_child(runtime)
        self._runtime_ingress_tasks[id(runtime)] = ingress_task
        runtime.on_company_runtime_children = self.parent.on_company_runtime_children
        try:
            await runtime.initialize()
            # The order's runtime builds a provider of its own: it takes the console's resolvers from the engine that
            # owns it, or a step on another provider's model finds no key variable and every such step fails.
            inherit = getattr(getattr(runtime, "llm", None), "inherit_bindings", None)
            if callable(inherit):
                inherit(getattr(self.parent, "llm", None))
        except BaseException:
            shutdown_completed = False
            try:
                await runtime.shutdown()
                shutdown_completed = True
            finally:
                if shutdown_completed:
                    self.parent._unregister_runtime_child(runtime, ingress_task)
                    self._runtime_ingress_tasks.pop(id(runtime), None)
            raise
        company_executor = getattr(runtime, "company_executor", None)
        if company_executor is not None:
            callback_factory = getattr(
                self.parent,
                "on_company_kanban_callback_factory",
                None,
            )
            if callable(callback_factory):
                company_executor.on_kanban_changed = callback_factory(runtime)
            else:
                parent_executor = getattr(self.parent, "company_executor", None)
                company_executor.on_kanban_changed = getattr(
                    parent_executor,
                    "on_kanban_changed",
                    None,
                )
        return runtime, resolved_org_id

    async def _shutdown_runtime(self, runtime: AmpulyEngine) -> None:
        from ampuly.layer2_organization.phase_hooks import unregister_dispatcher_wake

        company_executor = getattr(runtime, "company_executor", None)
        wake = getattr(company_executor, "_signal_dispatcher_wake", None)
        if wake is not None:
            unregister_dispatcher_wake(wake)
        await runtime.shutdown()
        ingress_task = self._runtime_ingress_tasks.pop(id(runtime), None)
        self.parent._unregister_runtime_child(runtime, ingress_task)

    async def dispatch_interaction_decision(
        self,
        lease: InteractionDecisionLease,
        *,
        org_id: str,
    ) -> Any:
        """Consume a claimed owner decision under its durable org config."""

        # Admission checkpoints are intentionally rootless: their durable UI
        # anchor owns the original workflow projection. Do not fall back to
        # today's shared org config merely because no worker Task exists yet.
        from ampuly.core.interaction_protocol import owner_interaction_actor_identity
        anchor_task_id, _ = owner_interaction_actor_identity(lease.checkpoint)
        input_task_id = lease.checkpoint.task_id or anchor_task_id
        inputs = await self._inputs_for_task(input_task_id,
            project_id=lease.checkpoint.project_id, org_id=org_id)
        if inputs is None and anchor_task_id and anchor_task_id != input_task_id:
            # A parked worker may have no origin_task_id after restart. Its
            # server-owned UI anchor still names the original pinned order.
            # Scope/legacy errors above are deliberately never caught here.
            inputs = await self._inputs_for_task(anchor_task_id,
                project_id=lease.checkpoint.project_id, org_id=org_id)
        runtime, _ = await self._create_runtime(
            project_id=lease.checkpoint.project_id,
            org_id=org_id,
            **({'runtime_inputs': inputs} if inputs is not None else {}),
        )
        try:
            return await runtime._dispatch_interaction_decision(lease)
        finally:
            await self._shutdown_runtime(runtime)

    async def process_message(
        self,
        content: str,
        *,
        project_id: str | None,
        session_id: str | None,
        org_id: str | None,
        preferred_agent: str | None,
        domains: list[str] | None,
        origin_task_id: str | None,
        attachment_refs: list[dict[str, Any]] | None,
        message_metadata: dict[str, Any] | None,
        creation_source_task_id: str | None = None,
        _first_owned_source_handoff_sink: object = None,
    ) -> str:
        creation_source_kwargs = {}
        if _first_owned_source_handoff_sink is not None:
            from ..layer0_interaction.message_bus import _validate_first_owned_source_handoff_sink
            _validate_first_owned_source_handoff_sink(_first_owned_source_handoff_sink)
            if creation_source_task_id is None:
                raise RuntimeError("creation source Task is required for handoff") from None
            creation_source_kwargs["_first_owned_source_handoff_sink"] = _first_owned_source_handoff_sink
        if creation_source_task_id is not None:
            _validate_creation_source_task_id(creation_source_task_id)
            creation_source_kwargs["creation_source_task_id"] = creation_source_task_id
        inputs = await self._inputs_for_task(creation_source_task_id or origin_task_id,
            project_id=project_id or self.parent.project_id or 'default', org_id=org_id)
        runtime, resolved_org_id = await self._create_runtime(
            project_id=project_id,
            org_id=org_id,
            **({'runtime_inputs': inputs} if inputs is not None else {}),
        )
        normalized_project_id = str(
            project_id or self.parent.project_id or "default"
        ).strip() or "default"
        org_config = runtime.config

        source_primary = None
        try:
            normalized_attachment_refs = runtime._normalize_attachment_refs(attachment_refs)
            metadata = {
                "mode": "company",
                "exec_mode": "org",
                "org_id": resolved_org_id,
                "organization_id": resolved_org_id,
                "organization_name": str(getattr(org_config.org, "organization_name", "") or "").strip(),
                "organization_config_file": str(getattr(org_config.org, "organization_config_file", "") or "").strip(),
                "preferred_agent": preferred_agent,
                "domains": domains or [],
                "company_profile": "custom",
                "origin_task_id": origin_task_id,
                "attachment_refs": normalized_attachment_refs,
            }
            if message_metadata:
                metadata.update(dict(message_metadata))
                metadata["mode"] = "company"
                metadata["exec_mode"] = "org"
                metadata["company_profile"] = "custom"
                metadata["org_id"] = resolved_org_id
                metadata["organization_id"] = resolved_org_id

            message = UserMessage(
                channel="cli",
                user_id="owner",
                content=content,
                attachments=normalized_attachment_refs,
                session_id=session_id or str(uuid.uuid4()),
                project_context=normalized_project_id,
                metadata=metadata,
            )
            response = await runtime.message_bus.process_single(message, **creation_source_kwargs)
            if creation_source_task_id is not None and response is None:
                raise RuntimeError("creation source Task response unavailable")
            return response.content if response else "No response generated."
        except BaseException as error:
            if creation_source_task_id is not None:
                source_primary = error
            raise
        finally:
            try:
                await self._shutdown_runtime(runtime)
            except BaseException:
                if creation_source_task_id is None or source_primary is None:
                    raise
