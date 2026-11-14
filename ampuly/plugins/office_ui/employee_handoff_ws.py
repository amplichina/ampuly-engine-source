"""Console surface for employee handoffs: list and decide (4.8 H09).

The routing table lives in ws_handler; this module holds the handlers so the
canonical handler source stays bounded. Owner checks and project scoping use the
console's existing helpers, never a request-supplied identity.
"""
from __future__ import annotations

from datetime import timedelta
import re
from typing import Any

# Same shape the employee-instance store uses. The owner may name a downstream
# instance; anything else is refused before a task is planned.
_INSTANCE_ID = re.compile(r"^ei-[a-f0-9]{16}$")


async def _owner_login(handler: Any, ws: Any) -> str:
    from .ws_handler import _ws_owner_actor

    actor = _ws_owner_actor(handler, ws)
    return str((actor or {}).get("login", "") or "")


def _acceptance_view(record: Any) -> tuple[str, bool, dict[str, Any]]:
    """DONE and a passing check stay pending until acceptance is stored."""
    from ampuly.core.delivery_acceptance import counts_as_delivered

    payload = dict(getattr(record, "payload", {}) or {})
    meta = dict(getattr(record, "metadata", {}) or {})
    check = payload.get("origin_check") if type(payload.get("origin_check")) is dict else {}
    legacy = not check and str(getattr(record, "status", "") or "") in {"accepted", "consumed"}
    stored = str(meta.get("acceptance_state") or "")
    if stored in {"pending", "accepted"}:
        state = stored
    elif legacy:
        state = "accepted"
    else:
        state = "pending"
    delivered = counts_as_delivered(
        state, check_passed=check.get("check_passed") is True, legacy=legacy,
    )
    public = {
        "kind": "origin_check",
        "role": str(check.get("role") or ""),
        "check_passed": check.get("check_passed") is True,
        "acceptance_state": "pending",
        "delivered": False,
        "tool": str(check.get("tool") or ""),
        "tool_passed": check.get("tool_passed") if type(check.get("tool_passed")) is bool else None,
        "delivery_status": "not_sent",
        "human_approval_granted": False,
        "code": str(check.get("code") or ""),
        "acceptance_criteria_count": int(check.get("acceptance_criteria_count") or 0)
        if type(check.get("acceptance_criteria_count")) is int else 0,
    } if check else {}
    return state, delivered, public


def _handoff_payload(record: Any) -> dict[str, Any]:
    payload = dict(getattr(record, "payload", {}) or {})
    artifacts = payload.get("artifacts")
    acceptance_state, delivered, origin_check = _acceptance_view(record)
    return {
        "handoff_id": record.handoff_id,
        "task_id": record.task_id,
        "from_role": record.from_role,
        "to_role": record.to_role,
        "status": record.status,
        "requires_ack": record.requires_ack,
        "summary": record.summary,
        "artifacts": list(artifacts) if isinstance(artifacts, list) else [],
        "source_handoff_id": payload.get("source_handoff_id", ""),
        "rework_of_handoff_id": payload.get("rework_of_handoff_id", ""),
        "attempt": payload.get("attempt", 1),
        # The exact employee/package version the artefacts were produced
        # under; the owner's card names it rather than inferring "latest".
        "instance_id": payload.get("instance_id", ""),
        "instance_revision": payload.get("instance_revision", ""),
        "package_id": payload.get("package_id", ""),
        "package_version": payload.get("package_version", ""),
        "package_fingerprint": payload.get("package_fingerprint", ""),
        "budget_id": payload.get("budget_id", ""),
        "limits": dict(payload.get("limits") or {}) if isinstance(payload.get("limits"), dict) else {},
        "budget": dict(payload.get("budget") or {}) if isinstance(payload.get("budget"), dict) else {},
        "decision": str(getattr(record, "response_summary", "") or ""),
        "decided_by": str((getattr(record, "metadata", {}) or {}).get("decided_by", "")),
        "entry": str(payload.get("entry") or ""),
        "node_id": str(payload.get("node_id") or ""),
        "workflow_id": str(payload.get("workflow_id") or ""),
        "order_id": str(payload.get("order_id") or ""),
        "acceptance_state": acceptance_state,
        "delivered": delivered,
        "origin_check": origin_check,
        "approvals": list((getattr(record, "metadata", {}) or {}).get("handoff_approvals", []) or []),
        "created_at": record.created_at.isoformat() if record.created_at else "",
        # Owner ruling : a sent handoff is due 72h after it was
        # written; the card says overdue from the server clock, not a guess.
        "due_at": ((record.created_at + timedelta(hours=72)).isoformat()
                   if record.created_at else ""),
    }


async def handle_employee_handoff_list(handler: Any, ws: Any, data: dict) -> None:
    """List this project's handoffs, newest first; read-only."""
    engine, project_id = await handler._engine_for_request(data)
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_list",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    status = str(data.get("status", "") or "").strip() or None
    task_id = str(data.get("task_id", "") or "").strip()
    if len(task_id) > 128 or any(ord(ch) < 32 for ch in task_id):
        await handler._send_ack(ws, ok=False, action="employee_handoff_list",
                                project_id=project_id, error="handoff_task_id_invalid")
        return
    try:
        offset = int(data.get("offset", 0) or 0)
    except (TypeError, ValueError):
        offset = -1
    if not 0 <= offset <= 100_000:
        await handler._send_ack(ws, ok=False, action="employee_handoff_list",
                                project_id=project_id, error="handoff_offset_invalid")
        return
    limit = 50
    try:
        records = await store.get_handoff_records(project_id, status=status, task_id=task_id or None,
                                                  limit=limit, offset=offset)
        counts = await store.handoff_status_counts(project_id, task_id=task_id or None, status=status)
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_list",
                                project_id=project_id, error="handoff_read_failed")
        return
    total = int(counts.get("total", 0))
    from .employee_handoff import handoff_materials
    payloads = []
    remaining = 256 * 1024
    for record in records:
        payload = _handoff_payload(record)
        try:
            contents = await handoff_materials(store, record)
            refs = list(payload.get("artifacts") or [])
            materials = []
            for index, content in enumerate(contents):
                if remaining <= 0 or index >= 10:
                    break
                text = str(content)
                if len(text.encode("utf-8")) > 8192:
                    text = text.encode("utf-8")[:8192].decode("utf-8", "replace")
                remaining -= len(text.encode("utf-8"))
                ref = refs[index] if index < len(refs) and isinstance(refs[index], dict) else {}
                materials.append({"name": str(ref.get("name", "") or ""),
                                  "sha256": str(ref.get("sha256", "") or ""),
                                  "content": text})
            payload["artifact_materials"] = materials
            payload["artifacts_verified"] = len(materials) == len(refs)
        except Exception:
            payload["artifact_materials"] = []
            payload["artifacts_verified"] = False
            payload["artifacts_error"] = "handoff_artifact_unverified"
        payloads.append(payload)
    await handler._send_ack(ws, ok=True, action="employee_handoff_list", project_id=project_id,
                            handoffs=payloads,
                            counts=counts, offset=offset, limit=limit, task_id=task_id,
                            has_more=offset + len(records) < total)


async def handle_employee_handoff_decide(handler: Any, ws: Any, data: dict) -> None:
    """Accept or reject one handoff; rework is a separate explicit task start."""
    engine, project_id = await handler._engine_for_request(data)
    actor = await _owner_login(handler, ws)
    if not actor:
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error="owner_required")
        return
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    handoff_id = str(data.get("handoff_id", "") or "").strip()
    decision = str(data.get("decision", "") or "").strip()
    if not handoff_id:
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error="handoff_id_missing")
        return
    # The id is caller-supplied: a handoff of another project must read as
    # absent, not be decided by this project's owner. Checked before artifacts.
    try:
        candidate = await store.get_handoff_record(handoff_id)
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error="handoff_read_failed")
        return
    if candidate is None or str(getattr(candidate, "project_id", "") or "") != str(project_id or ""):
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error="handoff_not_found")
        return
    from .employee_handoff import HandoffRefused, accept_handoff, reject_handoff

    required = []
    principal = actor
    if decision == "accept":
        accounts = getattr(handler, "_account_store", None)
        if accounts is not None:
            getter = getattr(accounts, "required_owner_signers", None) or getattr(accounts, "active_owner_logins", None)
            if callable(getter):
                required = getter()
            if callable(getattr(accounts, "signing_identity", None)):
                principal = accounts.signing_identity(actor)

    try:
        if decision == "accept":
            record = await accept_handoff(store, handoff_id, actor=actor,
                                          note=str(data.get("note", "") or ""),
                                          signature_actor=principal, required_signers=tuple(required))
        elif decision == "reject":
            record = await reject_handoff(store, handoff_id, actor=actor,
                                          reason=str(data.get("reason", "") or ""))
        else:
            await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                    project_id=project_id, error="handoff_decision_invalid")
            return
    except (HandoffRefused, ValueError) as exc:
        await handler._send_ack(ws, ok=False, action="employee_handoff_decide",
                                project_id=project_id, error=getattr(exc, "code", str(exc)))
        return
    await handler._send_ack(ws, ok=True, action="employee_handoff_decide",
                            project_id=project_id, handoff=_handoff_payload(record),
                            signatures_required=required,
                            signatures_signed=list((record.metadata or {}).get("handoff_approvals", [])),
                            status=record.status)

async def handle_employee_handoff_rework(handler: Any, ws: Any, data: dict) -> None:
    """Start one explicit rework attempt from a rejected handoff.

    The original instance and frozen handoff are named by the server record;
    the owner supplies only the instruction. The direct-task service re-reads
    the rejected record and enforces the attempt limit.
    """
    engine, project_id = await handler._engine_for_request(data)
    actor = await _owner_login(handler, ws)
    if not actor:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="owner_required")
        return
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    handoff_id = str(data.get("handoff_id", "") or "").strip()
    instruction = str(data.get("instruction", "") or "").strip()
    if not handoff_id or len(handoff_id) > 512:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_id_missing")
        return
    if not 2 <= len(instruction) <= 8000:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_rework_instruction_invalid")
        return
    try:
        record = await store.get_handoff_record(handoff_id)
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_read_failed")
        return
    if (record is None or str(getattr(record, "project_id", "") or "") != str(project_id or "")
            or str(record.status) != "rejected"):
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_not_rejected")
        return
    payload = dict(getattr(record, "payload", {}) or {})
    instance_id = str(payload.get("instance_id", "") or "").strip()
    if not instance_id:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_instance_missing")
        return
    from .employee_direct_task import EmployeeDirectRefused, run_employee_direct_task
    try:
        result = await run_employee_direct_task(
            engine=engine, project_id=project_id, instance_id=instance_id,
            instruction=instruction, actor=actor,
            rework_of_handoff_id=handoff_id,
        )
    except EmployeeDirectRefused as exc:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error=exc.code)
        return
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_rework",
                                project_id=project_id, error="handoff_rework_failed")
        return
    await handler._send_ack(ws, ok=True, action="employee_handoff_rework",
                            project_id=project_id, handoff_id=handoff_id,
                            task_id=str(result.get("task_id", "") or ""),
                            status=str(result.get("status", "") or ""),
                            new_handoff_id=str(result.get("handoff_id", "") or ""),
                            attempt=int(result.get("attempt", 1) or 1))

def _continue_instance_id(data: dict, payload: dict) -> tuple[str, str]:
    """Owner-selected downstream instance, or the handoff's own when omitted.

    A blank value keeps the previous path. A present but malformed value is
    refused so the card cannot silently fall back to the frozen instance.
    """
    raw = data.get("instance_id", None)
    if raw is None or str(raw).strip() == "":
        return str(payload.get("instance_id", "") or "").strip(), ""
    chosen = str(raw).strip()
    if _INSTANCE_ID.fullmatch(chosen) is None:
        return "", "handoff_instance_invalid"
    return chosen, ""


async def handle_employee_handoff_continue(handler: Any, ws: Any, data: dict) -> None:
    """Start one downstream employee task from an accepted handoff.

    The frozen handoff names the producing instance. The owner may switch the
    downstream instance before continue; an omitted id keeps that producing
    instance. admit_downstream runs inside run_employee_direct_task via
    source_handoff_id — no second admit path.
    """
    engine, project_id = await handler._engine_for_request(data)
    actor = await _owner_login(handler, ws)
    if not actor:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="owner_required")
        return
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    handoff_id = str(data.get("handoff_id", "") or "").strip()
    instruction = str(data.get("instruction", "") or "").strip()
    if not handoff_id or len(handoff_id) > 512:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_id_missing")
        return
    if not 2 <= len(instruction) <= 8000:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_continue_instruction_invalid")
        return
    try:
        record = await store.get_handoff_record(handoff_id)
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_read_failed")
        return
    if (record is None or str(getattr(record, "project_id", "") or "") != str(project_id or "")
            or str(record.status) != "accepted"):
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_not_accepted")
        return
    payload = dict(getattr(record, "payload", {}) or {})
    instance_id, instance_error = _continue_instance_id(data, payload)
    if instance_error:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error=instance_error)
        return
    if not instance_id:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_instance_missing")
        return
    from .employee_direct_task import EmployeeDirectRefused, run_employee_direct_task
    try:
        result = await run_employee_direct_task(
            engine=engine, project_id=project_id, instance_id=instance_id,
            instruction=instruction, actor=actor,
            source_handoff_id=handoff_id,
        )
    except EmployeeDirectRefused as exc:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error=exc.code)
        return
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_continue",
                                project_id=project_id, error="handoff_continue_failed")
        return
    await handler._send_ack(ws, ok=True, action="employee_handoff_continue",
                            project_id=project_id, handoff_id=handoff_id,
                            task_id=str(result.get("task_id", "") or ""),
                            status=str(result.get("status", "") or ""),
                            new_handoff_id=str(result.get("handoff_id", "") or ""),
                            attempt=int(result.get("attempt", 1) or 1))


def _bounded_id(value: Any, *, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        return ""
    return text


async def handle_employee_handoff_check(handler: Any, ws: Any, data: dict) -> None:
    """Return the origin check. This does not accept the handoff."""
    engine, project_id = await handler._engine_for_request(data)
    actor = await _owner_login(handler, ws)
    if not actor:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="owner_required")
        return
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    handoff_id = _bounded_id(data.get("handoff_id"), limit=512)
    if not handoff_id:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="handoff_id_missing")
        return
    try:
        record = await store.get_handoff_record(handoff_id)
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="handoff_read_failed")
        return
    if record is None or str(getattr(record, "project_id", "") or "") != str(project_id or ""):
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="handoff_not_found")
        return
    from .employee_handoff import HandoffRefused, handoff_materials

    try:
        await handoff_materials(store, record)
    except HandoffRefused as exc:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error=exc.code)
        return
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, error="handoff_artifact_unverified")
        return
    state, delivered, check = _acceptance_view(record)
    if not check:
        await handler._send_ack(ws, ok=False, action="employee_handoff_check",
                                project_id=project_id, handoff_id=handoff_id,
                                error="handoff_check_missing")
        return
    await handler._send_ack(ws, ok=True, action="employee_handoff_check",
                            project_id=project_id, handoff_id=handoff_id,
                            acceptance_state=state, delivered=delivered,
                            origin_check=check, status=record.status)


async def handle_employee_handoff_node_list(handler: Any, ws: Any, data: dict) -> None:
    """Handoffs whose payload names this workflow node. Does not invent rows."""
    engine, project_id = await handler._engine_for_request(data)
    actor = await _owner_login(handler, ws)
    if not actor:
        await handler._send_ack(ws, ok=False, action="employee_handoff_node_list",
                                project_id=project_id, error="owner_required")
        return
    store = getattr(engine, "store", None)
    if store is None:
        await handler._send_ack(ws, ok=False, action="employee_handoff_node_list",
                                project_id=project_id, error="handoff_store_unavailable")
        return
    node_id = _bounded_id(data.get("node_id"))
    if not node_id:
        await handler._send_ack(ws, ok=False, action="employee_handoff_node_list",
                                project_id=project_id, error="handoff_node_id_invalid")
        return
    status = str(data.get("status", "") or "").strip() or None
    matched: list[Any] = []
    truncated = False
    page_size = 200
    try:
        for page_index in range(4):
            page = await store.get_handoff_records(
                project_id, status=status, limit=page_size, offset=page_index * page_size,
            )
            for record in page:
                payload = getattr(record, "payload", {}) or {}
                if str(payload.get("node_id") or "") == node_id:
                    matched.append(record)
            if len(page) < page_size:
                break
        else:
            truncated = True
    except Exception:
        await handler._send_ack(ws, ok=False, action="employee_handoff_node_list",
                                project_id=project_id, error="handoff_read_failed")
        return
    shown = matched[:50]
    counts: dict[str, int] = {}
    for record in matched:
        counts[str(record.status)] = counts.get(str(record.status), 0) + 1
    counts["total"] = len(matched)
    await handler._send_ack(
        ws, ok=True, action="employee_handoff_node_list", project_id=project_id,
        node_id=node_id, handoffs=[_handoff_payload(record) for record in shown],
        counts=counts, offset=0, limit=50, has_more=len(matched) > 50 or truncated,
        truncated=truncated,
    )

