"""Private first-Root runtime Task bindings; reference DDL, never installed here.

The integrated strict Task/link/fact transaction establishes a new binding, not
permission, admission, a worker gate, a frozen node source or an employee Run.
Retained facts survive entity deletion; current reads cannot repair old links.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3

from ..core.models import DelegationWorkItem, Phase, Task
from ..layer2_organization.work_item_identity import work_item_projection_id_from_metadata
from ..layer2_organization.work_item_runtime_invariants import is_company_runtime_projection_task
from . import order_task_origin as _task
from . import order_run_origin as _run
from . import order_root_origin as _root
from . import atomic_work_item_claim as _owned


TABLE_NAME = "order_root_task_binding_facts_v1"
FACT_COLUMNS = (
    "fact_id", "root_creation_fact_id", "project_id", "run_id",
    "root_work_item_id", "runtime_task_id", "runtime_session_id", "materialization_kind",
)
FINGERPRINT_VERSION = "order_root_task_binding.v1"
MATERIALIZATION_KIND = "created_task_new_binding"
_MAX_ROW_BYTES = 2 * 1024 * 1024
_MESSAGES = {
    "invalid_source": "原订单 Root Task 绑定来源无法核验。",
    "unavailable": "原订单 Root Task 绑定来源暂不可用。",
    "conflict": "原订单 Root Task 绑定事实已存在。",
    "outcome_unknown": "原订单 Root Task 绑定结果未确认。",
}


class OrderRootTaskBindingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class OrderRootTaskBindingFact:
    fact_id: str
    root_creation_fact_id: str
    project_id: str
    run_id: str
    root_work_item_id: str
    runtime_task_id: str
    runtime_session_id: str
    materialization_kind: str


_COLUMN_DEFINITIONS = (
    _task._digest_column("fact_id"), _task._digest_column("root_creation_fact_id"),
    _task._identity_column("project_id"), _task._identity_column("run_id"),
    _task._identity_column("root_work_item_id"), _task._identity_column("runtime_task_id"),
    _task._identity_column("runtime_session_id"), _task._identity_column("materialization_kind"),
)
_SCHEMA_OBJECTS = (("table", TABLE_NAME), ("trigger", TABLE_NAME + "_no_update"),
                   ("trigger", TABLE_NAME + "_no_delete"), ("trigger", TABLE_NAME + "_no_duplicate"))
SCHEMA_STATEMENTS = (
    "CREATE TABLE " + TABLE_NAME + " (\n    " + ",\n    ".join(_COLUMN_DEFINITIONS)
    + ",\n    PRIMARY KEY (fact_id),\n    UNIQUE (root_creation_fact_id),\n    UNIQUE (run_id),"
    + "\n    UNIQUE (root_work_item_id),\n    UNIQUE (runtime_task_id)\n)",
    f"CREATE TRIGGER {TABLE_NAME}_no_update BEFORE UPDATE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root Task bindings are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_delete BEFORE DELETE ON {TABLE_NAME}\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root Task bindings are append-only');\nEND",
    f"CREATE TRIGGER {TABLE_NAME}_no_duplicate BEFORE INSERT ON {TABLE_NAME}\n"
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_NAME} WHERE fact_id = NEW.fact_id "
    "OR root_creation_fact_id = NEW.root_creation_fact_id OR run_id = NEW.run_id "
    "OR root_work_item_id = NEW.root_work_item_id OR runtime_task_id = NEW.runtime_task_id)\n"
    "BEGIN\n    SELECT RAISE(ABORT, 'private Root Task binding conflict');\nEND",
)
LINK_INSERT_SQL = """INSERT INTO work_item_runtime_links
                (work_item_id, runtime_task_id, link_kind, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)"""
_WORK_ITEM_COLUMNS = (
    "work_item_id", "run_id", "cell_id", "team_instance_id", "team_id", "role_id",
    "seat_id", "seat_state_id", "role_runtime_session_id", "parent_work_item_id",
    "source_role_id", "source_seat_id", "title", "summary", "kind", "projection_id",
    "phase", "batch_id", "batch_index", "deliverable_summary", "blocked_reason",
    "handoff_status", "continuation_source", "manager_role_id", "manager_seat_id",
    "claimed_by_role_runtime_session_id", "claimed_by_seat_id", "metadata", "created_at", "updated_at",
)
_TASK_COLUMNS = (
    "id", "session_id", "parent_session_id", "title", "description", "assigned_to",
    "status", "priority", "dependencies", "execution_lock", "context_snapshot",
    "assigned_external_agent", "created_at", "deadline", "result", "parent_id", "project_id",
    "tags", "comments", "retry_count", "max_retries", "metadata", "org_id", "goal_id",
    "checkout_run_id", "execution_locked_at",
)


def _failure_code(error: Exception) -> str:
    if type(error) in (OrderRootTaskBindingError, _root.OrderRootOriginError,
                       _run.OrderRunOriginError, _task.OrderTaskOriginError):
        return error.code if type(error.code) is str and error.code in _MESSAGES else "unavailable"
    if isinstance(error, sqlite3.IntegrityError):
        return "conflict"
    return "unavailable"


def validate_order_root_task_selector(*, project_id: str, session_id: str,
                                      source_task_id: str, work_item: DelegationWorkItem) -> None:
    failed = False
    try:
        if type(work_item) is not DelegationWorkItem:
            raise OrderRootTaskBindingError("invalid_source")
        _run.validate_order_run_source(project_id=project_id, session_id=session_id,
                                       source_task_id=source_task_id, run_id=work_item.run_id)
        if (type(work_item.kind) is not str
                or work_item.kind != "intake" or work_item.parent_work_item_id is not None
                or type(work_item.metadata) is not dict):
            raise OrderRootTaskBindingError("invalid_source")
        _task._text(work_item.work_item_id)
    except Exception:
        failed = True
    if failed:
        raise OrderRootTaskBindingError("invalid_source") from None


def _schema(conn: sqlite3.Connection) -> None:
    for (kind, name), expected in zip(_SCHEMA_OBJECTS, SCHEMA_STATEMENTS):
        metadata = _task._rows(conn, "SELECT typeof(sql), length(CAST(sql AS BLOB)) FROM sqlite_master "
                               "WHERE type = ? AND name = ? LIMIT 2", (kind, name))
        if not metadata:
            raise OrderRootTaskBindingError("unavailable" if kind == "table" else "invalid_source")
        if (len(metadata) != 1 or len(metadata[0]) != 2 or metadata[0][0] != "text"
                or type(metadata[0][1]) is not int or not 0 < metadata[0][1] <= _task._MAX_SCHEMA_BYTES):
            raise OrderRootTaskBindingError("invalid_source")
        rows = _task._rows(conn, "SELECT substr(CAST(sql AS BLOB), 1, ?) FROM sqlite_master "
                           "WHERE type = ? AND name = ? LIMIT 2", (_task._MAX_SCHEMA_BYTES + 1, kind, name))
        if (len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not bytes
                or len(rows[0][0]) != metadata[0][1]
                or _task._normalized_schema(_task._decode(rows[0][0])) != _task._normalized_schema(expected)):
            raise OrderRootTaskBindingError("invalid_source")


def _values(fact: OrderRootTaskBindingFact) -> tuple[str, ...]:
    if type(fact) is not OrderRootTaskBindingFact:
        raise OrderRootTaskBindingError("invalid_source")
    values = (fact.root_creation_fact_id, fact.project_id, fact.run_id, fact.root_work_item_id,
              fact.runtime_task_id, fact.runtime_session_id, fact.materialization_kind)
    _task._digest(fact.fact_id)
    _task._digest(values[0])
    for value in values[1:]:
        _task._text(value)
    _task._project_id(fact.project_id)
    if fact.materialization_kind != MATERIALIZATION_KIND:
        raise OrderRootTaskBindingError("invalid_source")
    return values


def _fingerprint(values: tuple[str, ...]) -> str:
    raw = json.dumps([FINGERPRINT_VERSION, *values], ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _retained(conn: sqlite3.Connection, *, project_id: str, run_id: str) -> OrderRootTaskBindingFact | None:
    project_id, run_id = _task._text(project_id), _task._text(run_id)
    _task._connection(conn, write=False)
    _task._project_connection(conn, project_id)
    _schema(conn)
    row = _run._record(conn, table=TABLE_NAME, columns=FACT_COLUMNS, key="run_id", value=run_id)
    if row is None:
        return None
    fact = OrderRootTaskBindingFact(*row)
    values = _values(fact)
    if fact.project_id != project_id or fact.run_id != run_id or fact.fact_id != _fingerprint(values):
        raise OrderRootTaskBindingError("invalid_source")
    root = _root.read_order_root_creation(conn, project_id=project_id, run_id=run_id)
    if (type(root) is not _root.OrderRootCreationFact or root.fact_id != fact.root_creation_fact_id
            or root.root_work_item_id != fact.root_work_item_id or root.session_id != fact.runtime_session_id):
        raise OrderRootTaskBindingError("invalid_source")
    return fact


def read_order_root_task_binding(conn: sqlite3.Connection, *, project_id: str,
                                 run_id: str) -> OrderRootTaskBindingFact | None:
    """Retained chain, not a current entity guarantee, repair or permission."""
    result = None
    failure = None
    try:
        result = _retained(conn, project_id=project_id, run_id=run_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRootTaskBindingError(failure) from None
    return result


def _current_source(conn: sqlite3.Connection, *, project_id: str, session_id: str,
                    source_task_id: str, work_item: DelegationWorkItem) -> _root.OrderRootCreationFact:
    _schema(conn)
    root = _root.read_order_root_creation(conn, project_id=project_id, run_id=work_item.run_id)
    if root is None:
        raise OrderRootTaskBindingError("unavailable")
    if (type(root) is not _root.OrderRootCreationFact or root.root_work_item_id != work_item.work_item_id
            or root.project_id != project_id or root.session_id != session_id or root.source_task_id != source_task_id):
        raise OrderRootTaskBindingError("invalid_source")
    identity = _run._record(conn, table="store_identity", columns=("scope", "project_id"), key="id", value=1)
    task = _run._record(conn, table="tasks", columns=("id", "project_id", "session_id"), key="id", value=source_task_id)
    run = _run._record(conn, table="delegation_runs", columns=("run_id", "project_id", "session_id"),
                       key="run_id", value=work_item.run_id)
    if identity != ("project", project_id) or task != (source_task_id, project_id, session_id) or run != (work_item.run_id, project_id, session_id):
        raise OrderRootTaskBindingError("invalid_source")
    return root


def _bounded_row(conn: sqlite3.Connection, *, table: str, columns: tuple[str, ...],
                  key: str, value: str) -> tuple | None:
    # Fixed identifiers only. Sizes are checked before carrying original DTO data.
    checks = ", ".join(f"typeof({column}), length(CAST({column} AS BLOB))" for column in columns)
    metadata = _task._rows(conn, f"SELECT {checks} FROM {table} WHERE {key} = ? LIMIT 2", (value,))
    if not metadata:
        return None
    if len(metadata) != 1 or len(metadata[0]) != len(columns) * 2:
        raise OrderRootTaskBindingError("invalid_source")
    total = 0
    for offset in range(0, len(metadata[0]), 2):
        kind, size = metadata[0][offset:offset + 2]
        if kind == "null" and size is None:
            continue
        if kind not in ("text", "integer") or type(size) is not int or size < 0:
            raise OrderRootTaskBindingError("invalid_source")
        total += size
        if total > _MAX_ROW_BYTES:
            raise OrderRootTaskBindingError("invalid_source")
    rows = _task._rows(conn, f"SELECT {', '.join(columns)} FROM {table} WHERE {key} = ? LIMIT 2", (value,))
    if len(rows) != 1 or len(rows[0]) != len(columns):
        raise OrderRootTaskBindingError("invalid_source")
    return rows[0]


def _links(conn: sqlite3.Connection, *, wid: str, task_id: str | None = None) -> tuple[str, str] | None:
    forward = _run._record(conn, table="work_item_runtime_links",
                          columns=("work_item_id", "runtime_task_id", "link_kind"), key="work_item_id", value=wid)
    if forward is None:
        if task_id is not None:
            reverse = _run._record(conn, table="work_item_runtime_links",
                                  columns=("work_item_id", "runtime_task_id", "link_kind"), key="runtime_task_id", value=task_id)
            if reverse is not None:
                raise OrderRootTaskBindingError("invalid_source")
        return None
    if forward[0] != wid or forward[2] != "primary" or (task_id is not None and forward[1] != task_id):
        raise OrderRootTaskBindingError("invalid_source")
    _task._text(forward[1])
    reverse = _run._record(conn, table="work_item_runtime_links",
                          columns=("work_item_id", "runtime_task_id", "link_kind"), key="runtime_task_id", value=forward[1])
    if reverse != forward:
        raise OrderRootTaskBindingError("invalid_source")
    return forward[0], forward[1]


def _same_work_item(persisted: DelegationWorkItem, supplied: DelegationWorkItem,
                    projection_id_for_work_item, canonical_turn_type) -> None:
    if (type(persisted) is not DelegationWorkItem or persisted.work_item_id != supplied.work_item_id
            or persisted.run_id != supplied.run_id or persisted.kind != "intake"
            or persisted.parent_work_item_id is not None or type(persisted.metadata) is not dict):
        raise OrderRootTaskBindingError("invalid_source")
    left, right = projection_id_for_work_item(persisted), projection_id_for_work_item(supplied)
    if (not left or left != right
            or canonical_turn_type(persisted.kind, fallback="") != canonical_turn_type(supplied.kind, fallback="")
            or not canonical_turn_type(persisted.kind, fallback="")
            or persisted.role_id != supplied.role_id or persisted.seat_id != supplied.seat_id
            or persisted.role_runtime_session_id != supplied.role_runtime_session_id):
        raise OrderRootTaskBindingError("invalid_source")


def _task_scope(task: Task, *, project_id: str, session_id: str) -> None:
    if type(task) is not Task or task.project_id != project_id or task.session_id != session_id or task.parent_session_id != session_id or type(task.metadata) is not dict:
        raise OrderRootTaskBindingError("invalid_source")
    _task._text(task.id)


def _current_projection(task, persisted, projection_id_for_work_item, validate_projection) -> None:
    if (not is_company_runtime_projection_task(task)
            or not work_item_projection_id_from_metadata(task.metadata, fallback="")
            or work_item_projection_id_from_metadata(task.metadata, fallback="") != projection_id_for_work_item(persisted)):
        raise OrderRootTaskBindingError("invalid_source")
    if any(issue.severity == "error" for issue in validate_projection(task, persisted)):
        raise OrderRootTaskBindingError("invalid_source")


def _projection_json(value):
    """Finite plain JSON for the three compatibility projections, not a codec grant."""
    remaining = [50000]

    def check(node, depth, parents):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 64:
            raise OrderRootTaskBindingError("invalid_source")
        if type(node) in (dict, list):
            size = len(node)
            if size > remaining[0] or id(node) in parents:
                raise OrderRootTaskBindingError("invalid_source")
            snapshot = tuple(node.items()) if type(node) is dict else tuple(node)
            if len(snapshot) != size or len(node) != size:
                raise OrderRootTaskBindingError("invalid_source")
            for index in range(size):
                if len(node) != size:
                    raise OrderRootTaskBindingError("invalid_source")
                if type(node) is dict:
                    key, child = snapshot[index]
                    if type(key) is not str:
                        raise OrderRootTaskBindingError("invalid_source")
                else:
                    child = snapshot[index]
                check(child, depth + 1, parents + (id(node),))
            if len(node) != size:
                raise OrderRootTaskBindingError("invalid_source")
        elif node is not None and type(node) not in (str, int, float, bool):
            raise OrderRootTaskBindingError("invalid_source")

    check(value, 0, ())
    raw = None
    failed = False
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > _MAX_ROW_BYTES:
            failed = True
    except Exception:
        failed = True
    if failed:
        raise OrderRootTaskBindingError("invalid_source")
    return json.loads(raw)


def _candidate_size(task: Task, json_dumps) -> None:
    # Budget only. The original _save_task_row remains the sole 26-value producer.
    total = 0
    for name in ("id", "session_id", "parent_session_id", "title", "description", "assigned_to",
                 "assigned_external_agent", "parent_id", "project_id", "org_id", "goal_id", "checkout_run_id"):
        value = getattr(task, name)
        if value is not None:
            if type(value) is not str:
                raise OrderRootTaskBindingError("invalid_source")
            total += len(value.encode("utf-8"))
    for name in ("dependencies", "context_snapshot", "result", "tags", "comments", "metadata"):
        total += len(json_dumps(getattr(task, name)).encode("utf-8"))
        if total > _MAX_ROW_BYTES - 512:
            raise OrderRootTaskBindingError("invalid_source")


def _insert_one(conn, statement, values) -> None:
    cursor = conn.execute(statement, values)
    try:
        if type(cursor.rowcount) is not int or cursor.rowcount != 1:
            raise OrderRootTaskBindingError("unavailable")
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()


async def _linked_task(conn, adapter, store, persisted, fact, *, project_id, session_id,
                        owner_token, projection_id_for_work_item, validate_projection, set_linked_work_item_id):
    if _links(conn, wid=persisted.work_item_id, task_id=fact.runtime_task_id) != (persisted.work_item_id, fact.runtime_task_id):
        raise OrderRootTaskBindingError("invalid_source")
    if _bounded_row(conn, table="tasks", columns=_TASK_COLUMNS, key="id", value=fact.runtime_task_id) is None:
        raise OrderRootTaskBindingError("invalid_source")
    # The original getter uses SELECT *. Refuse unknown columns before that read.
    count = _task._rows(conn, "SELECT COUNT(*) FROM pragma_table_info('tasks')", ())
    unknown = _task._rows(conn, "SELECT 1 FROM pragma_table_info('tasks') WHERE name NOT IN ("
                          + ", ".join("?" for _ in _TASK_COLUMNS) + ") LIMIT 2", _TASK_COLUMNS)
    if count != [(len(_TASK_COLUMNS),)] or unknown:
        raise OrderRootTaskBindingError("invalid_source")
    # Original getter/materializer, explicit new adapter; never shared self._db.
    task = await store._get_task_unhydrated(fact.runtime_task_id, db=adapter)
    _task_scope(task, project_id=project_id, session_id=session_id)
    if task.assigned_to != persisted.role_id or task.metadata.get("delegation_run_id") != persisted.run_id:
        raise OrderRootTaskBindingError("invalid_source")
    set_linked_work_item_id(task, persisted.work_item_id)
    unclaimed = not bool(persisted.claimed_by_role_runtime_session_id or persisted.claimed_by_seat_id or persisted.metadata.get("claimed_task_id", ""))
    if owner_token and unclaimed and store._task_has_company_attempt_credential(task):
        raise OrderRootTaskBindingError("invalid_source")
    _current_projection(task, persisted, projection_id_for_work_item, validate_projection)
    return task


def append_order_root_task_binding(conn: sqlite3.Connection, *, root: _root.OrderRootCreationFact,
                                   task: Task) -> OrderRootTaskBindingFact:
    """Same transaction append only; standalone use does not establish INSERT."""
    result = None
    failure = None
    try:
        _task._connection(conn, write=True)
        _schema(conn)
        if type(root) is not _root.OrderRootCreationFact:
            raise OrderRootTaskBindingError("invalid_source")
        actual = _root.read_order_root_creation(conn, project_id=root.project_id, run_id=root.run_id)
        if actual != root:
            raise OrderRootTaskBindingError("invalid_source")
        _task_scope(task, project_id=root.project_id, session_id=root.session_id)
        direct = _run._record(conn, table="tasks", columns=("id", "project_id", "session_id", "parent_session_id"), key="id", value=task.id)
        if direct != (task.id, root.project_id, root.session_id, root.session_id) or _links(conn, wid=root.root_work_item_id, task_id=task.id) != (root.root_work_item_id, task.id):
            raise OrderRootTaskBindingError("invalid_source")
        values = (_task._digest(root.fact_id), root.project_id, root.run_id, root.root_work_item_id,
                  task.id, root.session_id, MATERIALIZATION_KIND)
        fact = OrderRootTaskBindingFact(_fingerprint(values), *values)
        _values(fact)
        if _task._rows(conn, f"SELECT 1 FROM {TABLE_NAME} WHERE run_id = ? OR root_work_item_id = ? OR runtime_task_id = ? LIMIT 2", (root.run_id, root.root_work_item_id, task.id)):
            raise OrderRootTaskBindingError("conflict")
        _insert_one(conn, f"INSERT INTO {TABLE_NAME} ({', '.join(FACT_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (fact.fact_id, *values))
        result = fact
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise OrderRootTaskBindingError(failure) from None
    return result


async def ensure_first_root_task_binding(*, store, adapter_type, work_item: DelegationWorkItem,
                                         task_factory, project_id: str, session_id: str, source_task_id: str,
                                         controller_owner_token: str, controller_lease_generation: int,
                                         read_only: bool, projection_id_for_work_item, canonical_turn_type,
                                         copy_execution_metadata, validate_projection, set_linked_work_item_id,
                                         json_dumps) -> Task | None:
    """Own existing connection lifecycle. None is healthy readonly no-fact/no-link only."""
    conn = result = None
    failure = cancelled = None
    commit_started = False
    path = before = None
    try:
        validate_order_root_task_selector(project_id=project_id, session_id=session_id,
                                          source_task_id=source_task_id, work_item=work_item)
        if type(read_only) is not bool or type(controller_owner_token) is not str or type(controller_lease_generation) is not int or not 0 <= controller_lease_generation <= 2**63 - 1:
            raise OrderRootTaskBindingError("invalid_source")
        if controller_owner_token:
            _task._text(controller_owner_token)
        if bool(controller_owner_token) != (controller_lease_generation > 0):
            raise OrderRootTaskBindingError("invalid_source")
        if store._require_db() is None:
            raise OrderRootTaskBindingError("unavailable")
        if type(store.project_id) is not str or store.project_id != project_id:
            raise OrderRootTaskBindingError("invalid_source")
        if type(store.db_path) is not str or not store.db_path or len(store.db_path) > 4096 or "\x00" in store.db_path or store.db_path.strip() != store.db_path:
            raise OrderRootTaskBindingError("invalid_source")
        path = Path(store.db_path)
        if not path.is_absolute() or ".." in path.parts or path.anchor.startswith("\\\\"):
            raise OrderRootTaskBindingError("invalid_source")
        before = _owned._file_snapshot(path)
        conn = _owned._connect_existing(path)
        if _owned._file_snapshot(path) != before:
            raise OrderRootTaskBindingError("unavailable")
        if read_only:
            conn.execute("PRAGMA query_only=ON").close()
            conn.execute("BEGIN").close()
        else:
            conn.execute("BEGIN IMMEDIATE").close()
        root = _current_source(conn, project_id=project_id, session_id=session_id,
                               source_task_id=source_task_id, work_item=work_item)
        row = _bounded_row(conn, table="delegation_work_items", columns=_WORK_ITEM_COLUMNS,
                           key="work_item_id", value=work_item.work_item_id)
        if row is None:
            raise OrderRootTaskBindingError("invalid_source")
        description = tuple((column,) for column in _WORK_ITEM_COLUMNS)
        persisted = store._row_to_delegation_work_item(row, description)
        _same_work_item(persisted, work_item, projection_id_for_work_item, canonical_turn_type)
        adapter = adapter_type.__new__(adapter_type)
        adapter._conn = conn
        await store._require_company_controller_record_write(
            adapter, run_id=persisted.run_id, project_id=project_id,
            owner_token=controller_owner_token, generation=controller_lease_generation,
            checked_at=datetime.now(), work_item_id=persisted.work_item_id,
        )
        fact = _retained(conn, project_id=project_id, run_id=work_item.run_id)
        if fact is not None:
            if fact.root_creation_fact_id != root.fact_id or fact.root_work_item_id != persisted.work_item_id or fact.runtime_session_id != session_id:
                raise OrderRootTaskBindingError("invalid_source")
            result = await _linked_task(conn, adapter, store, persisted, fact,
                                        project_id=project_id, session_id=session_id,
                                        owner_token=controller_owner_token,
                                        projection_id_for_work_item=projection_id_for_work_item, validate_projection=validate_projection,
                                        set_linked_work_item_id=set_linked_work_item_id)
        else:
            if _links(conn, wid=persisted.work_item_id) is not None:
                raise OrderRootTaskBindingError("invalid_source")
            if persisted.phase is not Phase.READY or work_item.phase is not Phase.READY:
                raise OrderRootTaskBindingError("invalid_source")
            if not read_only:
                candidate = task_factory() if callable(task_factory) else task_factory
                if inspect.isawaitable(candidate):
                    if inspect.iscoroutine(candidate):
                        candidate.close()
                    raise OrderRootTaskBindingError("invalid_source")
                _task_scope(candidate, project_id=project_id, session_id=session_id)
                _candidate_size(candidate, json_dumps)
                if candidate.id == source_task_id:
                    raise OrderRootTaskBindingError("conflict")
                if candidate.metadata.get("delegation_run_id") != persisted.run_id:
                    raise OrderRootTaskBindingError("invalid_source")
                if controller_owner_token and store._task_has_company_attempt_credential(candidate):
                    raise OrderRootTaskBindingError("invalid_source")
                if _task._rows(conn, "SELECT 1 FROM tasks WHERE id = ? LIMIT 2", (candidate.id,)):
                    raise OrderRootTaskBindingError("conflict")
                if _links(conn, wid=persisted.work_item_id, task_id=candidate.id) is not None:
                    raise OrderRootTaskBindingError("conflict")
                # Original finite execution-copy merge; no source identity from metadata.
                metadata = dict(persisted.metadata or {})
                updates = {}
                execution_copy = copy_execution_metadata(work_item)
                for key, expected_type in (("employee_assignment", dict), ("employee_prompt_context", str), ("employee_delta_context", str)):
                    if key not in work_item.metadata:
                        continue
                    value = work_item.metadata[key]
                    if type(value) is not expected_type:
                        raise OrderRootTaskBindingError("invalid_source")
                    if value:
                        execution_copy[key] = _projection_json(value)
                for key, supplied_value in execution_copy.items():
                    persisted_value = metadata.get(key)
                    if persisted_value not in (None, "", [], {}):
                        if persisted_value != supplied_value:
                            raise OrderRootTaskBindingError("invalid_source")
                        continue
                    updates[key] = copy.deepcopy(supplied_value)
                if updates:
                    metadata.update(updates)
                    raw = json_dumps(metadata)
                    if len(raw.encode("utf-8")) > _MAX_ROW_BYTES:
                        raise OrderRootTaskBindingError("invalid_source")
                    persisted.metadata = metadata
                    persisted.updated_at = datetime.now()
                    _insert_one(conn, "UPDATE delegation_work_items SET metadata=?, updated_at=? WHERE work_item_id=?",
                                (raw, persisted.updated_at.isoformat(), persisted.work_item_id))
                set_linked_work_item_id(candidate, persisted.work_item_id)
                _current_projection(candidate, persisted, projection_id_for_work_item, validate_projection)
                await store._save_task_row(candidate, db=adapter, commit=False, insert_only=True)
                if _bounded_row(conn, table="tasks", columns=_TASK_COLUMNS, key="id", value=candidate.id) is None:
                    raise OrderRootTaskBindingError("invalid_source")
                stamp = datetime.now().isoformat()
                _insert_one(conn, LINK_INSERT_SQL, (persisted.work_item_id, candidate.id, "primary", stamp, stamp))
                fact = append_order_root_task_binding(conn, root=root, task=candidate)
                result = await _linked_task(conn, adapter, store, persisted, fact,
                                            project_id=project_id, session_id=session_id,
                                            owner_token=controller_owner_token,
                                            projection_id_for_work_item=projection_id_for_work_item, validate_projection=validate_projection,
                                            set_linked_work_item_id=set_linked_work_item_id)
        if result is not None and (type(result) is not Task or fact is None or result.id != fact.runtime_task_id):
            raise OrderRootTaskBindingError("invalid_source")
        if read_only:
            _owned._rollback_confirmed(conn)
        else:
            if result is None or fact is None:
                raise OrderRootTaskBindingError("invalid_source")
            commit_started = True
            _root._commit_confirmed(conn)
        if _owned._file_snapshot(path) != before:
            raise OrderRootTaskBindingError("outcome_unknown")
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
            _root._close_confirmed(conn)
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
        raise OrderRootTaskBindingError(failure) from None
    if result is None and not read_only:
        raise OrderRootTaskBindingError("outcome_unknown") from None
    return result
