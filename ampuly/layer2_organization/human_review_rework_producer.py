"""Retain a new human rework record alongside the existing FAILED/refusal write.

No retroactive migration of legacy rework metadata. The package must already
be on the persisted original human checkpoint and durable review Task before
the refusal. The caller owns the existing SQLite write transaction.
"""
from __future__ import annotations

from .human_review_material import verified_material
from .human_review_rework import ReworkScope, build_pending_record, _canonical, _fail
from .human_review_rework_commit import _json, _one, _task


async def retain_human_review_rework_record(db, *, task, observation,
        fact_id: str, run_id: str, work_item_id: str, attempt_seq: int,
        controller_generation: int) -> dict | None:
    material = task.metadata.get("human_review_material")
    if material is None:
        # Old tasks remain blocked with their existing explanatory metadata.
        # A UI click must not turn the latest artifacts into alleged history.
        return None
    if getattr(getattr(db, "_conn", None), "in_transaction", False) is not True:
        _fail("rework_source_unavailable")
    material = verified_material(material)
    _, durable_metadata, _ = await _task(db, task.id, task.project_id)
    if _canonical(durable_metadata.get("human_review_material")) != _canonical(material):
        _fail("rework_stale")
    if observation.reply_truncated is not False:
        _fail("rework_source_unavailable")
    package = material["package"]
    if (package.get("project_id") != task.project_id or package.get("delegation_run_id") != run_id
            or package.get("review_task_id") != task.id or package.get("review_work_item_id") != work_item_id
            or package.get("review_node_id") != observation.observed_node_id
            or package.get("target_node_id") != observation.observed_rework_to):
        _fail("rework_stale")
    row = await _one(db,
        "SELECT checkpoint_id,project_id,task_id,checkpoint_type,status,"
        "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=2097152 THEN payload ELSE NULL END "
        "FROM execution_checkpoints WHERE project_id=? AND task_id=? "
        "AND json_extract(payload,'$.source_event_id')=? LIMIT 2",
        (task.project_id, task.id, material["checkpoint_source_event_id"]))
    if (tuple(row[1:4]) != (task.project_id, task.id, "task_user_input")
            or row[4] not in {"consuming", "resolved"}):
        _fail("rework_source_unavailable")
    payload = _json(row[5])
    if _canonical(payload.get("human_review_material")) != _canonical(material):
        _fail("rework_source_unavailable")
    scope = ReworkScope(**{key: (row[0] if key == "checkpoint_id" else package.get(key))
                          for key in ReworkScope.__slots__})
    return build_pending_record(scope=scope, expected_attempt=package.get("expected_attempt"),
        previous_employee_run_id=package.get("previous_employee_run_id"),
        reviewed_artifact_id=material["reviewed_artifact_id"],
        reviewed_artifact_version=material["reviewed_artifact_version"],
        reviewed_content_hash=material["reviewed_content_hash"],
        revision_note=observation.reply_excerpt, source_refusal_fact_id=fact_id,
        source_review_attempt=attempt_seq, source_review_epoch=controller_generation)
