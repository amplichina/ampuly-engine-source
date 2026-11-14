"""Human-review-only checks inside the existing controller reset transaction.

This context is constructed by the server runtime host, never RPC deserialization.
The Store remains sole writer of Task/WorkItem/run changes. This object checks
their exact proposed deltas and appends one confirmation receipt on that same
connection; it does not open a connection, manage a transaction or schedule work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import SimpleNamespace

from .human_review_rework import (
    ReworkScope, ReworkBranch, PreparedHumanRework, prepare_confirmation,
    normalize_revision_note, _canonical, _integer, _fail,
)
from .human_review_material import verified_material
from .human_review_employee_basis import verified_retained_employee_run
from .human_review_rework_receipts import (
    read_human_review_rework_receipt_on_connection,
    append_human_review_rework_receipt_on_connection,
)


def _json(raw):
    if type(raw) is not str or len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        _fail("rework_source_unavailable")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("rework_source_unavailable")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: _fail("rework_source_unavailable"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("rework_source_unavailable")
    if type(result) is not dict:
        _fail("rework_source_unavailable")
    return result


async def _one(db, sql, params):
    async with db.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    if len(rows) != 1:
        _fail("rework_source_unavailable")
    return rows[0]


async def _task(db, task_id, project_id):
    row = await _one(db,
        "SELECT id,project_id,status,"
        "CASE WHEN typeof(metadata)='text' AND length(CAST(metadata AS BLOB))<=2097152 THEN metadata ELSE NULL END,"
        "CASE WHEN result IS NULL THEN '{}' WHEN typeof(result)='text' AND length(CAST(result AS BLOB))<=2097152 "
        "THEN result ELSE NULL END "
        "FROM tasks WHERE id=? AND project_id=? LIMIT 2", (task_id, project_id))
    if row[0] != task_id or row[1] != project_id:
        _fail("rework_stale")
    return row[2], _json(row[3]), _json(row[4])


@dataclass(slots=True)
class HumanReworkCommit:
    scope: ReworkScope
    request: dict
    branch: ReworkBranch
    host: object = field(repr=False)
    confirmed_at: str
    controller_generation: int
    executor: object = field(repr=False)
    prepared: PreparedHumanRework | None = field(default=None, init=False)
    marker: dict | None = field(default=None, init=False)
    completed_record: dict | None = field(default=None, init=False)
    _record: dict | None = field(default=None, init=False, repr=False)
    _actor_login: str = field(default="", init=False, repr=False)
    _rework_count: int = field(default=-1, init=False, repr=False)

    async def validate_on_connection(self, db, *, controller_context, mutations,
                                     task_snapshots) -> PreparedHumanRework:
        """Re-read actual origin facts; payload/read-only inspection is no proof."""
        self.scope.validate()
        if (type(self.branch) is not ReworkBranch or self.host is None
                or not callable(getattr(self.host, "confirm_rework", None))
                or getattr(getattr(db, "_conn", None), "in_transaction", False) is not True):
            _fail("rework_source_unavailable")
        scope = self.scope
        if (controller_context.project_id != scope.project_id
                or controller_context.run_id != scope.delegation_run_id
                or controller_context.task_id != scope.review_task_id
                or controller_context.work_item_id != scope.review_work_item_id
                or controller_context.generation != self.controller_generation):
            _fail("rework_stale")
        identity = await _one(db, "SELECT scope,project_id FROM store_identity WHERE id=1 LIMIT 2", ())
        if tuple(identity) != ("project", scope.project_id):
            _fail("rework_source_unavailable")
        receipt = await read_human_review_rework_receipt_on_connection(db,
            project_id=scope.project_id, record_id=self.request["rework_record_id"], request_id=self.request["request_id"])
        if receipt is not None:
            original = receipt.get("original_record")
            if type(original) is not dict or receipt.get("scope") != {
                key: getattr(scope, key) for key in scope.__slots__}:
                _fail("rework_source_unavailable")
            authority = await self.host.confirm_rework(scope, original)
            if type(authority) is not dict or set(authority) != {"actor_login", "max_rework"}:
                _fail("rework_source_unavailable")
            prepared = prepare_confirmation(request=self.request, record={**original, "dispatched": True},
                scope=scope, material={key: original[key] for key in (
                    "reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")},
                current_employee_run_id=original.get("previous_employee_run_id"),
                current_employee_attempt=original.get("expected_attempt"),
                current_review_attempt=original.get("source_review_attempt"),
                current_review_epoch=original.get("source_review_epoch"),
                rework_count=0, max_rework=authority["max_rework"], receipt=receipt)
            self.prepared = prepared
            return prepared
        review_status, review_metadata, _ = await _task(db, scope.review_task_id, scope.project_id)
        _, target_metadata, target_result = await _task(db, scope.target_task_id, scope.project_id)
        record = review_metadata.get("human_review_rework_to")
        if type(record) is not dict:
            _fail("rework_source_unavailable")
        # Validate the refusal fact from the actual same-transaction table.
        from ampuly.database.controller_human_refusal import require_controller_human_refusal_schema
        require_controller_human_refusal_schema(db._conn)
        fact = await _one(db,
            "SELECT project_id,run_id,work_item_id,task_id,claim_attempt_seq,controller_lease_generation,"
            "record_sha256,CASE WHEN typeof(record_bytes)='blob' AND length(record_bytes)<=65536 "
            "THEN record_bytes ELSE NULL END FROM controller_human_refusal_facts_v1 WHERE fact_id=? LIMIT 2",
            (record.get("source_refusal_fact_id"),))
        if (tuple(fact[:4]) != (scope.project_id, scope.delegation_run_id, scope.review_work_item_id, scope.review_task_id)
                or type(fact[7]) is not bytes or hashlib.sha256(fact[7]).hexdigest() != fact[6]):
            _fail("rework_source_unavailable")
        refusal = _json(fact[7].decode("utf-8"))
        if (refusal.get("source_kind") != "controller_refusal_only"
                or refusal.get("observed_node_id") != scope.review_node_id
                or refusal.get("observed_rework_to") != scope.target_node_id
                or refusal.get("reply_truncated") is not False
                or normalize_revision_note(refusal.get("reply_excerpt")) != record.get("revision_note")):
            _fail("rework_source_unavailable")
        checkpoint = await _one(db,
            "SELECT project_id,task_id,checkpoint_type,status,"
            "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=2097152 THEN payload ELSE NULL END "
            "FROM execution_checkpoints WHERE checkpoint_id=? LIMIT 2", (scope.checkpoint_id,))
        if (tuple(checkpoint[:3]) != (scope.project_id, scope.review_task_id, "task_user_input")
                or checkpoint[3] != "resolved"):
            _fail("rework_source_unavailable")
        checkpoint_payload = _json(checkpoint[4])
        material = verified_material(checkpoint_payload.get("human_review_material"))
        package = material["package"]
        for key in ("project_id", "order_id", "order_task_id", "delegation_run_id",
                    "review_node_id", "review_task_id", "review_work_item_id",
                    "target_node_id", "target_task_id", "target_work_item_id"):
            if package.get(key) != getattr(scope, key):
                _fail("rework_stale")
        if _canonical(target_result) != _canonical(package.get("target_result")):
            _fail("rework_stale")
        envelope = target_metadata.get("employee_run")
        progress = target_metadata.get("employee_run_progress")
        if type(envelope) is not dict or type(progress) is not dict:
            _fail("rework_source_unavailable")
        restored = await verified_retained_employee_run(self.executor,
            SimpleNamespace(id=scope.target_task_id, project_id=scope.project_id, metadata=target_metadata),
            project_id=scope.project_id, order_task_id=scope.order_task_id,
            target_node_id=scope.target_node_id, order_id=scope.order_id)
        if restored.fingerprint != package.get("employee_run_fingerprint"):
            _fail("rework_stale")
        if target_metadata.get("employee_run_fingerprint") != package.get("employee_run_fingerprint"):
            _fail("rework_stale")
        for key, expected in (("order_id", scope.order_id), ("order_task_id", scope.order_task_id),
                              ("node_id", scope.target_node_id)):
            if envelope.get(key) != expected:
                _fail("rework_stale")
        # This is the real server-owned host, not a callback supplied by RPC.
        # The runtime keeps checking again before every model/tool action.
        authority = await self.host.confirm_rework(scope, record)
        if type(authority) is not dict or set(authority) != {"actor_login", "max_rework"}:
            _fail("rework_source_unavailable")
        _integer(authority["max_rework"], zero=True)
        if type(authority["actor_login"]) is not str or not authority["actor_login"]:
            _fail("rework_source_unavailable")
        prepared = prepare_confirmation(request=self.request, record=record, scope=scope,
            material={key: material[key] for key in ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")},
            current_employee_run_id=restored.identity.run_id, current_employee_attempt=restored.identity.attempt,
            current_review_attempt=fact[4], current_review_epoch=fact[5],
            rework_count=progress.get("rework_count"), max_rework=authority["max_rework"], receipt=receipt)
        if prepared.already_applied:
            self.prepared = prepared
            return prepared
        if review_status != "failed" or controller_context.attempt_seq != fact[4]:
            _fail("rework_stale")
        if progress.get("state") not in {"waiting_review", "completed"}:
            _fail("rework_stale")
        expected_ids = {item.work_item_id for item in self.branch.reset}
        if ({mutation.work_item_id for mutation in mutations} != expected_ids
                or len(mutations) != len(expected_ids)
                or {task.id for task in task_snapshots} != {item.task_id for item in self.branch.reset}):
            _fail("rework_graph_invalid")
        # Assert the database still has the exact scope/link/phase/attempt for
        # every reset and deliberately untouched descendant. Existing Store
        # preimage CAS additionally protects all proposed Task replacements.
        projection_by_id = {item.work_item_id: item.projection_id for item in self.branch.all_items}
        for item in self.branch.all_items:
            row = await _one(db,
                "SELECT w.run_id,w.projection_id,w.phase,"
                "CASE WHEN typeof(w.metadata)='text' AND length(CAST(w.metadata AS BLOB))<=2097152 THEN w.metadata ELSE NULL END,"
                "l.runtime_task_id FROM delegation_work_items w JOIN work_item_runtime_links l "
                "ON l.work_item_id=w.work_item_id WHERE w.work_item_id=? LIMIT 2", (item.work_item_id,))
            metadata = _json(row[3])
            _integer(metadata.get("attempt_seq", 0), zero=True)
            dependencies = metadata.get("dependency_work_item_ids", [])
            if (type(dependencies) is not list or any(type(key) is not str or key not in projection_by_id for key in dependencies)
                    or len(set(dependencies)) != len(dependencies)
                    or {projection_by_id[key] for key in dependencies} != set(item.dependency_projection_ids)):
                _fail("rework_graph_invalid")
            if (row[0] != scope.delegation_run_id or row[1] != item.projection_id or row[2] != item.phase
                    or row[4] != item.task_id or metadata.get("scope_key", "") != item.node_id
                    or metadata.get("attempt_seq", 0) != item.attempt_seq
                    or metadata.get("attempt_settled", False) is not item.attempt_settled
                    or metadata.get("last_transition_reason", "") != item.last_transition_reason
                    or metadata.get("dispatch_hold", "")):
                _fail("rework_stale")
        self.prepared = prepared
        self._record = json.loads(_canonical(record))
        self._actor_login = authority["actor_login"]
        self._rework_count = progress["rework_count"]
        self.marker = self.marker_for(prepared, material, authority["actor_login"])
        self.completed_record = {**record, "dispatched": True, "request_id": prepared.request_id,
            "payload_sha256": prepared.payload_sha256, "dispatched_at": self.confirmed_at,
            "dispatched_by": self._actor_login, "next_attempt": prepared.previous_attempt + 1}
        by_task = {task.id: task for task in task_snapshots}
        source_snapshot = by_task[scope.review_task_id]
        target_snapshot = by_task[scope.target_task_id]
        if (source_snapshot.metadata.get("human_review_rework_to") != self.completed_record
                or target_snapshot.metadata.get("employee_rework_request") != self.marker):
            _fail("rework_source_unavailable")
        for key in ("employee_run", "employee_run_fingerprint", "employee_run_progress", "employee_run_history"):
            if target_snapshot.metadata.get(key) != target_metadata.get(key):
                _fail("rework_source_unavailable")
        return prepared

    def marker_for(self, prepared, material, actor_login) -> dict:
        return {
            "schema_version": 1, "source": "human_review_rework_confirm",
            "request_id": prepared.request_id, "payload_sha256": prepared.payload_sha256,
            "rework_record_id": prepared.record_id,
            "project_id": self.scope.project_id, "order_id": self.scope.order_id,
            "order_task_id": self.scope.order_task_id, "review_node_id": self.scope.review_node_id,
            "review_task_id": self.scope.review_task_id, "target_node_id": self.scope.target_node_id,
            "target_task_id": self.scope.target_task_id, "target_work_item_id": self.scope.target_work_item_id,
            "previous_employee_run_id": prepared.previous_employee_run_id,
            "previous_attempt": prepared.previous_attempt, "revision_note_hash": prepared.note_hash,
            "revision_note": prepared.note,
            **{key: material[key] for key in ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")},
            "confirmed_by": actor_login, "confirmed_at": self.confirmed_at,
            "controller_generation": self.controller_generation,
        }

    async def append_receipt_on_connection(self, db) -> dict:
        if self.prepared is None or self.prepared.already_applied or self._record is None or self.marker is None:
            _fail("rework_source_unavailable")
        authority = await self.host.confirm_rework(self.scope, self._record)
        if (type(authority) is not dict or authority.get("actor_login") != self._actor_login
                or type(authority.get("max_rework")) is not int
                or self._rework_count < 0 or self._rework_count >= authority["max_rework"]):
            _fail("rework_authority_changed")
        return await append_human_review_rework_receipt_on_connection(db,
            scope=self.scope, prepared=self.prepared, marker=self.marker,
            actor_login=self._actor_login, confirmed_at=self.confirmed_at,
            controller_generation=self.controller_generation, original_record=self._record)
