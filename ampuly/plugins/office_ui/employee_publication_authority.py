"""Staged, host-internal publication ledger; no production provider or RPC.

DI is NOT proof of D2 isolation. The host must supply non-creating, bounded,
private connections, a real frozen owner/evidence lease and a native read-only
observer. No model is run here. Host-only dispatch can invoke a fixed, bounded
asset writer after a confirmed durable claim; it is not a production adapter.
Pending records never expire/release automatically. No recovery writes exist.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Callable, ContextManager, Protocol

from .ai_employee_contract import (
    AIEmployeeContractError, MAX_FILE_BYTES, canonical_json, package_fingerprint,
    validate_package_manifest,
)
from .employee_package_versions import (
    MAX_LEDGER_BYTES, VersionCatalog, _decode, _package_id, _sha, _text, _time,
)
from .employee_publication_namespace import validate_publication_layout
from .employee_publication_contents import MAX_SKILL_FILE_BYTES, bounded_publication_paths, publication_content_fingerprint

if TYPE_CHECKING:
    from .employee_publication_source_snapshot import PublicationSourceSnapshot

ZERO = "0" * 64
MAX_HEADS = 2000
MAX_REQUESTS = 10000  # Fail closed; never prune durable request tombstones.
MAX_EPOCH = 2**53 - 1
_ACTIONS = frozenset({"employee_package_publish", "employee_package_retire"})


def _fail(code: str = "publication_authority_invalid") -> None:
    raise AIEmployeeContractError(code, "发布权威记录不可用或不一致；未回显内部数据。")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _epoch(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_EPOCH:
        _fail()
    return value


def _require_sync_callback(callback: object, code: str) -> None:
    if callback is None:
        return  # Missing dependencies have their existing pre-marker codes.
    try:
        call_method = getattr(callback, "__call__", None)
        asynchronous = any(inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value)
                           for value in (callback, call_method))
        valid = callable(callback) and not asynchronous
    except Exception:
        _fail(code)
    if not valid:
        _fail(code)


def _reject_async_result(result: object) -> None:
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        # Closing only an unstarted native coroutine runs no coroutine body.
        # Never await it, resume it, or call arbitrary awaitable cleanup methods.
        if inspect.iscoroutine(result) and inspect.getcoroutinestate(result) == inspect.CORO_CREATED:
            result.close()
        _fail("publication_callback_async_unsupported")


@dataclass(frozen=True)
class RequestIdentity:
    action: str
    project_id: str | None  # Explicit null is distinct from every named scope.
    request_id: str
    actor: str
    payload_sha256: str

    def key(self) -> str:
        _identity(self)
        return _digest(canonical_json([self.action, self.project_id, self.request_id]))


def _identity(value: RequestIdentity) -> dict:
    if (type(value) is not RequestIdentity or type(value.action) is not str
            or value.action not in _ACTIONS):
        _fail()
    if value.project_id is not None:
        _text(value.project_id, "scope", 128)
    _text(value.request_id, "request_id", 128)
    _text(value.actor, "actor", 128)
    _sha(value.payload_sha256, "payload_sha256")
    return {"action": value.action, "project_id": value.project_id,
            "request_id": value.request_id, "actor": value.actor,
            "payload_sha256": value.payload_sha256}


@dataclass(frozen=True)
class ExpectedFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class FrozenPublication:
    request: RequestIdentity
    package_id: str
    version: str
    fingerprint: str
    at: str
    effect_id: str
    expected_head_epoch: int
    before_tail: str
    manifest_raw: bytes
    files: tuple[ExpectedFile, ...]
    target_scope_ref: str
    context_digest: str
    evidence_ref: str
    source_draft_revision: str | None
    reason: str | None = None


def _plan_data(plan: FrozenPublication) -> dict:
    if type(plan) is not FrozenPublication:
        _fail()
    identity = _identity(plan.request)
    _package_id(plan.package_id)
    _text(plan.version, "version")
    _sha(plan.fingerprint, "fingerprint")
    _time(plan.at, "at")
    _text(plan.effect_id, "effect_id", 128)
    _epoch(plan.expected_head_epoch)
    _sha(plan.before_tail, "before_tail")
    _sha(plan.context_digest, "context_digest")
    _text(plan.evidence_ref, "evidence_ref", 256)
    _text(plan.target_scope_ref, "target_scope_ref", 256)
    manifest = _decode(plan.manifest_raw, "manifest")
    if type(manifest) is not dict:
        _fail()
    if (manifest.get("status") != "published"
            or manifest.get("version") != plan.version):
        _fail("publication_authority_unsupported")
    if any(row["severity"] == "error" for row in validate_package_manifest(manifest)):
        _fail()
    layout = validate_publication_layout(manifest, package_id=plan.package_id,
                                         fingerprint=plan.fingerprint)
    if plan.request.action == "employee_package_publish":
        _sha(plan.source_draft_revision, "source_draft_revision")
        if (plan.reason is not None or manifest.get("published_by") != plan.request.actor
                or manifest.get("published_at") != plan.at):
            _fail()
    else:
        _text(plan.reason, "reason", 4096, multiline=True)
        if plan.source_draft_revision is not None:
            _fail()
    if type(plan.files) is not tuple or len(plan.files) >= 64:
        _fail()
    if any(type(row) is not ExpectedFile for row in plan.files):
        _fail()
    ordered_paths = bounded_publication_paths(manifest, (row.path for row in plan.files))
    expected_paths = set(ordered_paths)
    rows = []
    total = len(plan.manifest_raw)
    for row in plan.files:
        if type(row) is not ExpectedFile or type(row.path) is not str or row.path not in expected_paths:
            _fail()
        if type(row.size) is not int or not 0 <= row.size <= MAX_FILE_BYTES:
            _fail()
        if (row.path not in {item["source_ref"] for item in layout["declared_files"]}
                and row.size > MAX_SKILL_FILE_BYTES):
            _fail()
        _sha(row.sha256, "file_sha256")
        total += row.size
        rows.append({"path": row.path, "size": row.size, "sha256": row.sha256})
    if (total > MAX_FILE_BYTES or len({row["path"] for row in rows}) != len(rows)
            or {row["path"] for row in rows} != expected_paths):
        _fail()
    result = {"schema_version": 1, "request": identity, "package_id": plan.package_id,
              "version": plan.version, "fingerprint": plan.fingerprint, "at": plan.at,
              "effect_id": plan.effect_id, "expected_head_epoch": plan.expected_head_epoch,
              "before_tail": plan.before_tail, "manifest_sha256": _digest(plan.manifest_raw),
              "manifest_size": len(plan.manifest_raw),
              "files": [next(row for row in rows if row["path"] == path) for path in ordered_paths],
              "target_scope_ref": plan.target_scope_ref, "context_digest": plan.context_digest,
              "evidence_ref": plan.evidence_ref, "source_draft_revision": plan.source_draft_revision,
              "reason": plan.reason}
    canonical_json(result)  # Existing bounded/depth-safe JSON contract.
    return result


@dataclass(frozen=True)
class AuthorizationLease:
    request: RequestIdentity
    package_id: str
    plan_sha256: str
    context_digest: str
    evidence_ref: str
    authorization_epoch: int
    receipt_id: str


@dataclass(frozen=True)
class GenesisReceipt:
    package_id: str
    authority_ref: str
    authorization_epoch: int
    index_raw: bytes | None  # Only an explicitly confirmed empty genesis.


class TrustedAuthorization(Protocol):
    """Internal host seam, not a callback accepting browser 'is_owner'.

    freeze must authenticate scope/actor, verify real readiness/approval facts
    and return a lease binding all arguments. verify must recheck live rights,
    revocation and the SAME frozen facts, returning the SAME receipt. Lookup
    checks current visibility rights, not a grant to execute an expired trial.
    Atomicity with the real owner epoch still needs a production lease contract.
    """
    def genesis(self, package_id: str) -> GenesisReceipt: ...
    def verify_genesis(self, receipt: GenesisReceipt) -> GenesisReceipt: ...
    def freeze(self, plan: FrozenPublication, plan_sha256: str) -> AuthorizationLease: ...
    def verify(self, plan: FrozenPublication, lease: AuthorizationLease,
               phase: str) -> AuthorizationLease: ...


def _lease_data(lease: AuthorizationLease, plan: FrozenPublication, digest: str) -> dict:
    if (type(lease) is not AuthorizationLease or lease.request != plan.request
            or lease.package_id != plan.package_id or lease.plan_sha256 != digest
            or lease.context_digest != plan.context_digest or lease.evidence_ref != plan.evidence_ref):
        _fail("publication_authorization_invalid")
    _epoch(lease.authorization_epoch)
    _text(lease.receipt_id, "authorization_receipt", 256)
    return {"request": _identity(lease.request), "package_id": lease.package_id,
            "plan_sha256": lease.plan_sha256, "context_digest": lease.context_digest,
            "evidence_ref": lease.evidence_ref, "authorization_epoch": lease.authorization_epoch,
            "receipt_id": lease.receipt_id}


@dataclass(frozen=True)
class PreparedRef:
    request_key: str
    plan_sha256: str
    package_id: str
    fence: int
    effect_id: str


@dataclass(frozen=True)
class LedgerStatus:
    reference: PreparedRef
    state: str
    after_tail: str
    after_index_raw: bytes
    # Historical ledger fact only; not a client success/adoption receipt.


@dataclass(frozen=True)
class ObservedFile:
    path: str
    raw: bytes


@dataclass(frozen=True)
class AssetObservation:
    request_key: str
    plan_sha256: str
    package_id: str
    effect_id: str
    target_scope_ref: str
    index_raw: bytes
    manifest_raw: bytes
    files: tuple[ObservedFile, ...]


# Only explicit test migration calls this DDL. No user_version, no scripts,
# no transaction ownership or filesystem operations in the migration step.
_DDL = (
    """CREATE TABLE IF NOT EXISTS ae_publication_meta (version INTEGER PRIMARY KEY, contract_sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ae_publication_heads (
        package_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, tail TEXT NOT NULL,
        index_raw BLOB, raw_sha256 TEXT, genesis_json BLOB NOT NULL, last_request_key TEXT)""",
    """CREATE TABLE IF NOT EXISTS ae_publication_requests (
        request_key TEXT PRIMARY KEY, package_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
        plan_json BLOB NOT NULL, manifest_raw BLOB NOT NULL, lease_json BLOB NOT NULL,
        fence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','committing','unknown','committed')),
        after_tail TEXT NOT NULL, after_index_raw BLOB NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ae_publication_reservations (
        package_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, fence INTEGER NOT NULL)""",
)
_SCHEMA_SHA = _digest(canonical_json(list(_DDL)))
_COLUMNS = {
    "ae_publication_meta": ("version", "contract_sha256"),
    "ae_publication_heads": ("package_id", "epoch", "tail", "index_raw", "raw_sha256", "genesis_json", "last_request_key"),
    "ae_publication_requests": ("request_key", "package_id", "plan_sha256", "plan_json", "manifest_raw", "lease_json", "fence", "state", "after_tail", "after_index_raw"),
    "ae_publication_reservations": ("package_id", "request_key", "fence"),
}
_TYPES = {
    "ae_publication_meta": ("INTEGER", "TEXT"),
    "ae_publication_heads": ("TEXT", "INTEGER", "TEXT", "BLOB", "TEXT", "BLOB", "TEXT"),
    "ae_publication_requests": ("TEXT", "TEXT", "TEXT", "BLOB", "BLOB", "BLOB", "INTEGER", "TEXT", "TEXT", "BLOB"),
    "ae_publication_reservations": ("TEXT", "TEXT", "INTEGER"),
}
_NULLABLE = {"ae_publication_meta": {"version"},
             "ae_publication_heads": {"package_id", "index_raw", "raw_sha256", "last_request_key"},
             "ae_publication_requests": {"request_key"},
             "ae_publication_reservations": {"package_id"}}


def initialize_empty_authority(conn: sqlite3.Connection) -> None:
    """Initialize a NEW, empty private asset ledger, never a runtime migration.

    This is called only from an explicit owner lifecycle write. A pre-existing
    database, including an old authority database, is not adopted or modified.
    """
    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        _fail("publication_schema_transaction_required")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        _fail("publication_schema_unavailable")
    for statement in _DDL:
        conn.execute(statement)
    conn.execute("INSERT INTO ae_publication_meta VALUES(1,?)", (_SCHEMA_SHA,))
    _require_schema(conn)


def apply_test_schema(conn: sqlite3.Connection) -> None:
    """Explicit synthetic-DB migration step. Caller owns an active transaction.

    Not registered with schema_version or BusinessStore. A production migration
    needs separate approval, backup/version selection and lifecycle review.
    """
    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        _fail("publication_schema_transaction_required")
    for statement in _DDL:
        conn.execute(statement)
    conn.execute("INSERT OR IGNORE INTO ae_publication_meta VALUES(1,?)", (_SCHEMA_SHA,))
    _require_schema(conn)


def _require_schema(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMNS.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if (tuple(row[1] for row in info) != columns
                or tuple(row[2] for row in info) != _TYPES[table]
                or tuple(row[3] for row in info) != tuple(0 if name in _NULLABLE[table] else 1 for name in columns)
                or tuple(row[5] for row in info) != (1,) + (0,) * (len(columns) - 1)):
            _fail("publication_schema_unavailable")
    rows = conn.execute("SELECT version, contract_sha256 FROM ae_publication_meta LIMIT 2").fetchall()
    if [tuple(row) for row in rows] != [(1, _SCHEMA_SHA)]:
        _fail("publication_schema_unavailable")


class PublicationAuthorityLedger:
    """Narrow persistence leaf. Both factories own/close PRIVATE connections.

    Read factory must open an existing DB with SQLite mode=ro; write factory
    must use mode=rw, neither creates a path/table. No factory is called at
    construction. Connections enter with no transaction; this leaf owns BEGIN,
    commit and rollback. Factories/observers must be bounded and non-reentrant.
    Observer must inventory regular, native-pinned bytes and verify original
    effect provenance. Its dataclass is not a security proof by itself. Writer
    is fixed by the host at construction; it receives the exact frozen AFTER
    index. Its return value is never evidence of a successful publication.
    """
    def __init__(self, *, read_connection: Callable[[], ContextManager[sqlite3.Connection]],
                 write_connection: Callable[[], ContextManager[sqlite3.Connection]],
                 authorization: TrustedAuthorization | None = None,
                  observer: Callable[[PreparedRef, FrozenPublication], AssetObservation] | None = None,
                  writer: Callable[[PreparedRef, FrozenPublication, bytes], None] | None = None,
                  source_root: Path | None = None,
                  source_bound_writer: Callable[[PreparedRef, FrozenPublication, bytes, PublicationSourceSnapshot], None] | None = None) -> None:
        _require_sync_callback(writer, "publication_writer_unsupported")
        _require_sync_callback(observer, "publication_observer_unsupported")
        _require_sync_callback(source_bound_writer, "publication_source_writer_unsupported")
        self._read_connection = read_connection
        self._write_connection = write_connection
        self._authorization = authorization
        self._observer = observer
        self._writer = writer
        self._source_root = source_root
        self._source_bound_writer = source_bound_writer
        self._commit_active = False  # Object-local guard, not a durable fence.

    @contextmanager
    def _transaction(self, *, write: bool):
        factory = self._write_connection if write else self._read_connection
        commit_attempted = False
        body_error = None
        cleanup_error = None
        rolled_back = False
        own_rejection = None
        owns_transaction = False
        try:
            with factory() as conn:
                try:
                    if type(conn) is not sqlite3.Connection or conn.in_transaction:
                        # Never rollback a transaction this leaf doesn't own.
                        own_rejection = AIEmployeeContractError("publication_connection_invalid",
                                                                 "发布连接不能共享活动事务。")
                        raise own_rejection
                    # SELECT results are plain tuples even if host uses sqlite3.Row.
                    conn.row_factory = None
                    # The validated connection has no foreign active transaction.
                    # Own the BEGIN attempt too, including a failed BEGIN receipt.
                    owns_transaction = True
                    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    _require_schema(conn)
                    yield conn
                    if write:
                        commit_attempted = True
                        conn.commit()
                    else:
                        conn.rollback()  # End read snapshot; never commit/query DDL.
                except BaseException as exc:
                    body_error = exc
                    if owns_transaction:
                        try:
                            conn.rollback()
                            rolled_back = not conn.in_transaction
                        except BaseException as rollback_exc:
                            cleanup_error = rollback_exc
                            raise
                    raise
            # A host factory may return True from __exit__, suppressing an error
            # raised inside its with block. Suppression must never turn a failed
            # claim into permission to dispatch or a failed effect into success.
            if cleanup_error is not None:
                raise cleanup_error
            if body_error is not None:
                raise body_error
            if own_rejection is not None:
                raise own_rejection
        except BaseException as exc:
            # Preserve cancellation even if rollback or factory exit replaces or
            # suppresses it. Also preserve a cancellation raised during cleanup.
            if body_error is not None and not isinstance(body_error, Exception):
                raise body_error
            if cleanup_error is not None and not isinstance(cleanup_error, Exception):
                raise cleanup_error
            if not isinstance(exc, Exception):
                raise
            if (not commit_attempted and isinstance(exc, AIEmployeeContractError)
                    and (exc is own_rejection or exc is body_error and rolled_back and cleanup_error is None)):
                raise
            # Write outcome can be uncertain, including a lost COMMIT receipt.
            # In particular, factory.__exit__ may raise ContractError AFTER
            # COMMIT: its error category cannot prove an effect-free rejection.
            code = "publication_outcome_unknown" if write else "publication_authority_unavailable"
            raise AIEmployeeContractError(code, "发布账本未确认；仅按原请求只读核对。") from exc

    def _provider(self) -> TrustedAuthorization:
        if self._authorization is None:
            _fail("publication_authority_unavailable")
        return self._authorization

    def _authorization_call(self, method: str, *arguments):
        try:
            return getattr(self._provider(), method)(*arguments)
        except Exception as exc:
            raise AIEmployeeContractError("publication_authorization_unavailable",
                                          "授权来源未确认；未回显内部数据。") from exc

    @staticmethod
    def _head(conn: sqlite3.Connection, package_id: str) -> tuple:
        row = conn.execute("SELECT epoch,tail,index_raw,raw_sha256,genesis_json,last_request_key FROM ae_publication_heads WHERE package_id=? AND (index_raw IS NULL OR length(index_raw)<=?) AND length(genesis_json)<=?",
                           (package_id, MAX_LEDGER_BYTES, MAX_LEDGER_BYTES)).fetchone()
        if row is None:
            _fail("publication_genesis_unavailable")
        epoch, tail, raw, digest, genesis, last_key = row
        _epoch(epoch)
        _sha(tail, "head_tail")
        if (raw is None and (digest is not None or epoch != 0 or tail != ZERO)
                or raw is not None and (type(raw) is not bytes or digest != _digest(raw))):
            _fail()
        catalog = VersionCatalog.from_index(package_id, raw, expected_tail_sha256=tail)
        if len(catalog.records) != epoch:
            _fail()
        evidence = _decode(genesis, "genesis")
        if (type(evidence) is not dict or set(evidence) != {"package_id", "authority_ref", "authorization_epoch", "empty_index_sha256"}
                or evidence["package_id"] != package_id):
            _fail()
        _text(evidence["authority_ref"], "genesis_ref", 256)
        _epoch(evidence["authorization_epoch"])
        if evidence["empty_index_sha256"] not in {None, _digest(b"")}:
            _fail()
        if epoch == 0 and digest != evidence["empty_index_sha256"]:
            _fail()
        if epoch == 0:
            if last_key is not None:
                _fail()
        else:
            _sha(last_key, "last_request_key")
            previous = PublicationAuthorityLedger._load(conn, last_key)
            if previous is None:
                _fail()
            last_plan, _, last_status = PublicationAuthorityLedger._unpack(last_key, previous)
            if (last_plan.package_id != package_id or last_status.state != "committed"
                    or last_status.reference.fence != epoch or last_status.after_tail != tail
                    or last_status.after_index_raw != raw):
                _fail()
        return row

    def current(self, package_id: str) -> dict:
        _package_id(package_id)
        with self._transaction(write=False) as conn:
            epoch, tail, raw, digest, _, _ = self._head(conn, package_id)
            return {"package_id": package_id, "epoch": epoch, "tail": tail,
                    "index_raw_sha256": digest, "index_missing": raw is None}

    def establish_genesis(self, package_id: str) -> None:
        """Explicit host operation; never called by prepare/read or migrations."""
        _package_id(package_id)
        receipt = self._authorization_call("genesis", package_id)
        if (type(receipt) is not GenesisReceipt or receipt.package_id != package_id
                or receipt.index_raw not in (None, b"") or type(receipt.index_raw) not in (bytes, type(None))):
            _fail("publication_authorization_invalid")
        _text(receipt.authority_ref, "genesis_ref", 256)
        _epoch(receipt.authorization_epoch)
        with self._transaction(write=True) as conn:
            if conn.execute("SELECT 1 FROM ae_publication_heads WHERE package_id=?", (package_id,)).fetchone():
                _fail("publication_genesis_exists")
            if conn.execute("SELECT COUNT(*) FROM ae_publication_heads").fetchone()[0] >= MAX_HEADS:
                _fail("publication_authority_limit")
            verified = self._authorization_call("verify_genesis", receipt)
            if type(verified) is not GenesisReceipt or verified != receipt:
                _fail("publication_authorization_invalid")
            digest = None if receipt.index_raw is None else _digest(receipt.index_raw)
            evidence = canonical_json({"package_id": package_id, "authority_ref": receipt.authority_ref,
                                       "authorization_epoch": receipt.authorization_epoch, "empty_index_sha256": digest})
            conn.execute("INSERT INTO ae_publication_heads VALUES(?,0,?,?,?,?,NULL)",
                         (package_id, ZERO, receipt.index_raw, digest, evidence))

    @staticmethod
    def _load(conn: sqlite3.Connection, key: str) -> tuple | None:
        row = conn.execute("SELECT package_id,plan_sha256,plan_json,manifest_raw,lease_json,fence,state,after_tail,after_index_raw FROM ae_publication_requests WHERE request_key=? AND length(plan_json)<=? AND length(manifest_raw)<=? AND length(lease_json)<=? AND length(after_index_raw)<=?",
                           (key, MAX_LEDGER_BYTES, MAX_LEDGER_BYTES, MAX_LEDGER_BYTES, MAX_LEDGER_BYTES)).fetchone()
        if row is None and conn.execute("SELECT 1 FROM ae_publication_requests WHERE request_key=?", (key,)).fetchone():
            _fail()
        return row

    @staticmethod
    def _unpack(key: str, row: tuple) -> tuple[FrozenPublication, AuthorizationLease, LedgerStatus]:
        package, digest, data_raw, manifest, lease_raw, fence, state, tail, after = row
        data = _decode(data_raw, "frozen_plan")
        if type(data) is not dict:
            _fail()
        try:
            request = RequestIdentity(**data["request"])
            plan = FrozenPublication(request=request, manifest_raw=manifest,
                                     files=tuple(ExpectedFile(**item) for item in data["files"]),
                                     **{name: data[name] for name in (
                                         "package_id", "version", "fingerprint", "at", "effect_id",
                                         "expected_head_epoch", "before_tail", "target_scope_ref",
                                         "context_digest", "evidence_ref", "source_draft_revision", "reason")})
            rebuilt = canonical_json(_plan_data(plan))
            if rebuilt != data_raw or _digest(rebuilt) != digest or request.key() != key or package != plan.package_id:
                _fail()
            frozen = _decode(lease_raw, "lease")
            lease = AuthorizationLease(request=RequestIdentity(**frozen["request"]),
                                       **{name: value for name, value in frozen.items() if name != "request"})
            if canonical_json(_lease_data(lease, plan, digest)) != lease_raw:
                _fail()
        except (KeyError, TypeError, ValueError) as exc:
            raise AIEmployeeContractError("publication_authority_invalid", "发布冻结记录损坏。") from exc
        _epoch(fence)
        if fence != plan.expected_head_epoch + 1 or state not in {"prepared", "committing", "unknown", "committed"}:
            _fail()
        catalog = VersionCatalog.from_index(package, after, expected_tail_sha256=tail)
        if len(catalog.records) != fence or not catalog.records:
            _fail()
        suffix = canonical_json(catalog.records[-1]) + b"\n"
        if not after.endswith(suffix):
            _fail()
        before = after[:-len(suffix)]
        exact_after, exact_tail = _append_from_head(plan, before, plan.before_tail)
        if after != exact_after or tail != exact_tail:
            _fail()
        reference = PreparedRef(key, digest, package, fence, plan.effect_id)
        return plan, lease, LedgerStatus(reference, "unknown" if state == "committing" else state, tail, after)

    def _verify(self, plan: FrozenPublication, lease: AuthorizationLease, digest: str, phase: str) -> None:
        current = self._authorization_call("verify", plan, lease, phase)
        _lease_data(current, plan, digest)
        if current != lease:
            _fail("publication_authorization_changed")

    def _coherence(self, conn: sqlite3.Connection, plan: FrozenPublication, status: LedgerStatus) -> tuple:
        head = self._head(conn, plan.package_id)
        epoch, tail, raw, _, _, _ = head
        reservation = conn.execute("SELECT request_key,fence FROM ae_publication_reservations WHERE package_id=?", (plan.package_id,)).fetchone()
        own = (status.reference.request_key, status.reference.fence)
        if status.state == "committed":
            # Later committed appends are allowed; this request is historical,
            # not proof that its after-tail remains the current head.
            if (epoch < status.reference.fence or type(raw) is not bytes
                    or not raw.startswith(status.after_index_raw) or reservation == own):
                _fail()
        elif (epoch != plan.expected_head_epoch or tail != plan.before_tail or reservation != own):
            _fail("publication_head_conflict")
        return head

    def lookup(self, identity: RequestIdentity, *, plan_sha256: str | None = None) -> LedgerStatus | None:
        key = identity.key()
        if plan_sha256 is not None:
            _sha(plan_sha256, "plan_sha256")
        with self._transaction(write=False) as conn:
            row = self._load(conn, key)
            if row is None:
                return None
            plan, lease, status = self._unpack(key, row)
            if identity != plan.request or plan_sha256 is not None and plan_sha256 != status.reference.plan_sha256:
                _fail("request_id_reused")
            self._verify(plan, lease, status.reference.plan_sha256, "lookup")
            self._coherence(conn, plan, status)
            return status

    def prepare(self, plan: FrozenPublication) -> LedgerStatus:
        data_raw = canonical_json(_plan_data(plan))
        digest = _digest(data_raw)
        key = plan.request.key()
        with self._transaction(write=True) as conn:
            existing = self._load(conn, key)
            if existing is not None:
                old_plan, lease, status = self._unpack(key, existing)
                if data_raw != existing[2] or plan.manifest_raw != old_plan.manifest_raw:
                    _fail("request_id_reused")
                self._verify(old_plan, lease, digest, "lookup")
                self._coherence(conn, old_plan, status)
                # No new claim/lease/context. This is not permission to redispatch.
                return status
            epoch, tail, raw, _, _, _ = self._head(conn, plan.package_id)
            if (epoch != plan.expected_head_epoch or tail != plan.before_tail or epoch >= MAX_EPOCH):
                _fail("publication_head_conflict")
            pending = conn.execute("SELECT request_key,fence FROM ae_publication_requests WHERE package_id=? AND state!='committed' LIMIT 2", (plan.package_id,)).fetchall()
            reservation = conn.execute("SELECT request_key,fence FROM ae_publication_reservations WHERE package_id=?", (plan.package_id,)).fetchone()
            if pending:
                if len(pending) != 1 or tuple(pending[0]) != reservation:
                    _fail()
                _fail("publication_request_pending")
            if reservation is not None:
                _fail()  # Orphan reservation is not permission to start another ID.
            if conn.execute("SELECT COUNT(*) FROM ae_publication_requests").fetchone()[0] >= MAX_REQUESTS:
                _fail("publication_authority_limit")
            after, after_tail = _append_from_head(plan, raw, tail)
            lease = self._authorization_call("freeze", plan, digest)
            lease_raw = canonical_json(_lease_data(lease, plan, digest))
            self._verify(plan, lease, digest, "prepare")
            fence = epoch + 1
            conn.execute("INSERT INTO ae_publication_requests VALUES(?,?,?,?,?,?,?,'prepared',?,?)",
                         (key, plan.package_id, digest, data_raw, plan.manifest_raw, lease_raw, fence, after_tail, after))
            conn.execute("INSERT INTO ae_publication_reservations VALUES(?,?,?)", (plan.package_id, key, fence))
            return LedgerStatus(PreparedRef(key, digest, plan.package_id, fence, plan.effect_id),
                                "prepared", after_tail, after)

    def commit(self, reference: PreparedRef, identity: RequestIdentity) -> LedgerStatus:
        """Legacy observer-only settlement. Never invokes the asset writer.

        Observer is injected once by host, not supplied to this call by writer.
        A durable one-shot committing marker precedes observation. Any lost
        receipt/cancellation then remains unknown and retains reservation.
        This method is not a fallback for an unavailable dispatch writer.
        Unknown is terminal: no recovery/second commit.
        """
        return self._commit_once(reference, identity, with_writer=False)

    def dispatch(self, reference: PreparedRef, identity: RequestIdentity) -> LedgerStatus:
        """Host-only, original one-shot asset dispatch and independent settlement.

        Both writer and observer are constructor-fixed dependencies, never RPC
        arguments. The durable marker must be confirmed before any writer call;
        a lost receipt, partial effect or cancellation permits only lookup.
        prepare/lookup replay never grants or performs a dispatch. No production
        provider, native writer, permission lease or schema is installed here.
        """
        return self._commit_once(reference, identity, with_writer=True)

    def dispatch_source_bound(self, reference: PreparedRef, identity: RequestIdentity) -> LedgerStatus:
        """Default-off host seam using the original source's frozen raw bytes.

        Neither the root, reader nor snapshot is supplied by this public call.
        Source inspection precedes the durable marker; the original marker and
        independent settlement still revalidate the stored request and lease.
        This installs no production authority, native writer or runtime grant.
        """
        if type(reference) is not PreparedRef or identity.key() != reference.request_key:
            _fail("request_id_reused")
        if self._commit_active:
            _fail("publication_reentrant")
        root = self._source_root
        if root is None:
            _fail("publication_source_root_unavailable")
        # Accept only this platform's concrete pathlib Path, not strings,
        # coercion hooks or subclasses overriding filesystem/path methods.
        if type(root) is not type(Path()) or not root.is_absolute():
            _fail("publication_source_root_invalid")
        source_writer = self._source_bound_writer
        if source_writer is None:
            _fail("publication_source_writer_unavailable")
        source_observer = self._observer
        if source_observer is None:
            _fail("publication_observer_unavailable")
        _require_sync_callback(source_writer, "publication_source_writer_unsupported")
        _require_sync_callback(source_observer, "publication_observer_unsupported")
        self._commit_active = True
        try:
            with self._transaction(write=False) as conn:
                plan, _, _, _, _ = self._commit_inputs(
                    conn, reference, identity, state="prepared", phase="source_read")
                if plan.request.action != "employee_package_publish":
                    _fail("publication_authority_unsupported")
            # Lazy import avoids snapshot -> authority's existing import cycle.
            # This is the real SafeFiles reader, never a injected reader/assertion.
            reader_failed = False
            try:
                from .employee_publication_source_snapshot import (
                    PublicationSourceSnapshot, read_publication_source_snapshot,
                )
                snapshot = read_publication_source_snapshot(root, plan)
            except AIEmployeeContractError:
                raise
            except Exception:
                reader_failed = True
            if reader_failed:
                _fail("publication_source_unavailable")
            _reject_async_result(snapshot)
            if (type(snapshot) is not PublicationSourceSnapshot
                    or snapshot.request_key != reference.request_key
                    or snapshot.plan_sha256 != reference.plan_sha256
                    or snapshot.package_id != plan.package_id or snapshot.version != plan.version
                    or snapshot.fingerprint != plan.fingerprint
                    or snapshot.source_draft_revision != plan.source_draft_revision):
                _fail("publication_source_plan_invalid")
            if (self._source_root is not root or self._source_bound_writer is not source_writer
                    or self._observer is not source_observer):
                _fail("publication_source_dependencies_changed")
            _require_sync_callback(source_writer, "publication_source_writer_unsupported")
            _require_sync_callback(source_observer, "publication_observer_unsupported")
            return self._commit_effects_once(reference, identity, with_writer=True,
                                             source_snapshot=snapshot, source_writer=source_writer,
                                             source_root=root, source_observer=source_observer)
        finally:
            self._commit_active = False

    def _commit_once(self, reference: PreparedRef, identity: RequestIdentity,
                     *, with_writer: bool) -> LedgerStatus:
        if type(reference) is not PreparedRef or identity.key() != reference.request_key:
            _fail("request_id_reused")
        if with_writer and self._writer is None:
            _fail("publication_writer_unavailable")
        if self._observer is None:
            _fail("publication_observer_unavailable")
        _require_sync_callback(self._observer, "publication_observer_unsupported")
        if with_writer:
            _require_sync_callback(self._writer, "publication_writer_unsupported")
        if self._commit_active:
            _fail("publication_reentrant")
        self._commit_active = True
        try:
            return self._commit_effects_once(reference, identity, with_writer=with_writer)
        finally:
            self._commit_active = False

    def _commit_effects_once(self, reference: PreparedRef, identity: RequestIdentity,
                            *, with_writer: bool,
                            source_snapshot: PublicationSourceSnapshot | None = None,
                            source_writer: Callable[[PreparedRef, FrozenPublication, bytes, PublicationSourceSnapshot], None] | None = None,
                            source_root: Path | None = None,
                            source_observer: Callable[[PreparedRef, FrozenPublication], AssetObservation] | None = None) -> LedgerStatus:
        def source_dependencies_current() -> None:
            if source_snapshot is not None:
                if (self._source_root is not source_root or self._source_bound_writer is not source_writer
                        or self._observer is not source_observer):
                    _fail("publication_source_dependencies_changed")
                _require_sync_callback(source_writer, "publication_source_writer_unsupported")
                _require_sync_callback(source_observer, "publication_observer_unsupported")

        with self._transaction(write=True) as conn:
            self._commit_inputs(conn, reference, identity, state="prepared", phase="commit_claim")
            source_dependencies_current()
            changed = conn.execute("UPDATE ae_publication_requests SET state='committing' WHERE request_key=? AND state='prepared'",
                                   (reference.request_key,)).rowcount
            if changed != 1:
                _fail()
        # Only this successful, uninterrupted invocation may proceed. A new
        # public commit sees committing/unknown and must use lookup instead.
        try:
            with self._transaction(write=True) as conn:
                plan, lease, status, epoch, tail = self._commit_inputs(
                    conn, reference, identity, state="committing",
                    phase="dispatch" if with_writer else "commit")
                source_dependencies_current()
                if with_writer:
                    # Ignore the writer's return value. Only the independently
                    # injected observer below can supply bytes for settlement.
                    if source_snapshot is None:
                        writer_result = self._writer(reference, plan, status.after_index_raw)
                    else:
                        # The SAME snapshot from this invocation; do not read a
                        # newer draft. Final manifest remains plan.manifest_raw,
                        # not source_snapshot.manifest_raw (draft/testing).
                        writer_result = source_writer(
                            reference, plan, status.after_index_raw, source_snapshot)
                    _reject_async_result(writer_result)
                    self._verify(plan, lease, reference.plan_sha256, "dispatch_after_write")
                source_dependencies_current()
                observation = (self._observer if source_snapshot is None else source_observer)(reference, plan)
                _reject_async_result(observation)
                _check_observation(reference, plan, status.after_index_raw, observation)
                self._verify(plan, lease, reference.plan_sha256, "commit_after_observation")
                source_dependencies_current()
                changed = conn.execute("UPDATE ae_publication_heads SET epoch=?,tail=?,index_raw=?,raw_sha256=?,last_request_key=? WHERE package_id=? AND epoch=? AND tail=?",
                                       (reference.fence, status.after_tail, status.after_index_raw,
                                        _digest(status.after_index_raw), reference.request_key, plan.package_id, epoch, tail)).rowcount
                if changed != 1:
                    _fail("publication_head_conflict")
                settled = conn.execute("UPDATE ae_publication_requests SET state='committed' WHERE request_key=? AND state='committing'",
                                       (reference.request_key,)).rowcount
                if settled != 1:
                    _fail()
                removed = conn.execute("DELETE FROM ae_publication_reservations WHERE package_id=? AND request_key=? AND fence=?",
                                       (plan.package_id, reference.request_key, reference.fence)).rowcount
                if removed != 1:
                    _fail()
        except Exception as exc:
            # The first transaction already confirmed the one-shot marker.
            # Authorization/schema/writer/observation/CAS/commit failures here are
            # uncertain, not fresh effect-free rejections. Rollback retains the
            # marker/reservation (or COMMIT may already have become durable).
            raise AIEmployeeContractError("publication_outcome_unknown", "提交未确认；保留原请求，仅只读核对。") from exc
        return LedgerStatus(reference, "committed", status.after_tail, status.after_index_raw)

    def _commit_inputs(self, conn: sqlite3.Connection, reference: PreparedRef,
                       identity: RequestIdentity, *, state: str, phase: str) -> tuple:
        row = self._load(conn, reference.request_key)
        if row is None:
            _fail()
        plan, lease, status = self._unpack(reference.request_key, row)
        if reference != status.reference or identity != plan.request:
            _fail("request_id_reused")
        if row[6] != state:
            _fail("publication_lookup_only")
        epoch, tail, raw, _, _, _ = self._coherence(conn, plan, status)
        exact_after, exact_tail = _append_from_head(plan, raw, tail)
        if exact_after != status.after_index_raw or exact_tail != status.after_tail:
            _fail()
        self._verify(plan, lease, reference.plan_sha256, phase)
        return plan, lease, status, epoch, tail


def _append_from_head(plan: FrozenPublication, raw: bytes | None, tail: str) -> tuple[bytes, str]:
    catalog = VersionCatalog.from_index(plan.package_id, raw, expected_tail_sha256=tail)
    if plan.request.action == "employee_package_publish":
        transition = catalog.publication_plan(version=plan.version, fingerprint=plan.fingerprint,
                                             actor=plan.request.actor, at=plan.at)
    else:
        selected = catalog.select(fingerprint=plan.fingerprint)
        if selected["version"] != plan.version:
            _fail()
        transition = catalog.retirement_plan(fingerprint=plan.fingerprint, reason=plan.reason,
                                             actor=plan.request.actor, at=plan.at)
    record = transition["append_record"]
    if record is None:
        _fail("publication_no_transition")  # No-op does not grant a success receipt.
    if raw and not raw.endswith(b"\n"):
        _fail("publication_index_append_unsupported")
    after = (raw or b"") + canonical_json(record) + b"\n"
    VersionCatalog.from_index(plan.package_id, after, expected_tail_sha256=record["record_sha256"])
    return after, record["record_sha256"]


def _check_observation(reference: PreparedRef, plan: FrozenPublication,
                       after: bytes, observed: AssetObservation) -> None:
    if (type(observed) is not AssetObservation or observed.request_key != reference.request_key
            or observed.plan_sha256 != reference.plan_sha256 or observed.package_id != plan.package_id
            or observed.effect_id != plan.effect_id or observed.target_scope_ref != plan.target_scope_ref
            or observed.index_raw != after or observed.manifest_raw != plan.manifest_raw
            or type(observed.files) is not tuple or len(observed.files) != len(plan.files)):
        _fail()
    expected = {row.path: row for row in plan.files}
    actual = {}
    total = 0
    for row in observed.files:
        if (type(row) is not ObservedFile or type(row.path) is not str or row.path not in expected
                or row.path in actual or type(row.raw) is not bytes):
            _fail()
        total += len(row.raw)
        target = expected[row.path]
        if total > MAX_FILE_BYTES or len(row.raw) != target.size or _digest(row.raw) != target.sha256:
            _fail()
        actual[row.path] = row.raw
    # Independent observations must preserve each skill's actual native walk order.
    if publication_content_fingerprint(_decode(observed.manifest_raw, "observed_manifest"), actual) != plan.fingerprint:
        _fail()
