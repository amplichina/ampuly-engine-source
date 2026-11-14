"""Existing-connection project source fences; no path, migration or authority.

The owning Store/host must also bind the selected physical database file and
recheck its unchanged selection. A project label is not a globally unique DB
identity, and historical creation facts alone never authorize execution.
"""
from __future__ import annotations

import re
import sqlite3

_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z")


class SourceProjectScopeError(ValueError):
    def __init__(self, code="invalid_source"):
        self.code = code if code in ("invalid_source", "unavailable") else "invalid_source"
        super().__init__("原项目运行库身份无法核验。")


def validate_source_project(value: object) -> str:
    if type(value) is not str or _PROJECT.fullmatch(value) is None:
        raise SourceProjectScopeError() from None
    return value


def _rows(conn, sql, params=(), *, limit=2):
    cursor = conn.execute(sql, params)
    primary = None
    result = None
    try:
        result = cursor.fetchmany(limit + 1)
        if type(result) is not list or len(result) > limit or any(type(row) is not tuple for row in result):
            raise SourceProjectScopeError()
    except BaseException as error:
        primary = error
    try:
        cursor.close()
    except BaseException:
        if primary is None:
            raise
    if primary is not None:
        raise primary
    return result


def require_source_project(conn: sqlite3.Connection, project_id: str, *, write: bool = False) -> str:
    """Same native transaction, main-only, exact declared project identity.

    Reject shared/global, missing/ambiguous identity, TEMP substitution and
    attached DBs before any original source lookup. Never change connection
    options, begin/commit, repair schema, or infer identity from a filename.
    """
    project_id = validate_source_project(project_id)
    failure = None
    try:
        if (type(conn) is not sqlite3.Connection or type(write) is not bool
                or conn.in_transaction is not True or conn.row_factory is not None
                or conn.text_factory is not str):
            raise SourceProjectScopeError()
        databases = _rows(conn, "SELECT name FROM pragma_database_list LIMIT 4", limit=3)
        names = [row[0] for row in databases if len(row) == 1 and type(row[0]) is str]
        if (len(names) != len(databases) or len(set(names)) != len(names)
                or "main" not in names or any(name not in ("main", "temp") for name in names)
                or _rows(conn, "SELECT 1 FROM sqlite_temp_master LIMIT 1", limit=1)):
            raise SourceProjectScopeError()
        query_only = _rows(conn, "PRAGMA query_only", limit=1)
        if query_only not in ([(0,)], [(1,)]) or write and query_only != [(0,)]:
            raise SourceProjectScopeError()
        identity_table = _rows(conn, "SELECT type FROM main.sqlite_master WHERE name = 'store_identity' LIMIT 2")
        if identity_table == []:
            # No such table: the original source is absent, not malformed.
            raise SourceProjectScopeError("unavailable")
        if identity_table != [("table",)]:
            raise SourceProjectScopeError()
        # Boolean comparisons bound returned data even for corrupt huge fields.
        identity = _rows(conn,
            "SELECT typeof(id) = 'integer' AND id = 1, "
            "typeof(scope) = 'text' AND scope COLLATE BINARY = 'project', "
            "typeof(project_id) = 'text' AND project_id COLLATE BINARY = ? "
            "FROM main.store_identity LIMIT 2", (project_id,))
        if identity != [(1, 1, 1)]:
            raise SourceProjectScopeError()
    except SourceProjectScopeError as error:
        failure = error.code
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise SourceProjectScopeError(failure) from None
    return project_id
