"""Native root-bound restoration quarantine, not a client-supplied permit.

Read construction and history do not create or initialize anything. Restored
business execution remains paused; explicit CURRENT-epoch employee trials can
use their original owner/budget/provider chain. This leaf does not itself
enable general work, replay a prior request or change a runtime database.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

from ampuly.layer4_tools.workspace_fs import (
    LexicalWorkspace, SecureWorkspace, WorkspaceBoundaryError, _read_bounded_fd,
)

MARKER = "ampuly-restoration.json"
TRIAL_EPOCH = "ampuly_recovery_epoch"
_EPOCH = re.compile(r"re-[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_FIELDS = {"schema_version", "recovery_epoch", "archive_sha256", "root_identity", "state"}
_PAUSED_OPERATIONS = frozenset({
    "startup_reconcile", "interaction_recovery", "gate_continuation_recovery",
    "effect_outbox_projection", "controller_admission", "task_execution",
    "order_create", "external_send", "tool_effect",
})
_ANCHOR_TABLE = "ampuly_restoration_roots_v1"
_ANCHOR_DDL = (
    """CREATE TABLE ampuly_restoration_roots_v1 (
        recovery_epoch TEXT PRIMARY KEY, archive_sha256 TEXT NOT NULL,
        root_dev TEXT NOT NULL, root_ino TEXT NOT NULL,
        marker_sha256 TEXT NOT NULL)""",
    """CREATE TRIGGER ampuly_restoration_roots_v1_no_update
        BEFORE UPDATE ON ampuly_restoration_roots_v1
        BEGIN SELECT RAISE(ABORT,'restoration root immutable'); END""",
    """CREATE TRIGGER ampuly_restoration_roots_v1_no_delete
        BEFORE DELETE ON ampuly_restoration_roots_v1
        BEGIN SELECT RAISE(ABORT,'restoration root immutable'); END""",
    """CREATE TRIGGER ampuly_restoration_roots_v1_no_replace
        BEFORE INSERT ON ampuly_restoration_roots_v1
        WHEN EXISTS(SELECT 1 FROM ampuly_restoration_roots_v1 WHERE recovery_epoch=NEW.recovery_epoch)
        BEGIN SELECT RAISE(ABORT,'restoration root immutable'); END""",
)
_ANCHOR_NAMES = (_ANCHOR_TABLE, _ANCHOR_TABLE + "_no_update", _ANCHOR_TABLE + "_no_delete", _ANCHOR_TABLE + "_no_replace")


class RestorationGateError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code="restoration_state_unknown"):
    raise RestorationGateError(code) from None


def _identity(workspace):
    if os.name == "nt":
        return tuple(int(value) for value in workspace._identity(workspace.root_handle))
    value = os.fstat(workspace.root_fd)
    return int(value.st_dev), int(value.st_ino)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _decode(raw, identity):
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: _fail())
    except (ValueError, UnicodeError, RecursionError):
        _fail()
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["recovery_epoch"]) is not str or _EPOCH.fullmatch(value["recovery_epoch"]) is None
            or type(value["archive_sha256"]) is not str or _SHA.fullmatch(value["archive_sha256"]) is None
            or value["state"] != "recovery_read_only"
            or type(value["root_identity"]) is not list or len(value["root_identity"]) != 2
            or any(type(item) is not int or item < 0 for item in value["root_identity"])
            or tuple(value["root_identity"]) != identity):
        _fail()
    return value


def _anchor_schema(conn):
    if type(conn) is not sqlite3.Connection:
        _fail()
    for name, expected in zip(_ANCHOR_NAMES, _ANCHOR_DDL):
        rows = conn.execute("SELECT sql FROM sqlite_schema WHERE name=? LIMIT 2", (name,)).fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not str or " ".join(rows[0][0].strip().rstrip(";").split()) != " ".join(expected.split()):
            _fail()
    names = conn.execute("SELECT name FROM sqlite_schema WHERE tbl_name=? LIMIT 8", (_ANCHOR_TABLE,)).fetchall()
    if {row[0] for row in names} != set(_ANCHOR_NAMES) | {"sqlite_autoindex_" + _ANCHOR_TABLE + "_1"} or len(names) != 5:
        _fail()


def _read_anchor(home, root, marker, digest):
    """A deleted marker cannot turn a previously restored ledger into legacy."""
    from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
    try:
        state = EmployeeLifecycleStore(home)
        if not state.exists():
            if marker is not None:
                _fail()
            return
        with state.connection() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE name=? LIMIT 2", (_ANCHOR_TABLE,)).fetchall()
            if not exists:
                if marker is not None:
                    _fail()
                return
            _anchor_schema(conn)
            if marker is None:
                _fail()
            row = conn.execute("SELECT recovery_epoch,archive_sha256,root_dev,root_ino,marker_sha256 "
                "FROM ampuly_restoration_roots_v1 WHERE recovery_epoch=? LIMIT 2", (marker["recovery_epoch"],)).fetchall()
            if row != [(marker["recovery_epoch"], marker["archive_sha256"], str(root[0]), str(root[1]), digest)]:
                _fail()
    except RestorationGateError:
        raise
    except Exception:
        _fail()


def _native_observe(home):
    """Return one verified root/marker snapshot; never follow lexical fallback."""
    if not home.exists():
        return None, None, None
    capability = SecureWorkspace.for_roots(str(home), str(home))
    if isinstance(capability, LexicalWorkspace):
        _fail()
    try:
        with capability as workspace:
            root = _identity(workspace)
            target = workspace.resolve(MARKER, use_output_root=False)
            try:
                fd = workspace.open_read(target)
            except FileNotFoundError:
                raw = None
            else:
                try:
                    before = os.fstat(fd)
                    raw = _read_bounded_fd(fd, 4096)
                    current = workspace.open_read(target)
                    try:
                        after = os.fstat(current)
                        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                            _fail()
                    finally:
                        os.close(current)
                finally:
                    os.close(fd)
            with SecureWorkspace.for_roots(str(home), str(home)) as named:
                if _identity(named) != root:
                    _fail()
            return root, None if raw is None else _decode(raw, root), None if raw is None else hashlib.sha256(raw).hexdigest()
    except (OSError, WorkspaceBoundaryError, ValueError):
        _fail()


@dataclass(frozen=True, slots=True)
class RestorationStatus:
    restored: bool
    epoch: str | None
    history_available: bool = True
    ordinary_execution_allowed: bool = False


class RestorationGate:
    """Bind once in server composition, then re-observe before every effect.

    An initialized gate refuses marker deletion/replacement or root change.
    A serialized status/dataclass, browser epoch or this class name is not an
    authorization capability. Normal owner, Stop and budget checks still run.
    """
    def __init__(self, home: Path):
        self.home = Path(os.path.abspath(home))
        self._bound = None
        self.status()

    def status(self):
        root, marker, digest = _native_observe(self.home)
        snapshot = (root, digest)
        if root is not None:
            if marker is not None or self._bound is None:
                _read_anchor(self.home, root, marker, digest)
            if self._bound is None:
                self._bound = snapshot
            elif self._bound != snapshot:
                _fail()
        elif self._bound is not None:
            _fail()
        return RestorationStatus(marker is not None, None if marker is None else marker["recovery_epoch"],
                                 ordinary_execution_allowed=marker is None)

    def require_ordinary_admission(self, operation: str):
        if type(operation) is not str or operation not in _PAUSED_OPERATIONS:
            _fail("restoration_operation_invalid")
        if self.status().restored:
            _fail("restoration_execution_paused")

    def trial_context(self, original: dict):
        """Only the original service.prepare may persist this server context."""
        if type(original) is not dict or TRIAL_EPOCH in original:
            _fail("restoration_trial_invalid")
        status = self.status()
        return dict(original) if not status.restored else {**original, TRIAL_EPOCH: status.epoch}

    def require_trial(self, state, trial_id):
        from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
        if type(state) is not EmployeeLifecycleStore or state.assets.root != self.home:
            _fail("restoration_trial_invalid")
        status = self.status()
        # Read the original native ledger, never a passed browser trial DTO.
        trial = state.trial(trial_id)
        if trial is None or status.restored and trial["context"].get(TRIAL_EPOCH) != status.epoch:
            _fail("restoration_trial_historical")
        self.status()
        return trial

    def current_verified_context(self, state, package_id, fingerprint, **context):
        """Add recovery freshness AFTER original native evidence verification."""
        from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
        if type(state) is not EmployeeLifecycleStore or state.assets.root != self.home:
            _fail("restoration_trial_invalid")
        verified = state.verified_context(package_id, fingerprint, **context)
        if verified is None:
            return None
        try:
            trial = self.require_trial(state, verified["trial_id"])
        except RestorationGateError as exc:
            if exc.code == "restoration_trial_historical":
                return None
            raise
        if trial["state"] != "approved" or trial["package_id"] != package_id or trial["fingerprint"] != fingerprint:
            return None
        return verified

    def package_revalidated(self, state, package_id, fingerprint):
        """Read-only environment flag, not permission to adopt or execute."""
        from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
        if type(state) is not EmployeeLifecycleStore or state.assets.root != self.home:
            _fail("restoration_trial_invalid")
        status = self.status()
        if not status.restored:
            return True
        approval = state.approval(package_id, fingerprint)
        if approval is None:
            return False
        try:
            trial = self.require_trial(state, approval["trial_id"])
        except RestorationGateError as exc:
            if exc.code == "restoration_trial_historical":
                return False
            raise
        context = trial["context"]
        return self.current_verified_context(state, package_id, fingerprint,
            model_config=context["model_config"], capability_impls=context["capability_impls"],
            params={}, runtime_profile_fingerprint=context.get("runtime_profile_fingerprint")) is not None


def _create_new_marker(workspace, target, raw, root_guard):
    """New staging only: handle-relative CREATE, never replacing a name.

    Reuse the original native writer's bounded write and receipt checks.
    Staging is not exposed as a runtime root until the owning restore has
    verified success. A failure leaves this exact new target for the owning
    restore's reconciliation/cleanup; this function never unlinks a name.
    """
    from ampuly.plugins.office_ui.ai_employee_safe_write import (
        _assert_named_file, _assert_parent_named, _open_at, _write_all,
    )
    if type(raw) is not bytes or not raw or len(raw) > 4096 or not callable(root_guard):
        _fail()
    with workspace._open_parent(target, create_dirs=False) as (parent, name):
        root_guard(workspace)
        _assert_parent_named(workspace, target, parent)
        try:
            fd = _open_at(workspace, parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            _fail("restoration_target_invalid")
        try:
            _write_all(fd, raw)
            os.fsync(fd)
            if os.name != "nt":
                os.fsync(parent)
            if os.fstat(fd).st_size != len(raw):
                _fail()
            _assert_named_file(workspace, target, parent, fd)
            root_guard(workspace)
        finally:
            os.close(fd)


def mark_new_restoration_staging(home: Path, *, archive_sha256: str):
    """Original restore-only producer for a NEW owned staging target.

    No public RPC/CLI invokes this. The original restore must have already
    verified the archive and require absent target roots. This intentionally
    refuses ordinary runtime-home names, existing markers and lexical I/O.
    Never call it against an installed source root.
    """
    home = Path(os.path.abspath(home))
    if (re.fullmatch(r"\.[^\\/]{1,128}\.restore-[0-9]+", home.name) is None
            or type(archive_sha256) is not str or _SHA.fullmatch(archive_sha256) is None):
        _fail("restoration_target_invalid")
    capability = SecureWorkspace.for_roots(str(home), str(home))
    if isinstance(capability, LexicalWorkspace):
        _fail()
    with capability as workspace:
        identity = _identity(workspace)
        target = workspace.resolve(MARKER, use_output_root=False)
        try:
            previous = workspace.open_read(target)
        except FileNotFoundError:
            pass
        else:
            os.close(previous)
            _fail("restoration_target_invalid")
        value = {"schema_version": 1, "recovery_epoch": "re-" + secrets.token_hex(16),
                 "archive_sha256": archive_sha256, "root_identity": list(identity), "state": "recovery_read_only"}
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        # Explicit new-staging-only restore producer. This initializes a new
        # private ledger if absent, never a live/source DB. Existing history
        # remains byte-for-byte in its old rows; only a new immutable root
        # fact is appended before the marker/target can become visible.
        from ampuly.plugins.office_ui.employee_lifecycle_store import EmployeeLifecycleStore
        state = EmployeeLifecycleStore(home)
        with state.assets._lock():
            # Bind the independently opened asset lock to THIS already-pinned
            # staging root before even creating/initializing a private ledger.
            state.assets._root_guard(workspace)
            try:
                previous = workspace.open_read(target)
            except FileNotFoundError:
                pass
            else:
                os.close(previous)
                _fail("restoration_target_invalid")
            state.initialize_new()
            with state.transaction() as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (_ANCHOR_TABLE,)).fetchone()
                if exists is None:
                    for sql in _ANCHOR_DDL:
                        conn.execute(sql)
                _anchor_schema(conn)
                if conn.execute("SELECT COUNT(*) FROM ampuly_restoration_roots_v1").fetchone()[0] >= 64:
                    _fail("restoration_history_limit")
                conn.execute("INSERT INTO ampuly_restoration_roots_v1 VALUES(?,?,?,?,?)", (
                    value["recovery_epoch"], archive_sha256, str(identity[0]), str(identity[1]), hashlib.sha256(raw).hexdigest()))
            state.assets._root_guard(workspace)
            _create_new_marker(workspace, target, raw, state.assets._root_guard)
            state.assets._root_guard(workspace)
    gate = RestorationGate(home)
    if gate.status().epoch != value["recovery_epoch"]:
        _fail()
    return value["recovery_epoch"]
