"""Durable single-owner outbox for simulation and later adapter integration.

No network, GUI control, model calls, production DB or Engine import occurs here.
The exclusive owner lock prevents a second runtime from recovering live sends.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .contracts import Context, bounded_text, canonical_json, SCHEMA_VERSION, MAX_INBOUND_TEXT_CHARS
from .delivery import DeliveryReceipt, bound_receipt_record, command_body_digest, to_runtime_result


LAB_DATABASE_VERSION = 2
LAB_APPLICATION_ID = 0x41504353

# Exact schema ownership includes constraints and immutable-evidence triggers.
# Existing lab v1 databases require a separately reviewed migration. Opening an
# existing v2 database validates it; it never repairs missing/drifted objects.
_SCHEMA_OBJECTS = {
    "sessions": ("table", "sessions", """CREATE TABLE sessions (
      key TEXT PRIMARY KEY, context TEXT NOT NULL, revision INTEGER NOT NULL,
      mode TEXT NOT NULL, last_message_id TEXT
    )"""),
    "inbox": ("table", "inbox", """CREATE TABLE inbox (
      session_key TEXT NOT NULL REFERENCES sessions(key), message_id TEXT NOT NULL,
      text TEXT NOT NULL, revision INTEGER NOT NULL,
      PRIMARY KEY(session_key,message_id)
    )"""),
    "commands": ("table", "commands", """CREATE TABLE commands (
      id TEXT PRIMARY KEY, session_key TEXT NOT NULL REFERENCES sessions(key),
      in_reply_to TEXT NOT NULL, revision INTEGER NOT NULL, text TEXT NOT NULL,
      status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', receipt_id TEXT,
      created_at REAL NOT NULL, updated_at REAL NOT NULL,
      UNIQUE(session_key,in_reply_to,revision)
    )"""),
    "ingress_nonces": ("table", "ingress_nonces", """CREATE TABLE ingress_nonces (
      principal TEXT NOT NULL, nonce TEXT NOT NULL, digest TEXT NOT NULL,
      PRIMARY KEY(principal,nonce)
    )"""),
    "audit_events": ("table", "audit_events", """CREATE TABLE audit_events (
      sequence INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
      event TEXT NOT NULL, session_key TEXT, command_id TEXT, reason TEXT NOT NULL
    )"""),
    "delivery_evidence": ("table", "delivery_evidence", """CREATE TABLE delivery_evidence (
      evidence_id TEXT PRIMARY KEY, command_id TEXT NOT NULL REFERENCES commands(id),
      context_json TEXT NOT NULL, revision INTEGER NOT NULL, in_reply_to TEXT NOT NULL,
      body_sha256 TEXT NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
      receipt_id TEXT, not_sent_proof_id TEXT, observed_at REAL NOT NULL
    )"""),
    "delivery_evidence_no_update": ("trigger", "delivery_evidence", """CREATE TRIGGER delivery_evidence_no_update
      BEFORE UPDATE ON delivery_evidence BEGIN
        SELECT RAISE(ABORT, 'Delivery evidence is immutable');
      END"""),
    "delivery_evidence_no_delete": ("trigger", "delivery_evidence", """CREATE TRIGGER delivery_evidence_no_delete
      BEFORE DELETE ON delivery_evidence BEGIN
        SELECT RAISE(ABORT, 'Delivery evidence is immutable');
      END"""),
    "command_intent_immutable": ("trigger", "commands", """CREATE TRIGGER command_intent_immutable
      BEFORE UPDATE OF id,session_key,in_reply_to,revision,text,created_at ON commands
      WHEN NEW.id IS NOT OLD.id OR NEW.session_key IS NOT OLD.session_key
        OR NEW.in_reply_to IS NOT OLD.in_reply_to OR NEW.revision IS NOT OLD.revision
        OR NEW.text IS NOT OLD.text OR NEW.created_at IS NOT OLD.created_at BEGIN
        SELECT RAISE(ABORT, 'Outbound intent is immutable');
      END"""),
    "session_context_immutable": ("trigger", "sessions", """CREATE TRIGGER session_context_immutable
      BEFORE UPDATE OF key,context ON sessions
      WHEN NEW.key IS NOT OLD.key OR NEW.context IS NOT OLD.context BEGIN
        SELECT RAISE(ABORT, 'Session identity is immutable');
      END"""),
}


@dataclass(frozen=True)
class ExecutionFence:
    """Original local intent snapshot; never an authorization grant.

    A trusted connector validates this immediately before sending and must also
    implement its own atomic ownership/idempotency protocol at that side effect.
    A local check cannot guarantee zero cross-platform takeover races.
    """
    command_id: str
    context: Context
    session_key: str
    in_reply_to: str
    revision: int
    text: str
    body_sha256: str


class Runtime:
    # Explicit subclasses may own a NEW store schema. Existing stores are never
    # migrated here; the default v2 laboratory outbox remains byte-compatible.
    _schema_version = LAB_DATABASE_VERSION
    _schema_objects = _SCHEMA_OBJECTS

    def __init__(self, db_path, clock=None, allowed_versions=("mock-1",)):
        self.path = Path(db_path).resolve()
        if self.path.exists() and not self.path.is_file():
            raise ValueError("Database must be a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self.allowed_versions = frozenset(allowed_versions)
        self._lock = threading.RLock()
        self._closed = False
        self._active_operations = 0
        self._owner = open(str(self.path) + ".owner", "a+b")
        try:
            self._acquire_owner()
            self.db = sqlite3.connect(self.path, timeout=2, isolation_level=None,
                                      check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self._create_schema()
            with self._transaction():
                for row in self.db.execute("SELECT id FROM commands WHERE status='dispatching'").fetchall():
                    self._update(row["id"], "unknown", "restart_during_send")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._owner.close()
            raise

    def _acquire_owner(self):
        self._owner.seek(0, os.SEEK_END)
        if self._owner.tell() == 0:
            self._owner.write(b"0")
            self._owner.flush()
        self._owner.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("This lab database already has a runtime owner") from exc

    def _create_schema(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if (version != 0 and application_id != LAB_APPLICATION_ID
                or version == 0 and application_id != 0):
            raise ValueError("Foreign database ownership marker; no migration performed")
        if version == 1:
            raise ValueError("Lab v1 requires an explicit controlled migration; no migration performed")
        if version not in (0, self._schema_version):
            raise ValueError("Unsupported lab database version; no migration performed")
        if version == 0:
            # Even views/triggers/indexes disqualify a purportedly empty store.
            if self.db.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise ValueError("Refusing to initialize a nonempty foreign database")
            script = ";\n".join(spec[2] for spec in self._schema_objects.values())
            try:
                self.db.executescript(
                    "BEGIN IMMEDIATE;\n" + script
                    + f";\nPRAGMA user_version={self._schema_version};"
                    + f"\nPRAGMA application_id={LAB_APPLICATION_ID};\nCOMMIT;")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        self._verify_schema()

    def _verify_schema(self):
        expected = {
            name: (kind, table, " ".join(sql.split()))
            for name, (kind, table, sql) in self._schema_objects.items()
        }
        observed = {
            row["name"]: (row["type"], row["tbl_name"], " ".join((row["sql"] or "").split()))
            for row in self.db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        }
        if observed != expected:
            raise ValueError("Incomplete or drifted lab schema; no repair or migration performed")

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime closed")
            self._active_operations += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_operations -= 1

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime closed")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _audit(self, event, key=None, command_id=None, reason=""):
        # No message bodies, keys, signatures or account tokens in audit records.
        self.db.execute("INSERT INTO audit_events(at,event,session_key,command_id,reason) VALUES(?,?,?,?,?)",
                        (self.clock(), event, key, command_id, reason))

    def _ensure(self, context):
        if not isinstance(context, Context):
            raise ValueError("Context required")
        self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,0,'assistant',NULL)",
                        (context.key, canonical_json(context.to_dict()).decode()))
        return self.db.execute("SELECT * FROM sessions WHERE key=?", (context.key,)).fetchone()

    def _cancel_pending(self, key, reason):
        for row in self.db.execute(
                "SELECT id FROM commands WHERE session_key=? AND status IN ('draft','approved')", (key,)).fetchall():
            self._update(row["id"], "cancelled", reason)

    def receive(self, context, message_id, text, *, authenticated_nonce=None):
        bounded_text(message_id, "message_id")
        bounded_text(text, "text", MAX_INBOUND_TEXT_CHARS)
        with self._transaction():
            session = self._ensure(context)
            if authenticated_nonce is not None:
                principal, nonce, digest = authenticated_nonce
                previous = self.db.execute("SELECT digest FROM ingress_nonces WHERE principal=? AND nonce=?",
                                           (principal, nonce)).fetchone()
                if previous and previous["digest"] != digest:
                    raise ValueError("Authenticated nonce conflict")
                self.db.execute("INSERT OR IGNORE INTO ingress_nonces VALUES(?,?,?)", (principal, nonce, digest))
            previous = self.db.execute("SELECT * FROM inbox WHERE session_key=? AND message_id=?",
                                       (context.key, message_id)).fetchone()
            if previous:
                if previous["text"] != text:
                    raise ValueError("Message ID reused with different content")
                self._audit("inbound_duplicate", context.key)
                return {"status": "duplicate", "revision": previous["revision"]}
            revision = session["revision"] + 1
            self.db.execute("INSERT INTO inbox VALUES(?,?,?,?)", (context.key, message_id, text, revision))
            self.db.execute("UPDATE sessions SET revision=?,last_message_id=? WHERE key=?",
                            (revision, message_id, context.key))
            self._cancel_pending(context.key, "new_buyer_message")
            self._audit("inbound_accepted", context.key)
            return {"status": "accepted", "revision": revision}

    def propose(self, context, in_reply_to, text, expected_revision):
        bounded_text(in_reply_to, "in_reply_to")
        bounded_text(text, "text", 8000)
        if type(expected_revision) is not int:
            raise ValueError("Revision must be an integer")
        with self._transaction():
            session = self._ensure(context)
            if (session["mode"] != "assistant" or session["revision"] != expected_revision
                    or session["last_message_id"] != in_reply_to):
                raise ValueError("Stale or unauthorized proposal")
            if self.db.execute("SELECT id FROM commands WHERE session_key=? AND status IN ('dispatching','unknown')",
                               (context.key,)).fetchone():
                raise ValueError("Resolve uncertain delivery before a new proposal")
            existing = self.db.execute("SELECT id,text FROM commands WHERE session_key=? AND in_reply_to=? AND revision=?",
                                       (context.key, in_reply_to, expected_revision)).fetchone()
            if existing:
                if existing["text"] != text:
                    raise ValueError("Proposal revision already exists with different content")
                return self._command(existing["id"])
            command_id = str(uuid.uuid4())
            now = self.clock()
            self.db.execute("INSERT INTO commands(id,session_key,in_reply_to,revision,text,status,created_at,updated_at) VALUES(?,?,?,?,?,'draft',?,?)",
                            (command_id, context.key, in_reply_to, expected_revision, text, now, now))
            self._audit("proposal_created", context.key, command_id)
            return self._command(command_id)

    def _command(self, command_id):
        row = self.db.execute("SELECT c.*,s.context FROM commands c JOIN sessions s ON c.session_key=s.key WHERE c.id=?",
                              (command_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown command")
        value = dict(row)
        value["command_id"] = value.pop("id")
        value["context"] = Context(**json.loads(value["context"]))
        value["schema_version"] = SCHEMA_VERSION
        return value

    def get_command(self, command_id):
        with self._lock:
            return self._command(command_id)

    @staticmethod
    def _fence_for_command(command):
        return ExecutionFence(
            command["command_id"], command["context"], command["session_key"],
            command["in_reply_to"], command["revision"], command["text"],
            command_body_digest(command),
        )

    def capture_execution_fence(self, command_id):
        """Capture a live dispatch's immutable original intent for its connector.

        Trusted connector/host entry only. It checks local conversation state,
        not Engine authorization, platform credentials or customer identity.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime closed")
            command = self._command(command_id)
            if command["status"] != "dispatching" or not self._current(command):
                raise ValueError("Execution fence is no longer current")
            return self._fence_for_command(command)

    def validate_execution_fence(self, fence, outgoing):
        """Compare the connector's exact outgoing intent with current storage.

        Call immediately before the irreversible SDK/desktop send. The connector
        must additionally serialize takeover/account switching and enforce the
        original command_id as an atomic platform idempotency key when supported.
        The returned snapshot never grants permission to make that send.
        """
        if type(fence) is not ExecutionFence or type(outgoing) is not dict:
            raise ValueError("Original execution fence and outgoing command required")
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime closed")
            command = self._command(fence.command_id)
            if (command["status"] != "dispatching" or not self._current(command)
                    or self._fence_for_command(command) != fence):
                raise ValueError("Execution fence is no longer current")
            fields = ("command_id", "context", "session_key", "in_reply_to", "revision", "text")
            if (any(outgoing.get(field) != command[field] for field in fields)
                    or type(outgoing.get("revision")) is not int
                    or command_body_digest(outgoing) != fence.body_sha256):
                raise ValueError("Outgoing command differs from immutable intent")
            return fence

    def delivery_evidence(self, command_id):
        """Trusted host read of full evidence; weak levels remain weak levels."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime closed")
            self._command(command_id)
            values = []
            for row in self.db.execute(
                    "SELECT * FROM delivery_evidence WHERE command_id=? ORDER BY observed_at,evidence_id",
                    (command_id,)):
                value = dict(row)
                value["context"] = Context(**json.loads(value.pop("context_json")))
                values.append(value)
            return values

    def _update(self, command_id, status, reason="", receipt_id=None):
        self.db.execute("UPDATE commands SET status=?,reason=?,receipt_id=COALESCE(?,receipt_id),updated_at=? WHERE id=?",
                        (status, reason, receipt_id, self.clock(), command_id))
        self._audit("command_" + status, command_id=command_id, reason=reason)

    def _current(self, command):
        row = self.db.execute("SELECT * FROM sessions WHERE key=?", (command["session_key"],)).fetchone()
        return (row["mode"] == "assistant" and row["revision"] == command["revision"]
                and row["last_message_id"] == command["in_reply_to"])

    def approve(self, command_id):
        """Trusted local lab-process entry, never a buyer-message operation.

        This records local operator approval; it does not verify Engine 4.7
        human-slot, identity, budget or business authorization.
        """
        with self._transaction():
            command = self._command(command_id)
            if command["status"] != "draft":
                return command
            if not self._current(command):
                self._update(command_id, "cancelled", "stale_at_approval")
            else:
                self._update(command_id, "approved", "local_operator_approved")
            return self._command(command_id)

    def _change_mode(self, context, mode):
        with self._transaction():
            session = self._ensure(context)
            revision = session["revision"] + 1
            self.db.execute("UPDATE sessions SET revision=?,mode=? WHERE key=?", (revision, mode, context.key))
            self._cancel_pending(context.key, "human_takeover" if mode == "human" else "ownership_changed")
            inflight = self.db.execute("SELECT COUNT(*) FROM commands WHERE session_key=? AND status IN ('dispatching','unknown')",
                                       (context.key,)).fetchone()[0]
            self._audit("mode_" + mode, context.key)
            return {"mode": mode, "revision": revision, "inflight_requires_reconciliation": bool(inflight)}

    def takeover(self, context):
        return self._change_mode(context, "human")

    def resume(self, context):
        return self._change_mode(context, "assistant")

    def dispatch(self, command_id, adapter):
        with self._operation():
            return self._dispatch(command_id, adapter)

    def _dispatch(self, command_id, adapter):
        with self._transaction():
            command = self._command(command_id)
            if command["status"] != "approved":
                return command
            if not self._current(command):
                self._update(command_id, "cancelled", "stale_before_snapshot")
                return self._command(command_id)
        try:
            snapshot = adapter.snapshot(command["context"])
            observed = snapshot.get("context")
            if isinstance(observed, dict):
                observed = Context(**observed)
            reason = ("recipient_mismatch" if observed != command["context"] else
                      "unsupported_client_version" if snapshot.get("version") not in self.allowed_versions else
                      "channel_not_ready" if snapshot.get("ready") is not True else
                      "manual_operator_active" if snapshot.get("manual_active") is not False else "")
        except Exception:
            reason = "snapshot_unavailable"
        with self._transaction():
            command = self._command(command_id)
            if command["status"] != "approved":
                return command
            if not self._current(command):
                self._update(command_id, "cancelled", "stale_after_snapshot")
                return self._command(command_id)
            if reason:
                self._update(command_id, "blocked", reason)
                return self._command(command_id)
            self._update(command_id, "dispatching", "write_ahead_before_adapter")
            command = self._command(command_id)
            fence = self._fence_for_command(command)
            command["execution_fence"] = fence
        # The external adapter must perform its own final ownership/recipient
        # guard. A takeover cannot recall an irreversible external side effect.
        try:
            self.validate_execution_fence(fence, command)
            result = adapter.send(command)
        except Exception:
            result = None
        with self._transaction():
            self._record_result(command_id, result)
            return self._command(command_id)

    def _record_result(self, command_id, result):
        # Reload immutable stored intent, not the mutable dict passed to an
        # adapter. Both send and lookup must pass this same receipt boundary.
        if result is None:
            self._update(command_id, "unknown", "delivery_unconfirmed")
            return
        if not isinstance(result, DeliveryReceipt):
            self._update(command_id, "unknown", "untyped_delivery_receipt")
            return
        try:
            original = self._command(command_id)
            verified = to_runtime_result(original, result)
            evidence = bound_receipt_record(original, result)
        except (ValueError, TypeError):
            # Keep only the local command/reason in audit; never copy a foreign
            # account's identifiers or arbitrary raw response into this record.
            self._update(command_id, "unknown", "receipt_identity_mismatch")
            return
        # Exact evidence IDs deduplicate repeated observations without overwriting
        # original context, body or weaker/stronger evidence already retained.
        evidence_id = sha256(canonical_json(evidence)).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO delivery_evidence "
            "(evidence_id,command_id,context_json,revision,in_reply_to,body_sha256,level,source,"
            "receipt_id,not_sent_proof_id,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (evidence_id, command_id, canonical_json(evidence["context"]).decode("utf-8"),
             evidence["revision"], evidence["in_reply_to"], evidence["body_sha256"],
             evidence["level"], evidence["source"], evidence["receipt_id"],
             evidence["not_sent_proof_id"], self.clock()),
        )
        state = verified["status"]
        if state == "confirmed":
            self._update(command_id, "confirmed", "adapter_receipt", verified["receipt_id"])
        elif state == "rejected":
            self._update(command_id, "rejected", "adapter_proved_not_sent")
        else:
            self._update(command_id, "unknown", "delivery_unconfirmed")

    def reconcile(self, command_id, adapter):
        with self._operation():
            return self._reconcile(command_id, adapter)

    def _reconcile(self, command_id, adapter):
        with self._lock:
            command = self._command(command_id)
            if command["status"] != "unknown":
                return command
        try:
            result = adapter.lookup(command)
        except Exception:
            result = None
        with self._transaction():
            # Do not downgrade a receipt another reconciliation already stored.
            if self._command(command_id)["status"] == "unknown":
                self._record_result(command_id, result)
            return self._command(command_id)

    def audit(self):
        with self._lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM audit_events ORDER BY sequence")]

    def close(self):
        with self._lock:
            if not self._closed:
                if self._active_operations:
                    raise RuntimeError("In-flight adapter operation; database owner retained")
                self.db.close()
                self._owner.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
