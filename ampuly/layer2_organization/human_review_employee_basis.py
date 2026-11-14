"""Restore the real retained envelope against original order pins, read only.

Project/step are associated EmployeeRunIdentity fields; the AE00 envelope body
does not contain them. Never infer those fields from an optional nested object.
The supplied executor is a server-owned source capability, not RPC data.
"""
from __future__ import annotations

from ampuly.plugins.office_ui.ai_employee_contract import canonical_json
from ampuly.plugins.office_ui.ai_employee_run_contract import FrozenEmployeePins
from ampuly.plugins.office_ui.ai_employee_run_envelope import EmployeeRunIdentity
from .human_review_rework import _fail


async def verified_retained_employee_run(executor, target, *, project_id,
        order_task_id, target_node_id, order_id=None):
    try:
        metadata = target.metadata
        envelope = metadata.get("employee_run") if type(metadata) is dict else None
        if type(envelope) is not dict or target.project_id != project_id:
            _fail("rework_source_unavailable")
        root = await executor.store.get_task(order_task_id)
        if root is None or root.project_id != project_id or type(root.metadata) is not dict:
            _fail("rework_source_unavailable")
        launch, contract = root.metadata.get("employee_launch"), root.metadata.get("workflow_contract")
        host = getattr(executor, "employee_runtime_host", None)
        if (type(launch) is not dict or type(contract) is not dict or host is None
                or not callable(getattr(host, "_original_pins", None))
                or type(launch.get("order_id")) is not str
                or order_id is not None and launch["order_id"] != order_id):
            _fail("rework_source_unavailable")
        pins = host._original_pins(project_id=project_id, order_id=launch["order_id"],
            command_id=launch["command_id"], workflow_id=contract["workflow_id"], order_task_id=order_task_id)
        if type(pins) is not FrozenEmployeePins or pins.fingerprint != launch.get("pins_fingerprint"):
            _fail("rework_source_unavailable")
        identity = EmployeeRunIdentity(project_id=project_id, order_id=launch["order_id"],
            order_task_id=order_task_id, step_task_id=target.id, workflow_id=contract["workflow_id"],
            workflow_version=contract["workflow_version"], node_id=target_node_id, binding_slot="executor",
            attempt=envelope.get("attempt"), run_id=envelope.get("run_id"))
        # The original host helper validates every predecessor, exact old pins,
        # canonical envelope bytes and independently retained scoped digest.
        from ampuly.plugins.office_ui.employee_runtime_host import _restore_attempt_chain
        restored = _restore_attempt_chain(metadata, identity, pins)
        if canonical_json(restored.to_dict()) != canonical_json(envelope):
            _fail("rework_source_unavailable")
        return restored
    except Exception:
        _fail("rework_source_unavailable")
