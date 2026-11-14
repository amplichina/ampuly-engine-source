"""Private original launch headers for a future Stop writer, never permission.

Only read the caller's existing native transaction. No connection, transaction,
query_only change, Stop schema, write, completion inference or execution grant.
The returned head is an observation unless an independently retained expected
head was found after the matching claim in the fully verified same-snapshot chain.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import sqlite3

from .employee_order_launch_anchor import (
    OrderLaunchAnchorError, _ReadBudget, _identity, _one_record, _json_dict,
    _rows, _MAX_SCALAR_BYTES, _MAX_JSON_BYTES, _SHA256,
    _MAX_TOTAL_BYTES, _MAX_JSON_NODES,
)
from .employee_order_claim_pins import _read_claim_events
from .order_launch_claim_source import _legacy_payload, _canonical


_CODES = frozenset((
    "invalid_scope", "transaction_required", "read_limit", "invalid_source", "unavailable",
))
_TABLES = ("work_orders", "business_outbox", "order_events")
_MAX_COLUMNS = 64
_MAX_SCHEMA_BYTES = 65536
_REQUIRED = {
    "work_orders": (
        ("order_id", "TEXT", 1), ("workflow_id", "TEXT", 0),
        ("status", "TEXT", 0), ("fields_json", "TEXT", 0),
        ("launched_task_id", "TEXT", 0),
    ),
    "business_outbox": (
        ("sequence", "INTEGER", 1), ("outbox_id", "TEXT", 0),
        ("aggregate_type", "TEXT", 0), ("aggregate_id", "TEXT", 0),
        ("event_type", "TEXT", 0), ("status", "TEXT", 0),
        ("payload_json", "TEXT", 0),
    ),
    "order_events": (
        ("id", "INTEGER", 1), ("order_id", "TEXT", 0),
        ("at", "TEXT", 0), ("kind", "TEXT", 0), ("actor", "TEXT", 0),
        ("note_json", "TEXT", 0), ("event_id", "TEXT", 0),
        ("correlation_id", "TEXT", 0), ("causation_id", "TEXT", 0),
        ("previous_hash", "TEXT", 0), ("event_hash", "TEXT", 0),
    ),
}
# All identifiers are local constants. Unknown additive column values are not selected.
_COLUMN_SQL = {
    "work_orders": """SELECT cid,
        CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB)) BETWEEN 1 AND 256
             THEN name ELSE NULL END,
        CASE WHEN typeof(type)='text' AND length(CAST(type AS BLOB))<=128
             THEN type ELSE NULL END, pk
        FROM pragma_table_info('work_orders','main') LIMIT 65""",
    "business_outbox": """SELECT cid,
        CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB)) BETWEEN 1 AND 256
             THEN name ELSE NULL END,
        CASE WHEN typeof(type)='text' AND length(CAST(type AS BLOB))<=128
             THEN type ELSE NULL END, pk
        FROM pragma_table_info('business_outbox','main') LIMIT 65""",
    "order_events": """SELECT cid,
        CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB)) BETWEEN 1 AND 256
             THEN name ELSE NULL END,
        CASE WHEN typeof(type)='text' AND length(CAST(type AS BLOB))<=128
             THEN type ELSE NULL END, pk
        FROM pragma_table_info('order_events','main') LIMIT 65""",
}
_WORK_COLUMNS = (
    ("order_id", _MAX_SCALAR_BYTES), ("workflow_id", _MAX_SCALAR_BYTES),
    ("status", _MAX_SCALAR_BYTES), ("launched_task_id", _MAX_SCALAR_BYTES),
    ("fields_json", _MAX_JSON_BYTES),
)
_OUTBOX_COLUMNS = (
    ("outbox_id", _MAX_SCALAR_BYTES), ("aggregate_type", _MAX_SCALAR_BYTES),
    ("aggregate_id", _MAX_SCALAR_BYTES), ("event_type", _MAX_SCALAR_BYTES),
    ("status", _MAX_SCALAR_BYTES), ("payload_json", _MAX_JSON_BYTES),
)


@dataclass(frozen=True, repr=False)
class LaunchStopSourceHeaders:
    project_id: str
    order_id: str
    outbox_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_sha256: str
    claim_event_id: str
    event_chain_head_sha256: str


def _expected_headers(value: LaunchStopSourceHeaders | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not LaunchStopSourceHeaders:
        raise OrderLaunchAnchorError("invalid_source")
    values = (
        _identity(value.project_id), _identity(value.order_id), _identity(value.outbox_id),
        _identity(value.workflow_id), _identity(value.workflow_version, empty=True),
        value.launch_payload_sha256, _identity(value.claim_event_id), value.event_chain_head_sha256,
    )
    for digest in (values[5], values[7]):
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise OrderLaunchAnchorError("invalid_source")
    return values


def _transaction(conn: sqlite3.Connection) -> None:
    if type(conn) is not sqlite3.Connection:
        raise OrderLaunchAnchorError("transaction_required")
    if not conn.in_transaction:
        raise OrderLaunchAnchorError("transaction_required")
    if (conn.row_factory is not None and conn.row_factory is not sqlite3.Row) or conn.text_factory is not str:
        raise OrderLaunchAnchorError("invalid_source")


def _seal_schema(conn: sqlite3.Connection) -> None:
    # Do not select attached database filenames. Three rows detect any attachment.
    databases = _rows(conn, "SELECT seq,name FROM pragma_database_list LIMIT 3", (), 3)
    if not 1 <= len(databases) <= 2:
        raise OrderLaunchAnchorError("invalid_source")
    observed = set()
    for row in databases:
        if (len(row) != 2 or type(row[0]) is not int or type(row[1]) is not str
                or (row[0], row[1]) not in ((0, "main"), (1, "temp"))
                or row[1] in observed):
            raise OrderLaunchAnchorError("invalid_source")
        observed.add(row[1])
    if "main" not in observed:
        raise OrderLaunchAnchorError("invalid_source")
    # The original chain core uses unqualified order_events. Any source-name or
    # source-table-associated TEMP object is rejected before invoking that core.
    shadows = _rows(conn, """SELECT 1 FROM temp.sqlite_schema
        WHERE name IN ('work_orders','business_outbox','order_events')
           OR tbl_name IN ('work_orders','business_outbox','order_events') LIMIT 1""", (), 1)
    if shadows:
        raise OrderLaunchAnchorError("invalid_source")
    for table in _TABLES:
        metadata = _rows(conn, """SELECT type,name,tbl_name,typeof(sql),length(CAST(sql AS BLOB)),
            substr(CAST(sql AS BLOB),1,64) FROM main.sqlite_schema WHERE name=? LIMIT 2""", (table,), 2)
        if not metadata:
            raise OrderLaunchAnchorError("unavailable")
        if len(metadata) != 1 or len(metadata[0]) != 6:
            raise OrderLaunchAnchorError("invalid_source")
        kind, name, associated, sql_kind, sql_bytes, prefix = metadata[0]
        if (kind != "table" or name != table or associated != table or sql_kind != "text"
                or type(sql_bytes) is not int or sql_bytes <= 0 or type(prefix) is not bytes):
            raise OrderLaunchAnchorError("invalid_source")
        if sql_bytes > _MAX_SCHEMA_BYTES:
            raise OrderLaunchAnchorError("read_limit")
        if len(prefix) != min(sql_bytes, 64) or re.match(rb"(?i)\ACREATE\s+TABLE\s+", prefix.lstrip()) is None:
            raise OrderLaunchAnchorError("invalid_source")  # No view/virtual-table producer.
        columns = _rows(conn, _COLUMN_SQL[table], (), _MAX_COLUMNS + 1)
        if len(columns) > _MAX_COLUMNS:
            raise OrderLaunchAnchorError("read_limit")
        by_name = {}
        cids = set()
        primary = 0
        for row in columns:
            if len(row) != 4:
                raise OrderLaunchAnchorError("invalid_source")
            cid, column, declaration, pk = row
            if (type(cid) is not int or not 0 <= cid < _MAX_COLUMNS or cid in cids
                    or type(column) is not str or type(declaration) is not str
                    or column in by_name or type(pk) is not int or pk not in (0, 1)):
                raise OrderLaunchAnchorError("invalid_source")
            cids.add(cid)
            by_name[column] = (declaration, pk)
            primary += pk
        if primary != 1 or any(by_name.get(column) != (declaration, pk)
                              for column, declaration, pk in _REQUIRED[table]):
            raise OrderLaunchAnchorError("invalid_source")


def _event_position(conn: sqlite3.Connection, sql: str, params: tuple[str, str]) -> int:
    rows = _rows(conn, sql, params, 2)
    if (len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not int
            or not 0 < rows[0][0] <= 2**63 - 1):
        raise OrderLaunchAnchorError("invalid_source")
    return rows[0][0]


def _read_source(
    conn: sqlite3.Connection, *, project_id: str, order_id: str,
    outbox_id: str, expected: LaunchStopSourceHeaders | None,
    _budget: _ReadBudget | None = None,
) -> LaunchStopSourceHeaders:
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    expected_values = _expected_headers(expected)
    if expected_values is not None and expected_values[:3] != (project_id, order_id, outbox_id):
        raise OrderLaunchAnchorError("invalid_source")
    if _budget is not None:
        if type(_budget) is not _ReadBudget:
            raise OrderLaunchAnchorError("invalid_source")
        bytes_used = getattr(_budget, "bytes", None)
        nodes_used = getattr(_budget, "nodes", None)
        if (type(bytes_used) is not int or type(nodes_used) is not int
                or not 0 <= bytes_used <= _MAX_TOTAL_BYTES
                or not 0 <= nodes_used <= _MAX_JSON_NODES):
            raise OrderLaunchAnchorError("invalid_source")
    _transaction(conn)
    _seal_schema(conn)
    budget = _ReadBudget() if _budget is None else _budget
    stored_order, workflow_id, status, launched_task, fields_raw = _one_record(
        conn, table="main.work_orders", selector="order_id = ?", params=(order_id,),
        columns=_WORK_COLUMNS, budget=budget,
    )
    if stored_order != order_id or _identity(workflow_id) != workflow_id:
        raise OrderLaunchAnchorError("invalid_source")
    if status != "launching" or launched_task != "":
        raise OrderLaunchAnchorError("unavailable")
    current_fields = _json_dict(fields_raw, budget)
    if type(current_fields.get("project_id")) is not str or current_fields["project_id"] != project_id:
        raise OrderLaunchAnchorError("invalid_source")
    stored_outbox, aggregate_type, aggregate_id, event_type, delivery_status, raw = _one_record(
        conn, table="main.business_outbox", selector="outbox_id = ?", params=(outbox_id,),
        columns=_OUTBOX_COLUMNS, budget=budget,
    )
    if (stored_outbox != outbox_id or aggregate_type != "work_order" or aggregate_id != order_id
            or event_type != "workflow_launch_requested"):
        raise OrderLaunchAnchorError("invalid_source")
    if delivery_status not in ("pending", "inflight", "outcome_unknown"):
        raise OrderLaunchAnchorError("unavailable")
    payload = _json_dict(raw, budget)
    employee_pin_source = None
    if type(payload.get("employee_pins")) is dict and payload["employee_pins"]:
        from .employee_launch_source import validate_employee_payload
        try:
            version, original_fields, _, employee_snapshot = validate_employee_payload(
                payload, project_id=project_id, order_id=order_id, workflow_id=workflow_id)
        except Exception:
            # A malformed employee payload is a bad source, not an unavailable read.
            raise OrderLaunchAnchorError("invalid_source") from None
        employee_pin_source = (employee_snapshot.to_bytes(), employee_snapshot.fingerprint)
    else:
        version, original_fields, _ = _legacy_payload(
            payload, project_id=project_id, order_id=order_id, workflow_id=workflow_id,
        )
    if _canonical(current_fields) != _canonical(original_fields):
        raise OrderLaunchAnchorError("invalid_source")
    payload_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    claim_id, pins_id, current_head = _read_claim_events(
        conn, order_id=order_id, outbox_id=outbox_id, payload_sha=payload_sha,
        budget=budget, pins_source=employee_pin_source, project_id=project_id,
        workflow_id=workflow_id, workflow_version=version,
    )
    claim_id = _identity(claim_id)
    if ((employee_pin_source is None and pins_id != "")
            or (employee_pin_source is not None and not pins_id)
            or type(current_head) is not str or _SHA256.fullmatch(current_head) is None):
        raise OrderLaunchAnchorError("invalid_source")
    seven = (project_id, order_id, outbox_id, workflow_id, version, payload_sha, claim_id)
    selected_head = current_head
    if expected_values is not None:
        if seven != expected_values[:7]:
            raise OrderLaunchAnchorError("invalid_source")
        claim_position = _event_position(conn,
            "SELECT id FROM main.order_events WHERE order_id=? AND event_id=? LIMIT 2",
            (order_id, claim_id))
        head_position = _event_position(conn,
            "SELECT id FROM main.order_events WHERE order_id=? AND event_hash=? LIMIT 2",
            (order_id, expected_values[7]))
        if head_position < claim_position:
            raise OrderLaunchAnchorError("invalid_source")
        selected_head = expected_values[7]
    _transaction(conn)
    return LaunchStopSourceHeaders(
        project_id=project_id, order_id=order_id, outbox_id=outbox_id,
        workflow_id=workflow_id, workflow_version=version,
        launch_payload_sha256=payload_sha, claim_event_id=claim_id,
        event_chain_head_sha256=selected_head,
    )


def read_launch_stop_source_headers(
    conn: sqlite3.Connection, *, project_id: str, order_id: str,
    outbox_id: str, expected: LaunchStopSourceHeaders | None = None,
    _budget: _ReadBudget | None = None,
) -> LaunchStopSourceHeaders:
    """Read one original legacy launch source, never current policy/Stop approval.

    expected is an independently supplied header expectation, not a capability.
    No expected means this observation's complete chain head, not an original Run
    pin. Caller owns the same transaction/connection throughout; no retry/fallback.
    Original cursor-finally cleanup failures retain their original Python limits.
    """
    failure = None
    result = None
    try:
        result = _read_source(conn, project_id=project_id, order_id=order_id,
                              outbox_id=outbox_id, expected=expected,
                              **({"_budget": _budget} if _budget is not None else {}))
    except OrderLaunchAnchorError as exc:
        failure = exc.code if (type(exc) is OrderLaunchAnchorError and type(exc.code) is str
                               and exc.code in _CODES) else "unavailable"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        # Outside this handler; the caller separately owns transaction cleanup.
        # No private parser/SQL failure is attached. BaseException not caught.
        raise OrderLaunchAnchorError(failure) from None
    return result
