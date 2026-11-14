"""Server composition for employee nodes on the EXISTING Company/native path.

No browser DTO activates this host. Original business claims select pins;
instance/package repositories select immutable contents. Controller lease,
current Stop and source-specific authorization are rechecked at dispatch.
"""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ampuly.core.models import TaskStatus
from ampuly.layer3_agent.employee_runtime import EmployeeLivePolicy, EmployeeNativeTurn, _stop
from .ai_employee_contract import canonical_json
from .ai_employee_run_contract import FrozenEmployeePins, ResolvedPinInput, freeze_employee_pins, restore_employee_pins
from .ai_employee_run_basis import EmployeeRunBasisSources, read_employee_run_basis
from .ai_employee_run_envelope import EmployeeRunIdentity, build_employee_run_envelope, restore_employee_run_envelope
from .employee_order_claim_restore import RestoredOriginalEmployeeOrderClaimPins
from .employee_launch_source import EmployeeOrderLaunchSource
from .employee_order_launch_anchor import read_original_employee_order_pins
from .employee_instances import EmployeeInstanceRepository, EmployeeBindingRepository
from .employee_package_catalog import PublishedEmployeeCatalog


def bound_nodes(nodes: list[dict]) -> dict[str, dict]:
    result = {}
    for node in nodes:
        if type(node) is not dict or "employee_binding" not in node:
            continue
        key = node.get("id")
        if type(key) is not str or not key or key in result or type(node["employee_binding"]) is not dict:
            _stop("employee_scope_invalid")
        result[key] = node
    return result


def live_granted_tools(pin: dict, facts: dict) -> frozenset[str]:
    """Intersect frozen composition capabilities with live host grants (case 08).

    Pins without composition fields keep the pre-C behaviour: grant keys only.
    """
    caps = pin.get("composition_capabilities") if type(pin) is dict else None
    if not isinstance(caps, dict):
        grants = facts.get("tool_grants", {}) if type(facts) is dict else {}
        return frozenset(grants if isinstance(grants, dict) else ())
    from .employee_composition import host_capabilities_from
    live = host_capabilities_from(facts.get("tool_inventory"), facts.get("tool_grants"))
    out: set[str] = set()
    for tool, actions in caps.items():
        if type(tool) is not str or type(actions) is not list:
            continue
        allowed = live.get(tool, frozenset())
        if any(type(a) is str and a in allowed for a in actions):
            out.add(tool)
    return frozenset(out)


def authorize_pin_action(pin: dict, tool: str, action: str, facts: dict, *,
                         veto_failed: bool = False) -> dict:
    """Live host-grant re-check for one tool:action against a wired pin (case 08)."""
    from .employee_composition import authorize_action, host_capabilities_from
    from .ai_employee_contract import AIEmployeeContractError
    if type(pin) is not dict or type(tool) is not str or type(action) is not str:
        raise AIEmployeeContractError("request_invalid", "授权复核输入无效。")
    caps = pin.get("composition_capabilities")
    if not isinstance(caps, dict):
        grants = facts.get("tool_grants", {}) if type(facts) is dict else {}
        if not isinstance(grants, dict) or tool not in grants:
            raise AIEmployeeContractError("capability_revoked", "宿主授权已收回，动作在执行前被拦下。", f"{tool}:{action}")
        return {"allowed": True, "action": f"{tool}:{action}", "composition_id": ""}
    record = {
        "effective": {"capabilities": caps},
        "disabled_actions": list(pin.get("composition_disabled_actions") or []),
        "composition_id": pin.get("composition_id") or "",
        "effect_kinds": {},
    }
    live = host_capabilities_from(facts.get("tool_inventory"), facts.get("tool_grants"))
    return authorize_action(record, tool, action, current_host_capabilities=live,
                            veto_failed=veto_failed)


def assemble_launch_composition(*, project_id: str, workflow_package: Any, node_id: str,
                                instance: dict, manifest: dict, facts: dict) -> dict | None:
    """Assemble C composition for a published workflow launch; None when not applicable.

    Non-published workflows keep the pre-C pin shape (no composition fields) so
    testing/trial freeze paths stay compatible. Formal published launches refuse
    assembly failures closed (no silent skip past a veto/overlay error).
    """
    status = str(getattr(workflow_package, "status", "") or "").strip().lower()
    if status != "published":
        return None
    from .employee_composition import assemble_composition, host_capabilities_from
    from .workflow_packages import package_candidate_fingerprint
    overlays = []
    overlay = instance.get("context_overlay")
    if overlay is not None:
        overlays.append(overlay)
    emp_pin = {
        "package_id": instance["package_id"],
        "version": instance["package_version"],
        "package_fingerprint": instance["package_fingerprint"],
        "status": "published",
        "freeze_source": "employee_packages_published",
        "draft_digest": None,
    }
    wf_pin = {
        "workflow_id": workflow_package.id,
        "version": str(workflow_package.manifest.get("version") or ""),
        "release_fingerprint": package_candidate_fingerprint(workflow_package),
        "status": "published",
    }
    host_caps = host_capabilities_from(facts.get("tool_inventory"), facts.get("tool_grants"))
    approved_k = list(instance.get("knowledge_refs") or [])
    return assemble_composition(
        project_id=project_id, workflow_id=workflow_package.id, node_id=node_id,
        purpose="formal", employee_pin=emp_pin, workflow_pin=wf_pin,
        manifest=manifest, instance=instance, overlays=overlays,
        host_capabilities=host_caps, approved_knowledge=approved_k,
        approved_destinations=[], output_formats=[],
    )


class EmployeeRuntimeHost:
    """One host per project engine, recreated (not serialized) on recovery.

    observe(instance, package, task=None) is a private source adapter returning
    AE00 readiness facts from current project/person/tool/model sources. It is
    required: absence never turns missing references or human duty into True.
    resolve_knowledge is optional only for packages without knowledge refs.
    """
    def __init__(self, *, engine: Any, business: Any, catalog: PublishedEmployeeCatalog,
                 instances: EmployeeInstanceRepository, bindings: EmployeeBindingRepository,
                 observe: Callable, platform_limits: dict,
                 resolve_knowledge: Callable | None = None,
                 context_source: Callable | None = None) -> None:
        if (type(catalog) is not PublishedEmployeeCatalog
                or type(instances) is not EmployeeInstanceRepository
                or type(bindings) is not EmployeeBindingRepository or not callable(observe)):
            _stop("employee_policy_unavailable")
        self.engine, self.business, self.catalog = engine, business, catalog
        self.instances, self.bindings = instances, bindings
        self.observe, self.platform_limits = observe, copy.deepcopy(platform_limits)
        self.resolve_knowledge = resolve_knowledge
        self.context_source = context_source

    def context_for_manifest(self, manifest: dict, *, model_config: dict | None = None) -> dict:
        if self.context_source is None:
            _stop("employee_policy_unavailable")
        return self.context_source(manifest, model_config=model_config)

    def for_owner_request(self, *, owner_login: str, recheck_owner_async: Callable):
        if type(owner_login) is not str or not owner_login or not callable(recheck_owner_async):
            _stop("employee_policy_unavailable")
        return _OwnerEmployeeRuntime(self, owner_login, recheck_owner_async)

    def freeze_for_launch(self, project_id: str, package: Any, *, order_id: str,
                          order_limits: dict | None = None) -> FrozenEmployeePins | None:
        nodes = bound_nodes(package.nodes)
        if not nodes:
            return None
        rows = []
        for node_id, node in nodes.items():
            resolution, _, _ = self.bindings._resolve(project_id, node["employee_binding"],
                workflow_id=package.id, node_id=node_id, binding_slot="executor")
            instance = resolution["instance"]
            published = self.catalog.read(instance["package_id"], version=instance["package_version"],
                fingerprint=instance["package_fingerprint"], purpose="new_adoption")
            if published.get("new_adoption_allowed") is not True:
                _stop("employee_policy_unavailable")
            manifest = published["package"]
            facts = self.observe(instance, manifest, {"order_id": order_id, "project_id": project_id})
            from .ai_employee_instance_contract import derive_readiness
            readiness = derive_readiness(instance, {**facts, "package_manifest": manifest,
                "platform_limits": self.platform_limits, "order_limits": order_limits or {}})
            if readiness.get("ready") is not True:
                _stop("employee_policy_unavailable")
            composition = assemble_launch_composition(
                project_id=project_id, workflow_package=package, node_id=node_id,
                instance=instance, manifest=manifest, facts=facts)
            rows.append(ResolvedPinInput(node_id=node_id, resolution=resolution,
                binding=node["employee_binding"], package_manifest=manifest,
                requires_review=node.get("type") == "human_review" or node.get("gate", "none") != "none",
                composition=composition))
        return freeze_employee_pins(rows, project_id=project_id, workflow_id=package.id,
            expected_bound_node_ids=set(nodes), platform_limits=self.platform_limits,
            order_limits=order_limits or {})

    def validate_launch(self, project_id: str, package: Any, source: Any) -> FrozenEmployeePins:
        if type(source) is EmployeeOrderLaunchSource:
            source = source.restored
        if type(source) is not RestoredOriginalEmployeeOrderClaimPins:
            _stop("employee_scope_invalid")
        pins = source.snapshot
        if (pins.project_id != project_id or pins.workflow_id != package.id
                or source.source.workflow_version != package.manifest.get("version")
                or set(pins.to_dict()) != set(bound_nodes(package.nodes))):
            _stop("employee_basis_conflict")
        # Recover original revisions, never call the current binding resolver.
        for node_id, pin in pins.to_dict().items():
            instance = self.instances.read_revision(project_id, pin["instance_id"],
                revision=pin["instance_revision"], revision_fingerprint=pin["revision_fingerprint"],
                package_fingerprint=pin["package_fingerprint"])
            package_view = self.catalog.read(pin["package_id"], version=pin["package_version"],
                fingerprint=pin["package_fingerprint"], purpose="pinned")
            current = self.instances._load(project_id, pin["instance_id"])[0]
            if current["execution_state"] != "active":
                _stop("employee_policy_unavailable")
            self._required_facts(instance, package_view["package"],
                                 {"order_id": source.source.order_id, "project_id": project_id})
        return pins

    def assert_nodes_supported(self, nodes) -> None:
        from .ai_employee_instance_contract import validate_binding
        for node in nodes:
            if type(node) is not dict or "employee_binding" not in node:
                continue
            if validate_binding(node["employee_binding"]) or node.get("type", "ai_do") != "ai_do":
                _stop("employee_scope_invalid")

    def validate_for_publication(self, package):
        """Current component proof, without claiming a not-yet-existing budget."""
        self.assert_nodes_supported(package.nodes)
        project_id = package.manifest.get("project_id") or self.engine.project_id
        if project_id != self.engine.project_id:
            _stop("employee_scope_invalid")
        for node_id, node in bound_nodes(package.nodes).items():
            resolution, _, _ = self.bindings._resolve(project_id, node["employee_binding"],
                workflow_id=package.id, node_id=node_id, binding_slot="executor")
            instance = resolution["instance"]
            published = self.catalog.read(instance["package_id"], version=instance["package_version"],
                fingerprint=instance["package_fingerprint"], purpose="new_adoption")
            if published.get("new_adoption_allowed") is not True:
                _stop("employee_policy_unavailable")
            facts = self.observe(instance, published["package"], None)
            from .ai_employee_instance_contract import derive_readiness
            result = derive_readiness(instance, {**facts, "package_manifest": published["package"],
                "platform_limits": self.platform_limits})
            # Budget is an order-time question. Publication never sets it True
            # and never opens an allowance, but all other source gaps still veto.
            if set(result["gaps"]) - {"budget_exhausted", "budget_unverified"}:
                _stop("employee_policy_unavailable")
        return None

    async def run_turn(self, executor, task, node, callback):
        turn = await self.prepare_turn(executor, task, node, consume_rework=True)
        from ampuly.layer3_agent.runtime_v2.runtime import native_steps_used
        before = native_steps_used(task)
        previous = task.metadata.get("employee_run_progress", {}).get("steps_used", 0)
        async def native_call():
            read_tools = getattr(self, "read_tools", None)
            if not turn.allowed_tools:
                return await callback()
            if read_tools is None:
                _stop("employee_tool_scope_unavailable")
            with read_tools.scope(task=task, manifest=turn.basis.package_manifest(),
                                  checkpoint=lambda: turn.checkpoint("recheck")):
                return await callback()
        result = await turn.run(native_call, steps_used=lambda: previous + max(0, native_steps_used(task) - before))
        return result

    def _stop_observed(self, launch: dict) -> bool:
        from .launch_stop_source import LaunchStopSourceHeaders
        from .launch_stop_intent import read_launch_stop_intent_on_connection
        from .employee_order_launch_anchor import _ReadBudget
        raw = launch.get("source_headers")
        if type(raw) is not dict or set(raw) != {field.name for field in fields(LaunchStopSourceHeaders)}:
            _stop("employee_scope_invalid")
        with self.business._connect() as conn:
            conn.execute("PRAGMA query_only = ON").close()
            conn.execute("BEGIN").close()
            return read_launch_stop_intent_on_connection(conn,
                source=LaunchStopSourceHeaders(**raw), _budget=_ReadBudget()) is not None

    def _required_facts(self, instance: dict, package: dict, task: Any) -> dict:
        facts = self.observe(copy.deepcopy(instance), copy.deepcopy(package), task)
        if type(facts) is not dict:
            _stop("employee_policy_unavailable")
        from .ai_employee_instance_contract import derive_readiness
        # Intake pause affects new claims only. Existing original pins may run
        # on a retired version, provided current permissions are still valid.
        check_instance = {**instance, "intake_state": "open", "execution_state": "active"}
        check_package = {**package, "status": "published"}
        result = derive_readiness(check_instance, {**facts, "package_manifest": check_package,
            "platform_limits": self.platform_limits})
        if not result["ready"]:
            _stop("employee_policy_unavailable")
        return facts

    def _original_pins(self, *, project_id: str, order_id: str, command_id: str,
                       workflow_id: str, order_task_id: str) -> FrozenEmployeePins:
        order = self.business.get_order(order_id, project_id=project_id)
        if order is None or order.workflow_id != workflow_id:
            _stop("employee_scope_invalid")
        if order.status == "launching":
            command = self.business.pending_launch_command(order_id, project_id=project_id)
            if command is None or command.outbox_id != command_id:
                _stop("employee_scope_invalid")
            restored = self.business.read_employee_order_claim_snapshot(project_id=project_id,
                order_id=order_id, outbox_id=command_id, workflow_id=workflow_id,
                expected_payload=command.payload, include_launch_source=False)
            if type(restored) is not RestoredOriginalEmployeeOrderClaimPins:
                _stop("employee_basis_conflict")
            # WS uses this pre-existing deterministic identity for every claim.
            expected_task = str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"superones://{project_id}/business-commands/{command_id}/task"))
            if order_task_id != expected_task:
                _stop("employee_scope_invalid")
            return restored.snapshot
        with self.business._connect() as conn:
            conn.execute("PRAGMA query_only = ON").close()
            conn.execute("BEGIN").close()
            source = read_original_employee_order_pins(conn, project_id=project_id,
                order_id=order_id, outbox_id=command_id)
        if source.anchor.order_task_id != order_task_id or source.anchor.workflow_id != workflow_id:
            _stop("employee_scope_invalid")
        return restore_employee_pins(source.pins_bytes, project_id=project_id,
            workflow_id=workflow_id, expected_fingerprint=source.pins_fingerprint)

    def _resources(self, package_id: str, version: str, fingerprint: str) -> tuple[dict, dict]:
        with self.catalog._request() as request:
            if request is None:
                _stop("employee_resources_invalid")
            view, resources = request.reader._snapshot(package_id, version=version,
                fingerprint=fingerprint, purpose="pinned")
            trees = {}
            from .employee_skill_tree_reader import EmployeeSkillTreeReader
            for skill in view["package"].get("skills", []):
                relative = f"employee_packages/{package_id}/.versions/{fingerprint}/skills/{skill['skill_name']}"
                reader = EmployeeSkillTreeReader(self.catalog.root)
                with reader.files.workspace() as workspace:
                    rows, _, _ = reader._scan(workspace, relative)
                entries = []
                for path, digest in rows:
                    raw = request.read_bytes(relative + "/" + path, 2 * 1024 * 1024)
                    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                        _stop("employee_resources_invalid")
                    entries.append((path, raw))
                if reader(relative) != skill["tree_fingerprint"]:
                    _stop("employee_resources_invalid")
                trees[skill["skill_name"]] = entries
            request.verify()
            return resources, trees

    async def prepare_turn(self, executor: Any, task: Any, node: dict, *, consume_rework=False) -> EmployeeNativeTurn:
        """Resolve through actual WorkItem -> root Task -> original order claim."""
        await executor._require_current_controller_lease()
        item = await executor.store.get_work_item_for_runtime_task(task.id)
        if item is None or item.kind != "execute":
            _stop("employee_scope_invalid")
        run = await executor.store.get_delegation_run(item.run_id)
        if run is None or run.project_id != task.project_id:
            _stop("employee_scope_invalid")
        items = await executor.store.list_delegation_work_items(run_id=item.run_id)
        roots = [row for row in items if row.parent_work_item_id is None and row.kind == "intake"]
        origins = {row.metadata.get("origin_task_id") for row in roots}
        if len(origins) != 1 or not all(type(value) is str and value for value in origins):
            _stop("employee_scope_invalid")
        order_task = await executor.store.get_task(origins.pop())
        if order_task is None or order_task.project_id != task.project_id or order_task.session_id != run.session_id:
            _stop("employee_scope_invalid")
        launch = order_task.metadata.get("employee_launch")
        contract = order_task.metadata.get("workflow_contract")
        if type(launch) is not dict or type(contract) is not dict:
            _stop("employee_scope_invalid")
        pins = self._original_pins(project_id=task.project_id, order_id=launch["order_id"],
            command_id=launch["command_id"], workflow_id=contract["workflow_id"], order_task_id=order_task.id)
        if pins.fingerprint != launch.get("pins_fingerprint"):
            _stop("employee_basis_conflict")
        node_id = node["id"]
        pin = pins.for_node(node_id)
        old = task.metadata.get("employee_run")
        attempt = old["attempt"] if type(old) is dict else 1
        identity = EmployeeRunIdentity(project_id=task.project_id, order_id=launch["order_id"],
            order_task_id=order_task.id, step_task_id=task.id, workflow_id=contract["workflow_id"],
            workflow_version=contract["workflow_version"], node_id=node_id, binding_slot="executor",
            attempt=attempt, run_id=old["run_id"] if type(old) is dict else "er-" + uuid.uuid4().hex[:16])
        # Original Task + Run facts are required, not inferred from mutable
        # metadata or a matching current id. This opens a bounded read snapshot
        # of the actual Store selected by the current executor (never by UI).
        from .employee_project_creation_reader import read_selected_store_creation_sources
        sources = read_selected_store_creation_sources(executor.store,
            identity=identity, delegation_run_id=item.run_id)
        # The Stop key cannot be an independently editable metadata identity.
        # It is reconstituted from the original claim and matched below.
        source_headers = {name: getattr(sources.task_fact.origin, name) for name in (
            "project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
            "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256")}
        if launch.get("source_headers") != source_headers:
            _stop("employee_scope_invalid")
        if self._stop_observed(launch):
            _stop("employee_run_stopped")
        if old is None:
            envelope = build_employee_run_envelope(identity, pins=pins, parent_order_id=launch.get("parent_order_id", ""),
                input_refs=copy.deepcopy(launch["input_refs"]), artifact_contract_ref="employee-output-v1",
                budget_id=task.metadata.get("budget_id") or order_task.metadata.get("budget_id"),
                pinned_at=datetime.now(timezone.utc).isoformat(), baseline_ref={"order_task_id": order_task.id,
                    "org_snapshot_sha256": hashlib.sha256(canonical_json(run.metadata["org_snapshot"])).hexdigest(),
                    "manifest_sha256": contract["manifest_sha256"],
                    "pin_source": "launch"}, rework_of="")
        else:
            envelope = _restore_attempt_chain(task.metadata, identity, pins)
        if consume_rework and task.metadata.get("employee_rework_request") is not None:
            envelope = await self._consume_rework(executor, task, envelope, pins, item)
            identity = envelope.identity
        basis = read_employee_run_basis(envelope, expected_identity=identity, pins=pins,
            expected_envelope_fingerprint=envelope.fingerprint, expected_pins_fingerprint=pins.fingerprint,
            sources=EmployeeRunBasisSources(self.instances.read_revision, self.catalog.read),
            previous=_restore_history(task.metadata, identity, pins))
        resources, trees = self._resources(pin["package_id"], pin["package_version"], pin["package_fingerprint"])
        historical = basis.instance_snapshot()
        knowledge = {}
        if historical.get("knowledge_refs"):
            if self.resolve_knowledge is None:
                _stop("employee_policy_unavailable")
            knowledge = self.resolve_knowledge(identity, historical["knowledge_refs"],
                                               instance_id=historical['instance_id'])

        async def observe() -> EmployeeLivePolicy:
            await executor._require_current_controller_lease()
            persisted = await executor.store.get_task(task.id)
            root = await executor.store.get_task(order_task.id)
            order = self.business.get_order(identity.order_id, project_id=identity.project_id)
            current = self.instances._load(identity.project_id, pin["instance_id"])[0]
            facts = self._required_facts(historical, basis.package_manifest(),
                {"order_id": identity.order_id, "project_id": identity.project_id})
            if persisted is None or root is None or order is None:
                _stop("employee_scope_invalid")
            stopped = (bool(asyncio.current_task().cancelling()) or self._stop_observed(launch)
                or persisted.status == TaskStatus.CANCELLED or root.status == TaskStatus.CANCELLED
                or order.status in {"cancelled", "rejected", "closed"}
                or any(obj.metadata.get("stop_requested") or obj.metadata.get("stop_intent") for obj in (persisted, root)))
            if historical.get("knowledge_refs"):
                current_knowledge = self.resolve_knowledge(identity, historical["knowledge_refs"],
                                                           instance_id=historical['instance_id'])
                if current_knowledge != knowledge:
                    _stop("employee_basis_conflict")
            return EmployeeLivePolicy(project_id=identity.project_id, order_id=identity.order_id,
                step_task_id=identity.step_task_id, instance_id=pin["instance_id"], execution_state=current["execution_state"],
                stopped=bool(stopped), controller_current=True, humans_on_duty=True,
                references_accessible=not historical.get("knowledge_refs") or bool(knowledge),
                model_available=facts.get("model_available") is True,
                model_verified=facts.get("model_verified") is True,
                granted_tools=live_granted_tools(pin, facts))

        role = executor.org_engine.get_role_for_work_item(task.assigned_to, task.tags)
        host_input = None
        from ampuly.components.customer_service.employee_executor import executor_profile
        if executor_profile(basis.package_manifest())["kind"] == "customer_service":
            from .employee_console_source import read_console_draft_input
            if node.get("gate") != "human_required" or not pin.get("reviewer_slot"):
                _stop("customer_service_console_human_gate_required")
            note = task.metadata.get("employee_rework_feedback", {})
            if note:
                history = task.metadata.get("employee_run_history", [])
                if not history:
                    _stop("employee_basis_conflict")
                previous = history[-1]
                receipt = await executor.store.get_human_review_rework_receipt(
                    record_id=previous.get("record_id"), request_id=previous.get("request_id"))
                if (type(receipt) is not dict or receipt.get("confirmation_id") != previous.get("receipt_id")
                        or receipt.get("marker") != note or receipt.get("project_id") != identity.project_id
                        or receipt.get("order_id") != identity.order_id
                        or note.get("target_task_id") != identity.step_task_id
                        or note.get("previous_employee_run_id") != previous["envelope"].get("run_id")):
                    _stop("employee_basis_conflict")
            host_input = read_console_draft_input(self.business, identity=identity, launch=launch,
                pins_fingerprint=pins.fingerprint, revision_note=note.get("revision_note", ""))
        async def finalize(turn, result):
            from .workflow_employee_delivery import publish_workflow_employee_delivery
            await publish_workflow_employee_delivery(executor.store, turn, result)

        turn = EmployeeNativeTurn(task=task, basis=basis, resources=resources, skill_trees=trees,
            resolved_knowledge=knowledge, role=role, read_policy=observe, save_task=executor.save_task,
            host_input=host_input, finalize=finalize)
        note = task.metadata.get("employee_rework_feedback")
        if note is not None:
            turn.context_messages.append({"role": "user", "content":
                "人工审核退回意见（当前固定材料的修订要求，不是权限扩张）：\n" + canonical_json(note).decode("utf-8")})
        return turn

    async def _consume_rework(self, executor, task, previous, pins, item):
        marker = task.metadata.get("employee_rework_request")
        if type(marker) is not dict or marker.get("source") != "human_review_rework_confirm":
            _stop("employee_scope_invalid")
        receipt = await executor.store.get_human_review_rework_receipt(
            record_id=marker.get("rework_record_id"), request_id=marker.get("request_id"))
        identity = previous.identity
        if (type(receipt) is not dict or receipt.get("operation") != "human_review_rework"
                or receipt.get("marker") != marker or receipt.get("project_id") != identity.project_id
                or receipt.get("delegation_run_id") != item.run_id
                or receipt.get("order_task_id") != identity.order_task_id
                or receipt.get("order_id") != identity.order_id
                or marker.get("target_work_item_id") != item.work_item_id
                or marker.get("target_task_id") != identity.step_task_id
                or marker.get("target_node_id") != identity.node_id
                or marker.get("previous_employee_run_id") != identity.run_id
                or marker.get("previous_attempt") != identity.attempt
                or type(marker.get("revision_note")) is not str
                or hashlib.sha256(marker["revision_note"].encode("utf-8")).hexdigest() != marker.get("revision_note_hash")):
            _stop("employee_scope_invalid")
        pin = pins.for_node(identity.node_id)
        current = self.instances._load(identity.project_id, pin["instance_id"])[0]
        maximum = min(pin["effective_limits"]["max_rework"],
                      current.get("limits", {}).get("max_rework", pin["effective_limits"]["max_rework"]))
        if identity.attempt > maximum or current["execution_state"] != "active":
            _stop("employee_rework_limit")
        progress = task.metadata.get("employee_run_progress")
        if type(progress) is not dict or progress.get("state") not in {"completed", "waiting_review", "failed"}:
            _stop("employee_outcome_pending")
        old = previous.to_dict()
        next_identity = replace(identity, attempt=identity.attempt + 1, run_id="er-" + uuid.uuid4().hex[:16])
        envelope = build_employee_run_envelope(next_identity, pins,
            parent_order_id=old["parent_order_id"], input_refs=old["input_refs"],
            artifact_contract_ref=old["artifact_contract_ref"], budget_id=old["budget_id"],
            pinned_at=datetime.now(timezone.utc).isoformat(), baseline_ref=old["baseline_ref"],
            rework_of=identity.run_id, previous=previous)
        history = copy.deepcopy(task.metadata.get("employee_run_history", []))
        history.append({"envelope": old, "fingerprint": previous.fingerprint,
            "progress": copy.deepcopy(progress), "receipt_id": receipt["confirmation_id"],
            "request_id": receipt["request_id"], "record_id": receipt["record_id"]})
        # Same controller-fenced Task update retains the full chain. A crash
        # after this save sees one pending new attempt, not another increment.
        await executor._require_current_controller_lease()
        task.metadata["employee_run_history"] = history
        task.metadata["employee_run"] = envelope.to_dict()
        task.metadata["employee_run_fingerprint"] = envelope.fingerprint
        task.metadata["employee_run_progress"] = {"state": "waiting_decision", "steps_used": 0,
            "model_calls_used": 0, "wall_clock_used_s": 0, "rework_count": next_identity.attempt - 1,
            "action_intents": [], "delivery_versions": [], "controls": [], "outcome_unknown": []}
        task.metadata["employee_rework_feedback"] = copy.deepcopy(marker)
        task.metadata.pop("employee_rework_request")
        # This only resets native step accounting for the new explicit attempt.
        # Original order budget, reservations and money accounting are retained.
        task.metadata["native_steps_used"] = 0
        await executor.save_task(task)
        return envelope


def _restore_history(metadata, identity, pins):
    rows = metadata.get("employee_run_history", [])
    if type(rows) is not list or len(rows) != identity.attempt - 1 or len(rows) > 50:
        _stop("employee_basis_conflict")
    previous = None
    for index, row in enumerate(rows, 1):
        if type(row) is not dict or type(row.get("envelope")) is not dict:
            _stop("employee_basis_conflict")
        previous_identity = replace(identity, attempt=index, run_id=row["envelope"].get("run_id"))
        previous = restore_employee_run_envelope(canonical_json(row["envelope"]), identity=previous_identity,
            pins=pins, expected_fingerprint=row.get("fingerprint"), previous=previous)
    return previous


def _restore_attempt_chain(metadata, identity, pins):
    return restore_employee_run_envelope(canonical_json(metadata["employee_run"]), identity=identity,
        pins=pins, expected_fingerprint=metadata.get("employee_run_fingerprint"),
        previous=_restore_history(metadata, identity, pins))


class _OwnerEmployeeRuntime:
    """Request-local owner capability, never retained on the engine or Task."""
    def __init__(self, host, login, recheck):
        self._host, self._login, self._recheck = host, login, recheck

    async def _owner(self):
        current = await self._recheck()
        if type(current) is not str or current != self._login:
            _stop("employee_policy_unavailable")
        return current

    async def observe_rework_receipt(self, scope, record):
        """Current owner may inspect a retained command without a new lease.

        The caller independently validates the immutable receipt and exact
        payload. This grants no execute permission and calls no model/tool.
        """
        await self._owner()
        host = self._host
        if (scope.project_id != host.engine.project_id
                or record.get("project_id") != scope.project_id
                or record.get("order_id") != scope.order_id
                or record.get("order_task_id") != scope.order_task_id):
            _stop("employee_scope_invalid")
        order = host.business.get_order(scope.order_id, project_id=scope.project_id)
        if order is None or order.project_id != scope.project_id or order.launched_task_id != scope.order_task_id:
            _stop("employee_scope_invalid")
        return await self._owner()

    async def confirm_rework(self, scope, record):
        await self._owner()
        host = self._host
        engine = host.engine
        if scope.project_id != engine.project_id:
            _stop("employee_scope_invalid")
        executor = engine.company_executor
        await executor._require_current_controller_lease()
        target = await executor.store.get_task(scope.target_task_id)
        item = await executor.store.get_work_item_for_runtime_task(scope.target_task_id)
        if (target is None or item is None or item.work_item_id != scope.target_work_item_id
                or item.run_id != scope.delegation_run_id):
            _stop("employee_scope_invalid")
        node = target.metadata.get("workflow_node")
        if type(node) is not dict or node.get("id") != scope.target_node_id:
            _stop("employee_scope_invalid")
        turn = await host.prepare_turn(executor, target, node)
        identity = turn.basis.identity
        historical = [row for row in target.metadata.get("employee_run_history", [])
            if row["envelope"].get("run_id") == record.get("previous_employee_run_id")
            and row["envelope"].get("attempt") == record.get("expected_attempt")]
        historical_match = False
        if historical:
            if len(historical) != 1:
                _stop("employee_scope_invalid")
            old = historical[0]
            receipt = await executor.store.get_human_review_rework_receipt(
                record_id=old.get("record_id"), request_id=old.get("request_id"))
            if (type(receipt) is not dict or receipt.get("confirmation_id") != old.get("receipt_id")
                    or receipt.get("original_record") != record
                    or receipt.get("scope") != {name: getattr(scope, name) for name in scope.__slots__}):
                _stop("employee_scope_invalid")
            historical_match = True
        if (identity.order_id != scope.order_id or identity.order_task_id != scope.order_task_id
                or not historical_match and (identity.run_id != record.get("previous_employee_run_id")
                    or identity.attempt != record.get("expected_attempt"))):
            _stop("employee_scope_invalid")
        await turn._policy()
        current = host.instances._load(scope.project_id, turn._pin["instance_id"])[0]
        current_limit = current.get("limits", {}).get("max_rework", turn.limits["max_rework"])
        if type(current_limit) is not int or current_limit < 0:
            _stop("employee_policy_unavailable")
        maximum = min(current_limit, turn.limits["max_rework"])
        if not historical_match and identity.attempt > maximum:
            _stop("employee_policy_unavailable")
        actor = await self._owner()
        return {"actor_login": actor, "max_rework": maximum}


def build_employee_runtime_host(engine) -> EmployeeRuntimeHost:
    """Compose the host from existing per-project engine authorities.

    No credential text is exposed or copied into lifecycle evidence. Tool
    existence is NOT implementation provenance: until a tool has a trusted
    implementation observer, nonempty capability sets are explicitly refused.
    The current supported positive slice is native text/JSON, with console
    human slots, no external connectors and no unversioned private knowledge.
    """
    from .business_store import BusinessStore
    from .employee_lifecycle import build_employee_repositories
    from .employee_lifecycle_store import EmployeeLifecycleStore

    business = BusinessStore(engine.ampuly_home)
    state = EmployeeLifecycleStore(engine.ampuly_home)
    from ampuly.layer4_tools.employee_builtin_read import BuiltinReadTools
    read_tools = BuiltinReadTools(registry=engine.tool_registry, ampuly_home=engine.ampuly_home,
                                 project_id=engine.project_id)
    from .employee_project_knowledge import EmployeeProjectKnowledge
    knowledge_source = EmployeeProjectKnowledge(engine=engine,
        read_current_instance=lambda project, instance_id: holder['host'].instances._load(project, instance_id)[0],
        project_active=lambda project: business.project_lifecycle(project).status == 'active')

    def context_source(manifest, *, model_config=None):
        if type(manifest) is not dict:
            _stop("employee_scope_invalid")
        observed_tools = read_tools.observe(manifest)
        default = engine.config.llm.default_model
        requested = copy.deepcopy(model_config) if model_config else {
            "provider": default.partition("/")[0], "model": default, "tier": "standard"}
        if (type(requested) is not dict or set(requested) != {"provider", "model", "tier"}
                or not all(type(value) is str and value for value in requested.values())
                or "/" not in requested["model"]
                or requested["provider"] != requested["model"].partition("/")[0]):
            _stop("employee_policy_unavailable")
        if engine.llm is None or engine.llm.model_listed(requested["model"]) is not True:
            _stop("employee_policy_unavailable")
        provider = engine.llm.for_employee_model(requested["model"])
        if not provider.has_credentials():
            _stop("employee_policy_unavailable")
        config = provider.config
        # Explicit non-secret runtime projection; do not dump LLMConfig which
        # also contains API credentials and provider connection information.
        profile = {key: getattr(config, key) for key in (
            "default_model", "temperature", "max_tokens", "reasoning_effort",
            "context_window", "context_window_overrides", "request_timeout",
            "stream_idle_timeout", "residency", "work_window_cap")}
        profile["native_runtime"] = engine.config.system.native_runtime.model_dump(mode="json")
        profile["runner_version"] = "employee-native-v1"
        profile["employee_output_token_cap"] = 3500
        profile['knowledge_reader_fingerprint'] = knowledge_source.profile()
        from ampuly.components.customer_service.employee_executor import executor_profile
        profile["employee_executor"] = executor_profile(manifest)
        if type(config.max_tokens) is not int or not 1 <= config.max_tokens <= 3500:
            _stop("employee_policy_unavailable")
        return {"model_config": requested, "capability_impls": observed_tools["capability_impls"],
            "runtime_profile_fingerprint": hashlib.sha256(canonical_json(profile)).hexdigest()}

    holder = {}

    def observe(instance, manifest, task=None):
        context = context_source(manifest, model_config=instance.get("model_config"))
        observed_tools = read_tools.observe(manifest)
        project = instance["project_id"]
        if project != engine.project_id:
            _stop("employee_scope_invalid")
        current = holder["host"].instances._load(project, instance["instance_id"])[0]
        verified = state.verified_context(manifest["package_id"], manifest["package_fingerprint"],
            model_config=context["model_config"], capability_impls=context["capability_impls"],
            params=instance.get("params", {}), runtime_profile_fingerprint=context["runtime_profile_fingerprint"],
            knowledge_refs=instance.get("knowledge_refs", []), instance=instance)
        saved = engine._load_project_company_staffing_defaults(None)
        selected = set()
        for selection in saved.get("staffing_selections", {}).values():
            parsed = engine._normalize_staffing_selection(selection)
            if parsed.get("kind") == "employee":
                selected.add(parsed.get("id"))
        humans = {}
        for member in engine.config.org.employees:
            metadata = member.metadata
            belongs = (member.employee_id in selected or metadata.get("project_id") == project
                       or project in metadata.get("project_ids", []))
            if belongs and member.kind == "human" and member.status == "active":
                humans[member.employee_id] = {"project_id": project, "kind": "human", "status": "active"}
        scope = task if type(task) is dict else getattr(task, "metadata", {}) if task is not None else {}
        order_id = scope.get("order_id", "")
        budget = business.open_budget_for_order(order_id, project_id=project) if order_id else None
        budget_available = bool(budget is not None and budget["state"] == "open" and budget["health"] == "ok")
        if budget_available:
            budget_available = all(value > 0 for value in budget.get("available", {}).values())
        # Current instance must still exist in the same project/package. An
        # upgrade does not rewrite old pins, but disable/revocation stops them.
        adoption = current["package_id"] == instance["package_id"] and current["execution_state"] == "active"
        channels = (not instance.get("connector_refs") and all(
            row.get("channel") == "console" for row in instance.get("human_assignments", {}).values()))
        return {"package_manifest": manifest, "model_available": True,
            "model_verified": bool(verified and verified.get("verified") is True),
            "tool_inventory": observed_tools["tool_inventory"], "tool_grants": observed_tools["tool_grants"],
            "authorization_valid": observed_tools["authorization_valid"],
            "knowledge_accessible": knowledge_source.accessible(instance),
            "human_members": humans, "connector_states": {}, "credential_refs": [],
            "budget_available": budget_available, "adoption_active": adoption,
            "project_active": business.project_lifecycle(project).status == "active",
            "channel_enabled": bool(channels), "verified_scope": verified or {}}

    catalog, instances, bindings = build_employee_repositories(engine.ampuly_home,
        context_provider=lambda instance, package: observe(instance, package))
    # Limits remain the minimum of platform, package, instance and order. The
    # monetary cap is enforced separately by the actual provider preflight.
    limits = {"max_steps": 100, "max_model_calls": 100, "max_wall_clock_s": 1800,
              "max_rework": 5, "max_retries": 0, "max_sub_agents": 0}
    host = EmployeeRuntimeHost(engine=engine, business=business, catalog=catalog,
        instances=instances, bindings=bindings, observe=observe, platform_limits=limits,
        context_source=context_source)
    host.resolve_knowledge = knowledge_source.resolve
    host.knowledge_source = knowledge_source
    holder["host"] = host
    host.read_tools = read_tools
    return host
