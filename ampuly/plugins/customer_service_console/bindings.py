"""Explicit channel selections in a separate, bounded append-only SQLite file.

No legacy lookup, migration, fallback, background work or global configuration.
Every operation opens and closes its own connection; there is no lifetime owner
lock that can prevent a separate authorized pause request from taking effect.
The host authenticates actors and owns real provider/budget authorization.
"""
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid


MAX_RECORDS = 256
MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_RECORD_BYTES = 8192
_APPLICATION_ID = 0x43534244
_SCHEMA_VERSION = 1
_HASH = re.compile(r"[a-f0-9]{64}")
_UUID = re.compile(r"[a-f0-9]{32}")
_BINDING_FIELDS = {"binding_id", "account_id", "suite_id", "actor", "budget_id",
                   "budget", "request_sha256", "enabled"}
_EVENT_FIELDS = {"sequence", "event", "event_actor", "previous_sha256", "binding"}
_ADMISSION_FIELDS = ("suite_id", "request_sha256", "budget_id")
_SCHEMA = {
    "bindings": """CREATE TABLE bindings (
      sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 1 AND 256),
      record_json TEXT NOT NULL CHECK(length(record_json) <= 8192),
      record_sha256 TEXT NOT NULL CHECK(length(record_sha256) = 64))""",
    "bindings_no_update": """CREATE TRIGGER bindings_no_update BEFORE UPDATE ON bindings
      BEGIN SELECT RAISE(ABORT, 'Channel binding facts are immutable'); END""",
    "bindings_no_delete": """CREATE TRIGGER bindings_no_delete BEFORE DELETE ON bindings
      BEGIN SELECT RAISE(ABORT, 'Channel binding facts are immutable'); END""",
}


class BindingError(ValueError):
    """Stable codes without contents of actor, account or budget records."""


def _fail(suffix):
    raise BindingError("customer_service_binding_" + suffix)


def _identifier(value):
    if (type(value) is not str or not 1 <= len(value) <= 128 or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        _fail("identifier_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("identifier_invalid")
    return value


def _budget(value):
    if type(value) is not dict or value.keys() != {"max_cost_cny", "max_model_calls", "max_wall_clock_s"}:
        _fail("budget_invalid")
    amount = value["max_cost_cny"]
    if (type(amount) is not str or re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?", amount) is None
            or Decimal(amount) <= 0):
        _fail("budget_invalid")
    for name, maximum in (("max_model_calls", 128), ("max_wall_clock_s", 600)):
        if type(value[name]) is not int or not 1 <= value[name] <= maximum:
            _fail("budget_invalid")
    return dict(value)


def _binding(value):
    if type(value) is not dict or value.keys() != _BINDING_FIELDS:
        _fail("record_invalid")
    for name in ("account_id", "actor", "budget_id"):
        _identifier(value[name])
    for name in ("binding_id", "suite_id"):
        if type(value[name]) is not str or _UUID.fullmatch(value[name]) is None:
            _fail("record_invalid")
    if (type(value["request_sha256"]) is not str or _HASH.fullmatch(value["request_sha256"]) is None
            or type(value["enabled"]) is not bool):
        _fail("record_invalid")
    return {**value, "budget": _budget(value["budget"])}


def _encode(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("record_invalid")
    if len(result) > _MAX_RECORD_BYTES:
        _fail("record_invalid")
    return result


def _replacement(current, binding, expected_record_sha256, history):
    if current is None:
        _fail("not_selected")
    if current["enabled"] is not False:
        _fail("pause_required")
    if (type(expected_record_sha256) is not str
            or _HASH.fullmatch(expected_record_sha256) is None):
        _fail("expected_record_invalid")
    if expected_record_sha256 != current["record_sha256"]:
        _fail("stale_record")
    for used in history:
        if binding["binding_id"] == used["binding_id"]:
            _fail("binding_id_reused")
        if used["account_id"] == binding["account_id"] and any(
                binding[name] == used[name] for name in _ADMISSION_FIELDS):
            # A -> B -> A must not revive an earlier admission or run budget.
            _fail("fresh_admission_required")


def _reserve_pause(latest, count):
    active = sum(item["enabled"] for item in latest.values())
    if count + active + 2 > MAX_RECORDS:
        _fail("selection_limit")


class BindingStore:
    """Fresh schema-1 store or an existing store; never silently initialize.

    authorize() must raise on denial and return exactly None on success. It is
    checked before each operation and again before its transaction commits.
    A failed post-check rolls back selection/replacement/pause. No close() is
    needed because no connection or file handle survives a method. A missing
    read stays absent.
    """

    def __init__(self, path, authorize, initialize=False):
        if not callable(authorize) or type(initialize) is not bool:
            _fail("host_required")
        self._authorize = authorize
        self._check()
        raw_path = Path(path)
        if (not raw_path.is_absolute() or ".." in raw_path.parts or raw_path.drive.startswith("\\\\")
                or raw_path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}):
            _fail("path_invalid")
        self.path = raw_path.absolute()
        exists = self._plain_path()
        if initialize:
            if exists or any(Path(str(self.path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
                _fail("new_file_required")
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            # Retain an incomplete new file on failure. Never erase or adopt it.
            with self._connection(write=True, initialize=True) as connection:
                for statement in _SCHEMA.values():
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                self._verify_schema(connection)
                self._check()
        else:
            self._check()

    def _check(self):
        try:
            if self._authorize() is not None:
                _fail("host_denied")
        except Exception:
            _fail("host_denied")

    def _plain_path(self):
        """Inspect original paths before resolve; reject links and extra aliases."""
        exists = False
        for entry in (*reversed(self.path.parents), self.path):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                if entry == self.path:
                    continue
                _fail("parent_missing")
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                _fail("linked_path")
            if entry == self.path:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    _fail("linked_or_nonfile_path")
                if info.st_size > MAX_DATABASE_BYTES:
                    _fail("size_limit")
                exists = True
            elif not stat.S_ISDIR(info.st_mode):
                _fail("parent_invalid")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            try:
                info = sidecar.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1):
                _fail("linked_sidecar")
            if info.st_size > MAX_DATABASE_BYTES + 65536:
                _fail("size_limit")
            if suffix in ("-wal", "-shm"):
                _fail("unexpected_sidecar")
        return exists

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        if not self._plain_path():
            _fail("store_missing")
        identity = self.path.stat()
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                        uri=True, timeout=0.25, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            if not initialize:
                # Inspect ownership before any write-oriented connection setup.
                self._verify_schema(connection)
            if initialize:
                connection.execute("PRAGMA page_size=4096")
                connection.execute("PRAGMA journal_mode=DELETE")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // 4096}")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize:
                self._verify_schema(connection)
            if not self._plain_path():
                _fail("store_changed")
            current = self.path.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                _fail("store_changed")
            yield connection
            if not self._plain_path():
                _fail("store_changed")
            current = self.path.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                _fail("store_changed")
            connection.execute("COMMIT")
        except sqlite3.Error:
            _fail("store_invalid_or_unavailable")
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()

    @staticmethod
    def _verify_schema(connection):
        if (connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION
                or connection.execute("PRAGMA page_size").fetchone()[0] != 4096
                or connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
            _fail("schema_invalid")
        observed = {row["name"]: " ".join((row["sql"] or "").split()) for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if observed != {name: " ".join(sql.split()) for name, sql in _SCHEMA.items()}:
            _fail("schema_invalid")

    @staticmethod
    def _records(connection):
        rows = connection.execute("SELECT * FROM bindings ORDER BY sequence LIMIT 257").fetchall()
        if len(rows) > MAX_RECORDS:
            _fail("record_limit")
        latest, identities, history, previous = {}, set(), [], None
        for sequence, row in enumerate(rows, 1):
            raw, recorded_hash = row["record_json"], row["record_sha256"]
            if (row["sequence"] != sequence or type(raw) is not str or len(raw) > _MAX_RECORD_BYTES
                    or type(recorded_hash) is not str or _HASH.fullmatch(recorded_hash) is None):
                _fail("record_invalid")
            try:
                encoded = raw.encode("utf-8")
                event = json.loads(raw)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                _fail("record_invalid")
            if type(event) is not dict:
                _fail("record_invalid")
            fields = _EVENT_FIELDS | {"expected_record_sha256"} if event.get("event") == "replaced" else _EVENT_FIELDS
            if (len(encoded) > _MAX_RECORD_BYTES or sha256(encoded).hexdigest() != recorded_hash
                    or _encode(event) != encoded or event.keys() != fields
                    or type(event["sequence"]) is not int or event["sequence"] != sequence
                    or event["previous_sha256"] != previous
                    or type(event["event"]) is not str or event["event"] not in {"selected", "paused", "replaced"}):
                _fail("record_invalid")
            operator = _identifier(event["event_actor"])
            binding = _binding(event["binding"])
            account = binding["account_id"]
            old = latest.get(account)
            if event["event"] in {"selected", "replaced"}:
                if binding["binding_id"] in identities or not binding["enabled"] or operator != binding["actor"]:
                    _fail("record_invalid")
                if event["event"] == "selected":
                    if old is not None:
                        _fail("record_invalid")
                else:
                    _replacement(old, binding, event["expected_record_sha256"], history)
                _reserve_pause(latest, sequence - 1)
                identities.add(binding["binding_id"])
                history.append({name: binding[name] for name in
                                ("account_id", "binding_id", *_ADMISSION_FIELDS)})
            else:
                if (old is None or old["enabled"] is not True or binding["enabled"] is not False
                        or binding != {**{name: old[name] for name in _BINDING_FIELDS}, "enabled": False}):
                    _fail("record_invalid")
            latest[account] = {**binding, "record_sha256": recorded_hash}
            previous = recorded_hash
        return latest, len(rows), previous, history

    @staticmethod
    def _append(connection, binding, *, event, actor, sequence, previous,
                expected_record_sha256=None):
        if sequence > MAX_RECORDS:
            _fail("record_limit")
        payload = {"sequence": sequence, "event": event, "event_actor": actor,
                   "previous_sha256": previous, "binding": binding}
        if event == "replaced":
            payload["expected_record_sha256"] = expected_record_sha256
        encoded = _encode(payload)
        record_hash = sha256(encoded).hexdigest()
        connection.execute("INSERT INTO bindings VALUES(?,?,?)", (sequence, encoded.decode("utf-8"), record_hash))
        return {**binding, "record_sha256": record_hash}

    def read(self, account_id):
        self._check()
        _identifier(account_id)
        if not self._plain_path():
            self._check()
            return None
        with self._connection() as connection:
            latest, _, _, _ = self._records(connection)
            result = latest.get(account_id)
            self._check()
        return result

    def select(self, account_id, *, suite_id, actor, budget_id, budget, request_sha256):
        self._check()
        binding = _binding({"binding_id": uuid.uuid4().hex, "account_id": account_id,
            "suite_id": suite_id, "actor": actor, "budget_id": budget_id,
            "budget": budget, "request_sha256": request_sha256, "enabled": True})
        with self._connection(write=True) as connection:
            latest, count, previous, history = self._records(connection)
            if account_id in latest:
                _fail("already_selected")
            if any(binding["binding_id"] == used["binding_id"] for used in history):
                _fail("binding_id_reused")
            # Keep one append slot for every active binding's future pause.
            # Exhausting selection capacity must not consume the stop capacity.
            _reserve_pause(latest, count)
            result = self._append(connection, binding, event="selected", actor=actor,
                                  sequence=count + 1, previous=previous)
            self._check()
        from ampuly.plugins.customer_service_console.restore_hold import clear_send_hold
        clear_send_hold(self.path.parent)
        return result

    def replace(self, account_id, *, expected_record_sha256, suite_id, actor,
                budget_id, budget, request_sha256):
        """Explicit new admission after pause; never resume an old permission.

        CAS binds the latest paused record for this account, not the global
        hash-chain tail. Every historical suite, request and budget identifier
        for this account remains retired. The host supplies the current owner.
        """
        self._check()
        binding = _binding({"binding_id": uuid.uuid4().hex, "account_id": account_id,
            "suite_id": suite_id, "actor": actor, "budget_id": budget_id,
            "budget": budget, "request_sha256": request_sha256, "enabled": True})
        with self._connection(write=True) as connection:
            latest, count, previous, history = self._records(connection)
            _replacement(latest.get(account_id), binding, expected_record_sha256, history)
            _reserve_pause(latest, count)
            result = self._append(connection, binding, event="replaced", actor=actor,
                                  sequence=count + 1, previous=previous,
                                  expected_record_sha256=expected_record_sha256)
            self._check()
        from ampuly.plugins.customer_service_console.restore_hold import clear_send_hold
        clear_send_hold(self.path.parent)
        return result

    def pause(self, account_id, *, actor):
        self._check()
        _identifier(account_id)
        _identifier(actor)
        with self._connection(write=True) as connection:
            latest, count, previous, _ = self._records(connection)
            current = latest.get(account_id)
            if current is None:
                _fail("not_selected")
            if current["enabled"] is False:
                result = current
            else:
                binding = {name: current[name] for name in _BINDING_FIELDS}
                binding["enabled"] = False
                result = self._append(connection, binding, event="paused", actor=actor,
                                      sequence=count + 1, previous=previous)
            self._check()
        return result
