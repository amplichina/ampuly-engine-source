"""Append-only human-review confirmations on the existing Store transaction.

No connection, schema installation, BEGIN/commit/rollback, filesystem access or
dispatch. Call the writer only after the Store has re-read the original review
record/attempt/material and current host authorization on this command. The
receipt records that command; copying it into a Task is not sufficient proof.
"""
from __future__ import annotations

import json
import hashlib
import re
from typing import Any

from .human_review_rework import (
    OPERATION, PreparedHumanRework, ReworkScope,
    _canonical, _id, _integer, _sha, _fail,
)


TABLE = "human_review_rework_confirmations_v1"
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS human_review_rework_confirmations_v1 (
       confirmation_id TEXT PRIMARY KEY NOT NULL,
       project_id TEXT NOT NULL,
       order_id TEXT NOT NULL,
       record_id TEXT NOT NULL,
       request_id TEXT NOT NULL,
       payload_sha256 TEXT NOT NULL,
       receipt_json TEXT NOT NULL,
       receipt_sha256 TEXT NOT NULL,
       UNIQUE(project_id,record_id),
       UNIQUE(project_id,request_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS human_rework_receipt_no_update_v1
       BEFORE UPDATE ON human_review_rework_confirmations_v1
       BEGIN SELECT RAISE(ABORT,'human_rework_receipt_immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS human_rework_receipt_no_delete_v1
       BEFORE DELETE ON human_review_rework_confirmations_v1
       BEGIN SELECT RAISE(ABORT,'human_rework_receipt_immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS human_rework_receipt_no_replace_v1
       BEFORE INSERT ON human_review_rework_confirmations_v1
       WHEN EXISTS(SELECT 1 FROM human_review_rework_confirmations_v1
           WHERE confirmation_id=NEW.confirmation_id OR
                 (project_id=NEW.project_id AND record_id=NEW.record_id) OR
                 (project_id=NEW.project_id AND request_id=NEW.request_id))
       BEGIN SELECT RAISE(ABORT,'human_rework_receipt_immutable'); END""",
)
_SCHEMA_NAMES = (TABLE, "human_rework_receipt_no_update_v1",
                 "human_rework_receipt_no_delete_v1", "human_rework_receipt_no_replace_v1")


def _normalized_sql(value: str) -> str:
    return " ".join(value.lower().replace("if not exists", "").split()).rstrip(";")


async def require_human_review_rework_receipt_schema(db: Any) -> None:
    """Schema metadata check only; missing support fails closed, never repairs."""
    for name, expected in zip(_SCHEMA_NAMES, SCHEMA_STATEMENTS):
        async with db.execute("SELECT type,sql FROM sqlite_schema WHERE name=? LIMIT 2", (name,)) as cursor:
            rows = await cursor.fetchall()
        if (len(rows) != 1 or rows[0][0] != ("table" if name == TABLE else "trigger")
                or type(rows[0][1]) is not str or len(rows[0][1]) > 16384
                or _normalized_sql(rows[0][1]) != _normalized_sql(expected)):
            _fail("rework_source_unavailable")


def _decode_receipt(raw: Any, expected_digest: Any) -> dict[str, Any]:
    if type(raw) is not str or len(raw.encode("utf-8")) > 65536:
        _fail("rework_source_unavailable")
    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                _fail("rework_source_unavailable")
            values[key] = value
        return values
    try:
        receipt = json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: _fail("rework_source_unavailable"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("rework_source_unavailable")
    encoded = _canonical(receipt)
    if (type(receipt) is not dict or encoded.decode("utf-8") != raw
            or hashlib.sha256(encoded).hexdigest() != _sha(expected_digest)
            or receipt.get("operation") != OPERATION or receipt.get("schema_version") != 1):
        _fail("rework_source_unavailable")
    return receipt


async def read_human_review_rework_receipt_on_connection(db: Any, *, project_id: str,
                                                        record_id: str, request_id: str) -> dict | None:
    """Read both uniqueness axes; a reused request cannot select another record.

    Called with the original project Store connection; no process-global cache
    or Task metadata is an alternative source. Missing schema is unavailable.
    """
    _id(project_id)
    _id(record_id)
    _id(request_id)
    await require_human_review_rework_receipt_schema(db)
    async with db.execute(
        "SELECT project_id,order_id,record_id,request_id,payload_sha256,"
        "CASE WHEN typeof(receipt_json)='text' AND length(CAST(receipt_json AS BLOB))<=65536 "
        "THEN receipt_json ELSE NULL END,receipt_sha256 "
        "FROM human_review_rework_confirmations_v1 "
        "WHERE project_id=? AND (record_id=? OR request_id=?) LIMIT 3",
        (project_id, record_id, request_id),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _fail("request_id_reused")
    row = rows[0]
    receipt = _decode_receipt(row[5], row[6])
    for index, name in enumerate(("project_id", "order_id", "record_id", "request_id", "payload_sha256")):
        if type(row[index]) is not str or receipt.get(name) != row[index]:
            _fail("rework_source_unavailable")
    if receipt["project_id"] != project_id:
        _fail("rework_source_unavailable")
    return receipt


async def append_human_review_rework_receipt_on_connection(db: Any, *, scope: ReworkScope,
        prepared: PreparedHumanRework, marker: dict[str, Any], actor_login: str,
        confirmed_at: str, controller_generation: int, original_record: dict) -> dict[str, Any]:
    """Write the exact command receipt inside the same reset transaction.

    The caller is the named Store command after revalidation, not a route.
    A same-request retry must take the read path and MUST NOT append again.
    """
    scope.validate()
    if type(prepared) is not PreparedHumanRework or prepared.already_applied:
        _fail("rework_invalid")
    if getattr(getattr(db, "_conn", None), "in_transaction", False) is not True:
        _fail("rework_source_unavailable")
    if type(actor_login) is not str or re.fullmatch(r"[A-Za-z0-9_.@-]{2,64}", actor_login) is None:
        _fail()
    _id(prepared.request_id)
    _id(prepared.record_id)
    _sha(prepared.payload_sha256)
    _integer(controller_generation)
    if type(confirmed_at) is not str or not 1 <= len(confirmed_at) <= 64:
        _fail()
    if type(marker) is not dict:
        _fail()
    if (type(original_record) is not dict or original_record.get("record_id") != prepared.record_id
            or original_record.get("dispatched") is not False):
        _fail("rework_source_unavailable")
    mandatory = {
        "source": "human_review_rework_confirm", "schema_version": 1,
        "request_id": prepared.request_id, "payload_sha256": prepared.payload_sha256,
        "rework_record_id": prepared.record_id,
        "project_id": scope.project_id, "order_id": scope.order_id,
        "order_task_id": scope.order_task_id, "target_task_id": scope.target_task_id,
        "target_work_item_id": scope.target_work_item_id, "target_node_id": scope.target_node_id,
        "review_task_id": scope.review_task_id, "review_node_id": scope.review_node_id,
        "previous_employee_run_id": prepared.previous_employee_run_id,
        "previous_attempt": prepared.previous_attempt, "revision_note_hash": prepared.note_hash,
        "revision_note": prepared.note, "confirmed_by": actor_login,
        "confirmed_at": confirmed_at, "controller_generation": controller_generation,
    }
    if any(type(marker.get(key)) is not type(value) or marker.get(key) != value
           for key, value in mandatory.items()):
        _fail("rework_source_unavailable")
    await require_human_review_rework_receipt_schema(db)
    prior = await read_human_review_rework_receipt_on_connection(db,
        project_id=scope.project_id, record_id=prepared.record_id, request_id=prepared.request_id)
    if prior is not None:
        _fail("request_id_reused" if prior.get("request_id") == prepared.request_id else "rework_stale")
    receipt = {
        "schema_version": 1, "operation": OPERATION,
        "confirmation_id": "hrc-" + hashlib.sha256(_canonical(
            [scope.project_id, prepared.record_id, prepared.request_id])).hexdigest(),
        "project_id": scope.project_id, "order_id": scope.order_id,
        "order_task_id": scope.order_task_id, "delegation_run_id": scope.delegation_run_id,
        "record_id": prepared.record_id, "request_id": prepared.request_id,
        "payload_sha256": prepared.payload_sha256, "scope": {
            key: getattr(scope, key) for key in scope.__slots__},
        "marker": json.loads(_canonical(marker)), "actor_login": actor_login,
        "original_record": json.loads(_canonical(original_record)),
        "confirmed_at": confirmed_at, "controller_generation": controller_generation,
    }
    raw = _canonical(receipt)
    cursor = await db.execute(
        "INSERT INTO human_review_rework_confirmations_v1 "
        "(confirmation_id,project_id,order_id,record_id,request_id,payload_sha256,receipt_json,receipt_sha256) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (receipt["confirmation_id"], scope.project_id, scope.order_id, prepared.record_id,
         prepared.request_id, prepared.payload_sha256, raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()),
    )
    if type(cursor.rowcount) is not int or cursor.rowcount != 1:
        _fail("rework_source_unavailable")
    return receipt
