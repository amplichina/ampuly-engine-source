"""Bind retained employee sources to the current selected Store, not a path DTO.

No initialization, migration, statement rewriting, fallback database or resume.
The existing schema-version opening checks are bounded best-effort physical
identity checks, not a new claim of a universal OS anti-race sandbox.
"""
from __future__ import annotations

import sqlite3

from .employee_order_creation_sources import (
    EmployeeOrderCreationSourcesError, read_employee_order_creation_sources,
)
from ...database.source_project_scope import validate_source_project, require_source_project
from .ai_employee_run_envelope import _identity


def read_selected_store_creation_sources(store, *, identity, delegation_run_id):
    from ...database import schema_version as schema
    from ...database.store import AmpulyStore, _SQLiteConnectionAdapter

    conn = None
    result = None
    primary = None
    failure = None
    try:
        if type(store) is not AmpulyStore:
            raise EmployeeOrderCreationSourcesError("invalid_source")
        selected_identity = _identity(identity)
        project = validate_source_project(identity.project_id)
        path = store.db_path
        shared = AmpulyStore._require_db(store)
        if (type(store.project_id) is not str or store.project_id != project
                or type(path) is not str or not path or len(path) > 4096
                or path != path.strip() or "\x00" in path
                or AmpulyStore.is_ready.fget(store) is not True
                or type(shared) is not _SQLiteConnectionAdapter
                or type(shared._conn) is not sqlite3.Connection):
            raise EmployeeOrderCreationSourcesError("invalid_source")
        native = shared._conn

        def unchanged(opening):
            if (_identity(identity) != selected_identity
                    or type(store.project_id) is not str or store.project_id != project
                    or type(store.db_path) is not str or store.db_path != path
                    or AmpulyStore.is_ready.fget(store) is not True
                    or AmpulyStore._require_db(store) is not shared or shared._conn is not native):
                raise EmployeeOrderCreationSourcesError("unavailable")
            schema._verify_database_opening(native, opening)
            if conn is not None:
                schema._verify_database_opening(conn, opening)

        with schema._database_opening(path, schema.RUNTIME, allow_create=False) as opening:
            unchanged(opening)
            conn = sqlite3.connect(opening.path.as_uri() + "?mode=ro", uri=True,
                                  timeout=0.0, isolation_level=None)
            try:
                if type(conn) is not sqlite3.Connection or conn.in_transaction is not False:
                    raise EmployeeOrderCreationSourcesError("invalid_source")
                unchanged(opening)
                conn.execute("PRAGMA query_only=ON").close()
                conn.execute("BEGIN").close()
                require_source_project(conn, project)
                result = read_employee_order_creation_sources(conn, identity=identity,
                                                               delegation_run_id=delegation_run_id)
                unchanged(opening)
                if result.identity != identity or result.identity.project_id != project:
                    raise EmployeeOrderCreationSourcesError("conflict")
            except BaseException as error:
                primary = error
            try:
                if conn.in_transaction:
                    sqlite3.Connection.rollback(conn)
                unchanged(opening)
            except BaseException as error:
                if primary is None:
                    primary = error
            try:
                sqlite3.Connection.close(conn)
            except BaseException as error:
                if primary is None:
                    primary = error
            conn = None
            if primary is not None:
                raise primary
            unchanged(opening)
    except EmployeeOrderCreationSourcesError as error:
        failure = error.code
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise EmployeeOrderCreationSourcesError(failure) from None
    return result
