"""Accept or reject a handoff only against its fixed artifacts (4.8 H01/H09).

The handoff fact is written by the producer; this module is the receiver's side.
Acceptance re-reads the artifact records and their content hashes, so a missing
or changed artifact is refused before anyone downstream starts work.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ampuly.core.models import HandoffRecord


class HandoffRefused(RuntimeError):
    """The handoff cannot be accepted/rejected in its current state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse(code: str) -> None:
    raise HandoffRefused(code)


async def _load(store: Any, handoff_id: str) -> HandoffRecord:
    record = await store.get_handoff_record(str(handoff_id or ""))
    if type(record) is not HandoffRecord:
        _refuse("handoff_not_found")
    return record


def _artifact_refs(record: HandoffRecord) -> list[dict[str, Any]]:
    refs = (record.payload or {}).get("artifacts")
    if type(refs) is not list or not refs:
        _refuse("handoff_artifact_missing")
    for ref in refs:
        if type(ref) is not dict or type(ref.get("artifact_id")) is not str or not ref["artifact_id"]:
            _refuse("handoff_artifact_missing")
    return refs


async def _verify_artifacts(store: Any, record: HandoffRecord, refs: list[dict[str, Any]]) -> list[str]:
    from ampuly.core.handoff_artifacts import verify_inline_artifact
    getter = getattr(store, "get_artifact", None)
    if not callable(getter) or len(refs) > 100:
        _refuse("handoff_artifact_missing")
    contents = []
    for ref in refs:
        row = await getter(record.project_id, ref["artifact_id"])
        try:
            contents.append(verify_inline_artifact(row, ref, project_id=record.project_id, task_id=record.task_id))
        except ValueError as exc:
            raise HandoffRefused(str(exc)) from exc
    return contents


async def handoff_materials(store: Any, record: HandoffRecord) -> list[str]:
    return await _verify_artifacts(store, record, _artifact_refs(record))


async def accept_handoff(store: Any, handoff_id: str, *, actor: str, note: str = "",
                         signature_actor: str = "", required_signers: tuple[str, ...] = ()) -> HandoffRecord:
    """Accept a sent handoff whose artifacts are still present and unchanged."""
    owner = str(actor or "").strip()
    if not owner:
        _refuse("handoff_actor_missing")
    record = await _load(store, handoff_id)
    if record.status != "sent":
        _refuse("handoff_state_invalid")
    check = (record.payload or {}).get("origin_check")
    if type(check) is dict and check.get("check_passed") is not True:
        _refuse("handoff_check_required")
    await _verify_artifacts(store, record, _artifact_refs(record))
    updated = await store.update_handoff_record(
        record.handoff_id, status="accepted", accepted_at=datetime.now(),
        response_summary=str(note or ""),
        metadata_updates={"decided_by": owner, "acceptance_state": "accepted"},
        expected_status="sent", verify_artifacts=True, signature_actor=signature_actor or owner,
        required_signers=required_signers,
    )
    if updated is None:
        _refuse("handoff_state_invalid")
    return updated


async def reject_handoff(store: Any, handoff_id: str, *, actor: str, reason: str) -> HandoffRecord:
    """Reject a sent handoff with a reason; no downstream work is started."""
    owner = str(actor or "").strip()
    why = str(reason or "").strip()
    if not owner:
        _refuse("handoff_actor_missing")
    if not why:
        _refuse("handoff_reason_missing")
    record = await _load(store, handoff_id)
    if record.status != "sent":
        _refuse("handoff_state_invalid")
    updated = await store.update_handoff_record(
        record.handoff_id, status="rejected", rejected_at=datetime.now(),
        response_summary=why, metadata_updates={"decided_by": owner}, expected_status="sent",
    )
    if updated is None:
        _refuse("handoff_state_invalid")
    return updated


async def admit_downstream(store: Any, handoff_id: str, *, project_id: str) -> HandoffRecord:
    """The execution-side gate: only an accepted handoff with fixed artifacts.

    This is deliberately called before a downstream task row is created, so a
    rejected or tampered handoff cannot start work that then has to be undone.
    """
    record = await _load(store, handoff_id)
    if str(record.project_id or "") != str(project_id or ""):
        _refuse("handoff_scope_invalid")
    if record.status != "accepted":
        _refuse("handoff_not_accepted")
    await _verify_artifacts(store, record, _artifact_refs(record))
    task_id = str(getattr(record, "task_id", "") or "")
    getter = getattr(store, "get_task", None)
    if callable(getter) and task_id:
        task = await getter(task_id)
        if task is None:
            _refuse("handoff_task_missing")
        status = str(getattr(getattr(task, "status", ""), "value",
                             getattr(task, "status", "")) or "")
        if status != "done":
            _refuse("handoff_task_not_done")
    return record


async def admit_rework(store: Any, handoff_id: str, *, project_id: str) -> HandoffRecord:
    """A rejected handoff may be reworked; an accepted or sent one may not."""
    record = await _load(store, handoff_id)
    if str(record.project_id or "") != str(project_id or ""):
        _refuse("handoff_scope_invalid")
    if record.status != "rejected":
        _refuse("handoff_not_rejected")
    return record
