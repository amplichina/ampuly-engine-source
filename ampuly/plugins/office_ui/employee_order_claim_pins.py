"""Bounded pending-claim observation; not an execution or authorization grant.

The host owns authentication and an existing query-only transaction. Completed
launch observations retain their separate reader and are never downgraded here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from .employee_order_launch_anchor import (
    OrderLaunchAnchorError, _ReadBudget, _identity, _rows, _one_record,
    _json_dict, _canonical_pins, _metadata_select, _bounded_select, _sizes,
    _texts, _EVENT_COLUMNS, _PIN_NOTE_KEYS, _MAX_EVENTS, _MAX_SCALAR_BYTES,
    _MAX_JSON_BYTES, _SHA256,
)


_CLAIM_NOTE_KEYS = frozenset(("outbox_id", "anchor_version", "launch_payload_sha256"))


@dataclass(frozen=True, repr=False)
class OriginalEmployeeOrderClaimPins:
    """Original source bytes, not FrozenEmployeePins or a trusted runtime ID.

    Pins may contain private human references: do not log this object or its
    fields. The chain head is the observed snapshot head, not a claim-time token.
    Consumers still require pin-schema restoration and independent authorization.
    """

    project_id: str
    order_id: str
    outbox_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_sha256: str
    claim_event_id: str
    pins_event_id: str
    event_chain_head_sha256: str
    pins_bytes: bytes
    pins_fingerprint: str


def _read_claim_events(
    conn: sqlite3.Connection, *, order_id: str, outbox_id: str, payload_sha: str,
    budget: _ReadBudget, pins_source: tuple[bytes, str] | None, project_id: str,
    workflow_id: str, workflow_version: str,
) -> tuple[str, str, str]:
    previous_hash = ""
    previous_id = 0
    count = 0
    seen_event_ids: set[str] = set()
    claim: tuple[int, str] | None = None
    pins_event: tuple[int, str] | None = None
    cursor = conn.execute(
        f"SELECT id, {_metadata_select(_EVENT_COLUMNS)} FROM order_events "
        "WHERE order_id = ? ORDER BY id ASC LIMIT 5001", (order_id,),
    )
    primary_failure = False
    try:
        while True:
            metadata = cursor.fetchone()
            if metadata is None:
                break
            count += 1
            if count > _MAX_EVENTS:
                raise OrderLaunchAnchorError("read_limit")
            event_number = metadata[0]
            if type(event_number) is not int or event_number <= previous_id:
                raise OrderLaunchAnchorError("invalid_source")
            sizes = _sizes(metadata[1:], _EVENT_COLUMNS, budget)
            previous_id = event_number
            rows = _rows(
                conn, f"SELECT {_bounded_select(_EVENT_COLUMNS)} FROM order_events "
                "WHERE order_id = ? AND id = ? LIMIT 1", (order_id, event_number), 1,
            )
            if len(rows) != 1:
                raise OrderLaunchAnchorError("invalid_source")
            (aggregate, at, kind, actor, note_raw, event_id, correlation, causation,
             stored_previous, event_hash) = _texts(rows[0], sizes)
            if (aggregate != order_id or not at or not kind or not event_id
                    or event_id in seen_event_ids or stored_previous != previous_hash
                    or _SHA256.fullmatch(event_hash) is None):
                raise OrderLaunchAnchorError("invalid_source")
            seen_event_ids.add(event_id)
            note = _json_dict(note_raw, budget)
            envelope = {
                "schema_version": 1, "event_id": event_id, "aggregate_type": "work_order",
                "aggregate_id": aggregate, "at": at, "kind": kind, "actor": actor,
                "correlation_id": correlation, "causation_id": causation,
                "previous_hash": previous_hash, "note": note,
            }
            expected = hashlib.sha256(json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            if expected != event_hash:
                raise OrderLaunchAnchorError("invalid_source")
            previous_hash = event_hash
            if (pins_source is None and kind == "employee_run.pinned"
                    and (causation == outbox_id or note.get("outbox_id") == outbox_id)):
                raise OrderLaunchAnchorError("invalid_source")
            if causation != outbox_id:
                continue
            # A completion for this outbox contradicts a pending observation;
            # never extract its task ID to invent a prelaunch identity.
            if kind == "launched":
                raise OrderLaunchAnchorError("invalid_source")
            if kind == "launching":
                if (claim is not None or correlation != order_id
                        or set(note) != _CLAIM_NOTE_KEYS or note["outbox_id"] != outbox_id
                        or type(note["anchor_version"]) is not int or note["anchor_version"] != 1
                        or note["launch_payload_sha256"] != payload_sha):
                    raise OrderLaunchAnchorError("invalid_source")
                claim = (event_number, event_id)
            elif kind == "employee_run.pinned":
                if (pins_event is not None or correlation != order_id
                        or set(note) != _PIN_NOTE_KEYS or note["project_id"] != project_id
                        or note["workflow_id"] != workflow_id or note["workflow_version"] != workflow_version
                        or note["outbox_id"] != outbox_id):
                    raise OrderLaunchAnchorError("invalid_source")
                recorded_pins = _canonical_pins(
                    note["employee_pins"], note["employee_pins_fingerprint"],
                    project_id=project_id, workflow_id=workflow_id,
                )
                if recorded_pins != pins_source:
                    raise OrderLaunchAnchorError("invalid_source")
                pins_event = (event_number, event_id)
                del recorded_pins
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            cursor.close()
        except BaseException:
            if not primary_failure:
                raise
    if pins_source is None:
        if claim is None:
            raise OrderLaunchAnchorError("unavailable")
        return claim[1], "", previous_hash
    if claim is None or pins_event is None:
        raise OrderLaunchAnchorError("unavailable")
    if claim[0] >= pins_event[0]:
        raise OrderLaunchAnchorError("invalid_source")
    return claim[1], pins_event[1], previous_hash


def _read_claim_pins(
    conn: sqlite3.Connection, *, project_id: str, order_id: str, outbox_id: str,
) -> OriginalEmployeeOrderClaimPins:
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise OrderLaunchAnchorError("transaction_required")
    if conn.row_factory is not None and conn.row_factory is not sqlite3.Row:
        raise OrderLaunchAnchorError("invalid_source")
    if conn.text_factory is not str:
        raise OrderLaunchAnchorError("invalid_source")
    query_only = _rows(conn, "PRAGMA query_only", (), 2)
    if len(query_only) != 1 or len(query_only[0]) != 1 or query_only[0][0] != 1:
        raise OrderLaunchAnchorError("transaction_required")

    budget = _ReadBudget()
    status, fields_raw = _one_record(
        conn, table="work_orders", selector="order_id = ?", params=(order_id,),
        columns=(("status", _MAX_SCALAR_BYTES), ("fields_json", _MAX_JSON_BYTES)), budget=budget,
    )
    fields = _json_dict(fields_raw, budget)
    if type(fields.get("project_id")) is not str or fields["project_id"] != project_id:
        raise OrderLaunchAnchorError("invalid_source")
    del fields, fields_raw
    if status != "launching":
        raise OrderLaunchAnchorError("unavailable")

    outbox_columns = (
        ("outbox_id", _MAX_SCALAR_BYTES), ("aggregate_type", _MAX_SCALAR_BYTES),
        ("aggregate_id", _MAX_SCALAR_BYTES), ("event_type", _MAX_SCALAR_BYTES),
        ("status", _MAX_SCALAR_BYTES), ("payload_json", _MAX_JSON_BYTES),
    )
    stored_id, aggregate_type, aggregate_id, event_type, status, payload_raw = _one_record(
        conn, table="business_outbox", selector="outbox_id = ?", params=(outbox_id,),
        columns=outbox_columns, budget=budget,
    )
    if (stored_id != outbox_id or aggregate_type != "work_order" or aggregate_id != order_id
            or event_type != "workflow_launch_requested"):
        raise OrderLaunchAnchorError("invalid_source")
    if status != "pending":
        raise OrderLaunchAnchorError("unavailable")
    payload = _json_dict(payload_raw, budget)
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("command") != "workflow.launch"
            or type(payload.get("order_id")) is not str or payload["order_id"] != order_id
            or type(payload.get("project_id")) is not str or payload["project_id"] != project_id):
        raise OrderLaunchAnchorError("invalid_source")
    workflow_id = _identity(payload.get("workflow_id"))
    if "employee_pins" not in payload or (type(payload["employee_pins"]) is dict and not payload["employee_pins"]):
        raise OrderLaunchAnchorError("unavailable")
    workflow_version = _identity(payload.get("workflow_version"))
    payload_sha = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
    pins_source = _canonical_pins(
        payload["employee_pins"], payload.get("employee_pins_fingerprint"),
        project_id=project_id, workflow_id=workflow_id,
    )
    del payload, payload_raw
    claim_id, pins_id, head_sha = _read_claim_events(
        conn, order_id=order_id, outbox_id=outbox_id, payload_sha=payload_sha, budget=budget,
        pins_source=pins_source, project_id=project_id, workflow_id=workflow_id,
        workflow_version=workflow_version,
    )
    return OriginalEmployeeOrderClaimPins(
        project_id=project_id, order_id=order_id, outbox_id=outbox_id,
        workflow_id=workflow_id, workflow_version=workflow_version,
        launch_payload_sha256=payload_sha, claim_event_id=claim_id, pins_event_id=pins_id,
        event_chain_head_sha256=head_sha, pins_bytes=pins_source[0], pins_fingerprint=pins_source[1],
    )


def read_original_employee_order_claim_pins(
    conn: sqlite3.Connection, *, project_id: str, order_id: str, outbox_id: str,
) -> OriginalEmployeeOrderClaimPins:
    """Observe pending original pins; never infer runtime identity or readiness."""
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    failed = False
    result = None
    try:
        result = _read_claim_pins(conn, project_id=project_id, order_id=order_id, outbox_id=outbox_id)
    except OrderLaunchAnchorError:
        raise
    except Exception:
        # Raise outside the handler so private SQL/parser failures are not chained.
        # All BaseException subclasses deliberately propagate without retry.
        failed = True
    if failed:
        raise OrderLaunchAnchorError("unavailable") from None
    assert type(result) is OriginalEmployeeOrderClaimPins
    return result
