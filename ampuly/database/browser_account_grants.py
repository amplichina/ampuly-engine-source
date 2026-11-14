"""Project browser-grant schema and read-only leaf; no Store factory or activation.

The host must supply a trusted source-bound transaction factory. Each call must
yield a NEW native, query-only connection in a read transaction and always close
it. The host owns file identity, original Store/connection continuity and actor
authorization. This leaf never opens a path, starts/ends a transaction, installs
schema on read, or grants browser execution. Full database rollback detection,
source restoration, catalog writers and ordered dispatch remain separate work.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import fields

from ampuly.database.source_project_scope import SourceProjectScopeError, require_source_project
from ampuly.layer4_tools.browser_account_selection import (
    AuthorizedTaskScope, BrowserAccountGrant, BrowserAccountSelectionRefused,
    GrantPolicySnapshot, GrantSnapshot, GrantSourceStamp, RetentionEvidence,
    resolve_account_selection,
)
from ampuly.layer4_tools.browser_account_status import LoginProbePolicy

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFEST_ENTRIES = 4096
MAX_ROW_BYTES = 64 * 1024
MAX_REVISION = (1 << 53) - 1
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_GRANT = re.compile(r"bag_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
META = "browser_account_catalog_meta"
TABLES = {
    "grant": "browser_account_grant_versions",
    "retention": "browser_account_retention_versions",
    "policy": "browser_account_policy_versions",
}


def _build_schema() -> tuple[tuple[str, str, str, str], ...]:
    objects = [(META, "table", META, f"""CREATE TABLE {META} (
        id INTEGER PRIMARY KEY CHECK(id=1),
        project_id TEXT NOT NULL CHECK(typeof(project_id)='text' AND length(CAST(project_id AS BLOB)) BETWEEN 1 AND 256),
        source_id TEXT NOT NULL CHECK(typeof(source_id)='text' AND length(source_id) BETWEEN 1 AND 128),
        source_epoch TEXT NOT NULL CHECK(typeof(source_epoch)='text' AND length(source_epoch) BETWEEN 1 AND 128),
        source_revision INTEGER NOT NULL CHECK(typeof(source_revision)='integer' AND source_revision BETWEEN 1 AND {MAX_REVISION}),
        schema_version INTEGER NOT NULL CHECK(typeof(schema_version)='integer' AND schema_version=1),
        content_digest TEXT NOT NULL CHECK(typeof(content_digest)='text' AND length(content_digest)=64),
        manifest_json TEXT NOT NULL CHECK(typeof(manifest_json)='text' AND length(CAST(manifest_json AS BLOB))<={MAX_MANIFEST_BYTES})
    )""")]
    meta_triggers = {
        "no_delete": f"BEFORE DELETE ON {META}",
        "no_replace": f"BEFORE INSERT ON {META} WHEN EXISTS(SELECT 1 FROM {META}) OR NEW.source_revision<>1",
        "advance": f"""BEFORE UPDATE ON {META} WHEN
            NEW.id<>OLD.id OR NEW.project_id<>OLD.project_id OR NEW.source_id<>OLD.source_id
            OR NEW.source_epoch<>OLD.source_epoch OR NEW.schema_version<>OLD.schema_version
            OR NEW.source_revision<>OLD.source_revision+1""",
    }
    for suffix, clause in meta_triggers.items():
        name = f"{META}_{suffix}_v1"
        objects.append((name, "trigger", META,
                        f"CREATE TRIGGER {name} {clause} BEGIN SELECT RAISE(ABORT,'catalog_version_guard'); END"))
    for kind, table in TABLES.items():
        state = ", state TEXT NOT NULL CHECK(state IN ('active','revoked'))" if kind == "grant" else ""
        objects.append((table, "table", table, f"""CREATE TABLE {table} (
            ref TEXT NOT NULL CHECK(typeof(ref)='text' AND length(ref) BETWEEN 1 AND 128),
            revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND {MAX_REVISION}),
            payload TEXT NOT NULL CHECK(typeof(payload)='text' AND length(CAST(payload AS BLOB))<={MAX_ROW_BYTES}),
            content_digest TEXT NOT NULL CHECK(typeof(content_digest)='text' AND length(content_digest)=64)
            {state}, PRIMARY KEY(ref,revision)
        ) WITHOUT ROWID"""))
        terminal = f" OR EXISTS(SELECT 1 FROM {table} WHERE ref=NEW.ref AND state='revoked')" if kind == "grant" else ""
        triggers = {
            "no_update": f"BEFORE UPDATE ON {table}",
            "no_delete": f"BEFORE DELETE ON {table}",
            "append": f"""BEFORE INSERT ON {table} WHEN
                NEW.revision<>COALESCE((SELECT MAX(revision) FROM {table} WHERE ref=NEW.ref),0)+1{terminal}""",
        }
        for suffix, clause in triggers.items():
            name = f"{table}_{suffix}_v1"
            objects.append((name, "trigger", table,
                            f"CREATE TRIGGER {name} {clause} BEGIN SELECT RAISE(ABORT,'catalog_version_guard'); END"))
    return tuple(objects)


_SCHEMA_OBJECTS = _build_schema()
SCHEMA_STATEMENTS = tuple(item[3] for item in _SCHEMA_OBJECTS)


def _refuse(reason: str = "account_source_invalid") -> None:
    raise BrowserAccountSelectionRefused(reason)


def _reference(value: object) -> bool:
    return type(value) is str and _REF.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_REVISION


def _scope_text(value: object) -> bool:
    if (type(value) is not str or not value or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def _rows(conn: sqlite3.Connection, sql: str, params=(), *, limit: int = 2) -> list[tuple]:
    cursor = conn.execute(sql, params)
    try:
        result = cursor.fetchmany(limit + 1)
        if len(result) > limit or any(type(row) is not tuple for row in result):
            _refuse()
        return result
    finally:
        cursor.close()


def _native(conn: sqlite3.Connection, *, query_only: bool = False) -> None:
    if (type(conn) is not sqlite3.Connection or conn.in_transaction is not True
            or conn.row_factory is not None or conn.text_factory is not str):
        _refuse()
    if query_only and _rows(conn, "PRAGMA query_only", limit=1) != [(1,)]:
        _refuse()
    if query_only and _rows(conn, "PRAGMA busy_timeout", limit=1) != [(0,)]:
        _refuse()


def _normalize_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split())



def browser_account_grant_structure_state(conn: sqlite3.Connection) -> str:
    """Lightweight structure check for RUNTIME Migration 4 / _current_schema_state.

    Like rework_receipt_schema_state: accepts a plain sqlite3.Connection and does
    not require an open transaction. Strict grant readers still use
    browser_account_grant_schema_state (which requires _native / in_transaction).
    """
    from ampuly.database.schema_version import _SourceSchemaError, _stored_create_sql

    if type(conn) is not sqlite3.Connection:
        raise _SourceSchemaError("invalid_source") from None
    names = (META, *TABLES.values())
    objects = tuple(item[0] for item in _SCHEMA_OBJECTS)
    params = (*names, *objects)
    cursor = conn.cursor()
    try:
        cursor.row_factory = None
        cursor.execute(
            "SELECT type,name,tbl_name,CASE WHEN typeof(sql)='text' "
            "AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END "
            "FROM main.sqlite_schema WHERE tbl_name IN ("
            + ",".join("?" for _ in names)
            + ") OR name IN ("
            + ",".join("?" for _ in objects)
            + ") ORDER BY name LIMIT ?",
            (*params, len(objects) + 2),
        )
        rows = cursor.fetchmany(len(objects) + 2)
    finally:
        cursor.close()
    if not rows:
        return "absent"
    if len(rows) > len(objects):
        raise _SourceSchemaError("invalid_source") from None
    expected = {
        name: (kind, table, _stored_create_sql(sql))
        for name, kind, table, sql in _SCHEMA_OBJECTS
    }
    found = set()
    for object_type, name, table, sql in rows:
        if (
            type(name) is str
            and name.startswith("sqlite_autoindex_")
            and object_type == "index"
            and sql is None
        ):
            continue
        if (
            name not in expected
            or name in found
            or type(sql) is not str
            or (object_type, table, _stored_create_sql(sql)) != expected[name]
        ):
            raise _SourceSchemaError("invalid_source") from None
        found.add(name)
    if found != set(expected):
        raise _SourceSchemaError("invalid_source") from None
    return "complete"


def browser_account_grant_schema_state(conn: sqlite3.Connection) -> str:
    """An independent exact bundle check; does not change the old origin bundle."""
    try:
        _native(conn)
        names = (META, *TABLES.values())
        objects = tuple(item[0] for item in _SCHEMA_OBJECTS)
        params = (*names, *objects)
        query = (
            "SELECT type,name,tbl_name,CASE WHEN typeof(sql)='text' "
            "AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END "
            "FROM main.sqlite_schema WHERE tbl_name IN (" + ",".join("?" for _ in names)
            + ") OR name IN (" + ",".join("?" for _ in objects) + ") ORDER BY name"
        )
        found = _rows(conn, query + " LIMIT ?", (*params, len(objects) + 2), limit=len(objects))
        if not found:
            return "absent"
        expected = {name: (kind, table, _normalize_sql(sql)) for name, kind, table, sql in _SCHEMA_OBJECTS}
        actual = {}
        for kind, name, table, sql in found:
            if name in actual or name not in expected or type(sql) is not str:
                _refuse()
            actual[name] = (kind, table, _normalize_sql(sql))
        if actual != expected:
            _refuse()
        return "complete"
    except BrowserAccountSelectionRefused:
        raise
    except sqlite3.OperationalError:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None


def install_browser_account_grant_structure(conn) -> None:
    """Structure-only CREATE for RUNTIME Migration 4 (shared or project).

    Runs SCHEMA_STATEMENTS only. Does not call require_source_project and does
    not seed catalog rows. Project-identity writes still use
    install_browser_account_grants. Must not be confused with that installer.
    """
    from ampuly.database.schema_version import MigrationConnection, _SourceSchemaError
    try:
        if type(conn) is not MigrationConnection:
            raise _SourceSchemaError("invalid_source") from None
        if type(conn._conn) is not sqlite3.Connection:
            raise _SourceSchemaError("invalid_source") from None
        if browser_account_grant_structure_state(conn._conn) == "absent":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement).close()
        if browser_account_grant_structure_state(conn._conn) != "complete":
            raise _SourceSchemaError("invalid_source") from None
    except _SourceSchemaError:
        raise
    except Exception:
        raise _SourceSchemaError("invalid_source") from None


def install_browser_account_grants(conn, *, project_id: str) -> None:
    """Explicit migration-wrapper use only; not registered or called on reads.

    B06-B2b: must NOT be registered in schema_version.MIGRATIONS[RUNTIME]. The
    installer requires project identity via require_source_project; blanket RUNTIME
    v4 registration would refuse fresh shared runtime initialize. See
    docs/4.9-B06-B2b-WINDOW-2026-10-27.zh.md.
    """
    from ampuly.database.schema_version import MigrationConnection
    try:
        if type(conn) is not MigrationConnection:
            _refuse()
        require_source_project(conn._conn, project_id, write=True)
        if browser_account_grant_schema_state(conn._conn) == "absent":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement).close()
        if browser_account_grant_schema_state(conn._conn) != "complete":
            _refuse()
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _no_number(value):
    _refuse()


def _canonical(value: object) -> str:
    # Payloads contain objects/arrays, text, booleans and bounded integer fields.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _document(raw: object, limit: int) -> dict:
    if type(raw) is not str or len(raw.encode("utf-8")) > limit:
        _refuse()
    value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_no_number, parse_float=_no_number)
    if type(value) is not dict or _canonical(value) != raw:
        _refuse()
    return value


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest(raw: str, digest: str) -> dict[tuple[str, str], tuple[int, str]]:
    value = _document(raw, MAX_MANIFEST_BYTES)
    if (set(value) != {"schema_version", "entries"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["entries"]) is not list
            or len(value["entries"]) > MAX_MANIFEST_ENTRIES or _sha(raw) != digest):
        _refuse()
    result = {}
    previous = None
    for entry in value["entries"]:
        if type(entry) is not dict or set(entry) != {"kind", "ref", "revision", "content_digest"}:
            _refuse()
        kind, ref = entry["kind"], entry["ref"]
        if (type(kind) is not str or kind not in TABLES or not _reference(ref)
                or kind == "grant" and _GRANT.fullmatch(ref) is None
                or not _revision(entry["revision"]) or not _digest(entry["content_digest"])):
            _refuse()
        key = (kind, ref)
        if previous is not None and key <= previous:
            _refuse()
        result[key] = (entry["revision"], entry["content_digest"])
        previous = key
    return result


def _header(conn: sqlite3.Connection, task_scope: AuthorizedTaskScope) -> tuple[GrantSourceStamp, dict]:
    # SQL bounds BEFORE fetching and parsing, including a malformed stored value.
    rows = _rows(conn, f"""SELECT
        typeof(id)='integer' AND id=1,
        CASE WHEN typeof(project_id)='text' AND length(CAST(project_id AS BLOB))<=256 THEN project_id END,
        CASE WHEN typeof(source_id)='text' AND length(CAST(source_id AS BLOB))<=128 THEN source_id END,
        CASE WHEN typeof(source_epoch)='text' AND length(CAST(source_epoch AS BLOB))<=128 THEN source_epoch END,
        CASE WHEN typeof(source_revision)='integer' AND source_revision BETWEEN 1 AND {MAX_REVISION} THEN source_revision END,
        CASE WHEN typeof(schema_version)='integer' AND schema_version=1 THEN schema_version END,
        CASE WHEN typeof(content_digest)='text' AND length(content_digest)=64 THEN content_digest END,
        CASE WHEN typeof(manifest_json)='text' AND length(CAST(manifest_json AS BLOB))<={MAX_MANIFEST_BYTES} THEN manifest_json END
        FROM main.{META} LIMIT 2""", limit=1)
    if not rows:
        _refuse("account_source_not_ready")
    identity, project, source_id, epoch, revision, version, digest, manifest = rows[0]
    if (identity != 1 or project != task_scope.project_id or not _reference(source_id) or not _reference(epoch)
            or not _revision(revision) or type(version) is not int or version != 1 or not _digest(digest)):
        _refuse()
    stamp = GrantSourceStamp(project, source_id, epoch, revision, digest, version)
    return stamp, _manifest(manifest, digest)


def _version(conn: sqlite3.Connection, manifest: dict, kind: str, ref: str) -> dict:
    if not _reference(ref):
        _refuse()
    current = manifest.get((kind, ref))
    if current is None:
        _refuse("account_grant_not_found" if kind == "grant" else
                "account_retention_unconfirmed" if kind == "retention" else "account_policy_unavailable")
    table = TABLES[kind]  # fixed internal identifiers, never caller SQL
    state = ", CASE WHEN typeof(state)='text' AND state IN ('active','revoked') THEN state END" if kind == "grant" else ""
    rows = _rows(conn, f"""SELECT CASE WHEN typeof(revision)='integer' AND revision BETWEEN 1 AND {MAX_REVISION} THEN revision END,
        CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<={MAX_ROW_BYTES} THEN payload END,
        CASE WHEN typeof(content_digest)='text' AND length(content_digest)=64 THEN content_digest END
        {state} FROM main.{table} WHERE ref=? COLLATE BINARY ORDER BY revision DESC LIMIT 1""", (ref,), limit=1)
    if len(rows) != 1:
        _refuse()
    revision, raw, digest = rows[0][:3]
    if not _revision(revision) or (revision, digest) != current or not _digest(digest):
        _refuse("account_source_changed")
    payload = _document(raw, MAX_ROW_BYTES)
    if _sha(raw) != digest:
        _refuse()
    if kind == "grant" and (payload.get("grant_id") != ref or payload.get("grant_revision") != revision
                            or payload.get("state") != rows[0][3]):
        _refuse()
    return payload


def _read_snapshot(conn: sqlite3.Connection, grant_id: str, task: AuthorizedTaskScope) -> GrantSnapshot:
    _native(conn, query_only=True)
    require_source_project(conn, task.project_id)
    if browser_account_grant_schema_state(conn) == "absent":
        _refuse("account_source_not_ready")
    source, manifest = _header(conn, task)
    raw = _version(conn, manifest, "grant", grant_id)
    if set(raw) != {f.name for f in fields(BrowserAccountGrant)} or type(raw["allowed_hosts"]) is not list:
        _refuse("account_grant_invalid")
    grant = BrowserAccountGrant(**{**raw, "allowed_hosts": tuple(raw["allowed_hosts"])})
    # Scope/state checks precede dependencies: no probing another customer's data.
    if (grant.project_id, grant.customer_id, grant.environment_id) != (task.project_id, task.customer_id, task.environment_id):
        _refuse("account_grant_scope_mismatch")
    if grant.state == "revoked":
        _refuse("account_grant_revoked")
    consent = _version(conn, manifest, "retention", grant.retention_receipt_ref)
    if (set(consent) != {"receipt_ref", "account_id", "confirmed", "actor_ref", "consent_method"}
            or consent["receipt_ref"] != grant.retention_receipt_ref or not _reference(consent["actor_ref"])
            or consent["consent_method"] != "explicit_profile_retention" or type(consent["confirmed"]) is not bool):
        _refuse("account_retention_unconfirmed")
    retention = RetentionEvidence(consent["receipt_ref"], consent["account_id"], consent["confirmed"], source)
    raw_policy = _version(conn, manifest, "policy", grant.login_policy_ref)
    if set(raw_policy) != {f.name for f in fields(LoginProbePolicy)}:
        _refuse("account_policy_mismatch")
    policy = LoginProbePolicy(**raw_policy)
    if _canonical(raw_policy) != _canonical({f.name: getattr(policy, f.name) for f in fields(LoginProbePolicy)}):
        _refuse("account_policy_mismatch")
    snapshot = GrantSnapshot(source, grant, retention, GrantPolicySnapshot(grant.login_policy_ref, policy, source))
    resolve_account_selection(snapshot, task_scope=task)
    return snapshot


class SQLiteBrowserAccountGrantReader:
    """Internal reader bound by the future trusted Store factory, never a client.

    read_transaction owns all connection lifecycle, and check_binding verifies
    original Store/connection/file continuity without modifying that connection.
    Neither dependency has a default. Each call requires a distinct connection;
    reusing an old read transaction is refused. No authority follows from merely
    constructing this class. Same-reader rollback checks do not detect a complete
    old-database restore after process restart.
    """

    def __init__(self, *, project_id: str, source_id: str, source_epoch: str,
                 read_transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
                 check_binding: Callable[[], None]) -> None:
        if (not _scope_text(project_id)
                or not _reference(source_id) or not _reference(source_epoch)
                or not callable(read_transaction) or not callable(check_binding)):
            _refuse()
        self._project_id, self._source_id, self._epoch = project_id, source_id, source_epoch
        self._read_transaction, self._check_binding = read_transaction, check_binding
        self._last_conn: sqlite3.Connection | None = None
        self._last_source: GrantSourceStamp | None = None
        self._reading = False

    def _binding(self) -> None:
        try:
            if self._check_binding() is not None:
                _refuse("account_source_changed")
        except BrowserAccountSelectionRefused:
            raise
        except Exception:
            raise BrowserAccountSelectionRefused("account_source_unavailable") from None

    def read_grant(self, grant_id: str, *, task_scope: AuthorizedTaskScope,
                   expected_source: GrantSourceStamp | None = None) -> GrantSnapshot:
        if type(grant_id) is not str or _GRANT.fullmatch(grant_id) is None:
            _refuse("account_ref_invalid")
        if (type(task_scope) is not AuthorizedTaskScope or task_scope.project_id != self._project_id
                or not all(_scope_text(v) for v in
                           (task_scope.project_id, task_scope.customer_id, task_scope.environment_id, task_scope.task_id))):
            _refuse("account_grant_scope_mismatch")
        if expected_source is not None:
            if (type(expected_source) is not GrantSourceStamp or type(expected_source.project_id) is not str
                    or expected_source.project_id != self._project_id or not _reference(expected_source.source_id)
                    or not _reference(expected_source.source_epoch) or not _revision(expected_source.source_revision)
                    or not _digest(expected_source.content_digest) or type(expected_source.schema_version) is not int
                    or expected_source.schema_version != 1):
                _refuse()
        if self._reading:
            _refuse("account_source_unavailable")
        self._reading = True
        try:
            self._binding()
            snapshot = None
            validated = False
            with self._read_transaction() as conn:
                if type(conn) is not sqlite3.Connection or conn is self._last_conn:
                    _refuse()
                self._last_conn = conn
                self._binding()
                snapshot = _read_snapshot(conn, grant_id, task_scope)
                if (snapshot.source.source_id, snapshot.source.source_epoch) != (self._source_id, self._epoch):
                    _refuse("account_source_changed")
                if expected_source is not None and snapshot.source != expected_source:
                    _refuse("account_source_changed")
                previous = self._last_source
                if previous is not None and (snapshot.source.source_revision < previous.source_revision
                        or snapshot.source.source_revision == previous.source_revision and snapshot.source != previous):
                    _refuse("account_source_changed")
                self._binding()
                validated = True
            if not validated or snapshot is None:  # never accept a suppressed failed read
                _refuse("account_source_unavailable")
            # A factory that retains an old read transaction cannot be reused
            # after an intervening read. Native Connection objects cannot reopen.
            try:
                conn.in_transaction
            except sqlite3.ProgrammingError:
                pass
            else:
                _refuse("account_source_invalid")
            self._binding()
            self._last_source = snapshot.source
            return snapshot
        except BrowserAccountSelectionRefused:
            raise
        except SourceProjectScopeError as error:
            raise BrowserAccountSelectionRefused(
                "account_source_unavailable" if error.code == "unavailable" else "account_source_invalid") from None
        except sqlite3.OperationalError:
            raise BrowserAccountSelectionRefused("account_source_unavailable") from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise BrowserAccountSelectionRefused("account_source_invalid") from None
        except Exception:
            raise BrowserAccountSelectionRefused("account_source_unavailable") from None
        finally:
            self._reading = False

    def assert_current(self, snapshot: GrantSnapshot, *, task_scope: AuthorizedTaskScope) -> None:
        if type(snapshot) is not GrantSnapshot or type(snapshot.grant) is not BrowserAccountGrant:
            _refuse()
        current = self.read_grant(snapshot.grant.grant_id, task_scope=task_scope, expected_source=snapshot.source)
        if current != snapshot:
            _refuse("account_source_changed")
