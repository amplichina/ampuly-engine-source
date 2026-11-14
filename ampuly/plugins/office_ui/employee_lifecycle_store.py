"""Private publication/evaluation facts; inert construction and read paths.

The first explicit lifecycle write creates a NEW dedicated asset ledger, never
migrates BusinessStore or a task database. Existing incompatible ledgers are
refused. The owning service holds the normal employee asset lock around writes.
This database is a server fact source, not a place to import browser 'passed'.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import time

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_safe_write import atomic_write
from .ai_employee_storage import AIEmployeeFileStore
from .employee_publication_authority import initialize_empty_authority, _require_schema

_RELATIVE = "employee_packages/.lifecycle/authority.db"
_MAX_BYTES = 20 * 1024 * 1024
_DDL = (
    "CREATE TABLE lifecycle_meta (version INTEGER PRIMARY KEY, schema_sha256 TEXT NOT NULL)",
    """CREATE TABLE employee_trials (
        trial_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, package_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, state TEXT NOT NULL,
        candidate_json BLOB NOT NULL, context_json BLOB NOT NULL,
        samples_json BLOB NOT NULL, observations_json BLOB NOT NULL,
        budget_id TEXT, observed_at TEXT NOT NULL, record_json BLOB,
        UNIQUE(project_id, request_id))""",
    """CREATE TABLE employee_approvals (
        package_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        trial_id TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, record_raw BLOB NOT NULL,
        PRIMARY KEY(request_id,actor), UNIQUE(trial_id))""",
)
_SCHEMA = hashlib.sha256(canonical_json(list(_DDL))).hexdigest()


def _error(code="employee_lifecycle_unavailable"):
    return AIEmployeeContractError(code, "岗位生命周期事实暂不能核实；未采用或重新执行。")


def _nonempty_sidecar(path, check):
    """'wal' or 'journal' when that sidecar exists and is non-empty."""
    found = None
    for suffix, kind in (("-wal", "wal"), ("-journal", "journal")):
        sidecar = Path(str(path) + suffix)
        check(sidecar)
        try:
            if sidecar.exists() and sidecar.stat().st_size:
                if kind == "wal":
                    return "wal"
                found = "journal"
        except OSError:
            raise _error("storage_busy") from None
    return found


def wait_out_rollback_journal(path, check, *, attempts=8, pause=0.025, sleep=time.sleep):
    """A DELETE-mode journal can still be visible just after our own commit.

    WAL is a different snapshot and is refused immediately. A rollback journal
    that disappears within the bound is the unlink race, not a hot writer.
    A journal that stays is still storage_busy.
    """
    if attempts < 1:
        raise _error("storage_busy")
    for attempt in range(attempts):
        kind = _nonempty_sidecar(path, check)
        if kind is None:
            return
        if kind == "wal" or attempt + 1 == attempts:
            raise _error("storage_busy")
        sleep(pause)


def _decode(raw):
    value = json.loads(raw)
    canonical_json(value)
    return value


class EmployeeLifecycleStore:
    def __init__(self, root: Path):
        self.assets = AIEmployeeFileStore(root)
        self.path = self.assets.root / _RELATIVE

    def exists(self):
        self.assets._check(self.path)
        if not self.path.exists():
            return False
        # Native reader refuses links, hard links and irregular objects. Read
        # is bounded; there is no schema adoption or repair on this path.
        raw = self.assets.files.read(_RELATIVE, _MAX_BYTES)
        if not raw or not raw.startswith(b"SQLite format 3\x00"):
            raise _error()
        return True

    def initialize_new(self):
        """Explicit write under assets._lock; no migration of an existing DB."""
        if self.assets._root_guard is None:
            raise _error("employee_lifecycle_lock_required")
        if self.exists():
            with self.connection() as conn:
                self._schema(conn)
            return
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Same DDL as the existing tested publication authority. No running
            # customer database is a target of this new-ledger initializer.
            initialize_empty_authority(conn)
            for sql in _DDL:
                conn.execute(sql)
            conn.execute("INSERT INTO lifecycle_meta VALUES(1,?)", (_SCHEMA,))
            conn.commit()
            raw = conn.serialize()
        finally:
            conn.close()
        if self.path.exists():
            raise _error("storage_busy")
        atomic_write(self.assets.root, _RELATIVE, raw, root_guard=self.assets._root_guard)

    @staticmethod
    def _schema(conn):
        _require_schema(conn)
        if conn.execute("SELECT version,schema_sha256 FROM lifecycle_meta LIMIT 2").fetchall() != [(1, _SCHEMA)]:
            raise _error()

    @contextmanager
    def connection(self, *, write=False):
        if write and self.assets._root_guard is None:
            raise _error("employee_lifecycle_lock_required")
        if not self.exists():
            raise _error()
        # This new private ledger is DELETE-journal only. Do not read an active
        # WAL as an old snapshot, change journal mode, or auto-recover hot files.
        # A rollback journal can outlive commit() for a moment (especially on
        # Windows). Waiting it out avoids refusing the trial route that starts
        # immediately after the start transaction releases the asset lock.
        wait_out_rollback_journal(self.path, self.assets._check)
        before = self.path.stat()
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                               uri=True, timeout=0, isolation_level=None)
        try:
            after = self.path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _error("outcome_unknown")
            if not write:
                conn.execute("PRAGMA query_only=ON")
            self._schema(conn)
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def trial(self, trial_id, *, project_id=None):
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM employee_trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None or project_id is not None and row[1] != project_id:
            return None
        return dict(trial_id=row[0], project_id=row[1], package_id=row[2], fingerprint=row[3],
                    actor=row[4], request_id=row[5], payload_sha256=row[6], state=row[7],
                    candidate=_decode(row[8]), context=_decode(row[9]), samples=_decode(row[10]),
                    observations=_decode(row[11]), budget_id=row[12], observed_at=row[13],
                    record=None if row[14] is None else _decode(row[14]))

    def by_request(self, project_id, request_id):
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT trial_id FROM employee_trials WHERE project_id=? AND request_id=?",
                               (project_id, request_id)).fetchone()
        return None if row is None else self.trial(row[0], project_id=project_id)

    def _approval_rows(self, package_id, fingerprint):
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT trial_id,actor,request_id,payload_sha256,record_raw,rowid FROM employee_approvals WHERE package_id=? AND fingerprint=? ORDER BY rowid DESC LIMIT 201",
                                (package_id, fingerprint)).fetchall()
        if len(rows) > 200:
            raise _error()
        return [dict(trial_id=row[0], actor=row[1], request_id=row[2],
                     payload_sha256=row[3], record_raw=row[4], review_epoch=row[5]) for row in rows]

    def approval(self, package_id, fingerprint):
        # Instance evidence cannot qualify publication of a shared package.
        for approval in self._approval_rows(package_id, fingerprint):
            trial = self.trial(approval["trial_id"])
            if trial is not None and trial["context"].get("qualification_scope", {"kind": "package"}) == {"kind": "package"}:
                return approval
        return None

    def review_request(self, request_id, actor):
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT trial_id,payload_sha256 FROM employee_approvals WHERE request_id=? AND actor=?",
                               (request_id, actor)).fetchone()
        return None if row is None else dict(trial_id=row[0], payload_sha256=row[1])

    def trial_review(self, trial_id):
        """Read the immutable original review receipt, never infer from state."""
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT request_id,record_raw FROM employee_approvals WHERE trial_id=?",
                               (trial_id,)).fetchone()
        if row is None:
            return None
        from .employee_package_versions import parse_approval_record
        record = parse_approval_record(row[1], expected_sha256=hashlib.sha256(row[1]).hexdigest())
        status = record["approval"]["status"]
        if status not in {"approved", "rejected"}:
            raise AIEmployeeContractError("publication_approval_invalid", "审核原始回执未确认。")
        return dict(review_request_id=row[0], decision="approve" if status == "approved" else "reject")

    def has_head(self, package_id):
        if not self.exists():
            return False
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM ae_publication_heads WHERE package_id=?", (package_id,)).fetchone() is not None

    def verified_context(self, package_id, fingerprint, *, model_config, capability_impls,
                         params=None, runtime_profile_fingerprint=None, knowledge_refs=None, instance=None):
        """Independent review facts, not a manifest's claimed verified_models.

        Exact parameters and native model configuration are part of the proof.
        A test of an empty/default instance cannot approve a modified instance.
        This provider never imports a test or performs a model call on lookup.
        """
        from .employee_qualification import context_matches
        if not runtime_profile_fingerprint:
            return None
        params = {} if params is None else params
        knowledge_refs = [] if knowledge_refs is None else knowledge_refs
        approval = trial = None
        # Records for another instance/configuration must neither grant nor
        # erase this configuration's decision. The newest matching rejection
        # does supersede an older approval of the SAME effective configuration.
        for candidate in self._approval_rows(package_id, fingerprint):
            value = self.trial(candidate["trial_id"])
            if (value is not None and value["package_id"] == package_id and value["fingerprint"] == fingerprint
                    and context_matches(value["context"], model_config=model_config,
                        capability_impls=capability_impls, params=params, knowledge_refs=knowledge_refs,
                        runtime_profile_fingerprint=runtime_profile_fingerprint, instance=instance)):
                approval, trial = candidate, value
                break
        if approval is None or trial["state"] != "approved" or trial["record"] is None:
            return None
        context, record = trial["context"], trial["record"]
        params_sha = hashlib.sha256(canonical_json(params)).hexdigest()
        # Parse the original hash chain and explicit human approval, not only
        # the SQLite status column. These records are bounded by their parsers.
        from .employee_package_versions import parse_evaluation_records, parse_approval_record
        parsed = parse_evaluation_records(canonical_json(record) + b"\n",
                                         expected_tail_sha256=record["record_sha256"])
        approval_envelope = parse_approval_record(approval["record_raw"],
            expected_sha256=hashlib.sha256(approval["record_raw"]).hexdigest())
        approval_value = approval_envelope["approval"]
        if (len(parsed) != 1 or parsed[0]["trial_id"] != trial["trial_id"]
                or parsed[0]["model_config"] != model_config or parsed[0]["capability_impls"] != capability_impls
                or parsed[0]["checker_versions"] != context["checker_versions"]
                or len(parsed[0]["repeats"]) != len(trial["samples"])
                or any(not r["completed"] or r["hard_fail_count"] or not r["non_inferior_met"]
                       or not r["absolute_threshold_met"] for r in parsed[0]["repeats"])
                or approval_envelope["package_id"] != package_id
                or approval_value["candidate_fingerprint"] != fingerprint
                or approval_value.get("decided_by") != approval["actor"]
                or approval_value["status"] != "approved"):
            return None
        return dict(package_id=package_id, package_fingerprint=fingerprint,
            model_config=model_config, capability_impls=capability_impls,
            params_fingerprint=params_sha, runtime_profile_fingerprint=runtime_profile_fingerprint,
            knowledge_refs_fingerprint=hashlib.sha256(canonical_json(knowledge_refs)).hexdigest(),
            qualification_scope=context.get("qualification_scope", {"kind": "package"}),
            checker_versions=context["checker_versions"], trial_id=trial["trial_id"],
            evaluation_record_sha256=record["record_sha256"],
            approval_sha256=hashlib.sha256(approval["record_raw"]).hexdigest(),
            approved_by=approval["actor"], verified=True)

    def current_tail(self, package_id):
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT tail FROM ae_publication_heads WHERE package_id=?", (package_id,)).fetchone()
            pending = conn.execute("SELECT 1 FROM ae_publication_requests WHERE package_id=? AND state!='committed' LIMIT 1",
                                   (package_id,)).fetchone()
        # A written-but-unsettled index never enables a new project adoption.
        return None if row is None or pending is not None else row[0]
