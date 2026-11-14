"""Explicit synthetic-copy migration. Never called by host/store initialization.

The immutable source and its historic exports/backups are NOT erased. A copy
migration is format conversion, not proof of irreversible anonymization.

Older replay/audit digests were unkeyed hashes over content (v3 hashed PII).
They are never carried over: every copied receipt is retired with a random
digest, audit digests are recomputed with the target's keyed non-PII HMAC, and
pending erasure plans are dropped.
"""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .contracts import canonical, fail, identifier
from .store import SCHEMA_VERSION, CRMStore

# Opportunity field name used by storage v1-v3; v4 calls it "pipeline_status".
LEGACY_PIPELINE_FIELD = "stage"
SOURCE_VERSIONS = (1, 2, 3)

TABLE_COLUMNS = {
    "crm_records": ("id", "project_id", "customer_id", "kind", "revision", "archived", "payload", "created_at", "updated_at"),
    "crm_requests": ("project_id", "customer_id", "actor", "request_key", "digest", "result"),
    "crm_audit": ("seq", "project_id", "customer_id", "actor", "action", "record_id", "revision", "at", "digest"),
    "crm_handoffs": ("id", "project_id", "customer_id", "opportunity_id", "revision", "status", "payload", "task_id", "created_at"),
}


def _paths(source, target, isolated_root):
    source, target, root = map(Path, (source, target, isolated_root))
    if not all(p.is_absolute() for p in (source, target, root)) or source == target:
        fail("migration_target_invalid")
    for path in (source, target, root):
        if ".ampuly" in [part.lower() for part in path.parts] or ".." in path.parts:
            fail("migration_target_invalid")
        for part in (*path.parents, path):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                fail("migration_target_invalid")
    if not source.is_relative_to(root) or not target.is_relative_to(root) or not (root / "CRM-SYNTHETIC-ONLY").is_file():
        fail("migration_synthetic_root_required")
    if not source.is_file() or source.stat().st_nlink != 1 or target.exists() or not target.parent.is_dir():
        fail("migration_target_invalid")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        fail("migration_source_not_quiescent")
    return source, target


def preview_copy(source, target, *, isolated_root, scope):
    scope.check(write=True)
    source, target = _paths(source, target, isolated_root)
    digest = sha256(source.read_bytes()).hexdigest()
    # closing(): the sqlite3 context manager only commits, it never closes.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in SOURCE_VERSIONS:
            fail("storage_version_unsupported")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            fail("migration_source_invalid")
        counts = {}
        for table in ("crm_records", "crm_requests", "crm_audit", "crm_handoffs"):
            if tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})")) != TABLE_COLUMNS[table]:
                fail("migration_source_invalid")
            if conn.execute(f"SELECT 1 FROM {table} WHERE project_id!=? OR customer_id!=? LIMIT 1",
                            (scope.project_id, scope.customer_id)).fetchone():
                fail("storage_scope_conflict")
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    binding = {"source": str(source), "target": str(target), "root": str(isolated_root),
               "project": scope.project_id, "customer": scope.customer_id, "actor": scope.actor,
               "source_sha256": digest, "source_version": version}
    return {"source_sha256": digest, "source_version": version, "target_version": SCHEMA_VERSION,
            "counts": counts, "phrase": "MIGRATE SYNTHETIC COPY", "source_preserved": True,
            "plan_sha256": sha256(canonical(binding).encode()).hexdigest()}


def migrate_copy(source, target, *, isolated_root, scope, source_sha256, plan_sha256, phrase, confirmed):
    if confirmed is not True or phrase != "MIGRATE SYNTHETIC COPY":
        fail("confirmation_required")
    plan = preview_copy(source, target, isolated_root=isolated_root, scope=scope)
    if plan["source_sha256"] != source_sha256 or plan["plan_sha256"] != plan_sha256:
        fail("confirmation_stale")
    source, target = _paths(source, target, isolated_root)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as old:
        old.row_factory = sqlite3.Row
        rows = {table: [dict(row) for row in old.execute(f"SELECT * FROM {table}")]
                for table in ("crm_records", "crm_requests", "crm_audit", "crm_handoffs")}
    # Exclusive creation never overwrites another file. Incomplete destinations
    # remain version 0 with tables and fail closed; the source is untouched.
    with target.open("xb"):
        pass
    store = CRMStore(target)
    with store.connect() as conn:
        conn.execute("PRAGMA user_version=0")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        record_ids = {identifier(row["id"]) for row in rows["crm_records"]}
        for row in rows["crm_records"]:
            if row["kind"] == "suggestion":
                refs = CRMStore._refs(json.loads(row["payload"])) & record_ids
                row["payload"] = canonical({"name": "历史建议已停用", "record_refs": sorted(refs)})
                row["archived"] = 1
            elif row["kind"] == "opportunity":
                payload = json.loads(row["payload"])
                if isinstance(payload, dict) and LEGACY_PIPELINE_FIELD in payload and "pipeline_status" not in payload:
                    payload["pipeline_status"] = payload.pop(LEGACY_PIPELINE_FIELD)
                    row["payload"] = canonical(payload)
            keys = TABLE_COLUMNS["crm_records"]
            conn.execute(f"INSERT INTO crm_records ({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [row[k] for k in keys])
        for row in rows["crm_requests"]:
            # Old digests are unkeyed content hashes; they cannot be re-keyed
            # without the original operation, so every receipt is retired.
            conn.execute("INSERT INTO crm_requests VALUES(?,?,?,?,?,?)", [row[k] for k in
                ("project_id", "customer_id", "actor", "request_key")] +
                [store._retired_digest(), canonical({"retired": True})])
        for row in rows["crm_audit"]:
            digest = store._mac("audit", {"project_id": row["project_id"], "customer_id": row["customer_id"],
                                          "actor": row["actor"], "action": row["action"], "record_id": row["record_id"],
                                          "revision": row["revision"], "at": row["at"]})
            conn.execute("INSERT INTO crm_audit VALUES(?,?,?,?,?,?,?,?,?)", [row[k] for k in
                ("seq", "project_id", "customer_id", "actor", "action", "record_id", "revision", "at")] + [digest])
        for row in rows["crm_handoffs"]:
            refs = CRMStore._refs(json.loads(row["payload"])) & record_ids
            conn.execute("INSERT INTO crm_handoffs VALUES(?,?,?,?,?,?,?,?,?)", [row[k] for k in
                ("id", "project_id", "customer_id", "opportunity_id", "revision")] +
                ["retired", canonical({"record_refs": sorted(refs)}), row["task_id"], row["created_at"]])
        if sha256(source.read_bytes()).hexdigest() != source_sha256:
            fail("confirmation_stale")
        target_id = "migration-" + sha256(str(target).encode()).hexdigest()[:32]
        store._audit(conn, scope, "migration_copy", {"id": target_id, "revision": SCHEMA_VERSION})
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return {**plan, "target_id": target_id,
            "legacy_task_references": sum(bool(row["task_id"]) for row in rows["crm_handoffs"]),
            "notice": "Original source, existing backups/exports and external Task descriptions remain outside this copy."}
