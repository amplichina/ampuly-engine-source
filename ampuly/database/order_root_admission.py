"""Private controller-fenced first-Root source records, never an execution permit.

This leaf reads/extends one caller-owned native transaction. It does not connect,
install schema, begin/commit/rollback/close, start a Task, verify host permissions,
Stop or budgets, or replace the Store's original async active-effect fence.
An expectation/prepared/fact is constructible, not an authority capability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import sqlite3

from . import order_task_origin as _task
from . import order_run_origin as _run
from . import order_root_origin as _root
from . import order_root_task_binding as _binding


TABLE_NAME = "order_root_admission_facts_v1"
FINGERPRINT_VERSION = "first_owned_root_admission.v1"
SOURCE_KIND = "first_owned_root_controller_admission"
SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_INT = 2**63 - 1
FACT_COLUMNS = (
    "fact_id", "project_id", "session_id", "source_task_id",
    "source_task_fact_id", "run_id", "run_creation_fact_id", "root_work_item_id",
    "root_creation_fact_id", "runtime_task_id", "root_task_binding_fact_id",
    "claim_attempt_seq", "controller_lease_generation", "controller_owner_sha256",
    "accepted_at", "source_kind", "record_sha256", "record_bytes",
)
_RECORD_COLUMNS = FACT_COLUMNS[:16]
_DIGEST_COLUMNS = frozenset({
    "fact_id", "source_task_fact_id", "run_creation_fact_id", "root_creation_fact_id",
    "root_task_binding_fact_id", "controller_owner_sha256", "record_sha256",
})
_INTEGER_COLUMNS = frozenset({"claim_attempt_seq", "controller_lease_generation"})
_MESSAGES = {
    "invalid_source": "原订单首 Root 接纳来源无法核验。",
    "unavailable": "原订单首 Root 接纳来源暂不可用。",
    "conflict": "原订单首 Root 接纳来源记录已存在。",
    "outcome_unknown": "原订单首 Root 接纳来源记录未确认。",
}


class OrderRootAdmissionError(ValueError):
    """Only fixed code/message, with no private diagnostics or source values."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, slots=True, repr=False)
class FirstOwnedRootAdmissionExpectation:
    """Selectors and comparison expectations, not permissions or source proof."""

    project_id: str
    session_id: str
    source_task_id: str
    run_id: str
    root_work_item_id: str
    runtime_task_id: str
    root_task_binding_fact_id: str
    claim_attempt_seq: int
    controller_lease_generation: int
    controller_owner_token: str
    role_runtime_session_id: str
    seat_id: str
    expected_origin: _task.OrderTaskOrigin


@dataclass(frozen=True, slots=True, repr=False)
class PreparedFirstOwnedRootAdmission:
    """A rechecked association; append must independently read it again."""

    expectation: FirstOwnedRootAdmissionExpectation
    task_creation_fact: _task.OrderTaskCreationFact
    run_creation_fact: _run.OrderRunCreationFact
    root_creation_fact: _root.OrderRootCreationFact
    root_task_binding_fact: _binding.OrderRootTaskBindingFact


@dataclass(frozen=True, slots=True, repr=False)
class FirstOwnedRootAdmissionFact:
    """Retained source record, not commit confirmation or worker admission."""

    fact_id: str
    project_id: str
    session_id: str
    source_task_id: str
    source_task_fact_id: str
    run_id: str
    run_creation_fact_id: str
    root_work_item_id: str
    root_creation_fact_id: str
    runtime_task_id: str
    root_task_binding_fact_id: str
    claim_attempt_seq: int
    controller_lease_generation: int
    controller_owner_sha256: str
    accepted_at: str
    source_kind: str
    record_sha256: str
    record_bytes: bytes


def _column_definition(name: str) -> str:
    if name in _DIGEST_COLUMNS:
        return _task._digest_column(name)
    if name in _INTEGER_COLUMNS:
        if name == "claim_attempt_seq":
            return f"{name} INTEGER NOT NULL CHECK(typeof({name})='integer' AND {name}=1)"
        return (f"{name} INTEGER NOT NULL CHECK(typeof({name})='integer' "
                f"AND {name} BETWEEN 1 AND {MAX_INT})")
    if name == "record_bytes":
        return (f"{name} BLOB NOT NULL CHECK(typeof({name})='blob' "
                f"AND length({name}) BETWEEN 1 AND {MAX_RECORD_BYTES})")
    definition = _task._identity_column(name)
    if name == "source_kind":
        definition += f" CHECK({name}='{SOURCE_KIND}')"
    if name == "accepted_at":
        definition += f" CHECK(length(CAST({name} AS BLOB)) BETWEEN 1 AND 128)"
    return definition


SCHEMA_STATEMENTS = (
    "CREATE TABLE " + TABLE_NAME + " (\n    "
    + ",\n    ".join(_column_definition(name) for name in FACT_COLUMNS)
    + ",\n    PRIMARY KEY (fact_id),\n    UNIQUE (root_task_binding_fact_id),"
    + "\n    UNIQUE (run_id),\n    UNIQUE (runtime_task_id)\n)",
    f"CREATE TRIGGER {TABLE_NAME}_no_update BEFORE UPDATE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root admission records are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_delete BEFORE DELETE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root admission records are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_duplicate BEFORE INSERT ON {TABLE_NAME}\n"
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_NAME} WHERE fact_id=NEW.fact_id "
    "OR root_task_binding_fact_id=NEW.root_task_binding_fact_id "
    "OR run_id=NEW.run_id OR runtime_task_id=NEW.runtime_task_id)\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root admission record conflict');\nEND",
)
_SCHEMA_OBJECTS = (("table", TABLE_NAME), ("trigger", TABLE_NAME + "_no_update"),
                   ("trigger", TABLE_NAME + "_no_delete"),
                   ("trigger", TABLE_NAME + "_no_duplicate"))


def _fail(code: str = "invalid_source") -> None:
    raise OrderRootAdmissionError(code) from None


def _failure_code(error: Exception) -> str:
    if type(error) in (OrderRootAdmissionError, _task.OrderTaskOriginError,
                       _run.OrderRunOriginError, _root.OrderRootOriginError,
                       _binding.OrderRootTaskBindingError):
        code = error.code
        return code if type(code) is str and code in _MESSAGES else "unavailable"
    return "unavailable"


def _integer(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INT:
        _fail()
    return value


def _text(value: object, *, limit: int = 256) -> str:
    result = _task._text(value)
    if len(result) > limit or len(result.encode("utf-8")) > limit:
        _fail()
    return result


def _expectation(value: object) -> FirstOwnedRootAdmissionExpectation:
    if type(value) is not FirstOwnedRootAdmissionExpectation:
        _fail()
    for name in ("project_id", "session_id", "source_task_id", "run_id",
                 "root_work_item_id", "runtime_task_id", "controller_owner_token",
                 "role_runtime_session_id", "seat_id"):
        _text(getattr(value, name))
    _task._digest(value.root_task_binding_fact_id)
    _integer(value.claim_attempt_seq)
    _integer(value.controller_lease_generation)
    _task._project_id(value.project_id)
    if (value.source_task_id == value.runtime_task_id
            or value.claim_attempt_seq != 1):
        _fail()
    _task.validate_order_task_origin(value.expected_origin, project_id=value.project_id)
    return value


def validate_first_owned_root_admission_expectation(
    expectation: FirstOwnedRootAdmissionExpectation,
) -> FirstOwnedRootAdmissionExpectation:
    failure = None
    result = None
    try:
        result = _expectation(expectation)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        _fail(failure)
    return result


def _rows(conn: sqlite3.Connection, sql: str, params: tuple, *, width: int,
          maximum: int = 2) -> list[tuple]:
    cursor = conn.execute(sql, params)
    try:
        rows = cursor.fetchmany(maximum + 1)
        if (type(rows) is not list or len(rows) > maximum
                or any(type(row) is not tuple or len(row) != width for row in rows)):
            _fail()
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()
        return rows


def _native_row(conn: sqlite3.Connection, *, table: str, expressions: tuple[str, ...],
                specs: tuple[tuple[str, int], ...], where: str, params: tuple,
                optional: bool = False) -> tuple | None:
    """All identifiers/expressions come from fixed private call sites below."""
    if table not in {"store_identity", "tasks", "delegation_runs",
                     "delegation_work_items", "work_item_runtime_links", TABLE_NAME}:
        _fail()
    if len(expressions) != len(specs):
        _fail()
    measures = ", ".join(f"typeof({value}),length(CAST({value} AS BLOB))" for value in expressions)
    metadata = _rows(conn, f"SELECT {measures} FROM {table} WHERE {where} LIMIT 2",
                     params, width=2 * len(expressions))
    if not metadata:
        if optional:
            return None
        _fail()
    if len(metadata) != 1:
        _fail()
    sizes = []
    kinds = []
    for index, (expected, limit) in enumerate(specs):
        kind, size = metadata[0][2 * index:2 * index + 2]
        if expected == "null":
            if kind != "null" or size is not None:
                _fail()
        elif expected == "nullable_text" and kind == "null" and size is None:
            pass
        elif (kind != ("text" if expected == "nullable_text" else expected)
              or type(size) is not int or not 0 <= size <= limit):
            _fail()
        sizes.append(size)
        kinds.append(kind)
    selections = []
    for expression, (expected, limit), kind in zip(expressions, specs, kinds):
        if kind == "null" or expected == "integer":
            selections.append(expression)
        else:
            selections.append(f"CASE WHEN length(CAST({expression} AS BLOB))=0 "
                              f"THEN CAST({expression} AS BLOB) ELSE "
                              f"substr(CAST({expression} AS BLOB),1,{limit + 1}) END")
    rows = _rows(conn, f"SELECT {', '.join(selections)} FROM {table} WHERE {where} LIMIT 2",
                 params, width=len(expressions))
    if len(rows) != 1:
        _fail()
    result = []
    for raw, size, kind in zip(rows[0], sizes, kinds):
        if kind == "null":
            if raw is not None:
                _fail()
            result.append(None)
        elif kind == "integer":
            if type(raw) is not int or len(str(raw).encode("ascii")) != size:
                _fail()
            result.append(raw)
        else:
            if type(raw) is not bytes or len(raw) != size:
                _fail()
            result.append(raw if kind == "blob" else _task._decode(raw))
    return tuple(result)


def _schema(conn: sqlite3.Connection) -> None:
    for (kind, name), statement in zip(_SCHEMA_OBJECTS, SCHEMA_STATEMENTS):
        measures = _rows(conn, "SELECT typeof(sql),length(CAST(sql AS BLOB)) "
                         "FROM sqlite_master WHERE type=? AND name=? LIMIT 2",
                         (kind, name), width=2)
        if not measures:
            _fail("unavailable" if kind == "table" else "invalid_source")
        if (len(measures) != 1 or measures[0][0] != "text"
                or type(measures[0][1]) is not int
                or not 0 < measures[0][1] <= _task._MAX_SCHEMA_BYTES):
            _fail()
        actual = _rows(conn, "SELECT substr(CAST(sql AS BLOB),1,?) "
                       "FROM sqlite_master WHERE type=? AND name=? LIMIT 2",
                       (_task._MAX_SCHEMA_BYTES + 1, kind, name), width=1)
        if (len(actual) != 1 or type(actual[0][0]) is not bytes
                or len(actual[0][0]) != measures[0][1]
                or _task._normalized_schema(_task._decode(actual[0][0]))
                != _task._normalized_schema(statement)):
            _fail()
    measures = _rows(conn, "SELECT typeof(name),length(CAST(name AS BLOB)) "
                     "FROM sqlite_master WHERE tbl_name=? ORDER BY name LIMIT 10",
                     (TABLE_NAME,), width=2, maximum=9)
    if any(kind != "text" or type(size) is not int or not 0 < size <= 512
           for kind, size in measures):
        _fail()
    names = _rows(conn, "SELECT name FROM sqlite_master WHERE tbl_name=? "
                  "ORDER BY name LIMIT 10", (TABLE_NAME,), width=1, maximum=9)
    expected_names = {name for _, name in _SCHEMA_OBJECTS} | {
        "sqlite_autoindex_" + TABLE_NAME + "_" + str(index) for index in range(1, 5)
    }
    if (len(names) != len(measures) or len(names) != len(expected_names)
            or any(type(row[0]) is not str for row in names)
            or {row[0] for row in names} != expected_names):
        _fail()


def _connection(conn: sqlite3.Connection, *, write: bool) -> None:
    _task._connection(conn, write=write)
    # Original source readers use fixed but unqualified table names. A fresh
    # owned main connection is required; no TEMP/attached source substitution.
    measures = _rows(conn, "SELECT typeof(name),length(CAST(name AS BLOB)) "
                     "FROM pragma_database_list LIMIT 4", (), width=2, maximum=3)
    if (not measures or any(kind != "text" or type(size) is not int or size != 4
                            for kind, size in measures)):
        _fail()
    names = _rows(conn, "SELECT substr(CAST(name AS BLOB),1,5) "
                  "FROM pragma_database_list LIMIT 4", (), width=1, maximum=3)
    if (len(names) != len(measures) or any(type(row[0]) is not bytes for row in names)
            or len({row[0] for row in names}) != len(names)
            or b"main" not in {row[0] for row in names}
            or any(row[0] not in {b"main", b"temp"} for row in names)):
        _fail()
    if _rows(conn, "SELECT 1 FROM sqlite_temp_master LIMIT 1", (), width=1):
        _fail()


def _chain(conn: sqlite3.Connection, *, project_id: str, session_id: str,
           source_task_id: str, run_id: str, root_work_item_id: str,
           runtime_task_id: str, root_task_binding_fact_id: str,
           expected_origin: _task.OrderTaskOrigin | None = None) -> tuple:
    task = _task.read_order_task_creation(conn, project_id=project_id, task_id=source_task_id)
    if task is None:
        _fail("unavailable")
    if type(task) is not _task.OrderTaskCreationFact:
        _fail()
    first = _run.read_order_first_run_creation(conn, project_id=project_id, source_task_id=source_task_id)
    run = _run.read_order_run_creation(conn, project_id=project_id, run_id=run_id)
    root = _root.read_order_root_creation(conn, project_id=project_id, run_id=run_id)
    binding = _binding.read_order_root_task_binding(conn, project_id=project_id, run_id=run_id)
    if first is None or run is None or root is None or binding is None:
        _fail("unavailable")
    if (type(first) is not _run.OrderRunCreationFact or type(run) is not _run.OrderRunCreationFact
            or type(root) is not _root.OrderRootCreationFact
            or type(binding) is not _binding.OrderRootTaskBindingFact
            or first != run or task.task_id != source_task_id
            or run.project_id != project_id or run.session_id != session_id
            or run.run_id != run_id or run.source_task_id != source_task_id
            or run.source_task_fact_id != task.fact_id
            or root.project_id != project_id or root.session_id != session_id
            or root.source_task_id != source_task_id or root.source_task_fact_id != task.fact_id
            or root.run_id != run_id or root.run_creation_fact_id != run.fact_id
            or root.root_work_item_id != root_work_item_id
            or binding.fact_id != root_task_binding_fact_id
            or binding.root_creation_fact_id != root.fact_id or binding.project_id != project_id
            or binding.run_id != run_id or binding.root_work_item_id != root_work_item_id
            or binding.runtime_task_id != runtime_task_id or binding.runtime_session_id != session_id
            or binding.materialization_kind != _binding.MATERIALIZATION_KIND
            or expected_origin is not None and task.origin != expected_origin):
        _fail()
    return task, run, root, binding


def _current(conn: sqlite3.Connection, value: FirstOwnedRootAdmissionExpectation) -> None:
    text = ("text", 256)
    integer = ("integer", 20)
    identity = _native_row(conn, table="store_identity", expressions=("scope", "project_id"),
                           specs=(text, text), where="id=1", params=())
    source_task = _native_row(conn, table="tasks", expressions=("id", "project_id", "session_id"),
                              specs=(text, text, text), where="id=?", params=(value.source_task_id,))
    runtime_task = _native_row(conn, table="tasks",
        expressions=("id", "project_id", "session_id", "parent_session_id"),
        specs=(text, text, text, text), where="id=?", params=(value.runtime_task_id,))
    run = _native_row(conn, table="delegation_runs",
        expressions=("run_id", "project_id", "session_id", "controller_owner_token",
                     "controller_lease_generation", "controller_lease_expires_at"),
        specs=(text, text, text, text, integer, ("text", 128)),
        where="run_id=?", params=(value.run_id,))
    if (identity != ("project", value.project_id)
            or source_task != (value.source_task_id, value.project_id, value.session_id)
            or runtime_task != (value.runtime_task_id, value.project_id, value.session_id, value.session_id)
            or run[:4] != (value.run_id, value.project_id, value.session_id, value.controller_owner_token)
            or type(run[4]) is not int or run[4] != value.controller_lease_generation):
        _fail()
    _integer(run[4])
    _text(run[3])
    _text(run[5], limit=128)
    # The original lease producer uses naive local datetime.now().isoformat().
    # A bounded string is not evidence of a date or an unexpired lease. Reject
    # offsets/non-canonical forms; accepted_at is never compared to this lease.
    malformed_expiry = False
    expires_at = None
    try:
        expires_at = datetime.fromisoformat(run[5])
    except ValueError:
        malformed_expiry = True
    if (malformed_expiry or expires_at.tzinfo is not None
            or expires_at.isoformat() != run[5]):
        _fail()
    checked_at = datetime.now()
    if expires_at <= checked_at:
        _fail("conflict")
    item = _native_row(conn, table="delegation_work_items",
        expressions=("work_item_id", "run_id", "kind", "parent_work_item_id", "phase",
                     "role_runtime_session_id", "claimed_by_role_runtime_session_id",
                     "seat_id", "claimed_by_seat_id"),
        specs=(text, text, text, ("null", 0), text, text, text, text, text),
        where="work_item_id=?", params=(value.root_work_item_id,))
    if item != (value.root_work_item_id, value.run_id, "intake", None, "running",
                value.role_runtime_session_id, value.role_runtime_session_id,
                value.seat_id, value.seat_id):
        _fail("conflict")
    for key, selected in (("work_item_id", value.root_work_item_id),
                          ("runtime_task_id", value.runtime_task_id)):
        link = _native_row(conn, table="work_item_runtime_links",
            expressions=("work_item_id", "runtime_task_id", "link_kind"),
            specs=(text, text, text), where=key + "=?", params=(selected,))
        if link != (value.root_work_item_id, value.runtime_task_id, "primary"):
            _fail()
    # Check metadata size before invoking SQLite's JSON parser. Do not transport
    # the JSON object, Task inputs or arbitrary metadata into Python.
    metadata = _rows(conn, "SELECT typeof(metadata),length(CAST(metadata AS BLOB)) "
                     "FROM delegation_work_items WHERE work_item_id=? LIMIT 2",
                     (value.root_work_item_id,), width=2)
    if (len(metadata) != 1 or metadata[0][0] != "text" or type(metadata[0][1]) is not int
            or not 1 <= metadata[0][1] <= MAX_METADATA_BYTES):
        _fail()
    valid = _rows(conn, "SELECT json_valid(metadata) FROM delegation_work_items "
                  "WHERE work_item_id=? LIMIT 2", (value.root_work_item_id,), width=1)
    if valid != [(1,)]:
        _fail()
    types = _rows(conn, "SELECT json_type(metadata),json_type(metadata,'$.attempt_seq'),"
                  "json_type(metadata,'$.attempt_settled'),json_type(metadata,'$.claimed_task_id'),"
                  "json_type(metadata,'$.claimed_by_role_session_id'),"
                  "json_type(metadata,'$.company_run_controller_owner_token'),"
                  "json_type(metadata,'$.company_run_controller_lease_generation'),"
                  "json_type(metadata,'$.dispatch_hold') FROM delegation_work_items "
                  "WHERE work_item_id=? LIMIT 2", (value.root_work_item_id,), width=8)
    if (len(types) != 1 or types[0][:7] != (
            "object", "integer", "false", "text", "text", "text", "integer")
            or types[0][7] not in (None, "text")):
        _fail()
    epoch = _native_row(conn, table="delegation_work_items",
        expressions=("json_extract(metadata,'$.attempt_seq')", "json_extract(metadata,'$.claimed_task_id')",
                     "json_extract(metadata,'$.claimed_by_role_session_id')",
                     "json_extract(metadata,'$.company_run_controller_owner_token')",
                     "json_extract(metadata,'$.company_run_controller_lease_generation')",
                     "json_extract(metadata,'$.dispatch_hold')"),
        specs=(integer, text, text, text, integer, ("nullable_text", 256)),
        where="work_item_id=?", params=(value.root_work_item_id,))
    if (epoch[:5] != (value.claim_attempt_seq, value.runtime_task_id,
                     value.role_runtime_session_id, value.controller_owner_token,
                     value.controller_lease_generation)
            or epoch[5] not in (None, "")):
        _fail("conflict")
    if _integer(epoch[0]) != 1:
        _fail("conflict")
    _integer(epoch[4])


def _prepare(conn: sqlite3.Connection, expectation: FirstOwnedRootAdmissionExpectation) -> PreparedFirstOwnedRootAdmission:
    value = _expectation(expectation)
    _connection(conn, write=True)
    _schema(conn)
    facts = _chain(conn, project_id=value.project_id, session_id=value.session_id,
                   source_task_id=value.source_task_id, run_id=value.run_id,
                   root_work_item_id=value.root_work_item_id, runtime_task_id=value.runtime_task_id,
                   root_task_binding_fact_id=value.root_task_binding_fact_id,
                   expected_origin=value.expected_origin)
    _current(conn, value)
    return PreparedFirstOwnedRootAdmission(value, *facts)


def prepare_first_owned_root_admission(
    conn: sqlite3.Connection, *, expectation: FirstOwnedRootAdmissionExpectation,
) -> PreparedFirstOwnedRootAdmission:
    failure = None
    result = None
    try:
        result = _prepare(conn, expectation)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        _fail(failure)
    return result


def _canonical(value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(raw) <= MAX_RECORD_BYTES:
        _fail()
    return raw


def _fact_identity(*, project_id: str, source_task_fact_id: str, run_creation_fact_id: str,
                   root_creation_fact_id: str, root_task_binding_fact_id: str,
                   runtime_task_id: str, claim_attempt_seq: int) -> str:
    return hashlib.sha256(_canonical([
        FINGERPRINT_VERSION, project_id, source_task_fact_id, run_creation_fact_id,
        root_creation_fact_id, root_task_binding_fact_id, runtime_task_id, claim_attempt_seq,
    ])).hexdigest()


def _record(values: tuple, origin: _task.OrderTaskOrigin) -> bytes:
    record = dict(zip(_RECORD_COLUMNS, values))
    record["schema_version"] = SCHEMA_VERSION
    record["origin"] = {name: getattr(origin, name) for name in _task.SOURCE_FIELDS}
    return _canonical(record)


def _append(conn: sqlite3.Connection, prepared: PreparedFirstOwnedRootAdmission,
            accepted_at: str) -> FirstOwnedRootAdmissionFact:
    if type(prepared) is not PreparedFirstOwnedRootAdmission:
        _fail()
    _text(accepted_at, limit=128)
    current = _prepare(conn, prepared.expectation)
    if current != prepared:
        _fail()
    value = current.expectation
    fact_id = _fact_identity(project_id=value.project_id,
        source_task_fact_id=current.task_creation_fact.fact_id,
        run_creation_fact_id=current.run_creation_fact.fact_id,
        root_creation_fact_id=current.root_creation_fact.fact_id,
        root_task_binding_fact_id=current.root_task_binding_fact.fact_id,
        runtime_task_id=value.runtime_task_id, claim_attempt_seq=value.claim_attempt_seq)
    for column, selected in (("fact_id", fact_id), ("root_task_binding_fact_id", value.root_task_binding_fact_id),
                             ("run_id", value.run_id), ("runtime_task_id", value.runtime_task_id)):
        if _rows(conn, f"SELECT 1 FROM {TABLE_NAME} WHERE {column}=? LIMIT 2",
                 (selected,), width=1):
            _fail("conflict")
    values = (fact_id, value.project_id, value.session_id, value.source_task_id,
              current.task_creation_fact.fact_id, value.run_id, current.run_creation_fact.fact_id,
              value.root_work_item_id, current.root_creation_fact.fact_id, value.runtime_task_id,
              current.root_task_binding_fact.fact_id, value.claim_attempt_seq,
              value.controller_lease_generation,
              hashlib.sha256(value.controller_owner_token.encode("utf-8")).hexdigest(),
              accepted_at, SOURCE_KIND)
    raw = _record(values, current.task_creation_fact.origin)
    complete = (*values, hashlib.sha256(raw).hexdigest(), raw)
    insert_failed = False
    cursor = None
    try:
        cursor = conn.execute(f"INSERT INTO {TABLE_NAME} ({', '.join(FACT_COLUMNS)}) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", complete)
    except sqlite3.IntegrityError:
        insert_failed = True
    if insert_failed:
        _fail("conflict")
    try:
        if type(cursor.rowcount) is not int or cursor.rowcount != 1:
            _fail("unavailable")
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()
    return FirstOwnedRootAdmissionFact(*complete)


def append_first_owned_root_admission(
    conn: sqlite3.Connection, *, prepared: PreparedFirstOwnedRootAdmission,
    accepted_at: str,
) -> FirstOwnedRootAdmissionFact:
    """Strict append only; Store must separately fence and confirm its commit."""
    failure = None
    result = None
    try:
        result = _append(conn, prepared, accepted_at)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        _fail(failure)
    return result


def _read(conn: sqlite3.Connection, *, project_id: str,
          root_task_binding_fact_id: str) -> FirstOwnedRootAdmissionFact | None:
    _text(project_id)
    _task._digest(root_task_binding_fact_id)
    _connection(conn, write=False)
    _task._project_connection(conn, project_id)
    _schema(conn)
    specs = tuple(("blob", MAX_RECORD_BYTES) if name == "record_bytes" else
                  ("integer", 20) if name in _INTEGER_COLUMNS else
                  ("text", 128) if name == "accepted_at" else ("text", 256)
                  for name in FACT_COLUMNS)
    row = _native_row(conn, table=TABLE_NAME, expressions=FACT_COLUMNS, specs=specs,
                      where="root_task_binding_fact_id=?", params=(root_task_binding_fact_id,), optional=True)
    if row is None:
        return None
    fact = FirstOwnedRootAdmissionFact(*row)
    for name in FACT_COLUMNS[:-1]:
        value = getattr(fact, name)
        if name in _DIGEST_COLUMNS:
            _task._digest(value)
        elif name in _INTEGER_COLUMNS:
            _integer(value)
        else:
            _text(value, limit=128 if name == "accepted_at" else 256)
    if (fact.project_id != project_id or fact.root_task_binding_fact_id != root_task_binding_fact_id
            or fact.source_kind != SOURCE_KIND or fact.source_task_id == fact.runtime_task_id
            or fact.claim_attempt_seq != 1):
        _fail()
    facts = _chain(conn, project_id=project_id, session_id=fact.session_id,
        source_task_id=fact.source_task_id, run_id=fact.run_id,
        root_work_item_id=fact.root_work_item_id, runtime_task_id=fact.runtime_task_id,
        root_task_binding_fact_id=fact.root_task_binding_fact_id)
    if ((fact.source_task_fact_id, fact.run_creation_fact_id, fact.root_creation_fact_id)
            != (facts[0].fact_id, facts[1].fact_id, facts[2].fact_id)):
        _fail()
    expected_id = _fact_identity(project_id=project_id, source_task_fact_id=fact.source_task_fact_id,
        run_creation_fact_id=fact.run_creation_fact_id, root_creation_fact_id=fact.root_creation_fact_id,
        root_task_binding_fact_id=fact.root_task_binding_fact_id, runtime_task_id=fact.runtime_task_id,
        claim_attempt_seq=fact.claim_attempt_seq)
    expected_raw = _record(tuple(getattr(fact, name) for name in _RECORD_COLUMNS), facts[0].origin)
    if (fact.fact_id != expected_id or fact.record_bytes != expected_raw
            or fact.record_sha256 != hashlib.sha256(fact.record_bytes).hexdigest()):
        _fail()
    return fact


def read_first_owned_root_admission(
    conn: sqlite3.Connection, *, project_id: str, root_task_binding_fact_id: str,
) -> FirstOwnedRootAdmissionFact | None:
    """Retained-only read; no current entity/Registry/policy check or gate opening."""
    failure = None
    result = None
    try:
        result = _read(conn, project_id=project_id, root_task_binding_fact_id=root_task_binding_fact_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        _fail(failure)
    return result
