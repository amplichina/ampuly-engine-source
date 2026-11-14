"""Freeze the concrete upstream delivery package before asking a human.

This freezes the retained Task result, not arbitrary bytes behind file paths.
The checkpoint must display this package and its version. External attachments
remain controlled references and are not silently re-read from a local path.
Legacy nodes without a retained EmployeeRun keep their old review UI and do not
gain the new automatic rework capability.
"""
from __future__ import annotations

import hashlib
import json

from .human_review_rework import _canonical, _id, _integer, _sha, _fail
from .human_review_employee_basis import verified_retained_employee_run


async def freeze_human_review_material(executor, task, node, *, wait_seq: int) -> dict | None:
    if type(node) is not dict or node.get("type") != "human_review" or not node.get("rework_to"):
        return None
    if type(task.metadata) is not dict:
        _fail("rework_source_unavailable")
    run_id = task.metadata.get("delegation_run_id")
    _id(run_id)
    _integer(wait_seq)
    target_node = _id(node["rework_to"])
    review_node = _id(node.get("id"))
    items = list(await executor.store.list_delegation_work_items(run_id) or ())
    if len(items) > 500:
        _fail("rework_graph_invalid")
    targets = [item for item in items if type(item.metadata) is dict
               and item.metadata.get("scope_key") == target_node]
    reviews = [item for item in items if type(item.metadata) is dict
               and item.metadata.get("scope_key") == review_node]
    if len(targets) != 1 or len(reviews) != 1:
        _fail("rework_graph_invalid")
    target, review = targets[0], reviews[0]
    if target.run_id != run_id or review.run_id != run_id:
        _fail("rework_graph_invalid")
    target_task = await executor.store.get_runtime_task_for_work_item(target.work_item_id)
    review_task = await executor.store.get_runtime_task_for_work_item(review.work_item_id)
    if target_task is None or review_task is None or review_task.id != task.id:
        _fail("rework_source_unavailable")
    if target_task.project_id != task.project_id or type(target_task.metadata) is not dict:
        _fail("rework_source_unavailable")
    envelope = target_task.metadata.get("employee_run")
    if envelope is None:
        return None
    if type(envelope) is not dict or type(target_task.result) is not dict or not target_task.result:
        _fail("rework_source_unavailable")
    _id(envelope.get("order_task_id"))
    restored = await verified_retained_employee_run(executor, target_task, project_id=task.project_id,
        order_task_id=envelope["order_task_id"], target_node_id=target_node)
    identity = restored.identity
    package = {
        "project_id": task.project_id, "order_id": identity.order_id,
        "order_task_id": identity.order_task_id, "delegation_run_id": run_id,
        "review_node_id": review_node, "review_task_id": task.id,
        "review_work_item_id": review.work_item_id, "target_node_id": target_node,
        "target_task_id": target_task.id, "target_work_item_id": target.work_item_id,
        "previous_employee_run_id": identity.run_id, "expected_attempt": identity.attempt,
        "employee_run_fingerprint": restored.fingerprint,
        "target_result": target_task.result,
    }
    raw = _canonical(package)
    return {
        "reviewed_artifact_id": "review-package:" + hashlib.sha256(
            _canonical([task.project_id, task.id, wait_seq])).hexdigest()[:32],
        "reviewed_artifact_version": wait_seq,
        "reviewed_content_hash": hashlib.sha256(raw).hexdigest(),
        "checkpoint_source_event_id": f"human-node:{task.id}:{review_node}:{wait_seq}",
        "package": json.loads(raw),
    }


def verified_material(value: object) -> dict:
    if type(value) is not dict or set(value) != {
        "reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash",
        "checkpoint_source_event_id", "package",
    }:
        _fail("rework_source_unavailable")
    _id(value["reviewed_artifact_id"])
    _integer(value["reviewed_artifact_version"])
    _sha(value["reviewed_content_hash"])
    _id(value["checkpoint_source_event_id"])
    if type(value["package"]) is not dict:
        _fail("rework_source_unavailable")
    if hashlib.sha256(_canonical(value["package"])).hexdigest() != value["reviewed_content_hash"]:
        _fail("rework_stale")
    return json.loads(_canonical(value))
