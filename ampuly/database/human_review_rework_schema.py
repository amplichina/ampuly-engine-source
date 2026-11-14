"""Runtime v3 receipt schema: explicit migration/new-store initialization only.

No path opening, backup, transaction lifecycle, startup repair or runtime writes.
Existing stores remain v2 until the owner runs the original guarded migrator.
"""
from __future__ import annotations

import sqlite3

from ampuly.layer2_organization.human_review_rework_receipts import SCHEMA_STATEMENTS, TABLE


def rework_receipt_schema_state(conn: sqlite3.Connection) -> str:
    from .schema_version import _SourceSchemaError, _stored_create_sql

    if type(conn) is not sqlite3.Connection:
        raise _SourceSchemaError('invalid_source')
    names = (TABLE, 'human_rework_receipt_no_update_v1', 'human_rework_receipt_no_delete_v1', 'human_rework_receipt_no_replace_v1')
    cursor = conn.cursor()
    try:
        cursor.row_factory = None
        cursor.execute(
            "SELECT type,name,tbl_name,CASE WHEN typeof(sql)='text' AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END "
            "FROM main.sqlite_schema WHERE tbl_name=? OR name IN (?,?,?,?) ORDER BY name LIMIT 9", (TABLE, *names))
        rows = cursor.fetchmany(9)
    finally:
        cursor.close()
    if not rows:
        return 'absent'
    if len(rows) > 8:
        raise _SourceSchemaError('invalid_source')
    expected = {name: ('table' if name == TABLE else 'trigger', TABLE, _stored_create_sql(statement))
                for name, statement in zip(names, SCHEMA_STATEMENTS)}
    found = set()
    for object_type, name, table, sql in rows:
        if (type(name) is str and name.startswith('sqlite_autoindex_' + TABLE + '_')
                and object_type == 'index' and table == TABLE and sql is None):
            suffix = name[len('sqlite_autoindex_' + TABLE + '_'):]
            if suffix not in {'1', '2', '3'}:
                raise _SourceSchemaError('invalid_source')
            continue
        if (name not in expected or name in found or type(sql) is not str
                or (object_type, table, _stored_create_sql(sql)) != expected[name]):
            raise _SourceSchemaError('invalid_source')
        found.add(name)
    if found != set(expected):
        raise _SourceSchemaError('invalid_source')
    return 'complete'


def install_rework_receipts(conn) -> None:
    """Only the original migrator's transaction wrapper may create the bundle."""
    from .schema_version import MigrationConnection, _SourceSchemaError

    if (type(conn) is not MigrationConnection or type(conn._conn) is not sqlite3.Connection
            or conn._conn.in_transaction is not True):
        raise _SourceSchemaError('invalid_source')
    state = rework_receipt_schema_state(conn._conn)
    if state == 'absent':
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement).close()
    if rework_receipt_schema_state(conn._conn) != 'complete':
        raise _SourceSchemaError('invalid_source')
