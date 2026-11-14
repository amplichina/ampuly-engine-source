"""Trusted, read-only browser catalog factory for an already-ready project Store.

No migration, initialization, actor authorization, account selection or dispatch.
The caller must authorize the project/task before using this internal reader.
Only Python attributes and opening evidence are read from the original Store
connection. Its SQL, TEMP and attached databases are not queried by this reader.
File checks are bounded continuity checks, not an OS anti-replacement sandbox.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from ampuly.database import schema_version as schema
from ampuly.database.browser_account_grants import (
    META, SQLiteBrowserAccountGrantReader, browser_account_grant_schema_state,
)
from ampuly.database.source_project_scope import SourceProjectScopeError, require_source_project, validate_source_project
from ampuly.layer4_tools.browser_account_selection import BrowserAccountSelectionRefused


def _refuse(reason="account_source_invalid"):
    raise BrowserAccountSelectionRefused(reason)


def _rows(conn, sql, parameters=(), *, limit=2):
    cursor = conn.execute(sql, parameters)
    try:
        rows = cursor.fetchmany(limit + 1)
        if len(rows) > limit or any(type(row) is not tuple for row in rows):
            _refuse()
        return rows
    finally:
        cursor.close()


def _mapped(error):
    if isinstance(error, BrowserAccountSelectionRefused):
        return error
    if isinstance(error, SourceProjectScopeError):
        reason = "account_source_unavailable" if error.code == "unavailable" else "account_source_invalid"
    elif isinstance(error, schema._SourceSchemaError):
        reason = "account_source_changed" if error.code == "outcome_unknown" else (
            "account_source_invalid" if error.code == "invalid_source" else "account_source_unavailable")
    else:
        reason = "account_source_unavailable"
    return BrowserAccountSelectionRefused(reason)


@dataclass(frozen=True, slots=True, repr=False)
class _ReadyProjectGrantBinding:
    store: object
    adapter: object
    original: sqlite3.Connection
    project_id: str
    db_path: str
    opening: schema._DatabaseOpening
    evidence: tuple

    @classmethod
    def capture(cls, store):
        # Deferred to avoid Store -> factory -> Store at module initialization.
        from ampuly.database.store import AmpulyStore, _SQLiteConnectionAdapter

        if type(store) is not AmpulyStore or AmpulyStore.is_ready.fget(store) is not True:
            _refuse("account_source_not_ready")
        project = validate_source_project(store.project_id)
        path = store.db_path
        if (type(path) is not str or not path or path.strip() != path
                or len(path) > 4096 or "\x00" in path):
            _refuse()
        adapter = AmpulyStore._require_db(store)
        if type(adapter) is not _SQLiteConnectionAdapter or type(adapter._conn) is not sqlite3.Connection:
            _refuse()
        evidence = getattr(store, "_browser_account_opening_evidence", None)
        if evidence is None:
            _refuse("account_source_not_ready")
        checked_path = schema._checked_database_path(path)
        if (type(evidence) is not tuple or len(evidence) != 3
                or evidence[0] is not adapter._conn
                or type(evidence[1]) is not type(checked_path) or evidence[1] != checked_path
                or type(evidence[2]) is not tuple or len(evidence[2]) != 2
                or any(type(value) is not int or value < 0 for value in evidence[2])):
            _refuse("account_source_changed")
        # Reuse the Store's successful opening identity; never adopt today's
        # file identity as if it had been attached to the original connection.
        opening = schema._DatabaseOpening(evidence[1], schema.RUNTIME, None, evidence[2])
        binding = cls(store, adapter, adapter._conn, project, path, opening, evidence)
        binding.assert_current()
        return binding

    def _check_objects(self):
        from ampuly.database.store import AmpulyStore, _SQLiteConnectionAdapter

        if (type(self.store) is not AmpulyStore or AmpulyStore.is_ready.fget(self.store) is not True
                or type(self.store.project_id) is not str or self.store.project_id != self.project_id
                or type(self.store.db_path) is not str or self.store.db_path != self.db_path
                or AmpulyStore._require_db(self.store) is not self.adapter
                or type(self.adapter) is not _SQLiteConnectionAdapter
                or self.adapter._conn is not self.original
                or getattr(self.store, "_browser_account_opening_evidence", None) is not self.evidence
                or type(self.original) is not sqlite3.Connection):
            _refuse("account_source_changed")
        if (self.original.in_transaction is not False or self.original.row_factory is not None
                or self.original.text_factory is not str):
            _refuse("account_source_unavailable")
        info = schema._path_identity(self.opening.path)
        if (info.st_dev, info.st_ino) != self.opening.identity:
            _refuse("account_source_changed")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.opening.path.with_name(self.opening.path.name + suffix)
            try:
                schema._path_identity(sidecar)
            except FileNotFoundError:
                continue
            if suffix == "-journal":
                _refuse("account_source_unavailable")

    @contextmanager
    def read_transaction(self):
        conn = None
        primary = None
        try:
            self._check_objects()
            with schema._database_opening(self.db_path, schema.RUNTIME, allow_create=False) as current:
                if current.path != self.opening.path or current.identity != self.opening.identity:
                    _refuse("account_source_changed")
                conn = sqlite3.connect(current.path.as_uri() + "?mode=ro", uri=True,
                                       timeout=0.0, isolation_level=None)
                if (type(conn) is not sqlite3.Connection or conn is self.original
                        or conn.in_transaction is not False or conn.row_factory is not None
                        or conn.text_factory is not str):
                    _refuse()
                _rows(conn, "PRAGMA query_only=ON", limit=1)
                if (_rows(conn, "PRAGMA query_only", limit=1) != [(1,)]
                        or _rows(conn, "PRAGMA busy_timeout", limit=1) != [(0,)]
                        or _rows(conn, "PRAGMA journal_mode", limit=1) not in ([("delete",)], [("wal",)])):
                    _refuse()
                _rows(conn, "BEGIN", limit=1)
                require_source_project(conn, self.project_id)
                schema._verify_database_opening(conn, self.opening)
                self._check_objects()
                yield conn
                if (conn.in_transaction is not True or conn.row_factory is not None
                        or conn.text_factory is not str
                        or _rows(conn, "PRAGMA query_only", limit=1) != [(1,)]
                        or _rows(conn, "PRAGMA busy_timeout", limit=1) != [(0,)]):
                    _refuse()
                require_source_project(conn, self.project_id)
                schema._verify_database_opening(conn, self.opening)
                self._check_objects()
        except BaseException as error:
            primary = error
        finally:
            if conn is not None and conn is not self.original:
                try:
                    sqlite3.Connection.rollback(conn)
                except BaseException as error:
                    if primary is None:
                        primary = error
                try:
                    sqlite3.Connection.close(conn)
                except BaseException as error:
                    if primary is None:
                        primary = error
        if primary is not None:
            if isinstance(primary, Exception):
                raise _mapped(primary) from None
            raise primary
        try:
            self._check_objects()
        except Exception as error:
            raise _mapped(error) from None

    def assert_current(self):
        # All SQL stays on zero-timeout connections, including when a writer
        # enters PENDING after our snapshot. The original busy handler is unused.
        with self.read_transaction():
            pass

    def read_source_identity(self):
        with self.read_transaction() as conn:
            if browser_account_grant_schema_state(conn) == "absent":
                _refuse("account_source_not_ready")
            rows = _rows(conn, f"""SELECT
                typeof(id)='integer' AND id=1,
                typeof(project_id)='text' AND project_id COLLATE BINARY=?,
                CASE WHEN typeof(source_id)='text' AND length(CAST(source_id AS BLOB))<=128 THEN source_id END,
                CASE WHEN typeof(source_epoch)='text' AND length(CAST(source_epoch AS BLOB))<=128 THEN source_epoch END,
                typeof(schema_version)='integer' AND schema_version=1
                FROM main.{META} LIMIT 2""", (self.project_id,), limit=1)
            if not rows:
                _refuse("account_source_not_ready")
            singleton, project, source_id, epoch, version = rows[0]
            if (singleton, project, version) != (1, 1, 1):
                _refuse()
            return source_id, epoch


def reader_from_ready_store(store):
    """Bind an existing catalog to its original Store; return no new authority."""
    try:
        binding = _ReadyProjectGrantBinding.capture(store)
        source_id, epoch = binding.read_source_identity()
        reader = SQLiteBrowserAccountGrantReader(
            project_id=binding.project_id, source_id=source_id, source_epoch=epoch,
            read_transaction=binding.read_transaction, check_binding=binding.assert_current,
        )
        binding.assert_current()
        return reader
    except Exception as error:
        raise _mapped(error) from None
