"""Plugin-owned SQLite, explicit scope predicates and transactional replay keys.

No connection to business.db or tasks.db and no import-time filesystem work.
The host must admit the project and provide an application-owned path.

Replay digests, audit digests and erasure fingerprints are keyed HMACs with a
per-store random secret kept beside the database (``<db>.hmac-key``, inside
the project's AMPULY_HOME data, never in the repository or logs). Audit digests
cover only non-PII identifiers. Erasure replaces derived request digests with
random values, so no stored digest can be recomputed offline from known PII.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from uuid import uuid4

from .contracts import Scope, canonical, fail, identifier, now, require_synthetic, text, validate_fields

SCHEMA_VERSION = 4
KEY_SUFFIX = ".hmac-key"
KEY_BYTES = 32
ADMISSION_MODES = ("synthetic_only",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_records (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 kind TEXT NOT NULL, revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_scope ON crm_records(project_id, customer_id, kind);
CREATE TABLE IF NOT EXISTS crm_requests (
 project_id TEXT NOT NULL, customer_id TEXT NOT NULL, actor TEXT NOT NULL,
 request_key TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(project_id, customer_id, actor, request_key)
);
CREATE TABLE IF NOT EXISTS crm_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 actor TEXT NOT NULL, action TEXT NOT NULL, record_id TEXT NOT NULL,
 revision INTEGER NOT NULL, at TEXT NOT NULL, digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_handoffs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 opportunity_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
 payload TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(project_id, customer_id, opportunity_id, revision)
);
CREATE TABLE IF NOT EXISTS crm_erasure_plans (
 token_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 actor TEXT NOT NULL, record_id TEXT NOT NULL, mode TEXT NOT NULL,
 fingerprint TEXT NOT NULL, expires_at INTEGER NOT NULL
);
PRAGMA user_version=4;
"""


class CRMStore:
    def __init__(self, path: Path, *, admission="synthetic_only", tombstones: Path | None = None):
        self.path = Path(path)
        if not self.path.is_absolute():
            fail("storage_unavailable")
        # Real-data admission is closed in this build (see settings.py).
        if admission not in ADMISSION_MODES:
            fail("real_data_admission_closed")
        self.admission = admission
        self.key_path = self.path.with_name(self.path.name + KEY_SUFFIX)
        self.tombstones = Path(tombstones) if tombstones is not None else None
        self._safe_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._safe_path()
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'crm_%' LIMIT 1").fetchone():
                fail("storage_migration_required")
            if version in (1, 2, 3):
                # Never mutate an existing older database implicitly. v1/v2
                # hold historic response bodies; v3 holds unkeyed digests
                # derived from PII. An explicit copy migration is required.
                fail("storage_migration_required")
            if version not in (0, SCHEMA_VERSION):
                fail("storage_version_unsupported")
            if version == 0:
                self._key = self._load_key(create=True)
                conn.executescript(SCHEMA)
            else:
                self._key = self._load_key(create=False)

    def _safe_path(self, path=None, *, is_file=True):
        target = self.path if path is None else path
        for part in (*reversed(target.parents), target):
            if not part.exists() and not part.is_symlink():
                continue
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                fail("storage_unavailable")
            if part == target and is_file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                fail("storage_unavailable")

    def _load_key(self, *, create):
        """Per-store HMAC secret: random, exclusive-created, never logged."""
        path = self.key_path
        self._safe_path(path)
        if create:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    os.write(fd, secrets.token_bytes(KEY_BYTES))
                    os.fsync(fd)
                finally:
                    os.close(fd)
        for _ in range(50):
            if not path.exists():
                fail("storage_key_unavailable")
            self._safe_path(path)
            data = path.read_bytes()
            if len(data) == KEY_BYTES:
                return data
            time.sleep(0.02)  # a concurrent creator is still writing
        fail("storage_key_unavailable")

    def _mac(self, purpose, value):
        return hmac.new(self._key, f"crm-{purpose}-v{SCHEMA_VERSION}\n{canonical(value)}".encode(), sha256).hexdigest()

    @staticmethod
    def _retired_digest():
        # Random and unrelated to any content: erasure leaves nothing to recompute.
        return "retired-" + secrets.token_hex(32)

    @contextmanager
    def connect(self):
        self._safe_path()
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, scope, key, operation):
        scope.check(write=True)
        identifier(key)
        digest = self._mac("request", {"project_id": scope.project_id, "customer_id": scope.customer_id,
                                       "actor": scope.actor, "request_key": key, "operation": operation})
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT digest,result FROM crm_requests WHERE project_id=? AND customer_id=? AND actor=? AND request_key=?",
                (scope.project_id, scope.customer_id, scope.actor, key)).fetchone()
            result = None
            if previous:
                receipt = json.loads(previous["result"])
                # Retired receipts no longer carry a content digest.
                if receipt.get("retired"):
                    fail("replay_obsolete")
                if not hmac.compare_digest(previous["digest"], digest):
                    fail("request_key_conflict")
                if receipt.get("erasure"):
                    result = receipt
                else:
                    try:
                        result = self._get(conn, scope, receipt["record_id"])
                    except Exception:
                        fail("replay_obsolete")
                    if result["revision"] != receipt["revision"]:
                        fail("replay_obsolete")
            box = {"replay": result, "result": None}
            yield conn, box
            if result is None:
                conn.execute("INSERT INTO crm_requests VALUES(?,?,?,?,?,?)",
                    (scope.project_id, scope.customer_id, scope.actor, key, digest,
                     canonical(box["result"] if box["result"].get("erasure") else
                               {"record_id": box["result"]["id"], "revision": box["result"]["revision"]})))

    @staticmethod
    def _row(row):
        return {"id": row["id"], "kind": row["kind"], "revision": row["revision"],
                "archived": bool(row["archived"]), "fields": json.loads(row["payload"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _get(self, conn, scope, record_id, kind=None, *, active=False):
        identifier(record_id)
        row = conn.execute("SELECT * FROM crm_records WHERE id=? AND project_id=? AND customer_id=?",
            (record_id, scope.project_id, scope.customer_id)).fetchone()
        if row is None or (kind and row["kind"] != kind) or (active and row["archived"]):
            fail("record_not_found")
        return self._row(row)

    def get(self, scope: Scope, record_id):
        scope.check()
        with self.connect() as conn:
            return self._get(conn, scope, record_id)

    def assert_scope(self, scope):
        """A rebound/restored project cannot silently adopt another customer's CRM."""
        scope.check()
        with self.connect() as conn:
            for table in ("crm_records", "crm_requests", "crm_audit", "crm_handoffs", "crm_erasure_plans"):
                if conn.execute(f"SELECT 1 FROM {table} WHERE project_id!=? OR customer_id!=? LIMIT 1",
                                (scope.project_id, scope.customer_id)).fetchone():
                    fail("storage_scope_conflict")

    def list(self, scope: Scope, *, kind="company", query="", owner_id="", pipeline_status="", archived=False):
        scope.check()
        if kind not in ("company", "contact", "opportunity", "activity"):
            fail("input_invalid")
        query = text(query, limit=200).casefold()
        owner_id = text(owner_id, limit=128)
        pipeline_status = text(pipeline_status, limit=40)
        if type(archived) is not bool:
            fail("input_invalid")
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM crm_records WHERE project_id=? AND customer_id=? AND kind=? AND archived=? ORDER BY updated_at DESC,id",
                (scope.project_id, scope.customer_id, kind, int(archived))).fetchall()
        records = [self._row(row) for row in rows]
        return [row for row in records
                if (not query or any(query in str(value).casefold() for value in row["fields"].values()))
                and (not owner_id or row["fields"].get("owner_id") == owner_id)
                and (not pipeline_status or row["fields"].get("pipeline_status") == pipeline_status)]

    def _references(self, conn, scope, fields):
        for name, kind in (("company_id", "company"), ("contact_id", "contact"), ("opportunity_id", "opportunity")):
            if fields.get(name):
                linked = self._get(conn, scope, fields[name], kind, active=True)
                if name == "contact_id" and linked["fields"].get("company_id") != fields.get("company_id"):
                    fail("contact_company_conflict")

    def export(self, scope, *, confirmed=False):
        scope.check()
        if confirmed is not True:
            fail("confirmation_required")
        with self.connect() as conn:
            records = [self._row(row) for row in conn.execute(
                "SELECT * FROM crm_records WHERE project_id=? AND customer_id=? ORDER BY id",
                (scope.project_id, scope.customer_id)).fetchall()]
            self._audit(conn, scope, "export", {"id": "export", "revision": 0, "count": len(records)})
        return {"schema_version": SCHEMA_VERSION, "project_id": scope.project_id, "customer_id": scope.customer_id,
                "exported_at": now(), "records": records}

    @staticmethod
    def _refs(value):
        if isinstance(value, dict):
            return set().union(*(CRMStore._refs(item) for item in value.values())) if value else set()
        if isinstance(value, list):
            return set().union(*(CRMStore._refs(item) for item in value)) if value else set()
        return {value} if isinstance(value, str) and value.startswith("crm-") else set()

    def _erasure_snapshot(self, conn, scope, record_id):
        self._get(conn, scope, record_id)
        rows = [self._row(row) for row in conn.execute(
            "SELECT * FROM crm_records WHERE project_id=? AND customer_id=? ORDER BY id",
            (scope.project_id, scope.customer_id))]
        selected = {record_id}
        while True:
            added = {row["id"] for row in rows if any(row["fields"].get(key) in selected
                     for key in ("company_id", "contact_id", "opportunity_id"))
                     or (row["kind"] == "suggestion" and self._refs(row["fields"]) & selected)}
            if added <= selected:
                break
            selected |= added
        affected = [row for row in rows if row["id"] in selected]
        handoffs = [dict(row) for row in conn.execute(
            "SELECT * FROM crm_handoffs WHERE project_id=? AND customer_id=? ORDER BY id",
            (scope.project_id, scope.customer_id))
            if row["opportunity_id"] in selected or self._refs(json.loads(row["payload"])) & selected]
        fingerprint = self._mac("erasure-plan", {"records": affected, "handoffs": handoffs})
        return affected, handoffs, fingerprint

    def erasure_preview(self, scope, *, record_id, mode, revalidate=lambda: None):
        scope.check(write=True)
        if mode not in ("delete", "anonymize"):
            fail("input_invalid")
        revalidate()
        token = uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            affected, handoffs, fingerprint = self._erasure_snapshot(conn, scope, record_id)
            revalidate()
            conn.execute("DELETE FROM crm_erasure_plans WHERE project_id=? AND customer_id=? AND actor=? AND expires_at<?",
                         (scope.project_id, scope.customer_id, scope.actor, int(time.time())))
            conn.execute("INSERT INTO crm_erasure_plans VALUES(?,?,?,?,?,?,?,?)",
                (sha256(token.encode()).hexdigest(), scope.project_id, scope.customer_id, scope.actor,
                 record_id, mode, fingerprint, int(time.time()) + 600))
        return {"token": token, "record_id": record_id, "mode": mode,
                "phrase": f"{mode.upper()} {record_id}", "expires_in": 600,
                "affected": [{"id": row["id"], "kind": row["kind"], "revision": row["revision"]} for row in affected],
                "retired_evidence_count": len(handoffs)}

    def erase(self, scope, *, token, record_id, mode, phrase, confirmed, request_key, revalidate=lambda: None):
        scope.check(write=True)
        identifier(token); identifier(record_id)
        if confirmed is not True or mode not in ("delete", "anonymize") or phrase != f"{mode.upper()} {record_id}":
            fail("confirmation_required")
        revalidate()
        operation = {"erasure_token": token, "record_id": record_id, "mode": mode, "phrase": phrase}
        with self.transaction(scope, request_key, operation) as (conn, box):
            revalidate()
            if box["replay"] is not None:
                return box["replay"]
            plan = conn.execute("SELECT * FROM crm_erasure_plans WHERE token_hash=? AND project_id=? AND customer_id=? AND actor=?",
                (sha256(token.encode()).hexdigest(), scope.project_id, scope.customer_id, scope.actor)).fetchone()
            if not plan or plan["record_id"] != record_id or plan["mode"] != mode or plan["expires_at"] < time.time():
                fail("confirmation_expired")
            affected, handoffs, fingerprint = self._erasure_snapshot(conn, scope, record_id)
            if fingerprint != plan["fingerprint"]:
                fail("confirmation_stale")
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                fail("erasure_storage_mode_unavailable")
            targets = {row["id"] for row in affected}
            self._apply_erasure(conn, scope, affected, handoffs, mode, action=mode)
            revalidate()
            # Tombstones are written after the last authority check and before
            # commit, so a later restore of an older backup re-applies this
            # erasure; a commit failure after this point fails toward erasure.
            self._record_tombstones(scope, {target: mode for target in targets})
            box["result"] = {"erasure": True, "mode": mode, "record_id": record_id, "count": len(targets),
                             "retired_evidence_count": len(handoffs)}
            return box["result"]

    def _apply_erasure(self, conn, scope, affected, handoffs, mode, *, action, targets=None):
        targets = {row["id"] for row in affected} if targets is None else set(targets)
        for row in affected:
            if mode == "delete":
                conn.execute("DELETE FROM crm_records WHERE id=? AND project_id=? AND customer_id=?",
                             (row["id"], scope.project_id, scope.customer_id))
            else:
                conn.execute("UPDATE crm_records SET payload=?,archived=1,revision=revision+1,updated_at=? WHERE id=? AND project_id=? AND customer_id=?",
                             (canonical({"name": "已匿名化"}), now(), row["id"], scope.project_id, scope.customer_id))
            self._audit(conn, scope, action, {"id": row["id"], "revision": row["revision"] + 1})
        # Only explicitly linked retired evidence is scrubbed. Preserve IDs,
        # task references and a non-sensitive outcome, never copied prose.
        for handoff in handoffs:
            refs = self._refs(json.loads(handoff["payload"]))
            conn.execute("UPDATE crm_handoffs SET payload=?,status='retired' WHERE id=? AND project_id=? AND customer_id=?",
                (canonical({"record_refs": sorted(refs - targets), "erased_refs": sorted(refs & targets)}),
                 handoff["id"], scope.project_id, scope.customer_id))
        # Receipts of erased records lose both their pointer and their keyed
        # content digest; the digest becomes a random, content-free value.
        for row in conn.execute("SELECT rowid,result FROM crm_requests WHERE project_id=? AND customer_id=?",
                                (scope.project_id, scope.customer_id)).fetchall():
            receipt = json.loads(row["result"])
            # Erasure receipts hold IDs only and must keep replaying idempotently.
            if not receipt.get("erasure") and receipt.get("record_id") in targets:
                conn.execute("UPDATE crm_requests SET result=?,digest=? WHERE rowid=?",
                             (canonical({"retired": True}), self._retired_digest(), row["rowid"]))
        # Every pending plan in scope fingerprinted a graph that just changed.
        conn.execute("DELETE FROM crm_erasure_plans WHERE project_id=? AND customer_id=?",
                     (scope.project_id, scope.customer_id))

    # --- erasure tombstones (ids only; outside the project tree) -------------

    def _read_tombstones(self, scope):
        path = self.tombstones
        if path is None or not path.exists():
            return {}
        self._safe_path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fail("tombstones_unavailable")
        if (type(data) is not dict or data.get("schema") != 1 or data.get("project_id") != scope.project_id
                or type(data.get("erased")) is not dict):
            fail("tombstones_unavailable")
        erased = {}
        for record_id, entry in data["erased"].items():
            identifier(record_id)
            if type(entry) is not dict or entry.get("mode") not in ("delete", "anonymize") or type(entry.get("customer_id")) is not str:
                fail("tombstones_unavailable")
            if entry["customer_id"] == scope.customer_id:
                erased[record_id] = entry["mode"]
        return erased

    def _record_tombstones(self, scope, entries):
        path = self.tombstones
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._safe_path(path.parent, is_file=False)
        current = {}
        if path.exists():
            self._safe_path(path)
            try:
                current = json.loads(path.read_text(encoding="utf-8"))["erased"]
            except (OSError, ValueError, KeyError, TypeError):
                fail("tombstones_unavailable")
        for record_id, mode in entries.items():
            previous = current.get(record_id, {})
            # A delete supersedes an anonymize; never downgrade.
            if previous.get("mode") == "delete":
                mode = "delete"
            current[record_id] = {"mode": mode, "customer_id": scope.customer_id, "at": previous.get("at") or now()}
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temp.write_text(canonical({"schema": 1, "project_id": scope.project_id, "erased": current}), encoding="utf-8")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _stale_receipt(receipt, ids):
        return not receipt.get("retired") and not receipt.get("erasure") and receipt.get("record_id") in ids

    def apply_tombstones(self, scope):
        """Re-apply recorded erasures, e.g. after restoring an older backup."""
        scope.check()
        erased = self._read_tombstones(scope)
        if not erased:
            return 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = [self._row(row) for row in conn.execute(
                "SELECT * FROM crm_records WHERE project_id=? AND customer_id=? ORDER BY id",
                (scope.project_id, scope.customer_id))]
            anonymized = canonical({"name": "已匿名化"})
            pending = {mode: [row for row in rows if erased.get(row["id"]) == mode
                              and (mode == "delete" or canonical(row["fields"]) != anonymized or not row["archived"])]
                       for mode in ("delete", "anonymize")}
            ids = set(erased)

            def needs_scrub(row):
                payload = json.loads(row["payload"])
                linked = row["opportunity_id"] in ids or self._refs(payload) & ids
                if not isinstance(payload, dict):
                    return bool(linked)
                dirty = set(payload) - {"record_refs", "erased_refs"} or set(payload.get("record_refs", [])) & ids
                return bool(linked and dirty)
            handoffs = [dict(row) for row in conn.execute(
                "SELECT * FROM crm_handoffs WHERE project_id=? AND customer_id=? ORDER BY id",
                (scope.project_id, scope.customer_id)) if needs_scrub(row)]
            stale_requests = [row for row in conn.execute(
                "SELECT result FROM crm_requests WHERE project_id=? AND customer_id=?",
                (scope.project_id, scope.customer_id)) if self._stale_receipt(json.loads(row["result"]), ids)]
            if not (pending["delete"] or pending["anonymize"] or handoffs or stale_requests):
                return 0
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                fail("erasure_storage_mode_unavailable")
            self._apply_erasure(conn, scope, pending["delete"], handoffs, "delete", action="erasure_reapplied", targets=ids)
            self._apply_erasure(conn, scope, pending["anonymize"], [], "anonymize", action="erasure_reapplied", targets=ids)
            return len(pending["delete"]) + len(pending["anonymize"])

    def _audit(self, conn, scope, action, record):
        # Keyed digest over non-PII identifiers only, never record contents.
        at = now()
        digest = self._mac("audit", {"project_id": scope.project_id, "customer_id": scope.customer_id,
                                     "actor": scope.actor, "action": action, "record_id": record["id"],
                                     "revision": record["revision"], "at": at})
        conn.execute("INSERT INTO crm_audit(project_id,customer_id,actor,action,record_id,revision,at,digest) VALUES(?,?,?,?,?,?,?,?)",
            (scope.project_id, scope.customer_id, scope.actor, action, record["id"], record["revision"], at, digest))

    def save(self, scope, *, kind, fields, request_key, record_id=None, expected_revision=None):
        scope.check(write=True)
        fields = validate_fields(kind, fields, scope)
        if self.admission == "synthetic_only":
            require_synthetic(kind, fields)
        operation = {"kind": kind, "fields": fields, "id": record_id, "revision": expected_revision}
        with self.transaction(scope, request_key, operation) as (conn, box):
            if box["replay"] is not None:
                return box["replay"]
            self._references(conn, scope, fields)
            stamp = now()
            if record_id is None:
                if expected_revision is not None:
                    fail("revision_invalid")
                record_id = "crm-" + uuid4().hex
                conn.execute("INSERT INTO crm_records VALUES(?,?,?,?,1,0,?,?,?)",
                    (record_id, scope.project_id, scope.customer_id, kind, canonical(fields), stamp, stamp))
            else:
                current = self._get(conn, scope, record_id, kind, active=True)
                if type(expected_revision) is not int or expected_revision != current["revision"]:
                    fail("revision_conflict")
                conn.execute("UPDATE crm_records SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                    (canonical(fields), stamp, record_id))
            record = self._get(conn, scope, record_id)
            self._audit(conn, scope, "save", record)
            box["result"] = record
            return record

    def archive(self, scope, *, record_id, expected_revision, request_key):
        with self.transaction(scope, request_key, {"archive": record_id, "revision": expected_revision}) as (conn, box):
            if box["replay"] is not None:
                return box["replay"]
            record = self._get(conn, scope, record_id, active=True)
            if type(expected_revision) is not int or record["revision"] != expected_revision:
                fail("revision_conflict")
            # Preserve linked facts and require explicit closure of children.
            others = conn.execute("SELECT payload FROM crm_records WHERE project_id=? AND customer_id=? AND archived=0 AND id!=?",
                (scope.project_id, scope.customer_id, record_id)).fetchall()
            if any(record_id in [json.loads(row[0]).get(key) for key in ("company_id", "contact_id", "opportunity_id")]
                   for row in others):
                fail("archive_has_active_links")
            conn.execute("UPDATE crm_records SET archived=1,revision=revision+1,updated_at=? WHERE id=?", (now(), record_id))
            record = self._get(conn, scope, record_id)
            self._audit(conn, scope, "archive", record)
            box["result"] = record
            return record
