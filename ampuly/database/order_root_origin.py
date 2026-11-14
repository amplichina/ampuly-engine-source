"""Private first Root creation facts; reference schema, never auto-installed.

Only strict WorkItem insertion and fact append on the same owned transaction
prove integrated creation. A fabricated carrier/helper call grants no authority.
No original workflow/node bytes, metadata or execution Task binding are added.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable

from ..core.models import DelegationWorkItem, Phase
from ..layer2_organization.phase import validate_transition
from . import order_task_origin as _task
from . import order_run_origin as _run
from . import atomic_work_item_claim as _owned


TABLE_NAME = "order_root_creation_facts_v1"
FACT_COLUMNS = (
    "fact_id", "project_id", "session_id", "source_task_id", "source_task_fact_id",
    "run_id", "run_creation_fact_id", "root_work_item_id",
)
FINGERPRINT_VERSION = "order_root_creation.v1"
_MAX_ROW_BYTES = 2 * 1024 * 1024
_MESSAGES = {
    "invalid_source": "原订单 Root 创建来源无法核验。",
    "unavailable": "原订单 Root 创建来源暂不可用。",
    "conflict": "原订单 Root 创建事实已存在。",
    "outcome_unknown": "原订单 Root 创建结果未确认。",
}


class OrderRootOriginError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class PreparedOrderRootSource:
    project_id: str
    session_id: str
    source_task_id: str
    source_task_fact_id: str
    run_id: str
    run_creation_fact_id: str
    root_work_item_id: str


@dataclass(frozen=True, repr=False)
class OrderRootCreationFact:
    fact_id: str
    project_id: str
    session_id: str
    source_task_id: str
    source_task_fact_id: str
    run_id: str
    run_creation_fact_id: str
    root_work_item_id: str


_COLUMN_DEFINITIONS = (
    _task._digest_column("fact_id"), _task._identity_column("project_id"),
    _task._identity_column("session_id"), _task._identity_column("source_task_id"),
    _task._digest_column("source_task_fact_id"), _task._identity_column("run_id"),
    _task._digest_column("run_creation_fact_id"), _task._identity_column("root_work_item_id"),
)
_SCHEMA_OBJECTS = (("table", TABLE_NAME), ("trigger", TABLE_NAME + "_no_update"),
                   ("trigger", TABLE_NAME + "_no_delete"), ("trigger", TABLE_NAME + "_no_duplicate"))
SCHEMA_STATEMENTS = (
    "CREATE TABLE " + TABLE_NAME + " (\n    " + ",\n    ".join(_COLUMN_DEFINITIONS)
    + ",\n    PRIMARY KEY (fact_id),\n    UNIQUE (run_id),\n    UNIQUE (root_work_item_id)\n)",
    f"CREATE TRIGGER {TABLE_NAME}_no_update BEFORE UPDATE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_delete BEFORE DELETE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root creation facts are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_duplicate BEFORE INSERT ON {TABLE_NAME}\n"
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_NAME} WHERE fact_id = NEW.fact_id "
    "OR run_id = NEW.run_id OR root_work_item_id = NEW.root_work_item_id)\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root creation fact conflict');\nEND",
)
# Only remove the original writer's ON CONFLICT tail, not its column/value order.
ROOT_INSERT_SQL = """INSERT INTO delegation_work_items
                (work_item_id, run_id, cell_id, team_instance_id, team_id, role_id, seat_id, seat_state_id,
                 role_runtime_session_id, parent_work_item_id, source_role_id, source_seat_id, title, summary,
                 kind, projection_id, phase, batch_id, batch_index,
                 deliverable_summary, blocked_reason, handoff_status, continuation_source, manager_role_id,
                 manager_seat_id, claimed_by_role_runtime_session_id, claimed_by_seat_id, metadata,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _failure_code(error: Exception) -> str:
    if type(error) in (OrderRootOriginError, _task.OrderTaskOriginError, _run.OrderRunOriginError):
        return error.code if type(error.code) is str and error.code in _MESSAGES else "unavailable"
    return "unavailable"


def validate_order_root_candidate(item: DelegationWorkItem, *, project_id: str,
                                  session_id: str, source_task_id: str) -> None:
    failed = False
    try:
        _run.validate_order_run_selector(project_id=project_id, session_id=session_id,
                                         source_task_id=source_task_id)
        if (type(item) is not DelegationWorkItem or item.phase is not Phase.READY
                or type(item.kind) is not str or item.kind != "intake"
                or item.parent_work_item_id is not None):
            raise OrderRootOriginError("invalid_source")
        _task._text(item.work_item_id)
        _task._text(item.run_id)
    except Exception:
        failed = True
    if failed:
        raise OrderRootOriginError("invalid_source") from None


def _schema(conn: sqlite3.Connection) -> None:
    for (kind, name), expected in zip(_SCHEMA_OBJECTS, SCHEMA_STATEMENTS):
        metadata = _task._rows(conn, "SELECT typeof(sql), length(CAST(sql AS BLOB)) FROM sqlite_master "
                               "WHERE type = ? AND name = ? LIMIT 2", (kind, name))
        if not metadata:
            raise OrderRootOriginError("unavailable" if kind == "table" else "invalid_source")
        if (len(metadata) != 1 or len(metadata[0]) != 2 or metadata[0][0] != "text"
                or type(metadata[0][1]) is not int or not 0 < metadata[0][1] <= _task._MAX_SCHEMA_BYTES):
            raise OrderRootOriginError("invalid_source")
        rows = _task._rows(conn, "SELECT substr(CAST(sql AS BLOB), 1, ?) FROM sqlite_master "
                           "WHERE type = ? AND name = ? LIMIT 2",
                           (_task._MAX_SCHEMA_BYTES + 1, kind, name))
        if (len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not bytes
                or len(rows[0][0]) != metadata[0][1]
                or _task._normalized_schema(_task._decode(rows[0][0])) != _task._normalized_schema(expected)):
            raise OrderRootOriginError("invalid_source")


def _linked_source(conn: sqlite3.Connection, *, project_id: str, session_id: str,
                   source_task_id: str, run_id: str, root_work_item_id: str,
                   current: bool) -> PreparedOrderRootSource:
    _run.validate_order_run_source(project_id=project_id, session_id=session_id,
                                   source_task_id=source_task_id, run_id=run_id)
    root_work_item_id = _task._text(root_work_item_id)
    _task._connection(conn, write=current)
    _schema(conn)
    first = _run.read_order_first_run_creation(conn, project_id=project_id, source_task_id=source_task_id)
    if first is None:
        raise OrderRootOriginError("unavailable")
    if type(first) is not _run.OrderRunCreationFact or first.run_id != run_id:
        raise OrderRootOriginError("invalid_source")
    specified = _run.read_order_run_creation(conn, project_id=project_id, run_id=run_id)
    if specified is None:
        raise OrderRootOriginError("unavailable")
    if (type(first) is not _run.OrderRunCreationFact or type(specified) is not _run.OrderRunCreationFact
            or first != specified or specified.run_id != run_id or specified.project_id != project_id
            or specified.session_id != session_id or specified.source_task_id != source_task_id):
        raise OrderRootOriginError("invalid_source")
    if current:
        identity = _run._record(conn, table="store_identity", columns=("scope", "project_id"),
                               key="id", value=1)
        task = _run._record(conn, table="tasks", columns=("id", "project_id", "session_id"),
                           key="id", value=source_task_id)
        run = _run._record(conn, table="delegation_runs", columns=("run_id", "project_id", "session_id"),
                          key="run_id", value=run_id)
        if identity != ("project", project_id) or task != (source_task_id, project_id, session_id) or run != (run_id, project_id, session_id):
            raise OrderRootOriginError("invalid_source")
    return PreparedOrderRootSource(project_id, session_id, source_task_id,
                                   _task._digest(specified.source_task_fact_id), run_id,
                                   _task._digest(specified.fact_id), root_work_item_id)


def _duplicate(conn: sqlite3.Connection, source: PreparedOrderRootSource) -> None:
    rows = _task._rows(conn, f"SELECT 1 FROM {TABLE_NAME} WHERE run_id = ? OR root_work_item_id = ? LIMIT 2",
                       (source.run_id, source.root_work_item_id))
    if rows:
        raise OrderRootOriginError("conflict")


def prepare_order_root_creation(conn: sqlite3.Connection, *, project_id: str, session_id: str,
                                source_task_id: str, run_id: str, root_work_item_id: str) -> PreparedOrderRootSource:
    result = None
    failure = None
    try:
        result = _linked_source(conn, project_id=project_id, session_id=session_id,
                                source_task_id=source_task_id, run_id=run_id,
                                root_work_item_id=root_work_item_id, current=True)
        _duplicate(conn, result)
        roots = _task._rows(conn, "SELECT 1 FROM delegation_work_items "
                            "INDEXED BY idx_delegation_work_items_run "
                            "WHERE run_id = ? AND kind = 'intake' "
                            "AND parent_work_item_id IS NULL LIMIT 2", (run_id,))
        if roots:
            raise OrderRootOriginError("conflict")
        rows = _task._rows(conn, "SELECT 1 FROM delegation_work_items WHERE work_item_id = ? LIMIT 2",
                           (root_work_item_id,))
        if rows:
            raise OrderRootOriginError("conflict")
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRootOriginError(failure) from None
    return result


def _values(source: PreparedOrderRootSource) -> tuple[str, ...]:
    if type(source) is not PreparedOrderRootSource:
        raise OrderRootOriginError("invalid_source")
    _run.validate_order_run_source(project_id=source.project_id, session_id=source.session_id,
                                   source_task_id=source.source_task_id, run_id=source.run_id)
    return (source.project_id, source.session_id, source.source_task_id,
            _task._digest(source.source_task_fact_id), source.run_id,
            _task._digest(source.run_creation_fact_id), _task._text(source.root_work_item_id))


def _fingerprint(values: tuple[str, ...]) -> str:
    raw = json.dumps([FINGERPRINT_VERSION, *values], ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def append_order_root_creation(conn: sqlite3.Connection, *, source: PreparedOrderRootSource) -> OrderRootCreationFact:
    """No lifecycle ownership; standalone append cannot establish first INSERT."""
    result = None
    failure = None
    try:
        values = _values(source)
        actual = _linked_source(conn, project_id=source.project_id, session_id=source.session_id,
                                source_task_id=source.source_task_id, run_id=source.run_id,
                                root_work_item_id=source.root_work_item_id, current=True)
        if actual != source:
            raise OrderRootOriginError("invalid_source")
        _duplicate(conn, source)
        root = _run._record(conn, table="delegation_work_items",
                            columns=("work_item_id", "run_id", "kind", "phase"),
                            key="work_item_id", value=source.root_work_item_id)
        no_parent = _task._rows(conn, "SELECT 1 FROM delegation_work_items "
                                "WHERE work_item_id = ? AND parent_work_item_id IS NULL LIMIT 2",
                                (source.root_work_item_id,))
        if root != (source.root_work_item_id, source.run_id, "intake", Phase.READY.value) or no_parent != [(1,)]:
            raise OrderRootOriginError("invalid_source")
        fact_id = _fingerprint(values)
        cursor = conn.execute(f"INSERT INTO {TABLE_NAME} ({', '.join(FACT_COLUMNS)}) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (fact_id, *values))
        try:
            if type(cursor.rowcount) is not int or cursor.rowcount != 1:
                raise OrderRootOriginError("unavailable")
        except BaseException:
            try:
                cursor.close()
            except BaseException:
                pass
            raise
        else:
            cursor.close()
        result = OrderRootCreationFact(fact_id, *values)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRootOriginError(failure) from None
    return result


def read_order_root_creation(conn: sqlite3.Connection, *, project_id: str, run_id: str) -> OrderRootCreationFact | None:
    """Read retained links, including after entity deletion; never permission."""
    result = None
    failure = None
    try:
        project_id, run_id = _task._text(project_id), _task._text(run_id)
        _task._connection(conn, write=False)
        _task._project_connection(conn, project_id)
        _schema(conn)
        row = _run._record(conn, table=TABLE_NAME, columns=FACT_COLUMNS, key="run_id", value=run_id)
        if row is not None:
            fact_id = _task._digest(row[0])
            source = PreparedOrderRootSource(*row[1:])
            values = _values(source)
            if source.project_id != project_id or source.run_id != run_id or fact_id != _fingerprint(values):
                raise OrderRootOriginError("invalid_source")
            linked = _linked_source(conn, project_id=project_id, session_id=source.session_id,
                                    source_task_id=source.source_task_id, run_id=run_id,
                                    root_work_item_id=source.root_work_item_id, current=False)
            if linked != source:
                raise OrderRootOriginError("invalid_source")
            result = OrderRootCreationFact(fact_id, *values)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRootOriginError(failure) from None
    return result


def _producer_values(item: DelegationWorkItem, insert_values: Callable) -> tuple:
    values = insert_values(item)
    if type(values) is not tuple or len(values) != 30:
        raise OrderRootOriginError("invalid_source")
    checks = ((0, item.work_item_id), (1, item.run_id), (9, None), (14, "intake"), (16, Phase.READY.value))
    for index, expected in checks:
        if type(values[index]) is not type(expected) or values[index] != expected:
            raise OrderRootOriginError("invalid_source")
    total = 0
    for value in values:
        if value is None:
            continue
        if type(value) is str:
            total += len(value.encode("utf-8"))
        elif type(value) is int:
            if not -(2**63) <= value <= 2**63 - 1:
                raise OrderRootOriginError("invalid_source")
            total += len(str(value))
        else:
            raise OrderRootOriginError("invalid_source")
        if total > _MAX_ROW_BYTES:
            raise OrderRootOriginError("invalid_source")
    return values


def _commit_confirmed(conn: sqlite3.Connection) -> None:
    if conn.commit() is not None or conn.in_transaction:
        raise OrderRootOriginError("outcome_unknown") from None


def _close_confirmed(conn: sqlite3.Connection) -> None:
    if conn.close() is not None:
        raise OrderRootOriginError("outcome_unknown") from None


def create_first_root_with_origin(*, db_path: str, project_id: str, session_id: str,
                                  source_task_id: str, item: DelegationWorkItem,
                                  insert_values: Callable, protect_identity: Callable) -> OrderRootCreationFact:
    """Existing mode-rw owned transaction, strict native INSERT plus fact; no retry."""
    conn = None
    result = None
    failure = None
    cancelled = None
    commit_started = False
    path = before = None
    try:
        validate_order_root_candidate(item, project_id=project_id, session_id=session_id,
                                      source_task_id=source_task_id)
        if not callable(insert_values) or not callable(protect_identity):
            raise OrderRootOriginError("invalid_source")
        if type(db_path) is not str or not db_path or len(db_path) > 4096 or "\x00" in db_path:
            raise OrderRootOriginError("invalid_source")
        path = Path(db_path)
        if (not path.is_absolute() or ".." in path.parts or path.anchor.startswith("\\\\")
                or db_path.strip() != db_path):
            raise OrderRootOriginError("invalid_source")
        before = _owned._file_snapshot(path)
        conn = _owned._connect_existing(path)
        if _owned._file_snapshot(path) != before:
            raise OrderRootOriginError("unavailable")
        conn.execute("BEGIN IMMEDIATE").close()
        source = prepare_order_root_creation(conn, project_id=project_id, session_id=session_id,
                                             source_task_id=source_task_id, run_id=item.run_id,
                                             root_work_item_id=item.work_item_id)
        validate_transition(None, item.phase)
        protect_identity(item, existing=None)
        validate_order_root_candidate(item, project_id=project_id, session_id=session_id,
                                      source_task_id=source_task_id)
        values = _producer_values(item, insert_values)
        if item.work_item_id != source.root_work_item_id or item.run_id != source.run_id:
            raise OrderRootOriginError("invalid_source")
        cursor = conn.execute(ROOT_INSERT_SQL, values)
        try:
            if type(cursor.rowcount) is not int or cursor.rowcount != 1:
                raise OrderRootOriginError("unavailable")
        except BaseException:
            try:
                cursor.close()
            except BaseException:
                pass
            raise
        else:
            cursor.close()
        result = append_order_root_creation(conn, source=source)
        if type(result) is not OrderRootCreationFact:
            raise OrderRootOriginError("invalid_source")
        commit_started = True
        _commit_confirmed(conn)
        if _owned._file_snapshot(path) != before:
            raise OrderRootOriginError("outcome_unknown")
    except Exception as error:
        failure = "outcome_unknown" if commit_started else _failure_code(error)
    except BaseException as error:
        cancelled = error
    if conn is not None:
        try:
            if conn.in_transaction:
                _owned._rollback_confirmed(conn)
        except BaseException as error:
            if failure is None and cancelled is None:
                if isinstance(error, Exception):
                    failure = "outcome_unknown"
                else:
                    cancelled = error
        try:
            _close_confirmed(conn)
        except BaseException as error:
            if failure is None and cancelled is None:
                if isinstance(error, Exception):
                    failure = "outcome_unknown"
                else:
                    cancelled = error
    if failure is None and cancelled is None and path is not None and before is not None:
        try:
            if _owned._file_snapshot(path) != before:
                failure = "outcome_unknown"
        except Exception:
            failure = "outcome_unknown"
        except BaseException as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled
    if failure is not None:
        raise OrderRootOriginError(failure) from None
    if not commit_started or type(result) is not OrderRootCreationFact:
        raise OrderRootOriginError("outcome_unknown") from None
    return result
