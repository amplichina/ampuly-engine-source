"""External ingress ownership in existing Task rows; no schema or new ledger.

The immutable source and one-shot execution claim share the Task transaction.
Broad runtime saves preserve these fields. Source Tasks are retained rather than
deleted, so archiving a request cannot silently make its signature executable.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3
import uuid

from ampuly.core.models import Task, TaskResult, TaskStatus

SOURCE = "external_agent_source"
STATE = "external_agent_receipt"
PROTOCOL = "external_agent.task/v1"
PREFIX = "external-agent-"


class ExternalAgentTaskRefused(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def task_id_for(principal_id: str, project_id: str, message_id: str) -> str:
    identity = json.dumps([principal_id, project_id, message_id], ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return PREFIX + hashlib.sha256(identity).hexdigest()


def protect_external_agent_fields(task: Task, persisted: dict | None, *, allow_creation=False, persisted_status=None):
    """Called under the existing Store save transaction, including stale saves."""
    offered = dict(task.metadata or {})
    original = (persisted or {}).get(SOURCE)
    if original is None:
        if SOURCE in offered or STATE in offered or task.id.startswith(PREFIX):
            if not allow_creation:
                raise ExternalAgentTaskRefused("external_agent_requires_strict_creation")
        return
    if not isinstance(original, dict) or original.get("protocol") != PROTOCOL:
        raise ExternalAgentTaskRefused("external_agent_source_invalid")
    if SOURCE in offered and offered[SOURCE] != original:
        raise ExternalAgentTaskRefused("external_agent_source_immutable")
    scope = original["scope"]
    if (task.project_id != original["project_id"]
            or hashlib.sha256(task.description.encode("utf-8")).hexdigest() != original["goal_sha256"]):
        raise ExternalAgentTaskRefused("external_agent_source_immutable")
    for name, value in scope.items():
        if name in offered and offered[name] != value:
            raise ExternalAgentTaskRefused("external_agent_scope_immutable")
        offered[name] = value
    offered[SOURCE] = deepcopy(original)
    offered[STATE] = deepcopy((persisted or {}).get(STATE, {}))
    task.metadata = offered
    if persisted_status == TaskStatus.CANCELLED.value:
        task.status = TaskStatus.CANCELLED
    if (persisted or {}).get("last_stop_reason") == "user_stop":
        task.metadata["last_stop_reason"] = "user_stop"
        task.status = TaskStatus(persisted_status)


class ExternalAgentTaskStore:
    """Atomic source admission and one execution attempt on an existing AmpulyStore."""

    def __init__(self, store, *, principal_id: str, project_id: str, binding_sha256: str):
        if not principal_id or not project_id or store.project_id != project_id:
            raise ExternalAgentTaskRefused("external_agent_store_scope")
        store._require_db()
        self.store = store
        self.principal_id = principal_id
        self.project_id = project_id
        self.binding_sha256 = binding_sha256
        # This existing binding's capture/read transaction verifies only the
        # ready Store's physical file and declared project; it does not require
        # or initialize a browser-account catalog.
        from .browser_account_grant_store import _ReadyProjectGrantBinding
        try:
            self._database = _ReadyProjectGrantBinding.capture(store)
        except Exception:
            raise ExternalAgentTaskRefused("external_agent_source_unavailable") from None

    @asynccontextmanager
    async def _transaction(self):
        from .store import _SQLiteConnectionAdapter
        from . import schema_version as schema
        from .source_project_scope import require_source_project
        self._database._check_objects()
        with schema._database_opening(self._database.db_path, schema.RUNTIME, allow_create=False) as opening:
            if (opening.path != self._database.opening.path
                    or opening.identity != self._database.opening.identity):
                raise ExternalAgentTaskRefused("external_agent_source_changed")
            db = _SQLiteConnectionAdapter(opening.path.as_uri() + "?mode=rw", uri=True, busy_timeout_ms=0)
            try:
                await db.execute("BEGIN IMMEDIATE")
                require_source_project(db._conn, self.project_id, write=True)
                schema._verify_database_opening(db._conn, self._database.opening)
                self._check_write_binding(db._conn)
                yield db
                require_source_project(db._conn, self.project_id, write=True)
                schema._verify_database_opening(db._conn, self._database.opening)
                self._check_write_binding(db._conn)
                await db.commit()
            except BaseException as exc:
                await db.rollback()
                code = getattr(exc, "sqlite_errorcode", None)
                if (isinstance(exc, sqlite3.OperationalError) and type(code) is int
                        and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
                    raise ExternalAgentTaskRefused("external_agent_source_busy") from None
                raise
            finally:
                await db.close()

    def _check_write_binding(self, conn):
        """Keep opening continuity while allowing this writer's own journal.

        The read-only binding already refused a pre-existing rollback journal
        before BEGIN IMMEDIATE. This exact connection then acquired SQLite's
        sole writer lock; a regular DELETE journal created by its writes is
        expected, not an unrelated hot journal to recover.
        """
        from . import schema_version as schema
        binding = self._database
        if (self.store._require_db() is not binding.adapter
                or binding.adapter._conn is not binding.original
                or self.store.db_path != binding.db_path
                or self.store.project_id != binding.project_id
                or getattr(self.store, "_browser_account_opening_evidence", None) is not binding.evidence
                or binding.original.in_transaction is not False
                or binding.original.row_factory is not None or binding.original.text_factory is not str
                or type(conn) is not sqlite3.Connection or conn is binding.original
                or conn.in_transaction is not True):
            raise ExternalAgentTaskRefused("external_agent_source_changed")
        schema._verify_database_opening(conn, binding.opening)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = binding.opening.path.with_name(binding.opening.path.name + suffix)
            try:
                schema._path_identity(sidecar)
            except FileNotFoundError:
                continue
            if suffix == "-journal":
                cursor = conn.execute("PRAGMA journal_mode")
                try:
                    mode = cursor.fetchone()
                finally:
                    cursor.close()
                if mode != ("delete",):
                    raise ExternalAgentTaskRefused("external_agent_source_unavailable")

    def _check(self, task):
        source = (getattr(task, "metadata", None) or {}).get(SOURCE, {})
        if (self.store.project_id != self.project_id or task is None or task.project_id != self.project_id
                or source.get("principal_id") != self.principal_id
                or source.get("project_id") != self.project_id
                or source.get("binding_sha256") != self.binding_sha256
                or source.get("protocol") != PROTOCOL):
            raise ExternalAgentTaskRefused("external_agent_task_unavailable")
        return task

    async def get(self, message_id: str):
        tid = task_id_for(self.principal_id, self.project_id, message_id)
        return self.read_task(tid)

    def read_task(self, task_id: str):
        try:
            with self._database.read_transaction() as conn:
                cursor = conn.execute("SELECT * FROM main.tasks WHERE id = ?", (task_id,))
                try:
                    row = cursor.fetchone()
                    task = self.store._row_to_task(row, cursor.description) if row is not None else None
                finally:
                    cursor.close()
            return self._check(task)
        except ExternalAgentTaskRefused:
            raise
        except Exception:
            raise ExternalAgentTaskRefused("external_agent_source_unavailable") from None

    async def create_or_get(self, task: Task):
        source = task.metadata[SOURCE]
        self._check(task)
        if task.id != task_id_for(self.principal_id, self.project_id, source["message_id"]):
            raise ExternalAgentTaskRefused("external_agent_source_invalid")
        async with self._transaction() as db:
            old = await self.store._get_task_unhydrated(task.id, db=db)
            if old is not None:
                self._check(old)
                if old.metadata[SOURCE] != source:
                    raise ExternalAgentTaskRefused("external_agent_request_conflict")
                return old, False
            # Replay is checked before sequence, so an old exact request remains
            # queryable after newer requests and after process restart.
            async with db.execute(
                "SELECT MAX(json_extract(metadata, '$.external_agent_source.sequence')) "
                "FROM tasks WHERE project_id = ? AND "
                "json_extract(metadata, '$.external_agent_source.principal_id') = ?",
                (self.project_id, self.principal_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0] is not None and source["sequence"] <= int(row[0]):
                raise ExternalAgentTaskRefused("external_agent_out_of_order")
            await self.store._save_task_row(
                task, db=db, commit=False, insert_only=True, allow_external_agent_creation=True,
            )
            return task, True

    async def claim_once(self, message_id: str):
        tid = task_id_for(self.principal_id, self.project_id, message_id)
        async with self._transaction() as db:
            task = self._check(await self.store._get_task_unhydrated(tid, db=db))
            receipt = dict(task.metadata.get(STATE) or {})
            if receipt.get("claim_id"):
                return task, ""
            if task.status != TaskStatus.PENDING:
                raise ExternalAgentTaskRefused("external_agent_not_pending")
            claim = uuid.uuid4().hex
            receipt["claim_id"] = claim
            task.metadata[STATE] = receipt
            task.status = TaskStatus.RUNNING
            await self._write(db, task)
            return task, claim

    async def finish(self, message_id: str, claim: str, result: TaskResult, *, artifact=None, handoff_id="",
                     harness_session_id="", harness_turn_id=""):
        async with self._transaction() as db:
            task = self._check(await self.store._get_task_unhydrated(
                task_id_for(self.principal_id, self.project_id, message_id), db=db))
            receipt = dict(task.metadata.get(STATE) or {})
            if not claim or receipt.get("claim_id") != claim or receipt.get("finished"):
                raise ExternalAgentTaskRefused("external_agent_claim_lost")
            if task.status == TaskStatus.CANCELLED or task.metadata.get("last_stop_reason") == "user_stop":
                result = TaskResult(status=task.status, content="")
                artifact, handoff_id = None, ""
            receipt.update(finished=True, body_status=result.status.value,
                           harness_session_id=harness_session_id, harness_turn_id=harness_turn_id)
            if artifact and handoff_id:
                receipt.update(artifact=deepcopy(artifact), handoff_id=handoff_id)
            task.metadata[STATE] = receipt
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            await self._write(db, task)
            return task

    async def bind_effect(self, message_id: str, effect_id: str):
        async with self._transaction() as db:
            task = self._check(await self.store._get_task_unhydrated(
                task_id_for(self.principal_id, self.project_id, message_id), db=db))
            receipt = dict(task.metadata.get(STATE) or {})
            if not receipt.get("artifact") or not receipt.get("handoff_id"):
                raise ExternalAgentTaskRefused("external_agent_body_unconfirmed")
            if receipt.get("effect_id") and receipt["effect_id"] != effect_id:
                raise ExternalAgentTaskRefused("external_agent_effect_conflict")
            receipt["effect_id"] = effect_id
            task.metadata[STATE] = receipt
            await self._write(db, task)
            return task

    async def record_readback(self, message_id: str, effect_id: str, proof: dict):
        async with self._transaction() as db:
            task = self._check(await self.store._get_task_unhydrated(
                task_id_for(self.principal_id, self.project_id, message_id), db=db))
            receipt = dict(task.metadata.get(STATE) or {})
            if receipt.get("effect_id") != effect_id:
                raise ExternalAgentTaskRefused("external_agent_effect_conflict")
            receipt["readback"] = deepcopy(proof)
            task.metadata[STATE] = receipt
            await self._write(db, task)
            return task

    @staticmethod
    async def _write(db, task):
        # Exact updates preserve the live Runtime context and its tool ledger.
        await db.execute(
            "UPDATE tasks SET metadata = ?, status = ?, result = ? WHERE id = ? AND project_id = ?",
            (json.dumps(task.metadata, ensure_ascii=False), task.status.value,
             json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
             task.id, task.project_id),
        )

