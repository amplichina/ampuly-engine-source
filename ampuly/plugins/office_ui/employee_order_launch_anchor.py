"""Bounded original-launch correlation, not execution or authorization evidence.

The caller owns authentication and an existing query-only read transaction.
This module never opens a connection or changes its transaction/configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import sqlite3
from typing import Any


_MAX_ID_BYTES = 512
_MAX_SCALAR_BYTES = 1024
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_EVENTS = 5000
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 250000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PIN_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PIN_NOTE_KEYS = frozenset((
    "employee_pins", "employee_pins_fingerprint", "workflow_id",
    "workflow_version", "project_id", "outbox_id",
))
_MESSAGES = {
    "invalid_scope": "原发单关联范围无效。",
    "transaction_required": "需要宿主提供只读事务。",
    "read_limit": "原发单关联读取超出限额。",
    "invalid_source": "原发单关联记录无法核验。",
    "unavailable": "原发单关联暂不可用。",
}


class OrderLaunchAnchorError(ValueError):
    """Fixed public failure; no input, SQL, or private record is included."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class OriginalOrderLaunchAnchor:
    project_id: str
    order_id: str
    outbox_id: str
    order_task_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_sha256: str
    claim_event_id: str
    completion_event_id: str
    event_chain_head_sha256: str


@dataclass(frozen=True, repr=False)
class OriginalEmployeeOrderPins:
    """Original JSON carrier, not FrozenEmployeePins or a runtime grant.

    Consumers still need restore_employee_pins and independent historical and
    current authorization. Original human references can be private; do not log.
    """

    anchor: OriginalOrderLaunchAnchor
    pins_bytes: bytes
    pins_fingerprint: str
    pins_event_id: str


class _ReadBudget:
    def __init__(self) -> None:
        self.bytes = 0
        self.nodes = 0

    def charge_bytes(self, count: int) -> None:
        if count > _MAX_TOTAL_BYTES - self.bytes:
            raise OrderLaunchAnchorError("read_limit")
        self.bytes += count

    def charge_node(self) -> None:
        if self.nodes >= _MAX_JSON_NODES:
            raise OrderLaunchAnchorError("read_limit")
        self.nodes += 1


def _identity(value: Any, *, empty: bool = False, scope: bool = False) -> str:
    code = "invalid_scope" if scope else "invalid_source"
    if (type(value) is not str or (not value and not empty)
            or value != value.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise OrderLaunchAnchorError(code)
    size = None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        pass
    if size is None or size > _MAX_ID_BYTES:
        raise OrderLaunchAnchorError(code)
    return value


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], limit: int) -> list[Any]:
    cursor = conn.execute(sql, params)
    primary_failure = False
    try:
        return cursor.fetchmany(limit)
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            cursor.close()
        except BaseException:
            if not primary_failure:
                raise


def _sizes(metadata: Any, columns: tuple[tuple[str, int], ...], budget: _ReadBudget) -> tuple[int, ...]:
    if len(metadata) != len(columns) * 2:
        raise OrderLaunchAnchorError("invalid_source")
    sizes = []
    for index, (_, maximum) in enumerate(columns):
        kind, size = metadata[index * 2], metadata[index * 2 + 1]
        if kind != "text" or type(size) is not int or size < 0:
            raise OrderLaunchAnchorError("invalid_source")
        if size > maximum:
            raise OrderLaunchAnchorError("read_limit")
        sizes.append(size)
    budget.charge_bytes(sum(sizes))
    return tuple(sizes)


def _metadata_select(columns: tuple[tuple[str, int], ...]) -> str:
    # All identifiers originate exclusively from this module's fixed tuples.
    return ", ".join(f"typeof({name}), length(CAST({name} AS BLOB))" for name, _ in columns)


def _bounded_select(columns: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(
        f"CASE WHEN typeof({name}) = 'text' AND length(CAST({name} AS BLOB)) <= {maximum} "
        f"THEN {name} ELSE NULL END" for name, maximum in columns
    )


def _texts(row: Any, sizes: tuple[int, ...]) -> tuple[str, ...]:
    if len(row) != len(sizes):
        raise OrderLaunchAnchorError("invalid_source")
    result = []
    for value, size in zip(row, sizes):
        if type(value) is not str or len(value.encode("utf-8")) != size:
            raise OrderLaunchAnchorError("invalid_source")
        result.append(value)
    return tuple(result)


def _one_record(
    conn: sqlite3.Connection,
    *,
    table: str,
    selector: str,
    params: tuple[Any, ...],
    columns: tuple[tuple[str, int], ...],
    budget: _ReadBudget,
) -> tuple[str, ...]:
    metadata = _rows(conn, f"SELECT {_metadata_select(columns)} FROM {table} WHERE {selector} LIMIT 2", params, 2)
    if not metadata:
        raise OrderLaunchAnchorError("unavailable")
    if len(metadata) != 1:
        raise OrderLaunchAnchorError("invalid_source")
    sizes = _sizes(metadata[0], columns, budget)
    rows = _rows(conn, f"SELECT {_bounded_select(columns)} FROM {table} WHERE {selector} LIMIT 1", params, 1)
    if len(rows) != 1:
        raise OrderLaunchAnchorError("invalid_source")
    return _texts(rows[0], sizes)


def _check_depth(raw: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise OrderLaunchAnchorError("read_limit")
        elif char in "]}":
            depth -= 1


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrderLaunchAnchorError("invalid_source")
        result[key] = value
    return result


def _nonfinite_constant(_: str) -> None:
    raise OrderLaunchAnchorError("invalid_source")


def _json_dict(raw: str, budget: _ReadBudget) -> dict[str, Any]:
    _check_depth(raw)
    value = None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_nonfinite_constant)
    except OrderLaunchAnchorError:
        raise
    except (ValueError, RecursionError):
        pass
    if type(value) is not dict:
        raise OrderLaunchAnchorError("invalid_source")
    pending = [value]
    while pending:
        item = pending.pop()
        budget.charge_node()
        if type(item) is dict:
            for key, child in item.items():
                budget.charge_node()  # Object keys are also part of the shared budget.
                pending.append(child)
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is float and not math.isfinite(item):
            raise OrderLaunchAnchorError("invalid_source")
    return value


def _canonical_pins(
    pins: Any, fingerprint: Any, *, project_id: str, workflow_id: str,
) -> tuple[bytes, str]:
    # Both arguments must come from an already strictly parsed original record.
    # This validates only source shape/integrity, not the employee pin schema.
    if (type(pins) is not dict or not 1 <= len(pins) <= 500
            or type(fingerprint) is not str or _SHA256.fullmatch(fingerprint) is None
            or any(type(node) is not str or _PIN_NODE.fullmatch(node) is None for node in pins)):
        raise OrderLaunchAnchorError("invalid_source")
    raw = json.dumps(
        pins, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(raw) > _MAX_JSON_BYTES:
        raise OrderLaunchAnchorError("read_limit")
    scope = json.dumps(
        {"project_id": project_id, "workflow_id": workflow_id},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    expected = hashlib.sha256(b"ai-employee-pins:v1\0" + scope + b"\0" + raw).hexdigest()
    if fingerprint != expected:
        raise OrderLaunchAnchorError("invalid_source")
    return raw, fingerprint  # Retain the actually read fingerprint, never synthesize a missing one.


_EVENT_COLUMNS = (
    ("order_id", _MAX_SCALAR_BYTES), ("at", _MAX_SCALAR_BYTES),
    ("kind", _MAX_SCALAR_BYTES), ("actor", _MAX_SCALAR_BYTES),
    ("note_json", _MAX_JSON_BYTES), ("event_id", _MAX_SCALAR_BYTES),
    ("correlation_id", _MAX_SCALAR_BYTES), ("causation_id", _MAX_SCALAR_BYTES),
    ("previous_hash", _MAX_SCALAR_BYTES), ("event_hash", _MAX_SCALAR_BYTES),
)


def _read_events(
    conn: sqlite3.Connection, *, order_id: str, outbox_id: str,
    payload_sha: str, budget: _ReadBudget,
    pins_source: tuple[bytes, str] | None = None,
    project_id: str = "", workflow_id: str = "", workflow_version: str = "",
) -> tuple[str, str, str, str, str]:
    previous_hash = ""
    previous_id = 0
    count = 0
    seen_event_ids: set[str] = set()
    claim: tuple[int, str] | None = None
    completion: tuple[int, str, str] | None = None
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
            # Metadata is charged before reading this one bounded text record.
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
            if pins_source is not None and causation == outbox_id and kind == "employee_run.pinned":
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
            if causation != outbox_id or kind not in ("launching", "launched"):
                continue
            if (correlation != order_id or note.get("outbox_id") != outbox_id
                    or type(note.get("anchor_version")) is not int or note["anchor_version"] != 1
                    or note.get("launch_payload_sha256") != payload_sha):
                raise OrderLaunchAnchorError("invalid_source")
            if kind == "launching":
                if claim is not None:
                    raise OrderLaunchAnchorError("invalid_source")
                claim = (event_number, event_id)
            else:
                if completion is not None:
                    raise OrderLaunchAnchorError("invalid_source")
                completion = (event_number, event_id, _identity(note.get("order_task_id")))
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            cursor.close()
        except BaseException:
            if not primary_failure:
                raise
    if claim is None or completion is None:
        raise OrderLaunchAnchorError("unavailable")
    if claim[0] >= completion[0]:
        raise OrderLaunchAnchorError("invalid_source")
    if pins_source is not None:
        if pins_event is None:
            raise OrderLaunchAnchorError("unavailable")
        if not claim[0] < pins_event[0] < completion[0]:
            raise OrderLaunchAnchorError("invalid_source")
    return completion[2], claim[1], completion[1], previous_hash, pins_event[1] if pins_event else ""


def _read_anchor(
    conn: sqlite3.Connection, *, project_id: str, order_id: str, outbox_id: str,
    include_pins: bool = False,
) -> OriginalOrderLaunchAnchor | OriginalEmployeeOrderPins:
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
    fields_raw, = _one_record(
        conn, table="work_orders", selector="order_id = ?", params=(order_id,),
        columns=(("fields_json", _MAX_JSON_BYTES),), budget=budget,
    )
    fields = _json_dict(fields_raw, budget)
    if type(fields.get("project_id")) is not str or fields["project_id"] != project_id:
        raise OrderLaunchAnchorError("invalid_source")
    del fields, fields_raw

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
            or event_type != "workflow_launch_requested" or status != "published"):
        raise OrderLaunchAnchorError("invalid_source")
    payload = _json_dict(payload_raw, budget)
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("command") != "workflow.launch"
            or type(payload.get("order_id")) is not str or payload["order_id"] != order_id
            or type(payload.get("project_id")) is not str or payload["project_id"] != project_id):
        raise OrderLaunchAnchorError("invalid_source")
    workflow_id = _identity(payload.get("workflow_id"))
    workflow_version = _identity(payload.get("workflow_version"), empty=True)
    payload_sha = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
    pins_source = None
    if include_pins:
        if "employee_pins" not in payload or (type(payload["employee_pins"]) is dict and not payload["employee_pins"]):
            raise OrderLaunchAnchorError("unavailable")
        _identity(workflow_version)  # New source requires the explicit nonempty original version.
        pins_source = _canonical_pins(
            payload["employee_pins"], payload.get("employee_pins_fingerprint"),
            project_id=project_id, workflow_id=workflow_id,
        )
    del payload, payload_raw
    task_id, claim_id, completion_id, head_sha, pins_event_id = _read_events(
        conn, order_id=order_id, outbox_id=outbox_id, payload_sha=payload_sha, budget=budget,
        pins_source=pins_source, project_id=project_id, workflow_id=workflow_id,
        workflow_version=workflow_version,
    )
    anchor = OriginalOrderLaunchAnchor(
        project_id=project_id, order_id=order_id, outbox_id=outbox_id,
        order_task_id=task_id, workflow_id=workflow_id, workflow_version=workflow_version,
        launch_payload_sha256=payload_sha, claim_event_id=claim_id, completion_event_id=completion_id,
        event_chain_head_sha256=head_sha,
    )
    if pins_source is not None:
        return OriginalEmployeeOrderPins(
            anchor=anchor, pins_bytes=pins_source[0], pins_fingerprint=pins_source[1],
            pins_event_id=pins_event_id,
        )
    return anchor


def read_original_order_launch_anchor(
    conn: sqlite3.Connection, *, project_id: str, order_id: str, outbox_id: str,
) -> OriginalOrderLaunchAnchor:
    """Observe a complete local correlation; never infer readiness or authority."""
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    failed = False
    result = None
    try:
        result = _read_anchor(conn, project_id=project_id, order_id=order_id, outbox_id=outbox_id)
    except OrderLaunchAnchorError:
        raise
    except Exception:
        # Raise outside this handler so private database/parser failures are not
        # chained into the public exception. BaseException is deliberately not caught.
        failed = True
    if failed:
        raise OrderLaunchAnchorError("unavailable") from None
    assert result is not None
    return result


def read_original_employee_order_pins(
    conn: sqlite3.Connection, *, project_id: str, order_id: str, outbox_id: str,
) -> OriginalEmployeeOrderPins:
    """Read original pins once, not current instances or permission to execute."""
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    failed = False
    result = None
    try:
        result = _read_anchor(
            conn, project_id=project_id, order_id=order_id, outbox_id=outbox_id, include_pins=True,
        )
    except OrderLaunchAnchorError:
        raise
    except Exception:
        # Keep ordinary private failures outside the public exception context.
        # Cancellation and all other BaseException subclasses propagate.
        failed = True
    if failed:
        raise OrderLaunchAnchorError("unavailable") from None
    assert type(result) is OriginalEmployeeOrderPins
    return result
