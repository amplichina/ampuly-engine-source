"""Private, owned-connection CAS receipt; not admission or source authority.

Only an already initialized project store may call this leaf.  It creates no
schema, retries no operation, and never uses or replaces the store's shared DB.
The post-commit snapshot is a guard, not a promise that a later Stop cannot win.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable

from ..core.models import DelegationWorkItem, Phase
from ..layer2_organization.phase import (
    IN_PROGRESS_PHASES,
    coerce_phase,
    validate_transition,
)
from ..layer2_organization.work_item_claim_observation import (
    WorkItemClaimExpectation,
    WorkItemClaimObservation,
    capture_work_item_claim_observation,
    prepare_work_item_claim_expectation,
)


_INVALID = "atomic work-item claim invalid"
_UNAVAILABLE = "atomic work-item claim unavailable"
_UNKNOWN = "atomic work-item claim outcome unknown"
_STALE = "atomic work-item claim no longer current"
_MAX_ROW_BYTES = 2 * 1024 * 1024
_MAX_SCOPE_BYTES = 512
_INDEXED_TABLES = {
    "store_identity": "store_identity",
    "tasks": "tasks INDEXED BY sqlite_autoindex_tasks_1",
    "delegation_runs": "delegation_runs INDEXED BY sqlite_autoindex_delegation_runs_1",
    "delegation_work_items": "delegation_work_items INDEXED BY sqlite_autoindex_delegation_work_items_1",
    "work_item_runtime_links": "work_item_runtime_links INDEXED BY sqlite_autoindex_work_item_runtime_links_1",
}
_WORK_ITEM_COLUMNS = (
    "work_item_id", "run_id", "cell_id", "team_instance_id", "team_id",
    "role_id", "seat_id", "seat_state_id", "role_runtime_session_id",
    "parent_work_item_id", "source_role_id", "source_seat_id", "title",
    "summary", "kind", "projection_id", "phase", "batch_id", "batch_index",
    "deliverable_summary", "blocked_reason", "handoff_status",
    "continuation_source", "manager_role_id", "manager_seat_id",
    "claimed_by_role_runtime_session_id", "claimed_by_seat_id", "metadata",
    "created_at", "updated_at",
)

# Byte-for-byte original claim_delegation_work_item_if_dispatchable SQL.
CLAIM_SQL = """UPDATE delegation_work_items
               SET phase = ?,
                   role_runtime_session_id = ?,
                   claimed_by_role_runtime_session_id = ?,
                   claimed_by_seat_id = ?,
                   metadata = json_set(
                       COALESCE(NULLIF(metadata, ''), '{}'),
                       '$.claimed_by_role_session_id', ?,
                       '$.claimed_task_id', ?,
                       '$.claimed_work_item_revision', ?,
                       '$.attempt_seq', COALESCE(
                           CAST(json_extract(metadata, '$.attempt_seq') AS INTEGER),
                           0
                       ) + 1,
                       '$.attempt_settled', json('false'),
                       '$.attempt_outcome', '',
                       '$.company_run_controller_owner_token', ?,
                       '$.company_run_controller_lease_generation', ?,
                       '$.attempt_started_at', ?
                   ),
                   updated_at = ?
               WHERE work_item_id = ?
                 AND phase = ?
                 AND COALESCE(claimed_by_role_runtime_session_id, '') = ''
                 AND COALESCE(claimed_by_seat_id, '') = ''
                 AND COALESCE(json_extract(metadata, '$.dispatch_hold'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.queued_behind_session'), '') = ''
                 AND COALESCE(
                       CAST(json_extract(metadata, '$.manager_mutation_revision') AS INTEGER),
                       0
                     ) = ?
                 AND (
                     NOT EXISTS (
                         SELECT 1 FROM delegation_runs
                         WHERE delegation_runs.run_id = delegation_work_items.run_id
                     )
                     OR (
                         ? != ''
                         AND EXISTS (
                         SELECT 1 FROM delegation_runs
                         WHERE delegation_runs.run_id = delegation_work_items.run_id
                           AND controller_owner_token = ?
                           AND controller_lease_generation = ?
                           AND controller_lease_expires_at > ?
                         )
                     )
                 )"""


class _ClaimFault(Exception):
    pass


def _fault(message: str) -> None:
    raise _ClaimFault(message) from None


def _text(value: Any) -> str:
    if type(value) is not str or not value or len(value) > _MAX_SCOPE_BYTES:
        _fault(_INVALID)
    if value.strip() != value or len(value.encode("utf-8")) > _MAX_SCOPE_BYTES:
        _fault(_INVALID)
    return value


def _file_snapshot(path: Path) -> tuple[int, int, int, int]:
    # lstat each ancestor, root first: do not resolve through a junction/link.
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fault(_UNAVAILABLE)
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            _fault(_UNAVAILABLE)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size < 100:
        _fault(_UNAVAILABLE)
    # Content/mtime legitimately change on commit; identity only is compared.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)


def _connect_existing(path: Path) -> sqlite3.Connection:
    # Path.as_uri encodes Chinese, '#', and '?' without changing the target.
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0)
    try:
        conn.execute("PRAGMA busy_timeout=0").close()
        conn.execute("PRAGMA foreign_keys=ON").close()
        busy = _one(conn, "PRAGMA busy_timeout", (), 1)
        foreign_keys = _one(conn, "PRAGMA foreign_keys", (), 1)
        if busy != (0,) or foreign_keys != (1,) or conn.in_transaction:
            _fault(_UNAVAILABLE)
    except BaseException:
        try:
            conn.close()
        except BaseException:
            pass
        raise
    return conn


def _one(conn: Any, sql: str, parameters: tuple, width: int) -> tuple:
    cursor = conn.execute(sql, parameters)
    try:
        rows = cursor.fetchmany(2)
        if type(rows) is not list or len(rows) != 1:
            _fault(_INVALID)
        row = rows[0]
        if type(row) is not tuple or len(row) != width:
            _fault(_INVALID)
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()
        return row


def _scope_row(conn: Any, table: str, columns: tuple[str, ...], where: str,
               parameters: tuple, *, reverse_link: bool = False) -> tuple:
    # SQL names are fixed private call-site literals, never caller identifiers.
    measures = ", ".join(
        f"typeof({column}), length(CAST({column} AS BLOB))" for column in columns
    )
    indexed_table = _INDEXED_TABLES[table]
    if reverse_link:
        if table != "work_item_runtime_links":
            _fault(_INVALID)
        indexed_table = "work_item_runtime_links INDEXED BY sqlite_autoindex_work_item_runtime_links_2"
    limits = _one(conn, f"SELECT {measures} FROM {indexed_table} WHERE {where} LIMIT 2",
                  parameters, 2 * len(columns))
    for index in range(len(columns)):
        kind, size = limits[2 * index:2 * index + 2]
        if kind not in ("text", "integer") or type(size) is not int or not 0 <= size <= _MAX_SCOPE_BYTES:
            _fault(_INVALID)
    return _one(conn, f"SELECT {', '.join(columns)} FROM {indexed_table} WHERE {where} LIMIT 2",
                parameters, len(columns))


def _check_scope(conn: Any, project_id: str, expected: WorkItemClaimExpectation) -> None:
    identity = _scope_row(conn, "store_identity", ("scope", "project_id"), "id = 1", ())
    if identity != ("project", project_id):
        _fault(_INVALID)
    item = _scope_row(conn, "delegation_work_items", ("work_item_id", "run_id", "seat_id"),
                      "work_item_id = ?", (expected.work_item_id,))
    if item != (expected.work_item_id, expected.run_id, expected.seat_id):
        _fault(_INVALID)
    task = _scope_row(conn, "tasks", ("id", "project_id"), "id = ?", (expected.task_id,))
    if task != (expected.task_id, project_id):
        _fault(_INVALID)
    run = _scope_row(conn, "delegation_runs",
                     ("run_id", "project_id", "controller_owner_token",
                      "controller_lease_generation", "controller_lease_expires_at"),
                     "run_id = ?", (expected.run_id,))
    if (run[:3] != (expected.run_id, project_id, expected.controller_owner_token)
            or type(run[3]) is not int or run[3] != expected.controller_lease_generation
            or type(run[4]) is not str or not run[4] or run[4] <= datetime.now().isoformat()):
        _fault(_INVALID)
    link = _scope_row(conn, "work_item_runtime_links", ("work_item_id", "runtime_task_id"),
                      "work_item_id = ?", (expected.work_item_id,))
    if link != (expected.work_item_id, expected.task_id):
        _fault(_INVALID)
    reverse = _scope_row(conn, "work_item_runtime_links", ("work_item_id", "runtime_task_id"),
                         "runtime_task_id = ?", (expected.task_id,), reverse_link=True)
    if reverse != link:
        _fault(_INVALID)


def _check_work_item_size(conn: Any, expected: WorkItemClaimExpectation) -> None:
    # Refuse an oversized row before transferring/decoding its contents.
    measures = ", ".join(
        f"typeof({column}), length(CAST({column} AS BLOB))"
        for column in _WORK_ITEM_COLUMNS
    )
    sizes = _one(conn, f"SELECT {measures} FROM {_INDEXED_TABLES['delegation_work_items']} "
                 "WHERE work_item_id = ? LIMIT 2", (expected.work_item_id,),
                 2 * len(_WORK_ITEM_COLUMNS))
    total = 0
    for index, column in enumerate(_WORK_ITEM_COLUMNS):
        kind, size = sizes[2 * index:2 * index + 2]
        if column == "batch_index":
            if kind not in ("integer", "null"):
                _fault(_INVALID)
        elif kind not in ("text", "null"):
            _fault(_INVALID)
        if kind != "null":
            if type(size) is not int or size < 0:
                _fault(_INVALID)
            total += size
    if total > _MAX_ROW_BYTES:
        _fault(_INVALID)


def _read_work_item(conn: Any, expected: WorkItemClaimExpectation,
                    materialize: Callable[..., DelegationWorkItem]) -> DelegationWorkItem:
    _check_work_item_size(conn, expected)
    row = _one(conn, f"SELECT {', '.join(_WORK_ITEM_COLUMNS)} FROM {_INDEXED_TABLES['delegation_work_items']} "
               "WHERE work_item_id = ? LIMIT 2", (expected.work_item_id,),
               len(_WORK_ITEM_COLUMNS))
    persisted = materialize(row, tuple((column,) for column in _WORK_ITEM_COLUMNS))
    if type(persisted) is not DelegationWorkItem:
        _fault(_INVALID)
    return persisted


def _rollback_confirmed(conn: Any) -> None:
    rollback_failed = False
    try:
        rollback_failed = conn.rollback() is not None or conn.in_transaction
    except Exception:
        rollback_failed = True
    if rollback_failed:
        _fault(_UNKNOWN)


def claim_work_item_with_observation(
    *, db_path: str, project_id: str, expected: WorkItemClaimExpectation,
    expected_phase: Phase | str, materialize: Callable[..., DelegationWorkItem],
) -> tuple[DelegationWorkItem, WorkItemClaimObservation] | None:
    """Freeze the winning epoch before commit, then verify it without replacing it."""
    conn = None
    result = None
    failure = None
    cancelled = None
    commit_started = False
    committed = False
    inputs_validated = False
    before = None
    path = None
    try:
        if type(expected) is not WorkItemClaimExpectation or not callable(materialize):
            _fault(_INVALID)
        validated = prepare_work_item_claim_expectation(
            work_item_id=expected.work_item_id, run_id=expected.run_id,
            role_runtime_session_id=expected.role_runtime_session_id, seat_id=expected.seat_id,
            task_id=expected.task_id, work_item_revision=expected.work_item_revision,
            controller_owner_token=expected.controller_owner_token,
            controller_lease_generation=expected.controller_lease_generation,
        )
        if validated != expected:
            _fault(_INVALID)
        _text(project_id)
        if type(db_path) is not str or not db_path or len(db_path) > 4096 or "\x00" in db_path:
            _fault(_INVALID)
        path = Path(db_path)
        if (not path.is_absolute() or ".." in path.parts or db_path.strip() != db_path
                or path.anchor.startswith("\\\\")):
            _fault(_INVALID)
        if type(expected_phase) is not str and type(expected_phase) is not Phase:
            _fault(_INVALID)
        phase = coerce_phase(expected_phase)
        if phase not in {Phase.READY, Phase.READY_FOR_REWORK, *IN_PROGRESS_PHASES}:
            _fault(_INVALID)
        if phase != Phase.RUNNING:
            validate_transition(phase, Phase.RUNNING)
        inputs_validated = True
        before = _file_snapshot(path)
        conn = _connect_existing(path)
        if _file_snapshot(path) != before:
            _fault(_UNAVAILABLE)
        conn.execute("BEGIN IMMEDIATE").close()
        if not conn.in_transaction:
            _fault(_UNAVAILABLE)
        _check_scope(conn, project_id, expected)
        _check_work_item_size(conn, expected)
        updated_at = datetime.now()
        cursor = conn.execute(CLAIM_SQL, (
            Phase.RUNNING.value, expected.role_runtime_session_id,
            expected.role_runtime_session_id, expected.seat_id,
            expected.role_runtime_session_id, expected.task_id,
            expected.work_item_revision, expected.controller_owner_token,
            expected.controller_lease_generation, updated_at.isoformat(),
            updated_at.isoformat(), expected.work_item_id, phase.value,
            expected.work_item_revision, expected.controller_owner_token,
            expected.controller_owner_token, expected.controller_lease_generation,
            datetime.now().isoformat(),
        ))
        try:
            changed = cursor.rowcount
        except BaseException:
            try:
                cursor.close()
            except BaseException:
                pass
            raise
        else:
            cursor.close()
        if type(changed) is not int or changed not in (0, 1):
            _fault(_INVALID)
        if changed == 0:
            _rollback_confirmed(conn)
        else:
            persisted = _read_work_item(conn, expected, materialize)
            capture_failed = False
            observation = None
            try:
                observation = capture_work_item_claim_observation(persisted, expected=expected)
            except Exception:
                capture_failed = True
            if capture_failed:
                _fault(_INVALID)
            commit_started = True
            if conn.commit() is not None or conn.in_transaction:
                _fault(_UNKNOWN)
            committed = True
            # All post-commit reads share one read snapshot on this owned handle.
            conn.execute("BEGIN").close()
            if not conn.in_transaction:
                _fault(_UNKNOWN)
            try:
                _check_scope(conn, project_id, expected)
                current = _read_work_item(conn, expected, materialize)
                capture_failed = False
                current_observation = None
                try:
                    current_observation = capture_work_item_claim_observation(current, expected=expected)
                except Exception:
                    capture_failed = True
                if capture_failed:
                    _fault(_STALE)
                if current_observation != observation:
                    _fault(_STALE)
            except _ClaimFault:
                _fault(_STALE)
            _rollback_confirmed(conn)
            result = (persisted, observation)
        if _file_snapshot(path) != before:
            _fault(_UNKNOWN)
    except _ClaimFault as exc:
        failure = exc.args[0] if exc.args in (
            (_INVALID,), (_UNAVAILABLE,), (_UNKNOWN,), (_STALE,),
        ) else _UNKNOWN
        if commit_started and failure != _STALE:
            failure = _UNKNOWN
    except Exception:
        failure = _UNKNOWN if commit_started else (_UNAVAILABLE if inputs_validated else _INVALID)
    except BaseException as exc:
        cancelled = exc

    # Preserve the primary error/cancellation even when cleanup itself fails.
    if conn is not None:
        try:
            if conn.in_transaction:
                _rollback_confirmed(conn)
        except BaseException as exc:
            if failure is None and cancelled is None:
                if isinstance(exc, Exception):
                    failure = _UNKNOWN
                else:
                    cancelled = exc
        try:
            if conn.close() is not None:
                _fault(_UNKNOWN)
        except BaseException as exc:
            if failure is None and cancelled is None:
                if isinstance(exc, Exception):
                    failure = _UNKNOWN
                else:
                    cancelled = exc
    if failure is None and cancelled is None and path is not None and before is not None:
        try:
            if _file_snapshot(path) != before:
                failure = _UNKNOWN
        except Exception:
            failure = _UNKNOWN
        except BaseException as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    if failure is not None:
        raise RuntimeError(failure) from None
    if committed and result is None:
        raise RuntimeError(_UNKNOWN) from None
    return result
