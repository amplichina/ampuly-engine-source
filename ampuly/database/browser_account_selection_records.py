"""Immutable browser account selection provenance records (B06-C1 write leaf).

Project-explicit install only. Never registered as RUNTIME Migration 4.
Writing a selection record does not advance browser_account_catalog_meta
source_revision. Catalog grant/retention/policy writers and dispatch_links
remain out of scope.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

from ampuly.database.browser_account_grants import browser_account_grant_schema_state
from ampuly.database.source_project_scope import require_source_project
from ampuly.layer4_tools.browser_account_selection import (
    AuthorizedTaskScope,
    BrowserAccountSelectionRefused,
    GrantSourceStamp,
)

MAX_ROW_BYTES = 64 * 1024
MAX_REVISION = (1 << 53) - 1
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SELECTION = re.compile(r"bas_[0-9a-f]{32}\Z")
_GRANT = re.compile(r"bag_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TABLE = "browser_account_selection_records"
SELECTION_RECORD_KIND = "selection_record_v1"


def _refuse(reason: str = "account_source_invalid") -> None:
    raise BrowserAccountSelectionRefused(reason)


def _native(conn: sqlite3.Connection, *, require_txn: bool = True) -> None:
    if type(conn) is not sqlite3.Connection or conn.row_factory is not None or conn.text_factory is not str:
        _refuse()
    if require_txn and conn.in_transaction is not True:
        _refuse()


def _reference(value: object) -> bool:
    return type(value) is str and _REF.fullmatch(value) is not None


def _text(value: object, limit: int) -> bool:
    if type(value) is not str or not value or value.strip() != value:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_REVISION


def _digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _rows(conn: sqlite3.Connection, sql: str, params=(), *, limit: int = 2) -> list[tuple]:
    cursor = conn.execute(sql, params)
    try:
        result = cursor.fetchmany(limit + 1)
        if len(result) > limit or any(type(row) is not tuple for row in result):
            _refuse()
        return result
    finally:
        cursor.close()


def _normalize_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split())


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _stamp_dict(stamp: GrantSourceStamp) -> dict:
    if type(stamp) is not GrantSourceStamp:
        _refuse()
    return {
        "project_id": stamp.project_id,
        "source_id": stamp.source_id,
        "source_epoch": stamp.source_epoch,
        "source_revision": stamp.source_revision,
        "content_digest": stamp.content_digest,
        "schema_version": stamp.schema_version,
    }


def _stamp_from(data: object) -> GrantSourceStamp:
    if type(data) is not dict:
        _refuse()
    try:
        return GrantSourceStamp(
            data["project_id"],
            data["source_id"],
            data["source_epoch"],
            data["source_revision"],
            data["content_digest"],
            data.get("schema_version", 1),
        )
    except (KeyError, TypeError, ValueError):
        _refuse()
        raise


def _scope_dict(scope: AuthorizedTaskScope) -> dict:
    if type(scope) is not AuthorizedTaskScope:
        _refuse()
    return {
        "project_id": scope.project_id,
        "customer_id": scope.customer_id,
        "environment_id": scope.environment_id,
        "task_id": scope.task_id,
    }


def _scope_from(data: object) -> AuthorizedTaskScope:
    if type(data) is not dict:
        _refuse()
    try:
        return AuthorizedTaskScope(
            data["project_id"],
            data["customer_id"],
            data["environment_id"],
            data["task_id"],
        )
    except (KeyError, TypeError, ValueError):
        _refuse()
        raise


@dataclass(frozen=True, repr=False)
class SelectionRecordFact:
    """Immutable selection provenance. Not an execution token."""

    record_kind: str
    selection_id: str
    selection_revision: int
    request_id: str
    request_digest: str
    actor_ref: str
    binding_key: str
    task_scope: AuthorizedTaskScope
    grant_id: str
    grant_revision: int
    account_id: str
    source: GrantSourceStamp
    retention_source: GrantSourceStamp
    policy_source: GrantSourceStamp
    retention_receipt_ref: str
    login_policy_ref: str
    allowed_hosts: tuple[str, ...]
    previous_selection_id: str | None
    commit_seq: int


def _build_schema() -> tuple[tuple[str, str, str, str], ...]:
    create = f"""CREATE TABLE {TABLE} (
        selection_id TEXT NOT NULL CHECK(typeof(selection_id)='text' AND length(selection_id)=36),
        selection_revision INTEGER NOT NULL CHECK(typeof(selection_revision)='integer' AND selection_revision BETWEEN 1 AND {MAX_REVISION}),
        request_id TEXT NOT NULL CHECK(typeof(request_id)='text' AND length(CAST(request_id AS BLOB)) BETWEEN 1 AND 128),
        request_digest TEXT NOT NULL CHECK(typeof(request_digest)='text' AND length(request_digest)=64),
        actor_ref TEXT NOT NULL CHECK(typeof(actor_ref)='text' AND length(CAST(actor_ref AS BLOB)) BETWEEN 1 AND 128),
        binding_key TEXT NOT NULL CHECK(typeof(binding_key)='text' AND length(CAST(binding_key AS BLOB)) BETWEEN 1 AND 128),
        payload TEXT NOT NULL CHECK(typeof(payload)='text' AND length(CAST(payload AS BLOB))<={MAX_ROW_BYTES}),
        content_digest TEXT NOT NULL CHECK(typeof(content_digest)='text' AND length(content_digest)=64),
        commit_seq INTEGER NOT NULL CHECK(typeof(commit_seq)='integer' AND commit_seq BETWEEN 1 AND {MAX_REVISION}),
        PRIMARY KEY(selection_id)
    ) WITHOUT ROWID"""
    request_index = f"{TABLE}_request_id_v1"
    objects = [
        (TABLE, "table", TABLE, create),
        (
            request_index,
            "index",
            TABLE,
            f"CREATE UNIQUE INDEX {request_index} ON {TABLE}(request_id)",
        ),
    ]
    triggers = {
        "no_update": f"BEFORE UPDATE ON {TABLE}",
        "no_delete": f"BEFORE DELETE ON {TABLE}",
        "append_seq": f"""BEFORE INSERT ON {TABLE} WHEN
            NEW.commit_seq<>COALESCE((SELECT MAX(commit_seq) FROM {TABLE}),0)+1
            OR NEW.selection_revision<>1""",
    }
    for suffix, clause in triggers.items():
        name = f"{TABLE}_{suffix}_v1"
        objects.append((
            name,
            "trigger",
            TABLE,
            f"CREATE TRIGGER {name} {clause} BEGIN SELECT RAISE(ABORT,'selection_record_guard'); END",
        ))
    return tuple(objects)


_SCHEMA_OBJECTS = _build_schema()
SCHEMA_STATEMENTS = tuple(item[3] for item in _SCHEMA_OBJECTS)


def browser_account_selection_record_schema_state(conn: sqlite3.Connection) -> str:
    """Exact bundle probe; does not require an open write transaction."""
    try:
        _native(conn, require_txn=False)
        objects = tuple(item[0] for item in _SCHEMA_OBJECTS)
        query = (
            "SELECT type,name,tbl_name,CASE WHEN typeof(sql)='text' "
            "AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END "
            "FROM main.sqlite_schema WHERE name IN ("
            + ",".join("?" for _ in objects)
            + ") ORDER BY name"
        )
        found = _rows(conn, query + " LIMIT ?", (*objects, len(objects) + 2), limit=len(objects))
        if not found:
            return "absent"
        expected = {
            name: (kind, table, _normalize_sql(sql))
            for name, kind, table, sql in _SCHEMA_OBJECTS
        }
        actual = {}
        for kind, name, table, sql in found:
            if name in actual or name not in expected or type(sql) is not str:
                return "incomplete"
            actual[name] = (kind, table, _normalize_sql(sql))
        if actual != expected:
            return "incomplete"
        return "complete"
    except BrowserAccountSelectionRefused:
        raise
    except sqlite3.OperationalError:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None


def install_browser_account_selection_records(conn, *, project_id: str) -> None:
    """Explicit MigrationConnection install; never RUNTIME Migration 4."""
    from ampuly.database.schema_version import MigrationConnection

    try:
        if type(conn) is not MigrationConnection:
            _refuse()
        require_source_project(conn._conn, project_id, write=True)
        if browser_account_grant_schema_state(conn._conn) != "complete":
            _refuse()
        state = browser_account_selection_record_schema_state(conn._conn)
        if state == "absent":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement).close()
            state = browser_account_selection_record_schema_state(conn._conn)
        if state != "complete":
            _refuse()
    except BrowserAccountSelectionRefused:
        raise
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None


def _request_digest(
    *,
    actor_ref: str,
    binding_key: str,
    task_scope: AuthorizedTaskScope,
    grant_id: str,
    grant_revision: int,
    account_id: str,
    source: GrantSourceStamp,
    retention_source: GrantSourceStamp,
    policy_source: GrantSourceStamp,
    retention_receipt_ref: str,
    login_policy_ref: str,
    allowed_hosts: tuple[str, ...],
    previous_selection_id: str | None,
) -> str:
    body = {
        "actor_ref": actor_ref,
        "binding_key": binding_key,
        "task_scope": _scope_dict(task_scope),
        "grant_id": grant_id,
        "grant_revision": grant_revision,
        "account_id": account_id,
        "source": _stamp_dict(source),
        "retention_source": _stamp_dict(retention_source),
        "policy_source": _stamp_dict(policy_source),
        "retention_receipt_ref": retention_receipt_ref,
        "login_policy_ref": login_policy_ref,
        "allowed_hosts": list(allowed_hosts),
        "previous_selection_id": previous_selection_id,
    }
    return _sha(_json(body))


def _payload_dict(
    *,
    selection_id: str,
    selection_revision: int,
    request_id: str,
    request_digest: str,
    actor_ref: str,
    binding_key: str,
    task_scope: AuthorizedTaskScope,
    grant_id: str,
    grant_revision: int,
    account_id: str,
    source: GrantSourceStamp,
    retention_source: GrantSourceStamp,
    policy_source: GrantSourceStamp,
    retention_receipt_ref: str,
    login_policy_ref: str,
    allowed_hosts: tuple[str, ...],
    previous_selection_id: str | None,
    commit_seq: int,
) -> dict:
    return {
        "record_kind": SELECTION_RECORD_KIND,
        "selection_id": selection_id,
        "selection_revision": selection_revision,
        "request_id": request_id,
        "request_digest": request_digest,
        "actor_ref": actor_ref,
        "binding_key": binding_key,
        "task_scope": _scope_dict(task_scope),
        "grant_id": grant_id,
        "grant_revision": grant_revision,
        "account_id": account_id,
        "source": _stamp_dict(source),
        "retention_source": _stamp_dict(retention_source),
        "policy_source": _stamp_dict(policy_source),
        "retention_receipt_ref": retention_receipt_ref,
        "login_policy_ref": login_policy_ref,
        "allowed_hosts": list(allowed_hosts),
        "previous_selection_id": previous_selection_id,
        "commit_seq": commit_seq,
    }


def _fact_from_payload(data: object) -> SelectionRecordFact:
    if type(data) is not dict:
        _refuse()
    try:
        hosts = data["allowed_hosts"]
        if type(hosts) is not list or any(type(h) is not str for h in hosts):
            _refuse()
        prev = data["previous_selection_id"]
        if prev is not None and not (type(prev) is str and _SELECTION.fullmatch(prev)):
            _refuse()
        fact = SelectionRecordFact(
            data["record_kind"],
            data["selection_id"],
            data["selection_revision"],
            data["request_id"],
            data["request_digest"],
            data["actor_ref"],
            data["binding_key"],
            _scope_from(data["task_scope"]),
            data["grant_id"],
            data["grant_revision"],
            data["account_id"],
            _stamp_from(data["source"]),
            _stamp_from(data["retention_source"]),
            _stamp_from(data["policy_source"]),
            data["retention_receipt_ref"],
            data["login_policy_ref"],
            tuple(hosts),
            prev,
            data["commit_seq"],
        )
    except (KeyError, TypeError, ValueError):
        _refuse()
        raise
    if (
        fact.record_kind != SELECTION_RECORD_KIND
        or not _SELECTION.fullmatch(fact.selection_id)
        or not _revision(fact.selection_revision)
        or not _reference(fact.request_id)
        or not _digest(fact.request_digest)
        or not _reference(fact.actor_ref)
        or not _reference(fact.binding_key)
        or not _GRANT.fullmatch(fact.grant_id)
        or not _revision(fact.grant_revision)
        or not _digest(fact.account_id)
        or not _reference(fact.retention_receipt_ref)
        or not _reference(fact.login_policy_ref)
        or not _revision(fact.commit_seq)
    ):
        _refuse()
    return fact


def commit_selection_record(
    conn: sqlite3.Connection,
    *,
    actor_ref: str,
    request_id: str,
    binding_key: str,
    task_scope: AuthorizedTaskScope,
    grant_id: str,
    grant_revision: int,
    account_id: str,
    source: GrantSourceStamp,
    retention_source: GrantSourceStamp,
    policy_source: GrantSourceStamp,
    retention_receipt_ref: str,
    login_policy_ref: str,
    allowed_hosts: tuple[str, ...],
    previous_selection_id: str | None = None,
) -> SelectionRecordFact:
    """Append-only insert or idempotent same-digest return. Never touches catalog meta."""
    try:
        _native(conn, require_txn=True)
        if browser_account_selection_record_schema_state(conn) != "complete":
            _refuse()
        if (
            not _reference(actor_ref)
            or not _reference(request_id)
            or not _reference(binding_key)
            or type(task_scope) is not AuthorizedTaskScope
            or not _GRANT.fullmatch(grant_id)
            or not _revision(grant_revision)
            or not _digest(account_id)
            or type(source) is not GrantSourceStamp
            or type(retention_source) is not GrantSourceStamp
            or type(policy_source) is not GrantSourceStamp
            or not _reference(retention_receipt_ref)
            or not _reference(login_policy_ref)
            or type(allowed_hosts) is not tuple
            or not allowed_hosts
            or any(type(h) is not str or not _text(h, 253) for h in allowed_hosts)
        ):
            _refuse()
        if previous_selection_id is not None and (
            type(previous_selection_id) is not str
            or _SELECTION.fullmatch(previous_selection_id) is None
        ):
            _refuse()
        request_digest = _request_digest(
            actor_ref=actor_ref,
            binding_key=binding_key,
            task_scope=task_scope,
            grant_id=grant_id,
            grant_revision=grant_revision,
            account_id=account_id,
            source=source,
            retention_source=retention_source,
            policy_source=policy_source,
            retention_receipt_ref=retention_receipt_ref,
            login_policy_ref=login_policy_ref,
            allowed_hosts=allowed_hosts,
            previous_selection_id=previous_selection_id,
        )
        existing = _rows(
            conn,
            f"SELECT selection_id, request_digest, payload FROM {TABLE} WHERE request_id=? LIMIT ?",
            (request_id, 2),
            limit=1,
        )
        if existing:
            selection_id, prior_digest, payload = existing[0]
            if prior_digest != request_digest or type(payload) is not str:
                _refuse("account_source_invalid")
            data = json.loads(payload)
            fact = _fact_from_payload(data)
            if fact.request_digest != request_digest or fact.selection_id != selection_id:
                _refuse()
            return fact
        selection_id = "bas_" + request_digest[:32]
        if _SELECTION.fullmatch(selection_id) is None:
            _refuse()
        seq_rows = _rows(conn, f"SELECT COALESCE(MAX(commit_seq),0) FROM {TABLE} LIMIT ?", (1,), limit=1)
        commit_seq = int(seq_rows[0][0]) + 1
        selection_revision = 1
        payload_obj = _payload_dict(
            selection_id=selection_id,
            selection_revision=selection_revision,
            request_id=request_id,
            request_digest=request_digest,
            actor_ref=actor_ref,
            binding_key=binding_key,
            task_scope=task_scope,
            grant_id=grant_id,
            grant_revision=grant_revision,
            account_id=account_id,
            source=source,
            retention_source=retention_source,
            policy_source=policy_source,
            retention_receipt_ref=retention_receipt_ref,
            login_policy_ref=login_policy_ref,
            allowed_hosts=allowed_hosts,
            previous_selection_id=previous_selection_id,
            commit_seq=commit_seq,
        )
        payload = _json(payload_obj)
        if len(payload.encode("utf-8")) > MAX_ROW_BYTES:
            _refuse()
        content_digest = _sha(payload)
        conn.execute(
            f"INSERT INTO {TABLE}("
            "selection_id,selection_revision,request_id,request_digest,"
            "actor_ref,binding_key,payload,content_digest,commit_seq"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                selection_id,
                selection_revision,
                request_id,
                request_digest,
                actor_ref,
                binding_key,
                payload,
                content_digest,
                commit_seq,
            ),
        ).close()
        return _fact_from_payload(payload_obj)
    except BrowserAccountSelectionRefused:
        raise
    except sqlite3.IntegrityError:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None


def read_selection_record(conn: sqlite3.Connection, *, selection_id: str) -> SelectionRecordFact:
    try:
        _native(conn, require_txn=False)
        if type(selection_id) is not str or _SELECTION.fullmatch(selection_id) is None:
            _refuse("account_ref_invalid")
        if browser_account_selection_record_schema_state(conn) != "complete":
            _refuse("account_source_not_ready")
        rows = _rows(
            conn,
            f"SELECT payload FROM {TABLE} WHERE selection_id=? LIMIT ?",
            (selection_id, 2),
            limit=1,
        )
        if not rows:
            _refuse("account_grant_not_found")
        payload = rows[0][0]
        if type(payload) is not str:
            _refuse()
        fact = _fact_from_payload(json.loads(payload))
        if fact.selection_id != selection_id:
            _refuse()
        return fact
    except BrowserAccountSelectionRefused:
        raise
    except sqlite3.OperationalError:
        raise BrowserAccountSelectionRefused("account_source_unavailable") from None
    except Exception:
        raise BrowserAccountSelectionRefused("account_source_invalid") from None
